"""Deterministic intent router (build-plan.md §5.3).

Classifies the engineer's last utterance before we call the LLM, so a slow check
can emit its "give me a sec" filler as the very first SSE chunk. Regex + keyword
lists over the handful of phrasings that actually come up — fully predictable,
no mid-stream tool surprises on stage.
"""
from __future__ import annotations

import re

FAST = "FAST"
SLOW = "SLOW"
CONFIRM_YES = "CONFIRM_YES"
CONFIRM_NO = "CONFIRM_NO"
END = "END"

# Voice-triggered slow-path checks and the phrasings that map to them.
CHECK_PHRASES: dict[str, list[str]] = {
    "compare_carriers": [
        "other carrier", "carriers ok", "carriers okay", "just one route", "which carrier",
        "all carriers", "carrier y", "carrier z", "compare carrier", "is it one carrier",
        "carrier why", "carrier wise", "other routes",
    ],
    "sample_failed_call": [
        "example call", "call uuid", "sample call", "give me a call", "failed call",
        "show me a call", "an example", "example of a failure",
    ],
    "recent_config_changes": [
        "config change", "change anything", "did they change", "recent change",
        "changed config", "any deploy", "did we change", "changed anything",
    ],
}

_YES = ["yes", "yeah", "yep", "yup", "sure", "go ahead", "do it", "please do", "go for it",
        "raise it", "sounds good", "go right ahead", "let's do it"]
# A yes-word inside a hedge or a question is not approval (sim 21 Nov: "Sure, but wait." and
# "should we do it?" both applied the fix).
_HEDGE = ["but", "maybe", "i guess", "wait", "hold on", "hmm", "perhaps", "might", "probably",
          "kind of", "sort of", "what if", "should we", "should i", "do you think", "is it"]
# "wait" / "hold on" are a pause ("wait, start again"), not a decline: real call, 21 Nov.
_NO = ["no", "not yet", "hold off", "don't", "do not", "nope", "nah", "not now",
       "not sure", "never mind", "cancel", "stop", "dont"]
_END = ["bye", "goodbye", "that's all", "that is all", "hang up", "we're done", "we are done",
        "talk later", "thanks bye", "i'm done", "im done", "i am done", "that's it for now", "call me later"]
# Asking the agent to act ("can you make a ticket?") counts as approval of a proposed fix.
_TICKET = ["ticket", "linear"]
_ACT = ["apply it",
        "apply the fix", "apply that", "fix it", "go ahead", "please proceed", "you can proceed", "do that", "do it", "push the fix",
        "make the change", "let's fix", "reroute", "enable it", "enable ha", "add the ip", "raise the cps",
        "switch it", "move it"]


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def classify(text: str) -> tuple[str, str | None]:
    """Return (intent, check_id). check_id is set only for SLOW."""
    t = text.lower().strip()

    if any(_has_word(t, w) or w in t for w in _END):
        return END, None

    for check, phrases in CHECK_PHRASES.items():
        if any(p in t for p in phrases):
            return SLOW, check

    # Negations first: "I'm not sure", "don't do it" must never read as a yes (a yes
    # changes real systems).
    if any(_has_word(t, w) or (" " in w and w in t) for w in _NO):
        return CONFIRM_NO, None
    if any(_has_word(t, w) or (" " in w and w in t) for w in _YES):
        if any(_has_word(t, h) or (" " in h and h in t) for h in _HEDGE):
            return FAST, None
        if t.rstrip(" .!").endswith("?") and not any(r in t for r in _REQUEST):
            return FAST, None
        return CONFIRM_YES, None

    return FAST, None


# Phrasings that ask for the root cause. Used to mark "verdict told" on the call
# (an unsure answer to one of these is what sets up the callback).
_CAUSE = ["cause", "what's wrong", "what is wrong", "why are", "why is", "why did", "what happened",
          "what's going on", "what is going on", "do you know", "any idea", "figured out",
          "what is it", "what's the issue", "what is the issue", "what's the problem",
          "what is the problem", "root", "diagnos", "is it the carrier", "found it"]


_REQUEST = ("can you", "could you", "will you", "would you", "please", "can we just", "let's")


def asks_to_loop_in(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in ("loop in", "lupe in", "loupe in", "loop them in", "loop the", "loop everyone", "loop all", "cc ", "copy in", "bring in", "add them to the ticket",
                                "add him to the ticket", "add her to the ticket", "escalate to", "notify the", "get the carrier",
                                "get the account", "pull in", "rope in"))


def asks_for_summary(text: str) -> bool:
    t = text.lower()
    return ("telegram" in t or "text me" in t or "message me" in t) and any(
        v in t for v in ("send", "summary", "summar", "text", "message", "drop", "ping", "update"))


def asks_for_ticket(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in _TICKET) and any(v in t for v in ("make", "raise", "open", "create", "file", "log", "need", "have to"))


def asks_to_act(text: str) -> bool:
    """A request to act. A bare question ("should we reroute?") is a question, not approval:
    approval changes real systems, so it must read as a request."""
    t = text.lower().strip()
    if not any(p in t for p in _ACT):
        return False
    if t.endswith("?") and not any(r in t for r in _REQUEST):
        return False
    return True


def asks_cause(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _CAUSE)


# v2: phrasings that need the background investigator to go and look (queued, answered on the call).
_DIG = ["check", "look at", "look into", "dig", "find out", "can you see", "what about", "is it the",
        "is it a", "is it their", "is it our", "could it be", "did they change", "any change", "which carrier",
        "which route", "how many", "show me", "pull", "compare", "is the", "are the", "why is", "why are",
        "when did", "what changed", "who owns", "who is responsible", "whose"]


def needs_investigation(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _DIG)
