"""The confidence formula — a pure function, unit-tested in isolation.

For a hypothesis h over the failures in the current window:

    match_share(h)  = failures matching h's signature / all failures in window
    stable(h)       = match_share >= 0.70 in each of the last 3 one-minute buckets
    corroborated(h) = the disconfirming alternative is healthy
                      (carrier_rejection: other carriers' failure rate <= 2x baseline)

    confidence(h)   = min(0.99, match_share + 0.05*stable + 0.05*corroborated)
                      and 0.0 if h is ruled out

The LLM is told this number; it is never allowed to invent or upgrade it.
"""
from __future__ import annotations

STABLE_BONUS = 0.05
CORROBORATED_BONUS = 0.05
CEILING = 0.99


def confidence(match_share: float, stable: bool, corroborated: bool, ruled_out: bool = False) -> float:
    if ruled_out:
        return 0.0
    score = match_share + (STABLE_BONUS if stable else 0.0) + (CORROBORATED_BONUS if corroborated else 0.0)
    return min(CEILING, score)


def verdict_for(top_confidence: float) -> str:
    """Map a confidence to the spoken verdict band (build-plan.md §4.4)."""
    if top_confidence >= 0.85:
        return "confident"
    if top_confidence >= 0.60:
        return "leaning"
    return "unsure"
