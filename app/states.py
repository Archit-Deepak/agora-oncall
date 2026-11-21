"""Allowed transitions for the incident state machine (build-plan.md §7).

Illegal transitions raise, so a logic bug shows up loudly (and in tests) rather
than silently corrupting the demo.
"""
from __future__ import annotations

from app.models import IncidentState as S

ALLOWED: dict[S, set[S]] = {
    S.IDLE: {S.MONITORING},
    S.MONITORING: {S.ALERTED, S.SUSPECTED},
    S.SUSPECTED: {S.ALERTED, S.AUTO_RECOVERED, S.TELEGRAM_SENT},
    S.AUTO_RECOVERED: {S.SUSPECTED, S.ALERTED},
    S.ALERTED: {S.CALLING, S.RETRY, S.TELEGRAM_SENT},
    S.CALLING: {S.IN_CALL, S.RETRY, S.TELEGRAM_SENT},
    S.RETRY: {S.CALLING, S.TELEGRAM_SENT, S.RETRY},
    S.TELEGRAM_SENT: {S.CALLING, S.EXECUTING, S.AUTO_RECOVERED, S.RESOLVED},
    S.IN_CALL: {S.UNSURE_TOLD, S.CONFIRM_ACTIONS, S.WAITING_FOR_EVIDENCE, S.RESOLVED, S.AUTO_RECOVERED,
                S.TELEGRAM_SENT},
    S.UNSURE_TOLD: {S.WAITING_FOR_EVIDENCE, S.IN_CALL, S.AUTO_RECOVERED},
    S.WAITING_FOR_EVIDENCE: {S.CALLING, S.AUTO_RECOVERED},
    S.CONFIRM_ACTIONS: {S.EXECUTING, S.IN_CALL, S.AUTO_RECOVERED, S.TELEGRAM_SENT},
    S.EXECUTING: {S.RESOLVED, S.VERIFYING, S.IN_CALL, S.TELEGRAM_SENT},
    S.VERIFYING: {S.RESOLVED, S.IN_CALL, S.TELEGRAM_SENT},
    S.RESOLVED: set(),
}


def check(current: S, new: S) -> None:
    if new == current:
        return
    if new not in ALLOWED.get(current, set()):
        raise ValueError(f"illegal transition: {current.value} -> {new.value}")
