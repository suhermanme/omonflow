"""Clean mode (SPEC §10): fast, local, rule-based tidying of a transcript.

Only removes things that carry no meaning in either English or Indonesian:
filler sounds and accidental repeats of short English words. Indonesian
reduplication ("orang orang", "anak-anak") is meaningful and never touched.
Everything else is kept exactly; nothing is translated or rephrased.
"""

from __future__ import annotations

import re

# Pure hesitation sounds. Words with meaning ("ya", "eh" as "hey", "ah", "oh",
# "well", "like") are deliberately not included.
FILLERS = ("um", "umm", "ummm", "uh", "uhh", "uhm", "uhmm", "er", "erm", "hmm", "hmmm", "hm", "mm", "mmm",
           "ehm", "emm", "eee", "eeh")
# Short English words that are never meaningfully doubled in dictation.
REPEATABLE = ("the", "a", "an", "i", "to", "and", "of", "in", "on", "is", "it", "that", "we", "so", "my",
              "you", "for", "with", "this", "be", "are", "was", "but", "or")

_FILLER_RE = re.compile(r"(?<![\w-])(?:" + "|".join(FILLERS) + r")(?![\w-])(?:,|…|\.\.\.)?\s*", re.I)
_REPEAT_RE = re.compile(r"\b(" + "|".join(REPEATABLE) + r")(\s+\1\b)+", re.I)


def clean_text(text: str) -> str:
    t = _FILLER_RE.sub("", text or "")
    t = _REPEAT_RE.sub(r"\1", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" +([,.!?;:])", r"\1", t)          # "word ," -> "word,"
    t = re.sub(r"([,;:])(?=[^\s\d/\\])", r"\1 ", t)  # "a,b" -> "a, b" (not 1,5 or paths)
    t = re.sub(r"^[\s,;:]+", "", t)                  # a removed filler can leave a leading comma
    t = re.sub(r",\s*([.!?])", r"\1", t)
    t = t.strip()
    if not t:
        return ""
    # Capitalise sentence starts, without touching words like "iPhone" or "macOS".
    t = re.sub(r"(^|[.!?]\s+)([a-z])(?=[a-z]*\b)", lambda m: m.group(1) + m.group(2).upper(), t)
    if t[-1].isalnum():
        t += "."
    return t
