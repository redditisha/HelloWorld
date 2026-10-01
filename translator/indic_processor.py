"""Pure-Python port of IndicTransToolkit's IndicProcessor (v1.1.1, MIT licence,
AI4Bharat). The upstream package ships only as a Cython extension with no
Windows wheels, so it can't be installed here without a C++ compiler.

IndicTrans2 expects inputs normalised, tokenised and transliterated into
Devanagari, prefixed with "<src_lang> <tgt_lang>", and outputs detokenised
with placeholders (numbers, URLs, emails) restored. Behaviour matches
upstream's preprocess_batch / postprocess_batch, except for the visarga fix
below.

Visarga fix: indic-nlp-library-itt's normalizers turn "<letter>:" into the
script's visarga, but their pattern is a mis-escaped raw string, so the
character class covers ASCII 0-9, :, A-Z etc. instead of the script's block,
and the replacement drops the matched character: "Sep 27:" -> "Sep 2\1ಃ",
"ICC:" -> "IC\1ಃ". Headlines are full of dates and acronyms before colons, so
we disable that step and apply the intended rule (colon right after a
character of the script -> visarga).
"""

from queue import Queue

import regex as re
from indicnlp.normalize.indic_normalize import IndicNormalizerFactory
from indicnlp.tokenize import indic_detokenize, indic_tokenize
from indicnlp.transliterate.unicode_transliterate import UnicodeIndicTransliterator
from sacremoses import MosesDetokenizer, MosesPunctNormalizer, MosesTokenizer

FLORES_TO_ISO = {
    "asm_Beng": "as", "awa_Deva": "hi", "ben_Beng": "bn", "bho_Deva": "hi", "brx_Deva": "hi",
    "doi_Deva": "hi", "eng_Latn": "en", "gom_Deva": "kK", "gon_Deva": "hi", "guj_Gujr": "gu",
    "hin_Deva": "hi", "hne_Deva": "hi", "kan_Knda": "kn", "kas_Arab": "ur", "kas_Deva": "hi",
    "kha_Latn": "en", "lus_Latn": "en", "mag_Deva": "hi", "mai_Deva": "hi", "mal_Mlym": "ml",
    "mar_Deva": "mr", "mni_Beng": "bn", "mni_Mtei": "hi", "npi_Deva": "ne", "ory_Orya": "or",
    "pan_Guru": "pa", "san_Deva": "hi", "sat_Olck": "or", "snd_Arab": "ur", "snd_Deva": "hi",
    "tam_Taml": "ta", "tel_Telu": "te", "urd_Arab": "ur", "unr_Deva": "hi",
}

# Indic digits (Bengali, Gujarati, Kannada, Devanagari, Arabic, Meitei, Oriya,
# Gurmukhi, Ol Chiki, Persian, Telugu) -> ASCII.
_DIGIT_BLOCKS = ["\u09e6", "\u0ae6", "\u0ce6", "\u0966", "\u0660", "\uabf0", "\u0b66", "\u0a66", "\u1c50", "\u06f0", "\u0c66"]
DIGITS = {ord(chr(ord(zero) + i)): str(i) for zero in _DIGIT_BLOCKS for i in range(10)}

PUNC_REPLACEMENTS = [
    (re.compile(r"\r"), ""),
    (re.compile(r"\(\s*"), "("),
    (re.compile(r"\s*\)"), ")"),
    (re.compile(r"\s:\s?"), ":"),
    (re.compile(r"\s;\s?"), ";"),
    (re.compile(r"[`´‘‚’]"), "'"),
    (re.compile(r"[„“”«»]"), '"'),
    (re.compile(r"[–—]"), "-"),
    (re.compile(r"\.\.\."), "..."),
    (re.compile(r" %"), "%"),
    (re.compile(r"nº "), "nº "),
    (re.compile(r" ºC"), " ºC"),
    (re.compile(r" [?!;]"), lambda m: m.group(0).strip()),
    (re.compile(r", "), ", "),
]
MULTISPACE = re.compile(r"[ ]{2,}")
END_BRACKET_SPACE_PUNC = re.compile(r"\) ([\.!:?;,])")
DIGIT_SPACE_PERCENT = re.compile(r"(\d) %")
DOUBLE_QUOT_PUNC = re.compile(r"\"([,\.]+)")
DIGIT_NBSP_DIGIT = re.compile(r"(\d) (\d)")

URL_PATTERN = re.compile(r"\b(?<![\w/.])(?:(?:https?|ftp)://)?(?:(?:[\w-]+\.)+(?!\.))(?:[\w/\-?#&=%.]+)+(?!\.\w+)\b")
NUMERAL_PATTERN = re.compile(
    r"(~?\d+\.?\d*\s?%?\s?-?\s?~?\d+\.?\d*\s?%|~?\d+%|\d+[-\/.,:']\d+[-\/.,:'+]\d+(?:\.\d+)?|\d+[-\/.:'+]\d+(?:\.\d+)?)"
)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}")
OTHER_PATTERN = re.compile(r"[A-Za-z0-9]*[#|@]\w+")
INDIC_LETTER = re.compile(r"[\u0900-\u0DFF]")  # Devanagari .. Sinhala blocks

# First code point of each script's Unicode block; visarga is block start + 3.
SCRIPT_BLOCKS = {
    "hi": 0x0900, "mr": 0x0900, "ne": 0x0900, "kK": 0x0900, "bn": 0x0980, "as": 0x0980,
    "pa": 0x0A00, "gu": 0x0A80, "or": 0x0B00, "ta": 0x0B80, "te": 0x0C00, "kn": 0x0C80, "ml": 0x0D00,
}
_NEVER = re.compile(r"(?!)")

# Ways the model sometimes mangles "<ID1>" in its output; all map back to the entity.
INDIC_FAILURE_CASES = [
    "آی ڈی ", "ꯑꯥꯏꯗꯤ", "आईडी", "आई . डी . ", "आई . डी .", "आई. डी. ", "आई. डी.", "आय. डी. ",
    "आय. डी.", "आय . डी . ",
    "आय . डी .आइ . डी . ",  # upstream is missing a comma here; kept for identical behaviour
    "आइ . डी .", "आइ. डी. ", "आइ. डी.", "ऐटि", "آئی ڈی ", "ᱟᱭᱰᱤ ᱾", "आयडी", "ऐडि", "आइडि", "ᱟᱭᱰᱤ",
]


class IndicProcessor:
    def __init__(self, inference: bool = True):
        self.inference = inference
        self._maps: Queue = Queue()
        self._en_tok = MosesTokenizer(lang="en")
        self._en_normalizer = MosesPunctNormalizer()
        self._en_detok = MosesDetokenizer(lang="en")
        self._xliterator = UnicodeIndicTransliterator()

    @staticmethod
    def _punc_norm(text: str) -> str:
        for pattern, repl in PUNC_REPLACEMENTS:
            text = pattern.sub(repl, text)
        text = MULTISPACE.sub(" ", text)
        text = END_BRACKET_SPACE_PUNC.sub(r")\1", text)
        text = DIGIT_SPACE_PERCENT.sub(r"\1%", text)
        text = DOUBLE_QUOT_PUNC.sub(r'\1"', text)
        text = DIGIT_NBSP_DIGIT.sub(r"\1.\2", text)
        return text.strip()

    def _wrap_with_placeholders(self, text: str) -> str:
        serial = 1
        entity_map: dict[str, str] = {}
        for pattern in (EMAIL_PATTERN, URL_PATTERN, NUMERAL_PATTERN, OTHER_PATTERN):
            for match in set(pattern.findall(text)):
                if pattern is URL_PATTERN and len(match.replace(".", "")) < 4:
                    continue
                # Deviation from upstream: its URL pattern also matches Indic
                # initials and dates ("ಡಿ.ಕೆ.", "ಸೆ.30ರಂದು"), which then pass
                # through untranslated. Real URLs/emails are Latin-script.
                if INDIC_LETTER.search(match):
                    continue
                if pattern is NUMERAL_PATTERN and len(match.replace(" ", "").replace(".", "").replace(":", "")) < 4:
                    continue
                for tag in ("ID", "id"):
                    for form in (
                        f"<{tag}{serial}>", f"< {tag}{serial} >", f"[{tag}{serial}]", f"[ {tag}{serial} ]",
                        f"[{tag} {serial}]", f"<{tag}{serial}]", f"< {tag}{serial}]", f"<{tag}{serial} ]",
                    ):
                        entity_map[form] = match
                for case in INDIC_FAILURE_CASES:
                    for form in (
                        f"<{case}{serial}>", f"< {case}{serial} >", f"< {case} {serial} >", f"<{case} {serial}]",
                        f"< {case} {serial} ]", f"[{case}{serial}]", f"[{case} {serial}]", f"[ {case}{serial} ]",
                        f"[ {case} {serial} ]", f"{case} {serial}", f"{case}{serial}",
                    ):
                        entity_map[form] = match
                text = text.replace(match, f"<ID{serial}>")
                serial += 1
        text = re.sub(r"\s+", " ", text).replace(">/", ">").replace("]/", "]")
        self._maps.put(entity_map)
        return text

    def _preprocess(self, sent: str, src_lang: str, tgt_lang: str, normalizer, is_target: bool) -> str:
        iso = FLORES_TO_ISO.get(src_lang, "hi")
        sent = self._punc_norm(sent).translate(DIGITS)
        if self.inference:
            sent = self._wrap_with_placeholders(sent)
        if iso == "en":
            tokens = self._en_tok.tokenize(self._en_normalizer.normalize(sent.strip()), escape=False)
            processed = " ".join(tokens)
        else:
            processed = " ".join(indic_tokenize.trivial_tokenize(normalizer.normalize(sent.strip()), iso))
            if src_lang.split("_")[1] not in ("Arab", "Aran", "Olck", "Mtei", "Latn"):
                processed = self._xliterator.transliterate(processed, iso, "hi").replace(" ् ", "्")
        processed = processed.strip()
        return processed if is_target else f"{src_lang} {tgt_lang} {processed}"

    def preprocess_batch(self, batch: list[str], src_lang: str, tgt_lang: str | None = None, is_target: bool = False) -> list[str]:
        normalizer = None
        if src_lang != "eng_Latn":
            iso = FLORES_TO_ISO.get(src_lang, "hi")
            normalizer = _fix_visarga(IndicNormalizerFactory().get_normalizer(iso), iso)
        return [self._preprocess(s, src_lang, tgt_lang, normalizer, is_target) for s in batch]

    def _postprocess(self, sent: str, lang: str, entity_map: dict) -> str:
        lang_code, script = lang.split("_", 1)
        iso = FLORES_TO_ISO.get(lang, "hi")
        if script in ("Arab", "Aran"):
            sent = sent.replace(" ؟", "؟").replace(" ۔", "۔").replace(" ،", "،").replace("ٮ۪", "ؠ")
        if lang_code == "ory":
            sent = sent.replace("ଯ଼", "ୟ")
        for k, v in entity_map.items():
            sent = sent.replace(k, v)
        if lang == "eng_Latn":
            return self._en_detok.detokenize(sent.split(" "))
        return indic_detokenize.trivial_detokenize(self._xliterator.transliterate(sent, "hi", iso), iso)

    def postprocess_batch(self, sents: list[str], lang: str = "hin_Deva", num_return_sequences: int = 1) -> list[str]:
        maps = [self._maps.get() for _ in range(len(sents) // num_return_sequences)]
        out = [self._postprocess(s, lang, maps[i // num_return_sequences]) for i, s in enumerate(sents)]
        self._maps.queue.clear()
        return out


class _VisargaFixed:
    """Wraps a normalizer: its broken visarga step is disabled, the intended one applied."""

    def __init__(self, inner, iso: str):
        self.inner = inner
        start = SCRIPT_BLOCKS[iso]
        self.pattern = re.compile(f"([{chr(start)}-{chr(start + 0x7F)}]):")
        self.visarga = chr(start + 3)

    def normalize(self, text: str) -> str:
        return self.pattern.sub(lambda m: m.group(1) + self.visarga, self.inner.normalize(text))


def _fix_visarga(normalizer, iso: str):
    if not hasattr(normalizer, "visarga_pattern") or iso not in SCRIPT_BLOCKS:
        return normalizer
    normalizer.visarga_pattern = _NEVER
    return _VisargaFixed(normalizer, iso)
