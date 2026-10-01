"""Cloud story grouping: articles about the same event become one story.

Same method as the PC's local/stories.py (which this replaces): TF-IDF on
each article's English headline (the translation for Indian languages),
compared with the centroid of every story active in the last WINDOW_HOURS;
an article joins the closest story above THRESHOLD or starts a new one.
Incremental, so a story keeps its id while it grows.

The sheet's _stories tab is the record (this job is its only writer):
  id, title, first_seen, last_seen, article_count, source_count,
  members (article ids), sources (source ids), title_lang
It keeps every story seen in the last KEEP_ALL_HOURS (singles too, so new
articles can join them) and stories with 2+ outlets for KEEP_DAYS. Stories
started here get ids from CLOUD_ID_BASE up, so they never collide with the
PC's older ones.
"""

import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.preprocessing import normalize

from common import get_logger

log = get_logger("stories")

WINDOW_HOURS = int(os.environ.get("STORY_WINDOW_HOURS", "48"))
THRESHOLD = float(os.environ.get("STORY_THRESHOLD", "0.3"))
KEEP_ALL_HOURS = 72
KEEP_DAYS = int(os.environ.get("SHEET_RETENTION_DAYS", "40"))
CLOUD_ID_BASE = 10_000_000
MAX_MEMBERS = 2500
STORIES_TAB = "_stories"
STORY_COLUMNS = ["id", "title", "first_seen", "last_seen", "article_count", "source_count", "members", "sources", "title_lang"]
DATE_TAB = re.compile(r"^\d{4}-\d{2}-\d{2}$")

NEWS_STOP_WORDS = {
    "live", "updates", "update", "breaking", "news", "latest", "watch", "video", "videos", "photos",
    "today", "says", "said", "report", "reports", "new", "day", "know", "here", "details", "check",
    "big", "top", "read", "full", "list", "amid", "ahead", "after", "over", "also", "just",
}
STOP_WORDS = list(ENGLISH_STOP_WORDS | NEWS_STOP_WORDS)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def window_articles(sheet, tabs: dict, since: str) -> list[dict]:
    """Articles of the last WINDOW_HOURS with an English headline, oldest first."""
    days = sorted((t for t in tabs if DATE_TAB.match(t)), reverse=True)[:3]
    reads = sheet.read([r for t in days for r in (sheet.range(t, "A2:G"), sheet.range(t, "J2:J"))])
    rows = []
    for k in range(len(days)):
        left, en = reads[2 * k], reads[2 * k + 1]
        for n, r in enumerate(left):
            r = r + [""] * (7 - len(r))
            art_id, published, fetched, source, _name, lang, title = r[:7]
            at = published or fetched
            if not art_id or not title or not at or at < since:
                continue
            title_en = en[n][0] if n < len(en) and en[n] else ""
            lang = lang or "en"
            if lang != "en" and not title_en:
                continue  # not translated yet — it joins a story once it is
            text = re.sub(r"\s+", " ", title if lang == "en" else title_en).strip()
            rows.append({"id": art_id, "at": at, "source": source, "lang": lang, "text": text})
    rows.sort(key=lambda r: r["at"])
    seen, out = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def read_stories(sheet, tabs: dict) -> dict[int, dict]:
    if STORIES_TAB not in tabs:
        return {}
    [rows] = sheet.read([sheet.range(STORIES_TAB, "A2:I")])
    stories = {}
    for r in rows:
        r = r + [""] * (9 - len(r))
        if not r[0].lstrip("-").isdigit():
            continue
        stories[int(r[0])] = {
            "title": r[1], "first_seen": r[2], "last_seen": r[3],
            "members": [m for m in r[6].split(",") if m],
            "sources": {s for s in r[7].split(",") if s},
            "title_lang": r[8] or "",
        }
    return stories


def write_stories(sheet, stories: dict[int, dict]) -> int:
    now = datetime.now(timezone.utc)
    keep_all, keep_multi = iso(now - timedelta(hours=KEEP_ALL_HOURS)), iso(now - timedelta(days=KEEP_DAYS))
    rows = []
    for sid, s in stories.items():
        n_src = len(s["sources"])
        if s["last_seen"] >= keep_all or (n_src >= 2 and s["last_seen"] >= keep_multi):
            rows.append([sid, s["title"], s["first_seen"], s["last_seen"], len(s["members"]), n_src,
                         ",".join(s["members"][:MAX_MEMBERS]), ",".join(sorted(s["sources"])), s["title_lang"]])
    rows.sort(key=lambda r: r[3], reverse=True)
    props = sheet.props()
    grid = {"rowCount": len(rows) + 1, "columnCount": len(STORY_COLUMNS)}
    if STORIES_TAB in props:
        req = {"updateSheetProperties": {"properties": {"sheetId": props[STORIES_TAB]["sheetId"], "gridProperties": grid},
                                         "fields": "gridProperties(rowCount,columnCount)"}}
    else:
        req = {"addSheet": {"properties": {"title": STORIES_TAB, "gridProperties": grid}}}
    sheet.call("POST", ":batchUpdate", {"requests": [req]})
    for i in range(0, len(rows) + 1, 5000):
        chunk = ([STORY_COLUMNS] + rows)[i : i + 5000]
        sheet.write([{"range": sheet.range(STORIES_TAB, f"A{i + 1}"), "values": chunk}])
    return len(rows)


def group(sheet) -> dict:
    tabs = sheet.tabs()
    since = iso(datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS))
    articles = window_articles(sheet, tabs, since)
    stories = read_stories(sheet, tabs)
    story_of = {a: sid for sid, s in stories.items() for a in s["members"]}
    if not articles:
        return {"assigned": 0, "new_stories": 0, "stories": len(stories)}

    vectorizer = TfidfVectorizer(stop_words=STOP_WORDS, ngram_range=(1, 2), sublinear_tf=True, min_df=1)
    try:
        X = normalize(vectorizer.fit_transform([a["text"] for a in articles]).tocsr())
    except ValueError:  # every headline was stop words
        return {"assigned": 0, "new_stories": 0, "stories": len(stories)}

    # Sparse centroids: cosine(vec, mean of members) = vec·sum / |sum|.
    sums: dict[int, dict[int, float]] = defaultdict(dict)
    norm2: dict[int, float] = defaultdict(float)
    postings: dict[int, dict[int, float]] = defaultdict(dict)

    def terms(i):
        row = X.getrow(i)
        return list(zip(row.indices.tolist(), row.data.tolist()))

    def add(sid, vec):
        story = sums[sid]
        for t, v in vec:
            old = story.get(t, 0.0)
            new = old + v
            story[t] = new
            postings[t][sid] = new
            norm2[sid] += new * new - old * old

    def closest(vec):
        scores: dict[int, float] = defaultdict(float)
        for t, v in vec:
            for sid, w in postings.get(t, {}).items():
                scores[sid] += v * w
        best, best_sim = None, 0.0
        for sid, dot in scores.items():
            sim = dot / math.sqrt(norm2[sid]) if norm2[sid] > 0 else 0.0
            if sim > best_sim:
                best, best_sim = sid, sim
        return best, best_sim

    for i, a in enumerate(articles):
        if a["id"] in story_of and story_of[a["id"]] in stories:
            add(story_of[a["id"]], terms(i))

    next_id = max([CLOUD_ID_BASE - 1, *stories]) + 1
    assigned = new_stories = 0
    for i, a in enumerate(articles):
        if a["id"] in story_of:
            continue
        vec = terms(i)
        if not vec:
            continue
        best, sim = closest(vec)
        if best is not None and sim >= THRESHOLD:
            sid = best
            s = stories[sid]
            # Prefer an English outlet's own wording as the story's headline.
            if s["title_lang"] != "en" and a["lang"] == "en":
                s["title"], s["title_lang"] = a["text"], "en"
        else:
            sid, next_id = next_id, next_id + 1
            stories[sid] = {"title": a["text"], "first_seen": a["at"], "last_seen": a["at"],
                            "members": [], "sources": set(), "title_lang": a["lang"]}
            new_stories += 1
        s = stories[sid]
        s["members"].append(a["id"])
        s["sources"].add(a["source"])
        s["first_seen"] = min(s["first_seen"] or a["at"], a["at"])
        s["last_seen"] = max(s["last_seen"] or a["at"], a["at"])
        story_of[a["id"]] = sid
        add(sid, vec)
        assigned += 1
    written = write_stories(sheet, stories)
    log.info("stories: %d articles assigned, %d new stories, %d in _stories", assigned, new_stories, written)
    return {"assigned": assigned, "new_stories": new_stories, "stories": written}
