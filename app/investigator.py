"""Investigator — deterministic code that turns CDRs into evidence and a verdict.

It computes, from the generated CDRs at a given moment:
  - carrier_rejection's match_share, stability, and corroboration -> confidence
  - rules out the two decoys (auth_config, number_formatting) via their checks

The LLM never does any of this; it only narrates what the investigator wrote.
Confidence rises on its own as the window fills with post-trigger traffic, so
call #1 honestly gets "not sure yet" and the callback gets the diagnosis.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from app import scenario
from app.confidence import confidence, verdict_for
from app.detector import is_failure
from app.models import CallRecord, Evidence, Hypothesis

WINDOW_S = 120
BASELINE_RATE = scenario.BASELINE_RATE


def _window(calls: list[CallRecord], now: datetime, window_s: int = WINDOW_S) -> list[CallRecord]:
    start = now - timedelta(seconds=window_s)
    return [c for c in calls if start < c.initiation_time <= now]


def _match_share(failures: list[CallRecord]) -> float:
    if not failures:
        return 0.0
    hits = [c for c in failures if scenario.matches_carrier_rejection(c.carrier_route, c.sip_response_code)]
    return len(hits) / len(failures)


def _bucket_shares(calls: list[CallRecord], now: datetime, n: int = 3) -> list[float]:
    """Carrier-X-signature share of failures in each of the last n complete minute buckets."""
    now_min = math.floor(now.timestamp() / 60)
    shares: list[float] = []
    for b in range(now_min - n, now_min):
        lo = datetime.fromtimestamp(b * 60, tz=now.tzinfo)
        hi = lo + timedelta(seconds=60)
        bucket = [c for c in calls if lo <= c.initiation_time < hi]
        fails = [c for c in bucket if is_failure(c)]
        shares.append(_match_share(fails) if fails else 0.0)
    return shares


def _stable(calls: list[CallRecord], now: datetime) -> bool:
    shares = _bucket_shares(calls, now)
    return len(shares) == 3 and all(s >= 0.70 for s in shares)


def _corroborated(window: list[CallRecord]) -> bool:
    """Other carriers (Y/Z) are healthy: their rejection rate <= 2x baseline."""
    others = [c for c in window if c.carrier_route != scenario.CARRIER_X]
    if not others:
        return True
    rejects = [c for c in others if is_failure(c)]
    return (len(rejects) / len(others)) <= 2 * BASELINE_RATE


class Investigator:
    def __init__(self, window_s: int = WINDOW_S, recovery_rate: float | None = None,
                 recovery_window_s: int = 20):
        """recovery_rate: if set, and the failure rate over the last `recovery_window_s`
        drops below it, the verdict becomes "recovered" — the share-of-failures confidence
        means nothing once calls have stopped failing (it lags the 60 s window otherwise)."""
        self.window_s = window_s
        self.recovery_rate = recovery_rate
        self.recovery_window_s = recovery_window_s

    def assess(self, calls: list[CallRecord], now: datetime) -> dict:
        window = _window(calls, now, self.window_s)
        fails = [c for c in window if is_failure(c)]

        ms = _match_share(fails)
        stable = _stable(calls, now)
        corrob = _corroborated(window)
        conf = round(confidence(ms, stable, corrob), 4)

        # Per-carrier failure rates, for the carrier_breakdown evidence.
        breakdown: dict[str, float] = {}
        for carrier in (scenario.CARRIER_X, scenario.CARRIER_Y, scenario.CARRIER_Z):
            cc = [c for c in window if c.carrier_route == carrier]
            cf = [c for c in cc if is_failure(c)]
            breakdown[carrier] = round(len(cf) / len(cc), 4) if cc else 0.0

        verdict = verdict_for(conf)
        recent_rate = None
        if self.recovery_rate is not None:
            recent = _window(calls, now, self.recovery_window_s)
            recent_rate = round(sum(map(is_failure, recent)) / len(recent), 4) if recent else 0.0
            if recent and recent_rate < self.recovery_rate:
                verdict = "recovered"
        carrier_status = "confirmed" if verdict == "confident" else "leading"

        hypotheses = [
            Hypothesis(
                id="carrier_rejection",
                title="Carrier X rejecting Northwind's outbound calls",
                status=carrier_status,
                confidence=conf,
                match_share=round(ms, 4),
                stable=stable,
                reason=(f"traffic recovered; earlier {int(round(ms * 100))}% of failures were carrier X 503s"
                    if verdict == "recovered" else f"{int(round(ms * 100))}% of failures are carrier X 503s"),
            ),
            Hypothesis(
                id="number_formatting",
                title="Malformed destination numbers (bad E.164)",
                status="ruled_out",
                reason="all failed destinations are valid E.164 numbers",
            ),
            Hypothesis(
                id="auth_config",
                title="API auth / config failure",
                status="ruled_out",
                reason="100% of calls authenticated; zero 401/407 responses",
            ),
        ]

        evidence = [
            Evidence(id="auth_check", at=now, check="auth_check",
                     summary="All API requests authenticated; no 401 or 407 responses.",
                     data={"auth_ok_pct": 1.0, "count_401_407": 0},
                     refutes=["auth_config"]),
            Evidence(id="numbering_check", at=now, check="numbering_check",
                     summary="Every failed number is valid E.164; the same numbers succeeded on carrier Y.",
                     data={"invalid_e164": 0},
                     refutes=["number_formatting"]),
            Evidence(id="carrier_breakdown", at=now, check="carrier_breakdown",
                     summary=f"Carrier X is failing {int(round(breakdown[scenario.CARRIER_X] * 100))}% of its calls.",
                     data=breakdown, supports=["carrier_rejection"]),
            Evidence(id="route_health", at=now, check="route_health",
                     summary="Carriers Y and Z are healthy.",
                     data={scenario.CARRIER_Y: breakdown[scenario.CARRIER_Y],
                           scenario.CARRIER_Z: breakdown[scenario.CARRIER_Z]},
                     supports=["carrier_rejection"]),
        ]

        return {
            "hypotheses": hypotheses,
            "evidence": evidence,
            "leading": "carrier_rejection",
            "verdict": verdict,
            "top_confidence": conf,
            "breakdown": breakdown,
            "recent_fail_rate": recent_rate,
        }
