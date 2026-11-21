"""CallSimulator — turns the scripted scenario into Plivo-style CDRs.

Deterministic: same trigger time in, same CDRs out. Generates whole minutes on
demand and caches them; for the current partial minute it only returns calls
whose offset has already "happened" by the queried scenario time, so a detector
polling early sees a realistic partially-filled window.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from app import scenario
from app.models import CallRecord

START_MIN = -5  # generate 5 minutes of healthy baseline before the trigger


class CallSimulator:
    def __init__(self, trigger: datetime, from_number: str = "+19085930363"):
        self.trigger = trigger
        self.from_number = from_number
        self._cache: dict[int, list[tuple[CallRecord, float]]] = {}

    # ── public API ─────────────────────────────────────────────────────────────
    def calls_through(self, t_seconds: float) -> list[CallRecord]:
        """Every CDR from START_MIN up to scenario time t (inclusive, partial minute aware)."""
        last_min = math.floor(t_seconds / 60)
        out: list[CallRecord] = []
        for m in range(START_MIN, last_min + 1):
            for rec, abs_offset in self._minute(m):
                if abs_offset <= t_seconds:
                    out.append(rec)
        return out

    # ── generation ─────────────────────────────────────────────────────────────
    def _minute(self, m: int) -> list[tuple[CallRecord, float]]:
        if m in self._cache:
            return self._cache[m]
        plan = scenario.plan_for_minute(m)

        # Build an outcome token per call for the minute, per carrier.
        tokens: list[tuple[str, int]] = []  # (carrier_route, sip_code)
        for carrier, n in scenario.CALLS_PER_CARRIER.items():
            fails: list[int] = []
            if carrier == scenario.CARRIER_X:
                fails += [scenario.SIP_CARRIER_REJECT] * plan.x_reject
                fails += [scenario.SIP_MISC] * plan.x_misc
            else:
                # split the Y/Z blips across the two carriers
                share = plan.yz_reject // 2 if carrier == scenario.CARRIER_Y else plan.yz_reject - plan.yz_reject // 2
                fails += [scenario.SIP_CARRIER_REJECT] * share
            successes = n - len(fails)
            for sip in fails:
                tokens.append((carrier, sip))
            for _ in range(max(0, successes)):
                tokens.append((carrier, scenario.SIP_OK))

        # Spread the minute's calls across its 60 seconds, interleaving failures
        # evenly so a partial window early in the minute is representative.
        tokens = self._interleave(tokens)
        records: list[tuple[CallRecord, float]] = []
        total = len(tokens)
        for i, (route, sip) in enumerate(tokens):
            offset_in_min = (i * 60.0) / total
            abs_offset = m * 60 + offset_in_min
            records.append((self._make_record(m, i, route, sip, abs_offset), abs_offset))
        self._cache[m] = records
        return records

    @staticmethod
    def _interleave(tokens: list[tuple[str, int]]) -> list[tuple[str, int]]:
        """Deterministically spread failures through the list (no randomness).

        Failures are first round-robined across their kinds (carrier X 503, carrier
        X 480, Y/Z 503) so that any partial-minute window is representative of the
        full minute's mix — otherwise an early poll would look carrier-X-heavy.
        """
        from collections import deque

        raw_fails = [t for t in tokens if t[1] != scenario.SIP_OK]
        oks = [t for t in tokens if t[1] == scenario.SIP_OK]
        if not raw_fails:
            return oks
        groups: dict[tuple[str, int], deque] = {}
        for t in raw_fails:
            groups.setdefault(t, deque()).append(t)
        queues = list(groups.values())
        fails: list[tuple[str, int]] = []
        while any(queues):
            for q in queues:
                if q:
                    fails.append(q.popleft())
        out: list[tuple[str, int]] = []
        step = len(tokens) / len(fails)
        fi = oi = 0
        for i in range(len(tokens)):
            if fi < len(fails) and i >= round(fi * step):
                out.append(fails[fi]); fi += 1
            elif oi < len(oks):
                out.append(oks[oi]); oi += 1
            elif fi < len(fails):
                out.append(fails[fi]); fi += 1
        return out

    def _make_record(self, m: int, i: int, route: str, sip: int, abs_offset: float) -> CallRecord:
        init = self.trigger + timedelta(seconds=abs_offset)
        ok = sip == scenario.SIP_OK
        if ok:
            name, code = scenario.HANGUP_NORMAL
        elif sip == scenario.SIP_CARRIER_REJECT:
            name, code = scenario.HANGUP_CARRIER_REJECT
        else:
            name, code = scenario.HANGUP_MISC
        duration = 42 if ok else 0
        # Valid NANP E.164 destination, deterministic per call.
        seq = (m - START_MIN) * 1000 + i
        to_number = f"+1415{2000000 + seq:07d}"
        return CallRecord(
            call_uuid=f"sim-{m:+03d}-{i:03d}",
            from_number=self.from_number,
            to_number=to_number,
            initiation_time=init,
            answer_time=init + timedelta(seconds=2) if ok else None,
            end_time=init + timedelta(seconds=duration + 2),
            call_duration=duration,
            bill_duration=duration,
            hangup_cause_name=name,
            hangup_cause_code=code,
            hangup_source="Callee" if ok else "Carrier",
            sip_response_code=sip,
            carrier_route=route,
            account_id=scenario.ACCOUNT_ID,
            api_auth_ok=True,  # auth is clean the whole time (auth_config is a decoy)
        )
