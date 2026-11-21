"""AccountWatch — per-account signals over the fleet stream (V2_SPEC "AccountWatch").

Three signals, checked in order per account; the first one breached fires an Alert:
  failure_rate  failed / calls             >= fail_rate    (needs min_calls)
  silent_rate   silent / answered          >= silent_rate  (answered calls with Krisp energy <= 5)
  volume_drop   calls / expected calls     <  volume_ratio (dashboards miss this: no failures at all)

Each signal is evaluated on the full window *and* on its most recent half (with half the
min_calls), so a fault that starts mid-window is caught in ~15-25 s instead of only once it
fills most of the 60 s window. Latches per account until `rearm(account_id)`. When several
accounts breach at once, the one with the most ₹ at risk is returned first.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.fleet import BASELINE_FAIL_RATE, FleetTraffic, is_silent
from app.models import Alert


class AccountWatch:
    def __init__(self, window_s: int = 60, fail_rate: float = 0.20, silent_rate: float = 0.15,
                 volume_ratio: float = 0.5, min_calls: int = 60):
        self.window_s = window_s
        self.fail_rate = fail_rate
        self.silent_rate = silent_rate
        self.volume_ratio = volume_ratio
        self.min_calls = min_calls
        self.latched: set[str] = set()
        self._n = 0

    def rearm(self, account_id: str) -> None:
        self.latched.discard(account_id)

    def signals(self, fleet: FleetTraffic, account_id: str, now: datetime) -> list[tuple[str, float, dict]]:
        """All breached signals for one account: [(signal, value, metrics)], in priority order."""
        out: list[tuple[str, float, dict]] = []
        for win, min_calls in ((self.window_s, self.min_calls), (self.window_s / 2, self.min_calls / 2)):
            m = fleet.window_metrics(account_id, now, win)
            if m["calls"] >= min_calls and m["fail_rate"] >= self.fail_rate:
                out.append(("failure_rate", m["fail_rate"], m))
            if m["answered"] >= min_calls / 2 and m["silent_rate"] >= self.silent_rate:
                out.append(("silent_rate", m["silent_rate"], m))
            if (m["effective_window_s"] >= win and m["expected_calls"] >= min_calls
                    and m["volume_ratio"] < self.volume_ratio):
                out.append(("volume_drop", m["volume_ratio"], m))
        order = {"failure_rate": 0, "silent_rate": 1, "volume_drop": 2}
        out.sort(key=lambda s: order[s[0]])
        return out

    def check(self, fleet: FleetTraffic, now: datetime) -> Alert | None:
        if not fleet.running:
            return None
        hits = []
        for aid, p in fleet.accounts.items():
            if aid in self.latched:
                continue
            sig = self.signals(fleet, aid, now)
            if sig:
                hits.append((aid, p, *sig[0]))
        if not hits:
            return None
        aid, p, signal, value, m = max(hits, key=lambda h: h[4]["at_risk_inr_per_hour"])
        self.latched.add(aid)
        self._n += 1
        calls = m["_calls"]
        if signal == "failure_rate":
            from app.detector import is_failure
            bad = [c.initiation_time for c in calls if is_failure(c)]
        elif signal == "silent_rate":
            bad = [c.initiation_time for c in calls if c.answer_time is not None and is_silent(c)]
        else:
            bad = []
        since = min(bad) if bad else now - timedelta(seconds=m["effective_window_s"])
        return Alert(
            incident_id=f"INC-{now:%Y%m%d}-{self._n}",
            customer_name=p.name, account_id=aid,
            failure_rate=round(m["fail_rate"], 4), baseline_rate=BASELINE_FAIL_RATE,
            since=since, direction=p.direction, window_calls=m["calls"], fired_at=now,
            signal=signal, signal_value=round(value, 4),
        )
