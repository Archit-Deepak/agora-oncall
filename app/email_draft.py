"""Customer email draft — template filled from the case file (no LLM needed)."""
from __future__ import annotations

from app import scenario
from app.models import CaseFile


def render(cf: CaseFile) -> str:
    a = cf.alert
    started = f"{a.since:%H:%M}" if a else "the start of the incident"
    impact = int(round((a.failure_rate if a else 0) * 100))
    return (
        f"Subject: [Update] Outbound call failures on your account\n\n"
        f"Hi {scenario.CUSTOMER_NAME} team,\n\n"
        f"We detected elevated failures on your outbound calls beginning around {started} "
        f"(roughly {impact}% of attempts). We've traced this to one of your upstream carrier "
        f"routes (carrier X) rejecting calls; your other routes are healthy.\n\n"
        f"We're rerouting affected traffic away from the impacted carrier and are monitoring "
        f"recovery. No action is needed on your side. We'll follow up with a confirmation once "
        f"failure rates are back to baseline.\n\n"
        f"Thanks for your patience,\nVoice Platform Support"
    )


def ticket_payload(cf: CaseFile) -> dict:
    a = cf.alert
    impact = int(round((a.failure_rate if a else 0) * 100))
    started = f"{a.since:%H:%M}" if a else "unknown"
    ruled_out = [h.title for h in cf.hypotheses if h.status == "ruled_out"]
    return {
        "title": f"{scenario.CUSTOMER_NAME}: carrier X rejecting outbound calls",
        "description": (
            f"**Impact:** ~{impact}% of outbound calls failing since {started}.\n"
            f"**Confidence:** {int(round(cf.top_confidence * 100))}%.\n"
            f"**Cause:** carrier X rejecting calls (SIP 503).\n"
            f"**Ruled out:** {', '.join(ruled_out) or 'n/a'}.\n"
            f"**Fix:** reroute affected traffic off carrier X; monitor recovery."
        ),
        "priority": "Urgent",
        "label": "incident",
    }
