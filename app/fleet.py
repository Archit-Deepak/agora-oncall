"""FleetTraffic — real-time CDR + edge-log stream for several accounts (V2_SPEC "FleetTraffic API").

Same lazy approach as app/traffic.py: nothing runs in the background. Every read fills in
calls up to "now" using the faults / account config active at each call's start time, and
every control (set_fault, apply_fix, start/stop) flushes first so history keeps the state
that was live when it happened. Deterministic per seed (one RNG per account).

Faults only change the *conditions* (a carrier rejecting, a dialer bursting, media moved to
us-east-1, the customer's SBC on a new IP, …). Outcomes are derived from the live account
config (CPS limit, HA, routes, ACL, caller ID, media region), so a fix works because it
changes the config, exactly like it would for real.
"""
from __future__ import annotations

import ipaddress
import math
import random
import re
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.accounts import INDIA_MEDIA_REGIONS, AccountProfile, default_accounts, spend_at_risk_per_hour
from app.models import CallRecord

KEEP_S = 15 * 60
SILENT_ENERGY = 5.0
BASELINE_FAIL_RATE = 0.02

# Zentrunk hangup causes (name, code) + the SIP response we record with them.
NORMAL = ("normal_hangup", 3000)            # normal hangup from carrier side
NORMAL_USER = ("normal_hangup", 3010)       # normal hangup from user (our customer)
CARRIER_REJECT = ("service_unavailable_by_carrier", 5350)   # SIP 503
TEMP_UNAVAILABLE = ("temporary_unavailable", 4340)          # SIP 480
TIMEOUT_CARRIER = ("request_timeout_carrier", 4160)         # SIP 408
BUSY = ("user_busy", 4410)                                  # SIP 486 (not a failure)
NO_ANSWER = ("alloted_timeout", 6030)                       # SIP 487 (not a failure)
CANCELLED = ("user_cancelled", 4550)                        # SIP 487 (not a failure)
CPS_LIMIT = ("cps_limit_reached", 5180)                     # SIP 503, Zentrunk
TIMEOUT_CUSTOMER = ("request_timeout_customer", 4170)       # SIP 408, customer PBX didn't answer INVITE
ANCHORING = ("domestic_anchored_terms_not_met", 4590)       # SIP 403

FAULTS = ("carrier_reject", "cps_burst", "silent_route", "pbx_timeout", "anchoring_4590", "acl_drop")
FIXES = ("enable_ha", "raise_cps", "pace_dialer", "reroute_destination", "switch_caller_id",
         "move_media_to_india", "add_ip_to_acl", "notify_customer")

FAILING_CARRIER = "TATA"        # carrier_reject / silent_route
SILENT_DESTINATION = "Vi-Delhi"  # silent_route
LCR_VI_DELHI_VIA_TATA = 0.93    # least-cost routing sends most Vi-Delhi traffic via TATA
BURST_PEAK, BURST_TAU_S = 0.45, 15.0   # retry overflow on the other carriers (carrier_reject)
CPS_BURST_FACTOR, CPS_CYCLE_S, CPS_BURST_S, CPS_IDLE_RATE = 3, 12.0, 5.0, 1.0
PBX_TIMEOUT_SHARE = 0.88
ACL_NEW_IP_SHARE = 0.95
CPS_PRICE_INR_MONTH = 1200

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_SCANNERS = ["185.243.115.84", "45.95.147.229", "193.46.255.103", "80.94.95.211", "141.98.11.37"]
_SCAN_REASONS = [("INVITE", "source IP not in trunk IP ACL"), ("INVITE", "source IP not in trunk IP ACL"),
                 ("OPTIONS", "source IP not in trunk IP ACL"), ("REGISTER", "method not allowed on termination trunk")]
_NEW_SBC_IP = {"acct_northwind": "34.93.201.77", "acct_zylo": "34.93.172.19", "acct_medley": "34.93.240.6"}
_HA_BACKUP = {"acct_northwind": ("+918040077310", "Airtel"), "acct_zylo": ("+912261800499", "Jio"),
              "acct_medley": ("+912240088199", "Airtel")}
_MOBILE_PREFIX = {"Jio": "70", "Airtel": "98", "Vi-Delhi": "99", "BSNL": "94", "Vi": "97"}


def is_silent(c: CallRecord) -> bool:
    return c.audio_energy is not None and c.audio_energy <= SILENT_ENERGY


def _is_failure(c: CallRecord) -> bool:
    from app.detector import is_failure
    return is_failure(c)


@dataclass
class _Acct:
    rng: random.Random
    cursor: float | None = None          # scenario seconds of the next call attempt
    next_noise: float | None = None      # next scanner hit on the trunk edge
    next_ping: float | None = None       # next SIP OPTIONS keepalive (inbound)
    calls: list[CallRecord] = field(default_factory=list)
    ts: list[float] = field(default_factory=list)
    edge: list[dict] = field(default_factory=list)
    edge_ts: list[float] = field(default_factory=list)
    cps_sec: int = -10**9
    cps_count: int = 0


class FleetTraffic:
    def __init__(self, anchor: datetime, seed: int = 7, accounts: dict[str, AccountProfile] | None = None):
        self.anchor = anchor
        self.seed = seed
        self.accounts: dict[str, AccountProfile] = accounts if accounts is not None else default_accounts(anchor)
        self.running = False
        self.started_at: datetime | None = None
        self._st = {aid: _Acct(rng=random.Random(f"{seed}:{aid}")) for aid in self.accounts}
        self._faults: dict[str, dict] = {}
        self.fix_log: list[dict] = []

    # ── time helpers ─────────────────────────────────────────────────────────
    def _rel(self, dt: datetime) -> float:
        return (dt - self.anchor).total_seconds()

    def _abs(self, s: float) -> datetime:
        return self.anchor + timedelta(seconds=s)

    # ── operator controls ────────────────────────────────────────────────────
    def start(self, now: datetime | None = None) -> None:
        now = now or datetime.now()
        if self.running:
            return
        self.running = True
        self.started_at = now
        r = self._rel(now)
        for st in self._st.values():
            st.cursor = r
            st.next_noise = r + st.rng.expovariate(1 / 20)
            st.next_ping = r + 10.0

    def stop(self, now: datetime | None = None) -> None:
        self._generate_until(now or datetime.now())
        self.running = False

    def set_fault(self, account_id: str, fault: str | None, now: datetime | None = None) -> None:
        now = now or datetime.now()
        p = self._profile(account_id)
        if fault is not None:
            if fault not in FAULTS:
                raise ValueError(f"unknown fault {fault!r}; expected one of {FAULTS}")
            why = self._fault_not_applicable(p, fault)
            if why:
                raise ValueError(f"{fault} does not apply to {p.name}: {why}")
        self._generate_until(now)
        self._clear(account_id)
        if fault is None:
            return
        f = {"fault": fault, "since": now, "restore": {}}
        if fault == "anchoring_4590":
            f["restore"]["media_region"] = p.media_region
            p.media_region = "us-east-1"
            p.change_log.append({"at": now, "change": "Media/agent infra moved to us-east-1 "
                                 "(voice-agent workers and media relay redeployed in N. Virginia)", "by": "customer"})
        elif fault == "acl_drop":
            f["restore"]["sbc_ip"] = p.sbc_ip
            p.sbc_ip = _NEW_SBC_IP.get(account_id, "34.93.199.10")
            p.change_log.append({"at": now, "change": "Customer migrated SBC/egress to new IP "
                                 "(new NAT gateway in front of the dialer SBC)", "by": "customer"})
        elif fault == "cps_burst":
            f["burst_cps"] = CPS_BURST_FACTOR * p.cps_limit
            f["paced"] = False
        self._faults[account_id] = f

    def clear_all(self, now: datetime | None = None) -> None:
        self._generate_until(now or datetime.now())
        for aid in list(self._faults):
            self._clear(aid)

    def _clear(self, account_id: str) -> None:
        f = self._faults.pop(account_id, None)
        if f:
            for k, v in f["restore"].items():
                setattr(self.accounts[account_id], k, v)

    def faults(self) -> dict[str, dict]:
        return {aid: {"fault": f["fault"], "since": f["since"], "mitigated": self._mitigated(aid)}
                for aid, f in self._faults.items()}

    @staticmethod
    def _fault_not_applicable(p: AccountProfile, fault: str) -> str | None:
        carriers = [c for c, _ in p.carriers]
        if fault == "pbx_timeout":
            return None if p.direction == "inbound" else "only inbound trunks deliver calls to a customer PBX"
        if p.direction != "outbound":
            return "account has no outbound traffic"
        if fault in ("carrier_reject", "silent_route") and FAILING_CARRIER not in carriers:
            return f"account does not route via {FAILING_CARRIER}"
        if fault == "silent_route" and SILENT_DESTINATION not in [d for d, _ in p.destinations]:
            return f"account does not call {SILENT_DESTINATION}"
        if fault == "acl_drop" and not p.ip_acl:
            return "account has no trunk IP ACL"
        return None

    def _mitigated(self, aid: str) -> bool:
        f, p = self._faults.get(aid), self.accounts[aid]
        if not f:
            return False
        kind = f["fault"]
        if kind == "carrier_reject":
            return p.ha_enabled
        if kind == "cps_burst":
            return f["paced"] or p.cps_limit >= f["burst_cps"] + 2
        if kind == "silent_route":
            return p.route_overrides.get(SILENT_DESTINATION, FAILING_CARRIER) != FAILING_CARRIER
        if kind == "anchoring_4590":
            return p.media_region in INDIA_MEDIA_REGIONS or bool(p.caller_id_override and not p.caller_id_override.startswith("+91"))
        if kind == "acl_drop":
            return _ip_in_acl(p.sbc_ip, p.ip_acl)
        return False  # pbx_timeout: nothing Plivo can do

    # ── fixes ────────────────────────────────────────────────────────────────
    def apply_fix(self, account_id: str, fix: str, params: dict | None = None,
                  now: datetime | None = None) -> dict:
        now = now or datetime.now()
        params = dict(params or {})
        if account_id not in self.accounts:
            return {"ok": False, "message": f"unknown account {account_id!r}", "cost": None}
        p = self.accounts[account_id]
        fn = getattr(self, f"_fix_{fix}", None) if fix in FIXES else None
        if fn is None:
            return {"ok": False, "message": f"unknown fix {fix!r}; valid: {', '.join(FIXES)}", "cost": None}
        self._generate_until(now)
        try:
            res = fn(account_id, p, params, now)
        except (TypeError, ValueError) as e:
            return {"ok": False, "message": f"bad params for {fix}: {e}", "cost": None}
        if res["ok"]:
            p.change_log.append({"at": now, "change": res.pop("change"), "by": "plivo-oncall-agent"})
            self.fix_log.append({"at": now, "account_id": account_id, "fix": fix, "params": params,
                                 "message": res["message"]})
        else:
            res.pop("change", None)
        return res

    @staticmethod
    def _reject(msg: str) -> dict:
        return {"ok": False, "message": msg, "cost": None}

    def _fix_enable_ha(self, aid, p, params, now):
        if p.direction != "outbound":
            return self._reject("HA numbers protect outbound caller IDs; this account is inbound-only.")
        if p.ha_enabled:
            return self._reject(f"HA is already enabled on {p.name} (backup {p.ha_backup_number or 'configured'}).")
        num, carrier = _HA_BACKUP.get(aid, ("+918040099990", "Airtel"))
        p.ha_enabled, p.ha_backup_number = True, num
        return {"ok": True, "cost": f"One extra India local number rental ({num}, {carrier}) at the standard "
                "India local number monthly rate",
                "message": f"HA enabled: calls rejected by the primary carrier now retry once via backup "
                           f"number {num} on {carrier}.",
                "change": f"HA numbers enabled; backup caller ID {num} ({carrier})"}

    def _fix_raise_cps(self, aid, p, params, now):
        cur = p.cps_limit
        if "cps_limit" in params:
            target = int(params["cps_limit"])
        elif "add" in params:
            target = cur + int(params["add"])
        else:
            target = max(cur + 1, self.peak_cps(aid, now, 60) + 2)  # observed peak + 2 CPS headroom
        if target <= cur:
            return self._reject(f"CPS limit is already {cur}; target {target} is not higher.")
        if target > 50:
            return self._reject(f"{target} CPS needs a sales-approved plan change (max self-serve 50).")
        delta = target - cur
        p.cps_limit, p.concurrency = target, p.concurrency + 25 * delta
        return {"ok": True, "cost": f"+{25 * delta} concurrency (+{delta} CPS) ≈ ₹{CPS_PRICE_INR_MONTH * delta:,}/month "
                f"(₹{CPS_PRICE_INR_MONTH:,}/month per CPS; 25 concurrency = 1 CPS)",
                "message": f"CPS limit raised {cur} → {target} (concurrency now {p.concurrency}).",
                "change": f"CPS limit raised from {cur} to {target} (concurrency {p.concurrency - 25 * delta} → {p.concurrency})"}

    def _fix_pace_dialer(self, aid, p, params, now):
        if p.direction != "outbound":
            return self._reject("No outbound dialer on this account.")
        cap = int(params.get("max_cps", p.cps_limit))
        if cap < 1:
            raise ValueError("max_cps must be >= 1")
        if cap > p.cps_limit:
            return self._reject(f"max_cps {cap} is above the account CPS limit {p.cps_limit}; calls over it still get 5180.")
        f = self._faults.get(aid)
        if f and f["fault"] == "cps_burst":
            f["paced"] = True
        return {"ok": True, "cost": "Free (customer-side dialer setting)",
                "message": f"Asked {p.name} to cap their dialer at {cap} calls/s and smooth bursts; "
                           f"assuming the change is live now.",
                "change": f"Customer dialer paced to max {cap} CPS"}

    def _fix_reroute_destination(self, aid, p, params, now):
        if p.direction != "outbound":
            return self._reject("Routing overrides apply to outbound traffic only.")
        dest = str(params.get("to_carrier", SILENT_DESTINATION))
        dests = [d for d, _ in p.destinations]
        carriers = [c for c, _ in p.carriers]
        if dest not in dests:
            return self._reject(f"{p.name} does not call {dest!r}; destinations: {dests}")
        via = str(params.get("via", "Jio" if "Jio" in carriers else carriers[0]))
        if via not in carriers:
            return self._reject(f"{via!r} is not one of {p.name}'s carriers {carriers}")
        if p.route_overrides.get(dest) == via:
            return self._reject(f"{dest} is already routed via {via}.")
        p.route_overrides[dest] = via
        return {"ok": True, "cost": "No charge (routing change; per-minute rate on the new route may differ slightly)",
                "message": f"Calls to {dest} now route via {via}.",
                "change": f"Routing override: destination {dest} via {via}"}

    def _fix_switch_caller_id(self, aid, p, params, now):
        if p.direction != "outbound":
            return self._reject("Caller ID applies to outbound calls only.")
        cid = params.get("caller_id") or next((c for c in p.caller_ids if not c.startswith("+91")), None)
        if not cid:
            return self._reject("No non-Indian caller ID on the account; pass caller_id (E.164, not +91).")
        cid = str(cid)
        if not _E164.match(cid):
            return self._reject(f"{cid!r} is not a valid E.164 number.")
        if cid.startswith("+91"):
            return self._reject("An Indian (+91) caller ID with media outside India still breaks domestic anchoring; "
                                "use a non-Indian caller ID or move media to India.")
        if p.caller_id_override == cid:
            return self._reject(f"Caller ID is already {cid}.")
        owned = cid in p.caller_ids
        if not owned:
            p.caller_ids.append(cid)
        p.caller_id_override = cid
        return {"ok": True, "cost": "No charge (number already on the account)" if owned
                else f"One new number rental for {cid}",
                "message": f"Outbound calls now present caller ID {cid}.",
                "change": f"Outbound caller ID switched to {cid}"}

    def _fix_move_media_to_india(self, aid, p, params, now):
        region = str(params.get("region", "ap-south-1"))
        if region not in INDIA_MEDIA_REGIONS:
            return self._reject(f"{region!r} is not an India media region {sorted(INDIA_MEDIA_REGIONS)}.")
        if p.media_region in INDIA_MEDIA_REGIONS:
            return self._reject(f"Media is already anchored in India ({p.media_region}).")
        old, p.media_region = p.media_region, region
        return {"ok": True, "cost": "No Plivo charge; customer runs agent/media workers in the India region",
                "message": f"Media/agent infra moved {old} → {region}; India→India calls are anchored in India again.",
                "change": f"Media region moved from {old} to {region}"}

    def _fix_add_ip_to_acl(self, aid, p, params, now):
        if "ip" not in params:
            raise ValueError("missing 'ip'")
        raw = str(params["ip"]).strip()
        net = ipaddress.ip_network(raw if "/" in raw else f"{raw}/32", strict=False)
        if not p.ip_acl and p.direction == "inbound":
            return self._reject("This inbound trunk has no ingress IP ACL.")
        if net.prefixlen < 24:
            return self._reject(f"{net} is too broad for a trunk ACL (use /24 or narrower).")
        if any(net.subnet_of(ipaddress.ip_network(a, strict=False)) for a in p.ip_acl):
            return self._reject(f"{net} is already allowed by the trunk IP ACL {p.ip_acl}.")
        p.ip_acl.append(str(net))
        return {"ok": True, "cost": "Free",
                "message": f"Added {net} to trunk {p.trunk_id} IP ACL.",
                "change": f"Trunk {p.trunk_id} IP ACL: added {net}"}

    def _fix_notify_customer(self, aid, p, params, now):
        to = p.contacts[0] if p.contacts else {"name": "customer", "email": "-"}
        msg = params.get("message") or "Please check reachability of your SIP endpoint from Plivo's signalling IPs."
        return {"ok": True, "cost": "Free",
                "message": f"Email drafted to {to['name']} <{to['email']}>: {msg} "
                           f"(no Plivo-side config change; recovery depends on the customer)",
                "change": f"Customer notified ({to['email']})"}

    # ── read side ────────────────────────────────────────────────────────────
    def calls_through(self, t_seconds: float) -> list[CallRecord]:
        now = self.anchor + timedelta(seconds=t_seconds)
        return self.calls(None, self.anchor - timedelta(seconds=KEEP_S * 2), now)

    def calls(self, account_id: str | None, since: datetime, until: datetime) -> list[CallRecord]:
        self._generate_until(until)
        lo, hi = self._rel(since), self._rel(until)
        if account_id is not None:
            st = self._st[account_id]
            return st.calls[bisect_right(st.ts, lo):bisect_right(st.ts, hi)]
        out: list[CallRecord] = []
        for st in self._st.values():
            out.extend(st.calls[bisect_right(st.ts, lo):bisect_right(st.ts, hi)])
        out.sort(key=lambda c: c.initiation_time)
        return out

    def edge_log(self, account_id: str, since: datetime, until: datetime) -> list[dict]:
        self._generate_until(until)
        st = self._st[account_id]
        lo, hi = self._rel(since), self._rel(until)
        return st.edge[bisect_right(st.edge_ts, lo):bisect_right(st.edge_ts, hi)]

    def expected_rate(self, account_id: str) -> float:
        return self.accounts[account_id].calls_per_s

    def recent(self, limit: int = 60, account_id: str | None = None) -> list[CallRecord]:
        now = datetime.now()
        self._generate_until(now)
        ids = [account_id] if account_id else list(self._st)
        out: list[CallRecord] = []
        for aid in ids:
            out.extend(self._st[aid].calls[-limit:])
        out.sort(key=lambda c: c.initiation_time, reverse=True)
        return out[:limit]

    def peak_cps(self, account_id: str, now: datetime, window_s: float = 60) -> int:
        calls = self.calls(account_id, now - timedelta(seconds=window_s), now)
        per: dict[int, int] = {}
        for c in calls:
            k = int(math.floor(self._rel(c.initiation_time)))
            per[k] = per.get(k, 0) + 1
        return max(per.values(), default=0)

    def window_metrics(self, account_id: str, now: datetime, window_s: float = 60) -> dict:
        """Core numbers for one account over (now - window_s, now]; shared by stats, AccountWatch and tools."""
        calls = self.calls(account_id, now - timedelta(seconds=window_s), now)
        n = len(calls)
        failed = sum(1 for c in calls if _is_failure(c))
        answered = [c for c in calls if c.answer_time is not None]
        silent = sum(1 for c in answered if is_silent(c))
        eff = window_s
        if self.started_at is not None:
            eff = max(0.0, min(window_s, (now - self.started_at).total_seconds()))
        expected = self.expected_rate(account_id) * eff
        vol = n / expected if expected > 0 else 1.0
        fail_rate = failed / n if n else 0.0
        silent_rate = silent / len(answered) if answered else 0.0
        bad_share = max((failed + silent) / n if n else 0.0, 1 - vol if eff >= window_s / 2 else 0.0)
        return {"calls": n, "failed": failed, "answered": len(answered), "silent": silent,
                "fail_rate": fail_rate, "silent_rate": silent_rate, "expected_calls": expected,
                "effective_window_s": eff, "volume_ratio": vol,
                "at_risk_inr_per_hour": spend_at_risk_per_hour(self.accounts[account_id], bad_share),
                "_calls": calls}

    def stats(self, now: datetime | None = None, window_s: int = 60) -> dict:
        now = now or datetime.now()
        self._generate_until(now)
        out = {}
        for aid, p in self.accounts.items():
            m = self.window_metrics(aid, now, window_s)
            causes: dict[str, int] = {}
            for c in m["_calls"]:
                k = f"{c.hangup_cause_code} {c.hangup_cause_name}"
                causes[k] = causes.get(k, 0) + 1
            st = self._st[aid]
            spark, spark_silent, spark_vol = [], [], []
            exp5 = self.expected_rate(aid) * 5
            r_now = self._rel(now)
            for i in range(36, 0, -1):
                hi = r_now - (i - 1) * 5
                b = st.calls[bisect_right(st.ts, hi - 5):bisect_right(st.ts, hi)]
                ans = [c for c in b if c.answer_time is not None]
                spark.append(round(sum(map(_is_failure, b)) / len(b), 4) if b else None)
                spark_silent.append(round(sum(map(is_silent, ans)) / len(ans), 4) if ans else None)
                started = self.started_at is not None and self._abs(hi - 5) >= self.started_at
                spark_vol.append(round(len(b) / exp5, 3) if started else None)
            f = self._faults.get(aid)
            out[aid] = {
                "name": p.name, "spend_per_day_inr": p.spend_per_day_inr, "direction": p.direction,
                "calls": m["calls"], "failed": m["failed"], "fail_rate": round(m["fail_rate"], 4),
                "answered": m["answered"], "silent": m["silent"], "silent_rate": round(m["silent_rate"], 4),
                "volume_ratio": round(m["volume_ratio"], 3),
                "at_risk_inr_per_hour": m["at_risk_inr_per_hour"],
                "fault": f["fault"] if f else None,
                "mitigated": self._mitigated(aid) if f else False,
                "top_causes": dict(sorted(causes.items(), key=lambda kv: -kv[1])[:6]),
                "spark": spark, "spark_silent": spark_silent, "spark_volume": spark_vol,
            }
        return {"running": self.running, "window_s": window_s, "accounts": out}

    # ── generation ───────────────────────────────────────────────────────────
    def _profile(self, account_id: str) -> AccountProfile:
        if account_id not in self.accounts:
            raise KeyError(f"unknown account {account_id!r}")
        return self.accounts[account_id]

    def _generate_until(self, now: datetime) -> None:
        if not self.running:
            return
        r_now = self._rel(now)
        for aid, st in self._st.items():
            if st.cursor is None:
                continue
            p = self.accounts[aid]
            outbound = p.direction == "outbound"
            while True:
                t = min(st.cursor, st.next_noise if outbound else st.next_ping)
                if t > r_now:
                    break
                if t == st.cursor:
                    self._attempt(aid, p, st, t)
                    st.cursor = t + self._gap(aid, p, st, t)
                elif outbound:
                    self._scanner_hit(p, st, t)
                    st.next_noise = t + st.rng.expovariate(1 / 20)
                else:
                    self._options_ping(aid, p, st, t)
                    st.next_ping = t + 10.0
            cutoff = r_now - KEEP_S
            if st.ts and st.ts[0] < cutoff - 30:
                i = bisect_left(st.ts, cutoff)
                del st.ts[:i], st.calls[:i]
            if st.edge_ts and st.edge_ts[0] < cutoff - 30:
                i = bisect_left(st.edge_ts, cutoff)
                del st.edge_ts[:i], st.edge[:i]

    def _gap(self, aid: str, p: AccountProfile, st: _Acct, t: float) -> float:
        rng = st.rng
        f = self._faults.get(aid)
        if f and f["fault"] == "cps_burst" and not f["paced"]:
            phase = (t - self._rel(f["since"])) % CPS_CYCLE_S
            if phase < CPS_BURST_S:
                return (1.0 / f["burst_cps"]) * (0.8 + 0.4 * rng.random())
            return (1.0 / CPS_IDLE_RATE) * (0.7 + 0.6 * rng.random())
        rate = p.calls_per_s
        if p.bursty:  # predictive dialer: slow waves around agent availability, still under the CPS limit
            rate *= 1 + 0.2 * math.sin(2 * math.pi * t / 90.0)
        return (1.0 / rate) * (0.65 + 0.7 * rng.random())

    def _edge(self, st: _Acct, t: float, entry: dict) -> None:
        entry["at"] = self._abs(t)
        st.edge.append(entry)
        st.edge_ts.append(t)

    def _scanner_hit(self, p: AccountProfile, st: _Acct, t: float) -> None:
        ip = st.rng.choice(_SCANNERS)
        method, reason = st.rng.choice(_SCAN_REASONS)
        self._edge(st, t, {"trunk_id": p.trunk_id, "peer_ip": ip, "method": method, "event": "ingress_reject",
                           "reason": reason, "msg": f"{method} from {ip} rejected: {reason}"})

    def _options_ping(self, aid: str, p: AccountProfile, st: _Acct, t: float) -> None:
        f = self._faults.get(aid)
        if f and f["fault"] == "pbx_timeout":
            ip = _pbx_ip(p)
            self._edge(st, t, {"trunk_id": p.trunk_id, "peer_ip": ip, "method": "OPTIONS", "event": "egress_timeout",
                               "reason": "no response to OPTIONS keepalive",
                               "msg": f"OPTIONS to {ip}:5060 timed out (no response in 32 s)"})

    def _attempt(self, aid: str, p: AccountProfile, st: _Acct, t: float) -> None:
        rng = st.rng
        f = self._faults.get(aid)
        fault = f["fault"] if f else None
        # Ingress ACL: INVITEs from an IP outside the trunk ACL never become a call.
        if p.direction == "outbound" and p.ip_acl:
            src = p.sbc_ip
            if fault == "acl_drop" and rng.random() > ACL_NEW_IP_SHARE:
                src = f["restore"].get("sbc_ip") or src  # stragglers still leaving via the old SBC
            if src and not _ip_in_acl(src, p.ip_acl):
                self._edge(st, t, {"trunk_id": p.trunk_id, "peer_ip": src, "method": "INVITE",
                                   "event": "ingress_reject", "reason": "source IP not in trunk IP ACL",
                                   "msg": f"INVITE from {src} rejected: source IP not in trunk IP ACL"})
                return
        sec = int(math.floor(t))
        if sec != st.cps_sec:
            st.cps_sec, st.cps_count = sec, 0
        st.cps_count += 1
        call = (self._outbound(aid, p, st, t, f) if p.direction == "outbound" else self._inbound(aid, p, st, t, f))
        st.calls.append(call)
        st.ts.append(t)

    def _pick(self, rng: random.Random, weights: list[tuple[str, float]]) -> str:
        r, acc = rng.random() * sum(w for _, w in weights), 0.0
        for k, w in weights:
            acc += w
            if r < acc:
                return k
        return weights[-1][0]

    def _outbound(self, aid, p: AccountProfile, st: _Acct, t: float, f: dict | None) -> CallRecord:
        rng = st.rng
        fault = f["fault"] if f else None
        dest = self._pick(rng, p.destinations)
        intl = dest in ("US-Mobile", "UK-Mobile")
        carriers = [c for c, _ in p.carriers]
        if dest in p.route_overrides:
            carrier = p.route_overrides[dest]
        elif dest == SILENT_DESTINATION and FAILING_CARRIER in carriers and rng.random() < LCR_VI_DELHI_VIA_TATA:
            carrier = FAILING_CARRIER
        else:
            carrier = self._pick(rng, p.carriers)
        if p.caller_id_override:
            frm = p.caller_id_override
        else:
            pool = [c for c in p.caller_ids if (not c.startswith("+91")) == intl] or p.caller_ids
            frm = rng.choice(pool)
        if dest == "US-Mobile":
            to = f"+1{rng.choice(['415', '646', '312', '512'])}555{rng.randint(1000, 9999)}"
        elif dest == "UK-Mobile":
            to = f"+447700{rng.randint(900000, 900999)}"
        else:
            to = f"+91{_MOBILE_PREFIX.get(dest, '9')}{rng.randint(10_000_000, 99_999_999)}"

        outcome = None
        r = rng.random()
        if st.cps_count > p.cps_limit:
            outcome = (503, CPS_LIMIT, "Zentrunk")
        elif (p.media_region not in INDIA_MEDIA_REGIONS and frm.startswith("+91") and to.startswith("+91")):
            outcome = (403, ANCHORING, "Plivo")
        elif fault == "carrier_reject":
            since = self._rel(f["since"])
            if carrier == FAILING_CARRIER:
                if p.ha_enabled and p.ha_backup_number:
                    # rejected on TATA, retried once from the backup number on another carrier
                    frm = p.ha_backup_number
                    carrier = _HA_BACKUP.get(aid, ("", "Airtel"))[1]
                elif r < 0.88:
                    outcome = (503, CARRIER_REJECT, "Carrier")
                else:
                    outcome = (480, TEMP_UNAVAILABLE, "Carrier")
            elif not p.ha_enabled and r < BURST_PEAK * math.exp(-(t - since) / BURST_TAU_S):
                outcome = (503, CARRIER_REJECT, "Carrier")  # retry overflow onto the healthy carriers
            if outcome is None:
                r = rng.random()
        if outcome is None:
            outcome = _healthy(r)
        silent_route = (fault == "silent_route" and carrier == FAILING_CARRIER and dest == SILENT_DESTINATION)
        return self._record(aid, p, st, t, frm, to, "outbound", carrier, dest, outcome, silent_route)

    def _inbound(self, aid, p: AccountProfile, st: _Acct, t: float, f: dict | None) -> CallRecord:
        rng = st.rng
        carrier = self._pick(rng, p.carriers)
        frm = f"+91{rng.choice(['98', '97', '70', '88', '63'])}{rng.randint(10_000_000, 99_999_999)}"
        to = rng.choice(p.caller_ids)
        if st.cps_count > p.cps_limit:
            outcome = (503, CPS_LIMIT, "Zentrunk")
        elif f and f["fault"] == "pbx_timeout" and rng.random() < PBX_TIMEOUT_SHARE:
            outcome = (408, TIMEOUT_CUSTOMER, "Zentrunk")
            ip = _pbx_ip(p)
            self._edge(st, t, {"trunk_id": p.trunk_id, "peer_ip": ip, "method": "INVITE",
                               "event": "egress_timeout", "reason": "no response to INVITE (Timer B)",
                               "msg": f"INVITE to {ip}:5060 got no 100 Trying; timed out after 32 s"})
        else:
            outcome = _healthy(rng.random())
        return self._record(aid, p, st, t, frm, to, "inbound", carrier, "customer_pbx", outcome, False)

    def _record(self, aid, p, st, t, frm, to, direction, carrier, dest, outcome, silent_route) -> CallRecord:
        rng = st.rng
        sip, (name, code), source = outcome
        answered = sip == 200
        at = self._abs(t)
        ring = round(2 + rng.random() * 8, 1)
        energy = None
        duration = 0
        if answered:
            if silent_route:
                energy = round(0.5 + rng.random() * 4.0, 1)
                duration = 8 + int(rng.random() * 13)  # callee hears nothing, hangs up in 8-20 s
                name, code, source = NORMAL[0], NORMAL[1], "Carrier"
            elif rng.random() < 0.01:  # ordinary dead air: voicemail greeting cut, muted handset
                energy = round(1.0 + rng.random() * 4.0, 1)
                duration = 5 + int(rng.random() * 30)
            else:
                energy = round(40 + rng.random() * 50, 1)
                duration = int(15 + rng.expovariate(1 / 70))
        elif code == 6030:
            ring = 30.0
        elif code == 4170:
            ring = 32.0
        return CallRecord.model_construct(
            call_uuid=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            from_number=frm, to_number=to, call_direction=direction,
            initiation_time=at,
            answer_time=at + timedelta(seconds=ring) if answered else None,
            end_time=at + timedelta(seconds=ring + duration),
            call_duration=duration,
            bill_duration=(math.ceil(duration / 60) * 60) if duration else 0,
            hangup_cause_name=name, hangup_cause_code=code, hangup_source=source,
            sip_response_code=sip, carrier_route=carrier, account_id=aid, api_auth_ok=True,
            to_carrier=dest, audio_energy=energy, trunk_id=p.trunk_id,
        )


def _healthy(r: float) -> tuple[int, tuple[str, int], str]:
    """Ordinary life of a call centre's traffic (same mix as app/traffic.py, plus rare carrier 503s)."""
    if r < 0.003:
        return 503, CARRIER_REJECT, "Carrier"       # the odd 503 on any carrier — background noise
    if r < 0.011:
        return 480, TEMP_UNAVAILABLE, "Carrier"
    if r < 0.018:
        return 408, TIMEOUT_CARRIER, "Carrier"
    if r < 0.080:
        return 486, BUSY, "Callee"
    if r < 0.150:
        return 487, NO_ANSWER, "Zentrunk"
    if r < 0.165:
        return 487, CANCELLED, "Customer"
    if r < 0.600:
        return 200, NORMAL, "Carrier"
    return 200, NORMAL_USER, "Customer"


def _pbx_ip(p: AccountProfile) -> str:
    m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", p.pbx_host or "")
    return m.group(1) if m else "203.0.113.24"


def _ip_in_acl(ip: str | None, acl: list[str]) -> bool:
    if not ip:
        return False
    a = ipaddress.ip_address(ip)
    return any(a in ipaddress.ip_network(n, strict=False) for n in acl)
