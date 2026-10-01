"""Throughput test: how many headlines can a GitHub runner translate?

Translates real headlines (sample.jsonl, the newest 3,000 Hindi/Kannada/
Tamil/Telugu headlines from the archive) with the same model and code as the
PC, in the PC's batching (16 at a time, grouped by language), for a fixed
time budget per setting:

  A  beam 5 (the PC's setting)       BENCH_SECONDS_A (default 180 s)
  B  beam 2 (faster, maybe a bit worse), on the same headlines as A,
     so quality can be compared side by side   BENCH_SECONDS_B (default 90 s)

Writes a Markdown report to the job summary and results.jsonl (headline,
beam-5 and beam-2 translations) for a quality check.
"""

import json
import os
import time
from collections import Counter

from translate import Translator

BATCH = 16


def run(translator, items, beams: int, budget: float):
    translator.num_beams = beams
    done, out = 0, []
    started = time.time()
    while done < len(items) and time.time() - started < budget:
        chunk = items[done : done + BATCH]
        by_lang: dict[str, list[int]] = {}
        for i, it in enumerate(chunk):
            by_lang.setdefault(it["language"], []).append(i)
        english = [""] * len(chunk)
        for lang, idx in by_lang.items():
            for i, en in zip(idx, translator.translate([chunk[i]["title"] for i in idx], lang)):
                english[i] = en
        out.extend(english)
        done += len(chunk)
    return out, time.time() - started


def main():
    items = [json.loads(l) for l in open(os.path.join(os.path.dirname(__file__), "sample.jsonl"), encoding="utf8")]
    t0 = time.time()
    translator = Translator()
    load_s = time.time() - t0

    a_out, a_s = run(translator, items, 5, float(os.environ.get("BENCH_SECONDS_A", "180")))
    b_out, b_s = run(translator, items[: len(a_out)], 2, float(os.environ.get("BENCH_SECONDS_B", "90")))

    rate_a, rate_b = len(a_out) / a_s, len(b_out) / b_s
    langs = Counter(it["language"] for it in items[: len(a_out)])
    lines = [
        "## Translation throughput on a GitHub runner",
        "",
        f"CPU cores: {os.cpu_count()} · model load: {load_s:.1f} s",
        "",
        "| Setting | Headlines | Time | Per headline | Per 15 min (all 15 min translating) | Per 5-min run |",
        "|---|---|---|---|---|---|",
        f"| beam 5 (PC setting) | {len(a_out)} | {a_s:.0f} s | {a_s / max(len(a_out), 1):.2f} s | {rate_a * 900:.0f} | {rate_a * 300:.0f} |",
        f"| beam 2 | {len(b_out)} | {b_s:.0f} s | {b_s / max(len(b_out), 1):.2f} s | {rate_b * 900:.0f} | {rate_b * 300:.0f} |",
        "",
        "Languages translated (beam 5): " + ", ".join(f"{k} {v}" for k, v in langs.most_common()),
        "",
        "### First 15 side by side",
        "",
        "| Original | beam 5 | beam 2 |",
        "|---|---|---|",
    ]
    for it, a, b in list(zip(items, a_out, b_out))[:15]:
        cell = lambda s: s.replace("|", "/").replace("\n", " ")
        lines.append(f"| {cell(it['title'])} | {cell(a)} | {cell(b)} |")
    report = "\n".join(lines)
    print(report)
    # Also as a run annotation: those show on the public run page without signing in.
    print(
        f"::notice title=Translation throughput::{os.cpu_count()} cores, model load {load_s:.0f}s. "
        f"beam 5: {len(a_out)} in {a_s:.0f}s = {a_s / max(len(a_out), 1):.2f}s each, {rate_a * 300:.0f} per 5 min, {rate_a * 900:.0f} per 15 min. "
        f"beam 2: {len(b_out)} in {b_s:.0f}s = {b_s / max(len(b_out), 1):.2f}s each, {rate_b * 300:.0f} per 5 min, {rate_b * 900:.0f} per 15 min."
    )
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf8") as f:
            f.write(report + "\n")
    with open("results.jsonl", "w", encoding="utf8") as f:
        for i, it in enumerate(items[: len(a_out)]):
            f.write(json.dumps({**it, "beam5": a_out[i], "beam2": b_out[i] if i < len(b_out) else None}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
