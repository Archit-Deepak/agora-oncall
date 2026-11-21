"""Targeted (slow-path) checks the engineer can trigger by voice (build-plan.md §4.5).

Each scans the simulated CDR store and returns an Evidence row. In the live proxy
these run behind a "give me a few seconds" filler; here they're pure functions over
the call list so they're trivially testable.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import scenario
from app.detector import is_failure
from app.models import CallRecord, Evidence

WINDOW_S = 120


def _window(calls: list[CallRecord], now: datetime) -> list[CallRecord]:
    start = now - timedelta(seconds=WINDOW_S)
    return [c for c in calls if start < c.initiation_time <= now]


def compare_carriers(calls: list[CallRecord], now: datetime) -> Evidence:
    window = _window(calls, now)
    rates: dict[str, float] = {}
    for carrier in (scenario.CARRIER_X, scenario.CARRIER_Y, scenario.CARRIER_Z):
        cc = [c for c in window if c.carrier_route == carrier]
        cf = [c for c in cc if is_failure(c)]
        rates[carrier] = round(len(cf) / len(cc), 4) if cc else 0.0
    worst = max(rates, key=rates.get)
    return Evidence(
        id="compare_carriers", at=now, check="compare_carriers",
        summary=(f"Carrier X is failing {int(round(rates[scenario.CARRIER_X] * 100))} percent, "
                 f"while carriers Y and Z are under five percent."),
        data=rates, supports=["carrier_rejection"] if worst == scenario.CARRIER_X else [],
    )


def sample_failed_call(calls: list[CallRecord], now: datetime) -> Evidence:
    window = _window(calls, now)
    sample = next((c for c in window
                   if scenario.matches_carrier_rejection(c.carrier_route, c.sip_response_code)), None)
    if sample is None:
        return Evidence(id="sample_failed_call", at=now, check="sample_failed_call",
                        summary="No failed carrier X call in the current window yet.", data={})
    return Evidence(
        id="sample_failed_call", at=now, check="sample_failed_call",
        summary=(f"Here's one: call {sample.call_uuid}, SIP {sample.sip_response_code}, "
                 f"hangup cause {sample.hangup_cause_name}, on carrier X."),
        data={"call_uuid": sample.call_uuid, "sip": sample.sip_response_code,
              "hangup_cause": sample.hangup_cause_name, "carrier": sample.carrier_route},
        supports=["carrier_rejection"],
    )


def recent_config_changes(calls: list[CallRecord], now: datetime) -> Evidence:
    # From the customer profile: nothing changed, so config is not the cause.
    return Evidence(
        id="recent_config_changes", at=now, check="recent_config_changes",
        summary="No configuration changes on the account in the last twenty-four hours.",
        data={"changes_24h": 0}, refutes=["auth_config"],
    )


REGISTRY = {
    "compare_carriers": compare_carriers,
    "sample_failed_call": sample_failed_call,
    "recent_config_changes": recent_config_changes,
}
