"""Cloud translator: fill in English headlines in the news sheet.

The news collector (redditisha/news-collector) appends articles to one sheet
tab per day; column J (title_en) holds the English headline. This job finds
non-English rows whose title_en is empty in the last TRANSLATE_DAYS day tabs
— newest tab first, newest rows first — translates them with IndicTrans2 (same model and code as the PC) for
up to TRANSLATE_SECONDS, and writes them back.

Rules shared with the PC and the dashboard's Translate button:
  - the sheet is the to-do list: an empty title_en means "not done yet";
  - results are written every batch, so a run cut short loses at most one;
  - just before writing, the cells are read again and only empty ones are
    written: the first translation wins, nothing is overwritten.

Each run is logged as a row in the _translate_runs tab (read by the PC and
shown on the Admin page).

Older tabs are left to the PC, which translates whatever is still empty a few
hours after collection.

Environment: SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON, TRANSLATE_SECONDS (300),
TRANSLATE_DAYS (3),
TRANSLATE_BATCH_SIZE (16), TRANSLATE_NUM_BEAMS (5), WRITE_EVERY (64).
"""

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from common import get_logger
from translate import FLORES, Translator

log = get_logger("cloud")

API = "https://sheets.googleapis.com/v4/spreadsheets"
DATE_TAB = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUNS_TAB = "_translate_runs"
RUN_COLUMNS = [
    "run_id", "started_at", "finished_at", "status", "translated", "already_filled", "pending_left",
    "seconds", "tabs", "trigger", "url", "error",
]
# Day-tab columns used here (see collector/src/layout.mjs): A id, F language, G title, J title_en.
COL_ID, COL_LANG, COL_TITLE, COL_EN = "A", "F", "G", "J"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Sheet:
    def __init__(self, sheet_id: str, key_json: str):
        self.id = sheet_id
        self.creds = service_account.Credentials.from_service_account_info(
            json.loads(key_json), scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        self.http = requests.Session()

    def call(self, method: str, path: str, body=None, attempt: int = 0):
        if not self.creds.valid:
            self.creds.refresh(Request())
        res = self.http.request(method, f"{API}/{self.id}{path}", json=body, timeout=60,
                                headers={"authorization": f"Bearer {self.creds.token}"})
        if (res.status_code == 429 or res.status_code >= 500) and attempt < 5:
            time.sleep(2 * 2 ** attempt)
            return self.call(method, path, body, attempt + 1)
        if not res.ok:
            raise RuntimeError(f"Sheets API {res.status_code}: {res.text[:300]}")
        return res.json()

    @staticmethod
    def range(tab: str, a1: str) -> str:
        return "'" + tab.replace("'", "''") + "'!" + a1

    def tabs(self) -> dict:
        d = self.call("GET", "?fields=sheets.properties(sheetId,title,gridProperties.rowCount)")
        return {s["properties"]["title"]: s["properties"].get("gridProperties", {}).get("rowCount", 0) for s in d.get("sheets", [])}

    def read(self, ranges: list[str]) -> list[list[list[str]]]:
        if not ranges:
            return []
        params = "&".join("ranges=" + quote(r, safe="") for r in ranges)
        d = self.call("GET", f"/values:batchGet?{params}&majorDimension=ROWS")
        return [v.get("values", []) for v in d.get("valueRanges", [])]

    def write(self, data: list[dict]):
        if data:
            self.call("POST", "/values:batchUpdate", {"valueInputOption": "RAW", "data": data})

    def append_row(self, tab: str, row: list):
        self.call("POST", f"/values/{quote(self.range(tab, 'A1'), safe='')}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
                  {"values": [row]})


def column(values: list[list[str]]) -> list[str]:
    return [(r[0] if r else "") for r in values]


def pending_rows(sheet: Sheet, tab: str) -> list[dict]:
    """Untranslated non-English rows of a tab, newest (bottom) first."""
    ids, langs, titles, en = sheet.read([sheet.range(tab, f"{c}1:{c}") for c in (COL_ID, COL_LANG, COL_TITLE, COL_EN)])
    ids, langs, titles, en = column(ids), column(langs), column(titles), column(en)
    if not en or en[0] != "title_en":
        return []  # not the current layout
    rows = []
    for n in range(1, len(ids)):
        lang = langs[n] if n < len(langs) else ""
        if ids[n] and lang in FLORES and (titles[n] if n < len(titles) else "") and not (en[n] if n < len(en) else ""):
            rows.append({"tab": tab, "row": n + 1, "id": ids[n], "lang": lang, "title": titles[n]})
    rows.reverse()
    return rows


def flush(sheet: Sheet, done: list[dict]) -> tuple[int, int]:
    """Write translations into cells that are still empty. Returns (written, already filled)."""
    if not done:
        return 0, 0
    by_tab: dict[str, list[dict]] = {}
    for d in done:
        by_tab.setdefault(d["tab"], []).append(d)
    tabs = list(by_tab)
    current = sheet.read([r for t in tabs for r in (sheet.range(t, f"{COL_ID}1:{COL_ID}"), sheet.range(t, f"{COL_EN}1:{COL_EN}"))])
    data, skipped = [], 0
    for k, tab in enumerate(tabs):
        ids, en = column(current[2 * k]), column(current[2 * k + 1])
        for d in by_tab[tab]:
            i = d["row"] - 1
            if i >= len(ids) or ids[i] != d["id"]:
                skipped += 1  # the row moved or the tab changed — leave it
            elif i < len(en) and en[i]:
                skipped += 1  # translated meanwhile (PC / Translate button): first one wins
            else:
                data.append({"range": sheet.range(tab, f"{COL_EN}{d['row']}"), "values": [[d["en"]]]})
    sheet.write(data)
    return len(data), skipped


def ensure_runs_tab(sheet: Sheet, tabs: dict):
    if RUNS_TAB in tabs:
        return
    sheet.call("POST", ":batchUpdate", {"requests": [{"addSheet": {"properties": {
        "title": RUNS_TAB, "gridProperties": {"rowCount": 1, "columnCount": len(RUN_COLUMNS)}}}}]})
    sheet.write([{"range": sheet.range(RUNS_TAB, "A1"), "values": [RUN_COLUMNS]}])


def main() -> int:
    started_at, t0 = now_iso(), time.time()
    budget = float(os.environ.get("TRANSLATE_SECONDS", "300"))
    write_every = int(os.environ.get("WRITE_EVERY", "64"))
    run_id = f"gh-{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    url = (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
           if os.environ.get("GITHUB_RUN_ID") else "")
    trigger = os.environ.get("GITHUB_EVENT_NAME", "local")
    sheet = Sheet(os.environ["SHEET_ID"], os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    stats = {"translated": 0, "skipped": 0, "pending": 0, "tabs": set()}
    status, error = "ok", ""
    try:
        tabs = sheet.tabs()
        ensure_runs_tab(sheet, tabs)
        days = int(os.environ.get("TRANSLATE_DAYS", "3"))
        day_tabs = sorted((t for t in tabs if DATE_TAB.match(t)), reverse=True)[:days]
        translator = Translator()
        done: list[dict] = []
        last_write = time.time()
        out_of_time = False
        for tab in day_tabs:
            if out_of_time:
                stats["pending"] += len(pending_rows(sheet, tab))
                continue
            todo = pending_rows(sheet, tab)
            if not todo:
                continue
            log.info("tab %s: %d untranslated", tab, len(todo))
            i = 0
            while i < len(todo):
                if time.time() - t0 > budget:
                    out_of_time = True
                    stats["pending"] += len(todo) - i
                    break
                batch = todo[i : i + translator.batch_size]
                by_lang: dict[str, list[dict]] = {}
                for r in batch:
                    by_lang.setdefault(r["lang"], []).append(r)
                for lang, group in by_lang.items():
                    for r, en in zip(group, translator.translate([g["title"] for g in group], lang)):
                        if en.strip():
                            done.append({**r, "en": en.strip()})
                i += len(batch)
                if len(done) >= write_every or time.time() - last_write > 30:
                    w, s = flush(sheet, done)
                    stats["translated"] += w
                    stats["skipped"] += s
                    stats["tabs"].add(tab)
                    done, last_write = [], time.time()
        w, s = flush(sheet, done)
        stats["translated"] += w
        stats["skipped"] += s
    except Exception as e:  # logged to the sheet, and the job fails
        status, error = "error", f"{type(e).__name__}: {e}"[:500]
        traceback.print_exc()
    seconds = round(time.time() - t0)
    log.info("%s: %d translated, %d already filled, %d still waiting, %ds", status, stats["translated"], stats["skipped"], stats["pending"], seconds)
    try:
        sheet.append_row(RUNS_TAB, [run_id, started_at, now_iso(), status, stats["translated"], stats["skipped"],
                                    stats["pending"], seconds, ",".join(sorted(stats["tabs"])), trigger, url, error])
    except Exception:
        traceback.print_exc()
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf8") as f:
            f.write(f"**{status}** — {stats['translated']} translated, {stats['skipped']} already filled, "
                    f"{stats['pending']} still waiting, {seconds}s\n")
    print(f"::notice title=Translated::{stats['translated']} translated, {stats['pending']} still waiting, {seconds}s")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
