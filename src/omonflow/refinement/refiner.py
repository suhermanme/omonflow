"""Text refinement: Raw, Clean or AI (SPEC §10). Independent of speech recognition.

AI never loses text: on any failure (server down, timeout, an answer that
changed the wording) the Clean text is used and the reason is reported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from omonflow.refinement import guard
from omonflow.refinement.ai import AIClient, AIConfig, AIError
from omonflow.refinement.clean import clean_text

LOG = logging.getLogger("omonflow.refine")
MODES = ("raw", "clean", "ai")


@dataclass
class RefineResult:
    text: str
    mode: str                  # mode actually used: raw | clean | ai
    requested: str             # mode the user chose
    ms: float = 0.0
    fallback_reason: str = ""  # set when AI was requested but Clean text is used

    @property
    def fell_back(self) -> bool:
        return bool(self.fallback_reason)


def local_refine(text: str, mode: str) -> RefineResult:
    """Raw and Clean: instant, on this computer."""
    if mode == "raw":
        return RefineResult(text, "raw", mode)
    return RefineResult(clean_text(text) or text, "clean", mode)


def ai_refine(text: str, cfg: AIConfig, client_factory=AIClient) -> RefineResult:
    """Blocking; run on a worker thread. Logs timings and verdicts only, never text."""
    fallback = clean_text(text) or text
    try:
        out, ms = client_factory(cfg).refine(text)
    except AIError as exc:
        LOG.info("AI refinement failed: %s", exc)
        return RefineResult(fallback, "clean", "ai", fallback_reason=str(exc))
    except Exception as exc:  # noqa: BLE001 - never lose the text over a bug
        LOG.warning("AI refinement error", exc_info=True)
        return RefineResult(fallback, "clean", "ai", fallback_reason=f"Unexpected error ({type(exc).__name__}).")
    verdict = guard.check(text, out)
    LOG.info("AI refinement: %.0f ms, similarity %.2f, %s", ms, verdict.similarity,
             "accepted" if verdict.ok else f"rejected ({verdict.reason})")
    if not verdict.ok:
        return RefineResult(fallback, "clean", "ai", ms,
                            fallback_reason=f"The AI result was not used: {verdict.reason}.")
    return RefineResult(out, "ai", "ai", ms)
