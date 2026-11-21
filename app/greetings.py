"""Opening lines, rendered from the case file in code (build-plan.md §5.2).

Only alert-time facts or already-computed verdicts, so the agent's first words
never wait on anything.
"""
from __future__ import annotations

from app import config, scenario
from app.models import CaseFile


def _pct(x: float) -> int:
    return int(round(x * 100))


def initial_greeting(cf: CaseFile) -> str:
    a = cf.alert
    assert a is not None
    return (
        f"Hi {config.ENGINEER_NAME}, this is your incident agent. "
        f"{a.customer_name}'s outbound calls started failing at {a.since:%-I:%M %p}. "
        f"About {_pct(a.failure_rate)} percent are failing right now against a normal "
        f"{_pct(a.baseline_rate)} percent. I've already started digging. What do you want to know?"
    )


def callback_greeting(cf: CaseFile) -> str:
    since = cf.alert.since if cf.alert else None
    when = f" since {since:%-I:%M %p}" if since else ""
    return (
        f"It's me again. Found it. Carrier X has been rejecting {scenario.CUSTOMER_NAME}'s "
        f"calls{when}, {_pct(cf.top_confidence)} percent confident. "
        "Want the details, or should I go straight to the fix?"
    )


def debugging_greeting(cf: CaseFile) -> str:
    """Single-call flow: the call goes out as soon as the outage is confirmed, before the cause is known."""
    a = cf.alert
    assert a is not None
    return (
        f"Hi {config.ENGINEER_NAME}, this is your incident agent. "
        f"{a.customer_name}'s outbound calls are failing right now, about {_pct(a.failure_rate)} percent "
        f"over the last minute against a normal {_pct(a.baseline_rate)} percent. "
        "I'm debugging it as we speak and I'll tell you the moment I find the cause. What do you want to know?"
    )


def found_it_update(cf: CaseFile) -> str:
    return (
        f"Update: found it. Carrier X is rejecting {scenario.CUSTOMER_NAME}'s calls with five oh three errors, "
        f"{_pct(cf.top_confidence)} percent confident. Carriers Y and Z are healthy. "
        "Should I raise the ticket and draft the customer email?"
    )


def recovered_update(cf: CaseFile) -> str:
    return (
        f"Update: {scenario.CUSTOMER_NAME}'s calls have recovered on their own. Failures are back to about "
        f"{_pct(cf.recent_fail_rate or 0)} percent. I'll keep watching and send you a summary on Telegram."
    )


def telegram_greeting(cf: CaseFile) -> str:
    return "You asked me to call. " + initial_greeting(cf)
