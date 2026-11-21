"""Detector — watches the CDR stream and fires one Alert when failures spike.

Sliding window over the last `window_s` seconds. Fires when the failure rate is
both high in absolute terms and well above the healthy baseline, with enough
calls in the window to be trustworthy. Latches: fires once per incident.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import scenario
from app.models import Alert, CallRecord


def is_failure(c: CallRecord) -> bool:
    # A "failure" is a rejection, not a normal no-answer/busy/cancel.
    return c.sip_response_code is not None and c.sip_response_code not in scenario.NON_FAILURE_SIP


class Detector:
    def __init__(self, window_s: int = 120, rate_threshold: float = 0.20,
                 baseline_rate: float = scenario.BASELINE_RATE, min_calls: int = 120):
        self.window_s = window_s
        self.rate_threshold = rate_threshold
        self.baseline_rate = baseline_rate
        self.min_calls = min_calls
        self.fired = False

    def check(self, calls: list[CallRecord], now: datetime) -> Alert | None:
        if self.fired:
            return None
        window_start = now - timedelta(seconds=self.window_s)
        window = [c for c in calls if window_start < c.initiation_time <= now]
        if len(window) < self.min_calls:
            return None
        fails = [c for c in window if is_failure(c)]
        rate = len(fails) / len(window)
        if rate >= self.rate_threshold and rate >= 3 * self.baseline_rate:
            self.fired = True
            since = min((c.initiation_time for c in fails), default=now)
            return Alert(
                incident_id=f"INC-{now:%Y%m%d}-1",
                customer_name=scenario.CUSTOMER_NAME,
                account_id=scenario.ACCOUNT_ID,
                failure_rate=round(rate, 4),
                baseline_rate=self.baseline_rate,
                since=since,
                direction="outbound",
                window_calls=len(window),
                fired_at=now,
            )
        return None
