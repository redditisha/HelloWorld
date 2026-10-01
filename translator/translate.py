"""Translate non-English headlines to English, locally, with IndicTrans2
(ai4bharat/indictrans2-indic-en-dist-200M, CPU).

    python local/translate.py                       translate pending titles in the archive
    python local/translate.py --sample FILE.jsonl   benchmark on {"title","language"} lines, no DB writes
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone

from common import get_logger, setting

log = get_logger("translate")

MODEL = "ai4bharat/indictrans2-indic-en-dist-200M"
TARGET = "eng_Latn"
# Source-language codes this app uses -> IndicTrans2 (FLORES) codes.
FLORES = {
    "hi": "hin_Deva", "kn": "kan_Knda", "ta": "tam_Taml", "te": "tel_Telu", "ml": "mal_Mlym",
    "mr": "mar_Deva", "bn": "ben_Beng", "gu": "guj_Gujr", "pa": "pan_Guru", "or": "ory_Orya",
    "as": "asm_Beng", "ur": "urd_Arab", "ne": "npi_Deva",
}


# The model translates one sentence at a time and tends to drop everything
# after the first sentence of a multi-sentence input, so headlines are split
# first. Only unambiguous sentence ends: ? ! । and runs of 2+ dots — a single
# "." is left alone because Kannada/Hindi abbreviations use it ("ರೂ.", "ಡಾ.").
SENTENCE_END = re.compile(r"(?<=[?!।])\s+|(?<=\.\.)\s+")


def split_sentences(title: str) -> list[str]:
    parts = [p.strip() for p in SENTENCE_END.split(title.strip())]
    return [p for p in parts if p] or [title]


class Translator:
    def __init__(self):
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        from indic_processor import IndicProcessor

        self.torch = torch
        torch.set_num_threads(os.cpu_count() or 4)
        self.batch_size = int(setting("TRANSLATE_BATCH_SIZE", "16"))
        self.num_beams = int(setting("TRANSLATE_NUM_BEAMS", "5"))
        started = time.time()
        # Prefer the cached copy so translation works offline; fall back to
        # the Hub (first run, a partly downloaded cache, or after an update).
        # A partial cache fails in several ways (OSError, ValueError,
        # AttributeError) depending on which files are missing, so any error
        # here just means "go online".
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True, local_files_only=True)
            self.model = AutoModelForSeq2SeqLM.from_pretrained(MODEL, trust_remote_code=True, local_files_only=True)
        except Exception:
            self.tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
            self.model = AutoModelForSeq2SeqLM.from_pretrained(MODEL, trust_remote_code=True)
        self.model.eval()
        self.ip = IndicProcessor(inference=True)
        log.info("model loaded in %.1fs", time.time() - started)

    def translate(self, titles: list[str], lang: str) -> list[str]:
        """Translate headlines, sentence by sentence, rejoining each headline."""
        owners, sentences = [], []
        for i, title in enumerate(titles):
            for sentence in split_sentences(title):
                owners.append(i)
                sentences.append(sentence)
        english = [[] for _ in titles]
        for start in range(0, len(sentences), self.batch_size):
            chunk = sentences[start : start + self.batch_size]
            for owner, text in zip(owners[start : start + self.batch_size], self._translate_batch(chunk, lang)):
                english[owner].append(text.strip())
        return [" ".join(t for t in parts if t) for parts in english]

    def _translate_batch(self, sentences: list[str], lang: str) -> list[str]:
        src = FLORES[lang]
        batch = self.ip.preprocess_batch(sentences, src_lang=src, tgt_lang=TARGET)
        inputs = self.tokenizer(batch, truncation=True, padding="longest", return_tensors="pt", return_attention_mask=True)
        with self.torch.no_grad():
            out = self.model.generate(
                **inputs, use_cache=True, min_length=0, max_length=256, num_beams=self.num_beams, num_return_sequences=1
            )
        decoded = self.tokenizer.batch_decode(out, skip_special_tokens=True, clean_up_tokenization_spaces=True)
        return self.ip.postprocess_batch(decoded, lang=TARGET)


def translate_pending(conn, limit: int | None = None, progress=None) -> int:
    """Fill title_en for untranslated non-English articles, newest first.

    progress(done, total) is called after each batch, if given.
    """
    limit = limit or int(setting("TRANSLATE_MAX_PER_RUN", "5000"))
    langs = tuple(FLORES)
    placeholders = ",".join("?" * len(langs))
    pending = conn.execute(
        f"select count(*) from articles where title_en is null and language in ({placeholders})", langs
    ).fetchone()[0]
    if not pending:
        return 0

    translator = Translator()
    done = 0
    started = time.time()
    while done < limit:
        rows = conn.execute(
            f"""select rowid, title, language from articles
                where title_en is null and language in ({placeholders})
                order by published_at desc limit ?""",
            (*langs, translator.batch_size),
        ).fetchall()
        if not rows:
            break
        conn.commit()  # hold no write lock while the model runs
        by_lang: dict[str, list] = {}
        for r in rows:
            by_lang.setdefault(r["language"], []).append(r)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        for lang, group in by_lang.items():
            english = translator.translate([r["title"] for r in group], lang)
            conn.executemany(
                "update articles set title_en = ?, translated_at = ? where rowid = ?",
                [(en.strip() or None, now, r["rowid"]) for en, r in zip(english, group)],
            )
        conn.commit()
        done += len(rows)
        if progress:
            progress(done, min(pending, limit))
    elapsed = time.time() - started
    log.info("translated %d titles in %.0fs (%.2f s/title), %d still pending",
             done, elapsed, elapsed / max(done, 1), max(pending - done, 0))
    return done


def benchmark(sample_file: str, n: int, out_file: str) -> None:
    rows = [json.loads(line) for line in open(sample_file, encoding="utf-8")]
    rows = [r for r in rows if r.get("language") in FLORES][:n]
    translator = Translator()
    started = time.time()
    results = []
    for lang in sorted({r["language"] for r in rows}):
        of_lang = [r for r in rows if r["language"] == lang]
        for i in range(0, len(of_lang), translator.batch_size):
            chunk = of_lang[i : i + translator.batch_size]
            english = translator.translate([r["title"] for r in chunk], lang)
            results += [{"source": r.get("source"), "original": r["title"], "english": en} for r, en in zip(chunk, english)]
    elapsed = time.time() - started
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"titles": len(results), "seconds": round(elapsed, 1), "results": results}, f, ensure_ascii=False, indent=1)
    print(f"{len(results)} titles in {elapsed:.1f}s = {elapsed / max(len(results), 1):.2f}s per title -> {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample")
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--out", default="translate_benchmark.json")
    args = parser.parse_args()
    if args.sample:
        benchmark(args.sample, args.n, args.out)
    else:
        from common import open_db

        print(translate_pending(open_db()))
