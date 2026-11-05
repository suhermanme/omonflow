"""Checks that an AI rewrite is still what the user said (SPEC §10 "must preserve").

A small model can translate a sentence, answer a dictated question or obey a
dictated instruction ("write a poem…"). The prompt asks it not to, but the
result is only accepted when:

- no word was added or replaced (case, punctuation and hyphens don't count),
- no word was dropped except filler sounds, hesitation words ("eh", "ah",
  "so", …) and repeats of a word that is still there, so a lost "not" or
  a changed "main" → "master" is caught,
- the words are still in the same order, and
- every protected token survives exactly: numbers, versions, paths, URLs,
  e-mail addresses, identifiers with digits, underscores or inner capitals.

Otherwise OmonFlow falls back to the Clean text.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass

from omonflow.refinement.clean import FILLERS

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_FILLER_SET = set(FILLERS)
# Hesitation words the AI may drop although they are words: they carry no content in dictation.
# Words that can carry meaning ("apa", "jadi", "terus", "just", "like") are not listed.
DROPPABLE = {"eh", "ah", "oh", "well", "so", "yeah", "okay", "ok", "oke", "nah", "anu", "gitu", "basically"}
# Tokens that must come through unchanged (compared as written, minus edge punctuation).
_PROTECTED = re.compile(
    r"https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.[\w.]+"     # URLs, e-mail
    r"|(?<!\S)(?:~|\.{1,2})?/[\w.@~+-]+(?:/[\w.@~+-]*)*"  # Unix paths
    r"|[A-Za-z]:\\\S+"                                  # Windows paths
    r"|\S*\d\S*"                                        # anything with a digit: 8080, 1.9.5, RTX3060, 3pm
    r"|\b\w*_\w+\b"                                     # snake_case
    r"|\b[a-z]+[A-Z]\w*\b"                              # camelCase, iPhone, macOS
    r"|(?<!\S)--?[a-z][\w-]*")                          # command-line flags
_EDGE = ".,;:!?\"'()[]{}<>"


@dataclass
class Verdict:
    ok: bool
    similarity: float
    reason: str = ""


def words(text: str) -> list[str]:
    return [w for w in (m.group(0).lower() for m in _WORD.finditer(text or "")) if w not in _FILLER_SET]


def protected_tokens(text: str) -> list[str]:
    return [t.strip(_EDGE) for t in _PROTECTED.findall(text or "") if t.strip(_EDGE)]


def _in_order(out: list[str], src: list[str]) -> bool:
    it = iter(src)
    return all(any(w == x for x in it) for w in out)


def check(source: str, output: str) -> Verdict:
    src, out = words(source), words(output)
    if not out:
        return Verdict(False, 0.0, "empty result")
    # similarity is reported for diagnostics (logged as a number, never the text)
    sim = difflib.SequenceMatcher(None, src, out, autojunk=False).ratio() if src else 0.0
    for tok in protected_tokens(source):
        if tok not in output:
            return Verdict(False, sim, "a number, name or path changed")
    have, got = Counter(src), Counter(out)
    if got - have:
        return Verdict(False, sim, "words were added or replaced")
    for w in have - got:
        if w not in DROPPABLE and got[w] == 0:
            return Verdict(False, sim, "a word was left out")
    if not _in_order(out, src):
        return Verdict(False, sim, "the word order changed")
    return Verdict(True, sim)
