"""The scripted incident: Northwind Health, carrier X rejecting outbound calls.

This holds the deterministic story — who the customer is, the three carriers, the
baseline, the hypothesis signatures, and a per-minute plan of how many calls fail
and why, relative to the trigger (t=0).

The confidence the agent reports is always *computed* from the generated CDRs
(see investigator/confidence). Only the per-minute plan here is scripted, so the
same run always produces the same numbers. Nothing is faked downstream.
"""
from __future__ import annotations

from dataclasses import dataclass

CUSTOMER_NAME = "Northwind Health"
ACCOUNT_ID = "acct_northwind"
BASELINE_RATE = 0.02  # ~2% failures when healthy

CARRIER_X = "carrier_x"  # the one that goes bad
CARRIER_Y = "carrier_y"
CARRIER_Z = "carrier_z"

# Per-minute traffic split (calls per carrier per minute). 100 calls/min total.
CALLS_PER_CARRIER = {CARRIER_X: 40, CARRIER_Y: 30, CARRIER_Z: 30}
CALLS_PER_MIN = sum(CALLS_PER_CARRIER.values())

# SIP codes we use.
SIP_OK = 200
SIP_CARRIER_REJECT = 503  # carrier X rejecting → the real cause's signature
SIP_MISC = 480  # transient blip on carrier X, NOT the rejection signature
SIP_INVALID_NUMBER = 484  # number-formatting signature (decoy; never actually emitted)
SIP_AUTH = 401  # auth/config signature (decoy; never actually emitted)

# Hangup cause names/codes from Plivo's Zentrunk hangup code table
# (plivo.com/docs/sip-trunking/troubleshooting/zentrunk-hangup-codes).
HANGUP_NORMAL = ("normal_hangup", 3000)             # normal hangup from carrier
HANGUP_NORMAL_USER = ("normal_hangup", 3010)        # normal hangup from user
HANGUP_CARRIER_REJECT = ("service_unavailable_by_carrier", 5350)  # SIP 503
HANGUP_MISC = ("temporary_unavailable", 4340)       # SIP 480, carrier side
HANGUP_BUSY = ("user_busy", 4410)                   # SIP 486 — normal outcome, not a failure
HANGUP_NO_ANSWER = ("alloted_timeout", 6030)        # answer timeout — normal outcome
HANGUP_CANCELLED = ("user_cancelled", 4550)         # caller gave up — normal outcome
HANGUP_TIMEOUT = ("request_timeout_carrier", 4160)  # SIP 408 — rare baseline failure

SIP_BUSY = 486
SIP_CANCELLED = 487  # request terminated (no answer / caller cancel)
SIP_TIMEOUT = 408
# Outcomes that are ordinary call life, not platform/carrier failures.
NON_FAILURE_SIP = {200, SIP_BUSY, SIP_CANCELLED}


@dataclass(frozen=True)
class MinutePlan:
    """How many calls fail, and how, in one scenario minute."""

    x_reject: int  # carrier X, 503 (the signature)
    x_misc: int    # carrier X, 480 (noise, not the signature)
    yz_reject: int  # carriers Y/Z, 503 (transient blips — elevated early, clears late)


def plan_for_minute(m: int) -> MinutePlan:
    """m is the scenario minute relative to trigger (0 = first bad minute)."""
    if m < 0:
        # Healthy: ~2 failures / 100 calls, spread over Y/Z, carrier X clean.
        return MinutePlan(x_reject=0, x_misc=0, yz_reject=2)
    # Incident: carrier X steadily rejecting; a retry-driven burst of Y/Z blips
    # that decays back to baseline over the first few minutes.
    yz_burst = {0: 24, 1: 14, 2: 6}.get(m, 2)
    return MinutePlan(x_reject=34, x_misc=5, yz_reject=yz_burst)


# ── Hypotheses the investigator weighs ───────────────────────────────────────
HYPOTHESES = [
    ("carrier_rejection", "Carrier X rejecting Northwind's outbound calls"),
    ("number_formatting", "Malformed destination numbers (bad E.164)"),
    ("auth_config", "API auth / config failure"),
]


def matches_carrier_rejection(route: str, sip: int | None) -> bool:
    return route == CARRIER_X and sip in (SIP_CARRIER_REJECT, 403)


def matches_number_formatting(sip: int | None) -> bool:
    return sip == SIP_INVALID_NUMBER


def matches_auth_config(sip: int | None, api_auth_ok: bool) -> bool:
    return (sip in (SIP_AUTH, 407)) or (api_auth_ok is False)
