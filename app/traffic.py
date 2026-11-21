"""LiveTraffic — operator-driven CDR stream for the logs dashboard.

Instead of the scripted per-minute plan, calls are generated in real time at a
steady rate, and their outcome depends on the mode the operator picks:

  pass  → healthy traffic: mostly normal_hangup, plus the ordinary busy / no answer /
          cancelled you'd see in any real Zentrunk log, and ~2% genuine failures.
  fail  → carrier X starts rejecting everything (5350 service_unavailable_by_carrier,
          SIP 503, with some 4340 temporary_unavailable noise). Carriers Y/Z get a burst
          of retry-overflow 503s that decays over ~2 minutes.

The decaying Y/Z burst is what keeps the diagnosis honest: right after the switch,
failures are spread across carriers, so the investigator is genuinely unsure; as the
burst fades, carrier X stands out and confidence climbs past 85% on its own.

Same `calls_through(t)` interface as CallSimulator, so the detector, investigator,
checks and orchestrator don't change. Generation is lazy: each call to
`calls_through` fills in every call up to "now" using the mode that was active at
the time (mode switches flush first), so no background task is needed.
"""
from __future__ import annotations

import math
import random
import uuid
from datetime import datetime, timedelta

from app import scenario
from app.models import CallRecord

CALLS_PER_SEC = 4.0          # 240 calls/min
KEEP_S = 15 * 60             # prune CDRs older than this
CARRIER_WEIGHTS = [(scenario.CARRIER_X, 0.40), (scenario.CARRIER_Y, 0.30), (scenario.CARRIER_Z, 0.30)]

# Retry-overflow burst on Y/Z after the failure starts: 2% + 45% · e^(−t/15s)
BURST_PEAK = 0.45
BURST_TAU_S = 15.0  # tuned: cause confidence crosses 85% ~70-80 s after 'fail' (~30 s into the call)

FROM_NUMBERS = ["+14155550142", "+14155550187", "+16465550119", "+13125550164"]


class LiveTraffic:
    def __init__(self, anchor: datetime, seed: int = 7):
        self.anchor = anchor           # scenario t=0 (same as the orchestrator's trigger)
        self.rng = random.Random(seed)
        self.mode = "pass"
        self.running = False
        self.fail_since: datetime | None = None
        self.calls: list[CallRecord] = []
        self._cursor: datetime | None = None   # time of the next call to generate
        self._seq = 0

    # ── operator controls ────────────────────────────────────────────────────
    def start(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        if not self.running:
            self.running = True
            self._cursor = now

    def stop(self, now: datetime | None = None) -> None:
        self._generate_until(now or datetime.now())
        self.running = False

    def set_mode(self, mode: str, now: datetime | None = None) -> None:
        assert mode in ("pass", "fail")
        now = now or datetime.now()
        self._generate_until(now)          # calls so far keep the old mode
        if mode == "fail" and self.mode != "fail":
            self.fail_since = now
        if mode == "pass":
            self.fail_since = None
        self.mode = mode

    # ── read side (CallSimulator-compatible) ─────────────────────────────────
    def calls_through(self, t_seconds: float) -> list[CallRecord]:
        now = self.anchor + timedelta(seconds=t_seconds)
        self._generate_until(now)
        return [c for c in self.calls if c.initiation_time <= now]

    def recent(self, limit: int = 60) -> list[CallRecord]:
        self._generate_until(datetime.now())
        return self.calls[-limit:][::-1]

    def stats(self, now: datetime | None = None, window_s: int = 60) -> dict:
        from app.detector import is_failure
        now = now or datetime.now()
        self._generate_until(now)
        lo = now - timedelta(seconds=window_s)
        win = [c for c in self.calls if lo < c.initiation_time <= now]
        fails = [c for c in win if is_failure(c)]
        per_carrier = {}
        for carrier, _ in CARRIER_WEIGHTS:
            cc = [c for c in win if c.carrier_route == carrier]
            per_carrier[carrier] = {"calls": len(cc),
                                    "fail_rate": round(sum(map(is_failure, cc)) / len(cc), 4) if cc else 0.0}
        causes: dict[str, int] = {}
        for c in win:
            k = f"{c.hangup_cause_code} {c.hangup_cause_name}"
            causes[k] = causes.get(k, 0) + 1
        # failure rate per 5 s bucket over the last 3 minutes, for the sparkline
        spark = []
        for i in range(36, 0, -1):
            b_hi = now - timedelta(seconds=(i - 1) * 5)
            b_lo = b_hi - timedelta(seconds=5)
            b = [c for c in self.calls if b_lo < c.initiation_time <= b_hi]
            spark.append(round(sum(map(is_failure, b)) / len(b), 4) if b else None)
        return {"running": self.running, "mode": self.mode, "window_s": window_s,
                "calls": len(win), "failed": len(fails),
                "fail_rate": round(len(fails) / len(win), 4) if win else 0.0,
                "per_carrier": per_carrier,
                "causes": dict(sorted(causes.items(), key=lambda kv: -kv[1])),
                "spark": spark}

    # ── generation ───────────────────────────────────────────────────────────
    def _generate_until(self, now: datetime) -> None:
        if not self.running or self._cursor is None:
            return
        step = timedelta(seconds=1.0 / CALLS_PER_SEC)
        while self._cursor <= now:
            self.calls.append(self._make(self._cursor))
            # small jitter so the stream doesn't look metronomic
            self._cursor += step * (0.6 + 0.8 * self.rng.random())
        cutoff = now - timedelta(seconds=KEEP_S)
        if self.calls and self.calls[0].initiation_time < cutoff:
            self.calls = [c for c in self.calls if c.initiation_time >= cutoff]

    def _pick_carrier(self) -> str:
        r, acc = self.rng.random(), 0.0
        for carrier, w in CARRIER_WEIGHTS:
            acc += w
            if r < acc:
                return carrier
        return CARRIER_WEIGHTS[-1][0]

    def _outcome(self, carrier: str, at: datetime) -> tuple[int, tuple[str, int], str]:
        """(sip, (cause_name, cause_code), hangup_source)"""
        r = self.rng.random()
        if self.mode == "fail" and self.fail_since is not None:
            if carrier == scenario.CARRIER_X:
                if r < 0.88:
                    return scenario.SIP_CARRIER_REJECT, scenario.HANGUP_CARRIER_REJECT, "Carrier"
                return scenario.SIP_MISC, scenario.HANGUP_MISC, "Carrier"
            t = (at - self.fail_since).total_seconds()
            burst = BURST_PEAK * math.exp(-t / BURST_TAU_S)
            if r < burst:
                return scenario.SIP_CARRIER_REJECT, scenario.HANGUP_CARRIER_REJECT, "Carrier"
            r = self.rng.random()
        return self._healthy(r)

    @staticmethod
    def _healthy(r: float) -> tuple[int, tuple[str, int], str]:
        # Ordinary life of a call centre's outbound traffic.
        if r < 0.010:
            return scenario.SIP_MISC, scenario.HANGUP_MISC, "Carrier"          # ~1% failure
        if r < 0.018:
            return scenario.SIP_TIMEOUT, scenario.HANGUP_TIMEOUT, "Carrier"    # ~0.8% failure
        if r < 0.080:
            return scenario.SIP_BUSY, scenario.HANGUP_BUSY, "Carrier"
        if r < 0.150:
            return scenario.SIP_CANCELLED, scenario.HANGUP_NO_ANSWER, "Zentrunk"
        if r < 0.165:
            return scenario.SIP_CANCELLED, scenario.HANGUP_CANCELLED, "Customer"
        if r < 0.600:
            return scenario.SIP_OK, scenario.HANGUP_NORMAL, "Carrier"
        return scenario.SIP_OK, scenario.HANGUP_NORMAL_USER, "Customer"

    def _make(self, at: datetime) -> CallRecord:
        self._seq += 1
        carrier = self._pick_carrier()
        sip, (name, code), source = self._outcome(carrier, at)
        answered = sip == scenario.SIP_OK
        ring = round(2 + self.rng.random() * 8, 1)
        if answered:
            duration = int(15 + self.rng.expovariate(1 / 70))
        else:
            duration = 0
        if code == 6030:
            ring = 30.0  # rang out
        end = at + timedelta(seconds=ring + duration)
        return CallRecord(
            call_uuid=str(uuid.UUID(int=self.rng.getrandbits(128), version=4)),
            from_number=self.rng.choice(FROM_NUMBERS),
            to_number=f"+1{self.rng.choice(['415', '646', '312', '213', '617'])}{self.rng.randint(2000000, 9999999)}",
            initiation_time=at,
            answer_time=at + timedelta(seconds=ring) if answered else None,
            end_time=end,
            call_duration=duration,
            bill_duration=(math.ceil(duration / 60) * 60) if duration else 0,
            hangup_cause_name=name,
            hangup_cause_code=code,
            hangup_source=source,
            sip_response_code=sip,
            carrier_route=carrier,
            account_id=scenario.ACCOUNT_ID,
            api_auth_ok=True,
        )
