"""Investigator tools — deterministic queries over the fleet's CDRs, edge logs and account config.

Every tool takes a ToolContext (fleet + "now") and returns a small JSON-serialisable dict
(rates rounded to 3 dp, lists capped, times as HH:MM:SS) so an LLM can read it cheaply.
`TOOL_SPECS` are OpenAI function-calling schemas; `run_tool(name, args, ctx)` dispatches
and turns any bad input into {"error": ...} instead of raising.
"""
from __future__ import annotations

import inspect
import ipaddress
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.detector import is_failure
from app.fleet import KEEP_S, FleetTraffic, is_silent

MAX_WINDOW_S = KEEP_S
BREAKDOWN_FIELDS = {
    "carrier": lambda c: c.carrier_route,
    "to_carrier": lambda c: c.to_carrier or "-",
    "hangup_cause": lambda c: f"{c.hangup_cause_code} {c.hangup_cause_name}",
    "direction": lambda c: c.call_direction,
    "from_number": lambda c: c.from_number,
    "hangup_source": lambda c: c.hangup_source,
}


@dataclass
class ToolContext:
    fleet: FleetTraffic
    now: datetime


# ── helpers ─────────────────────────────────────────────────────────────────
def _r(x: float) -> float:
    return round(float(x), 3)


def _hms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def _ago(now: datetime, dt: datetime) -> str:
    s = int((now - dt).total_seconds())
    if s < 0:
        return "in the future"
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h {s % 3600 // 60}m ago"
    return f"{s // 86400}d ago"


def _check_account(ctx: ToolContext, account_id: str):
    if account_id not in ctx.fleet.accounts:
        raise ValueError(f"unknown account_id {account_id!r}; valid: {sorted(ctx.fleet.accounts)}")
    return ctx.fleet.accounts[account_id]


def _window(window_s, default: int = 60) -> int:
    w = int(window_s if window_s is not None else default)
    if not 5 <= w <= MAX_WINDOW_S:
        raise ValueError(f"window_s must be between 5 and {MAX_WINDOW_S}")
    return w


def _call_row(c, now: datetime) -> dict:
    return {
        "call_uuid": c.call_uuid, "time": _hms(c.initiation_time), "ago_s": int((now - c.initiation_time).total_seconds()),
        "direction": c.call_direction, "from": c.from_number, "to": c.to_number,
        "carrier": c.carrier_route, "to_carrier": c.to_carrier,
        "hangup": f"{c.hangup_cause_code} {c.hangup_cause_name}", "hangup_source": c.hangup_source,
        "sip": c.sip_response_code, "answered": c.answer_time is not None, "duration_s": c.call_duration,
        "audio_energy": c.audio_energy, "trunk_id": c.trunk_id,
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


# ── tools ───────────────────────────────────────────────────────────────────
def overview(ctx: ToolContext, account_id: str, window_s: int = 60) -> dict:
    p = _check_account(ctx, account_id)
    w = _window(window_s)
    m = ctx.fleet.window_metrics(account_id, ctx.now, w)
    prev = ctx.fleet.window_metrics(account_id, ctx.now - timedelta(seconds=w), w)
    causes: dict[str, int] = {}
    for c in m["_calls"]:
        k = f"{c.hangup_cause_code} {c.hangup_cause_name}"
        causes[k] = causes.get(k, 0) + 1
    top = sorted(causes.items(), key=lambda kv: -kv[1])[:5]
    n = m["calls"] or 1
    return {
        "account_id": account_id, "name": p.name, "direction": p.direction, "as_of": _hms(ctx.now), "window_s": w,
        "calls": m["calls"], "expected_calls": round(m["expected_calls"]), "volume_ratio": _r(m["volume_ratio"]),
        "failed": m["failed"], "fail_rate": _r(m["fail_rate"]),
        "answered": m["answered"], "silent": m["silent"], "silent_rate": _r(m["silent_rate"]),
        "previous_window": {"calls": prev["calls"], "fail_rate": _r(prev["fail_rate"]),
                            "silent_rate": _r(prev["silent_rate"]), "volume_ratio": _r(prev["volume_ratio"])},
        "top_hangup_causes": [{"cause": k, "count": v, "share": _r(v / n)} for k, v in top],
        "at_risk_inr_per_hour": m["at_risk_inr_per_hour"],
        "notes": "failure = SIP not in {200, 486 busy, 487 no-answer/cancel}; silent = answered with Krisp energy <= 5",
    }


def breakdown(ctx: ToolContext, account_id: str, by: str, window_s: int = 60, only_failures: bool = False) -> dict:
    _check_account(ctx, account_id)
    if by not in BREAKDOWN_FIELDS:
        raise ValueError(f"by must be one of {sorted(BREAKDOWN_FIELDS)}")
    w = _window(window_s)
    calls = ctx.fleet.calls(account_id, ctx.now - timedelta(seconds=w), ctx.now)
    key = BREAKDOWN_FIELDS[by]
    groups: dict[str, list[int]] = {}  # key -> [calls, failed, answered, silent]
    for c in calls:
        f = is_failure(c)
        if only_failures and not f:
            continue
        g = groups.setdefault(key(c), [0, 0, 0, 0])
        g[0] += 1
        g[1] += f
        if c.answer_time is not None:
            g[2] += 1
            g[3] += is_silent(c)
    total = sum(g[0] for g in groups.values()) or 1
    rows = sorted(groups.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = [{"key": k, "calls": g[0], "share": _r(g[0] / total), "failed": g[1], "fail_rate": _r(g[1] / g[0]),
            "answered": g[2], "silent": g[3]} for k, g in rows[:10]]
    return {"account_id": account_id, "by": by, "window_s": w, "only_failures": bool(only_failures),
            "total": sum(g[0] for g in groups.values()), "groups": out,
            **({"more_groups": len(rows) - 10} if len(rows) > 10 else {})}


def cps_profile(ctx: ToolContext, account_id: str, window_s: int = 60) -> dict:
    p = _check_account(ctx, account_id)
    w = _window(window_s)
    calls = ctx.fleet.calls(account_id, ctx.now - timedelta(seconds=w), ctx.now)
    end = math.floor(ctx.fleet._rel(ctx.now))
    per = [0] * w
    n5180 = 0
    for c in calls:
        i = w - 1 - (end - math.floor(ctx.fleet._rel(c.initiation_time)))
        if 0 <= i < w:
            per[i] += 1
        n5180 += c.hangup_cause_code == 5180
    srt = sorted(per)
    over = [i for i, v in enumerate(per) if v > p.cps_limit]
    # burst pattern: runs of consecutive seconds above the limit
    runs, start = [], None
    for i, v in enumerate(per + [0]):
        if v > p.cps_limit and start is None:
            start = i
        elif v <= p.cps_limit and start is not None:
            runs.append((start, i - start))
            start = None
    if runs:
        period = (runs[-1][0] - runs[0][0]) / (len(runs) - 1) if len(runs) > 1 else None
        pattern = (f"{len(runs)} bursts above the limit, ~{sum(r[1] for r in runs) / len(runs):.0f} s long, "
                   f"peaking at {max(per)} calls/s" + (f", every ~{period:.0f} s" if period else ""))
    else:
        pattern = f"no second above the CPS limit of {p.cps_limit}"
    return {
        "account_id": account_id, "window_s": w, "cps_limit": p.cps_limit, "concurrency": p.concurrency,
        "attempts": len(calls), "mean_cps": _r(len(calls) / w), "peak_cps": max(per, default=0),
        "p95_cps": srt[min(len(srt) - 1, int(0.95 * len(srt)))] if srt else 0,
        "seconds_over_limit": len(over), "count_5180": n5180, "share_5180": _r(n5180 / len(calls)) if calls else 0.0,
        "burst_pattern": pattern, "last_30s_per_second": per[-30:],
    }


def audio_by_route(ctx: ToolContext, account_id: str, window_s: int = 120) -> dict:
    _check_account(ctx, account_id)
    w = _window(window_s, 120)
    calls = ctx.fleet.calls(account_id, ctx.now - timedelta(seconds=w), ctx.now)
    groups: dict[tuple[str, str], list] = {}
    for c in calls:
        if c.answer_time is None:
            continue
        groups.setdefault((c.carrier_route, c.to_carrier or "-"), []).append(c)
    rows = []
    for (car, dest), cs in groups.items():
        sil = sum(map(is_silent, cs))
        rows.append({"carrier": car, "to_carrier": dest, "answered": len(cs), "silent": sil,
                     "silent_rate": _r(sil / len(cs)),
                     "median_energy": _median([c.audio_energy for c in cs if c.audio_energy is not None]),
                     "median_duration_s": _median([c.call_duration for c in cs])})
    rows.sort(key=lambda r: (-r["silent"], -r["answered"]))
    ans = sum(r["answered"] for r in rows)
    sil = sum(r["silent"] for r in rows)
    return {"account_id": account_id, "window_s": w, "answered": ans, "silent": sil,
            "silent_rate": _r(sil / ans) if ans else 0.0, "silent_threshold_energy": 5,
            "routes": rows[:12], **({"more_routes": len(rows) - 12} if len(rows) > 12 else {})}


def volume_trend(ctx: ToolContext, account_id: str, minutes: int = 5) -> dict:
    _check_account(ctx, account_id)
    minutes = int(minutes)
    if not 1 <= minutes <= 15:
        raise ValueError("minutes must be between 1 and 15")
    f = ctx.fleet
    rate = f.expected_rate(account_id)
    rows = []
    for i in range(minutes, 0, -1):
        hi = ctx.now - timedelta(seconds=(i - 1) * 60)
        lo = hi - timedelta(seconds=60)
        cs = f.calls(account_id, lo, hi)
        covered = 60.0
        if f.started_at is not None:
            covered = max(0.0, min(60.0, (hi - max(lo, f.started_at)).total_seconds()))
        exp = rate * covered
        rows.append({"minute_ending": _hms(hi), "calls": len(cs), "expected": round(exp),
                     "ratio": _r(len(cs) / exp) if exp else None,
                     "fail_rate": _r(sum(map(is_failure, cs)) / len(cs)) if cs else 0.0})
    return {"account_id": account_id, "expected_calls_per_min": round(rate * 60), "minutes": rows}


def sample_calls(ctx: ToolContext, account_id: str, hangup_code: int | None = None, carrier: str | None = None,
                 to_carrier: str | None = None, silent: bool | None = None, n: int = 3) -> dict:
    _check_account(ctx, account_id)
    n = max(1, min(int(n), 10))
    calls = ctx.fleet.calls(account_id, ctx.now - timedelta(seconds=MAX_WINDOW_S), ctx.now)
    hits, matched = [], 0
    for c in reversed(calls):
        if hangup_code is not None and c.hangup_cause_code != int(hangup_code):
            continue
        if carrier is not None and c.carrier_route.lower() != str(carrier).lower():
            continue
        if to_carrier is not None and c.to_carrier.lower() != str(to_carrier).lower():
            continue
        if silent is not None and (c.answer_time is not None and is_silent(c)) != bool(silent):
            continue
        matched += 1
        if len(hits) < n:
            hits.append(_call_row(c, ctx.now))
    return {"account_id": account_id, "filters": {k: v for k, v in {"hangup_code": hangup_code, "carrier": carrier,
            "to_carrier": to_carrier, "silent": silent}.items() if v is not None},
            "matched_last_15m": matched, "calls": hits}


def edge_log(ctx: ToolContext, account_id: str, window_s: int = 120) -> dict:
    p = _check_account(ctx, account_id)
    w = _window(window_s, 120)
    entries = ctx.fleet.edge_log(account_id, ctx.now - timedelta(seconds=w), ctx.now)
    groups: dict[tuple, dict] = {}
    for e in entries:
        k = (e["peer_ip"], e["method"], e["event"], e["reason"])
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"peer_ip": e["peer_ip"], "method": e["method"], "event": e["event"], "reason": e["reason"],
                             "count": 0, "first": _hms(e["at"]), "example": e["msg"]}
        g["count"] += 1
        g["last"] = _hms(e["at"])
    rows = sorted(groups.values(), key=lambda g: -g["count"])
    for g in rows:
        if g["event"] == "ingress_reject" and p.ip_acl:
            ip = ipaddress.ip_address(g["peer_ip"])
            g["in_trunk_acl"] = any(ip in ipaddress.ip_network(a, strict=False) for a in p.ip_acl)
    # Context the model needs to not over-read background noise (live run 21 Nov: a handful of
    # scanner rejects sent the investigator chasing acl_drop on a carrier-reject incident).
    exp = ctx.fleet.expected_rate(account_id) * w
    got = len(ctx.fleet.calls(account_id, ctx.now - timedelta(seconds=w), ctx.now))
    vol = round(got / exp, 2) if exp else 1.0
    top = rows[0]["count"] if rows else 0
    if vol >= 0.7:
        verdict = (f"call volume is normal ({int(vol * 100)}% of expected), so these {len(entries)} edge events are "
                   "background noise (SIP scanners, OPTIONS probes), NOT the cause of this incident")
    elif top >= 10:
        verdict = (f"call volume is down to {int(vol * 100)}% of expected and one source IP has {top} rejects: "
                   "consistent with the customer's traffic being blocked at the trunk ACL")
    else:
        verdict = f"call volume is {int(vol * 100)}% of expected; edge events are sparse"
    return {"account_id": account_id, "trunk_id": p.trunk_id, "window_s": w, "trunk_ip_acl": list(p.ip_acl),
            "total_events": len(entries), "volume_vs_expected": vol, "verdict": verdict, "groups": rows[:8],
            **({"more_groups": len(rows) - 8} if len(rows) > 8 else {}),
            "notes": "edge events happen before a call exists: rejected INVITEs produce no CDR"}


def account_profile(ctx: ToolContext, account_id: str) -> dict:
    p = _check_account(ctx, account_id)
    lo = ctx.now - timedelta(hours=24)
    recent = [c for c in p.change_log if lo <= c["at"] <= ctx.now]
    return {
        "account_id": p.id, "name": p.name, "use_case": p.use_case, "direction": p.direction, "plan": p.plan,
        "spend_per_day_inr": p.spend_per_day_inr, "expected_calls_per_s": p.calls_per_s,
        "cps_limit": p.cps_limit, "concurrency": p.concurrency,
        "ha_enabled": p.ha_enabled, "ha_backup_number": p.ha_backup_number,
        "caller_ids": p.caller_ids, "caller_id_override": p.caller_id_override,
        "media_region": p.media_region, "trunk_id": p.trunk_id, "trunk_ip_acl": p.ip_acl,
        **({"pbx_uri": p.pbx_host} if p.pbx_host else {}),
        "carriers": {c: w for c, w in p.carriers}, "destination_networks": {d: w for d, w in p.destinations},
        "route_overrides": p.route_overrides,
        "contacts": p.contacts,
        "change_log_24h": [{"at": _hms(c["at"]), "ago": _ago(ctx.now, c["at"]), "change": c["change"], "by": c.get("by", "")}
                           for c in sorted(recent, key=lambda c: c["at"])][-10:],
        "older_changes": len([c for c in p.change_log if c["at"] < lo]),
    }


def past_incidents(ctx: ToolContext, account_id: str) -> dict:
    p = _check_account(ctx, account_id)
    return {"account_id": account_id, "incidents": p.past_incidents[-5:]}


_KB = None


def search_kb(ctx: ToolContext, query: str, k: int = 3) -> dict:
    global _KB
    if not str(query).strip():
        raise ValueError("query must not be empty")
    if _KB is None:
        from app.rag import KnowledgeBase
        _KB = KnowledgeBase()
    k = max(1, min(int(k), 5))
    hits = _KB.top_k(str(query), k=k)
    return {"query": query, "results": [{"source": h.source, "heading": h.heading, "text": h.text[:700]} for h in hits]}


TOOLS = {f.__name__: f for f in (overview, breakdown, cps_profile, audio_by_route, volume_trend, sample_calls,
                                 edge_log, account_profile, past_incidents, search_kb)}

# ── OpenAI function-calling schemas ─────────────────────────────────────────
_ACCT = {"type": "string", "description": "Account id, e.g. 'acct_northwind'."}


def _spec(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}}


TOOL_SPECS: list[dict] = [
    _spec("overview", "START HERE. Health of one account over the last window: call volume vs expected "
          "(volume_ratio), failure rate, silent-call rate, top 5 hangup causes, ₹ at risk per hour, and the same "
          "numbers for the previous window for comparison.",
          {"account_id": _ACCT, "window_s": {"type": "integer", "description": "Window in seconds (default 60)."}},
          ["account_id"]),
    _spec("breakdown", "Group the account's recent calls by one dimension and show calls/failures/silent per group. "
          "Use to localise a problem: by='carrier' (outbound carrier route), 'to_carrier' (destination network), "
          "'hangup_cause', 'hangup_source' (who ended it: Carrier/Zentrunk/Plivo/Callee/Customer), 'from_number' "
          "(caller ID), 'direction'. Set only_failures=true to see where the failures concentrate.",
          {"account_id": _ACCT, "by": {"type": "string", "enum": sorted(BREAKDOWN_FIELDS)},
           "window_s": {"type": "integer", "description": "Window in seconds (default 60)."},
           "only_failures": {"type": "boolean", "description": "Count only failed calls (default false)."}},
          ["account_id", "by"]),
    _spec("cps_profile", "Calls-per-second profile: per-second attempt counts vs the account's CPS limit, peak, p95, "
          "seconds over the limit, count of 5180 cps_limit_reached and the burst pattern. Use when you see 5180s "
          "or suspect a dialer is sending bursts.",
          {"account_id": _ACCT, "window_s": {"type": "integer", "description": "Window in seconds (default 60)."}},
          ["account_id"]),
    _spec("audio_by_route", "Audio quality of ANSWERED calls per carrier x destination network: silent calls "
          "(Krisp max noise energy <= 5), silent rate, median energy and duration. Use when calls connect but "
          "users report silence/dead air, or silent_rate is high while the failure rate looks healthy.",
          {"account_id": _ACCT, "window_s": {"type": "integer", "description": "Window in seconds (default 120)."}},
          ["account_id"]),
    _spec("volume_trend", "Calls per minute for the last N minutes vs the expected volume. Use when volume_ratio "
          "is low or traffic seems to have stopped without failures (calls may be dropped before a CDR exists).",
          {"account_id": _ACCT, "minutes": {"type": "integer", "description": "1-15 (default 5)."}},
          ["account_id"]),
    _spec("sample_calls", "Fetch a few recent CDRs (newest first, last 15 min) matching filters, to inspect real "
          "examples: caller ID, destination, carrier, hangup cause/source, SIP code, duration, audio energy.",
          {"account_id": _ACCT, "hangup_code": {"type": "integer", "description": "Plivo hangup code, e.g. 5350, 5180, 4170, 4590, 3000."},
           "carrier": {"type": "string", "description": "Outbound carrier route, e.g. 'TATA'."},
           "to_carrier": {"type": "string", "description": "Destination network, e.g. 'Vi-Delhi'."},
           "silent": {"type": "boolean", "description": "true = only answered silent calls; false = exclude them."},
           "n": {"type": "integer", "description": "How many (1-10, default 3)."}},
          ["account_id"]),
    _spec("edge_log", "SIP edge events for the account's trunk, grouped by peer IP + reason: INVITEs rejected at "
          "ingress (e.g. source IP not in the trunk IP ACL) and egress timeouts towards a customer PBX. These "
          "happen BEFORE a call exists, so they never show up in CDRs. Some scanner noise is normal.",
          {"account_id": _ACCT, "window_s": {"type": "integer", "description": "Window in seconds (default 120)."}},
          ["account_id"]),
    _spec("account_profile", "Account configuration: plan, CPS limit, concurrency, HA numbers, caller IDs, media "
          "region, trunk IP ACL, carriers, destination mix, routing overrides, contacts and the change log of "
          "the last 24 h. Use to check what changed and which fixes are possible.",
          {"account_id": _ACCT}, ["account_id"]),
    _spec("past_incidents", "Previous incidents on this account and how they were fixed.", {"account_id": _ACCT},
          ["account_id"]),
    _spec("search_kb", "Search the Plivo knowledge base (hangup codes, HA numbers, CPS limits, silent calls, PBX "
          "timeouts, domestic anchoring 4590, trunk IP ACLs, customer profiles). Use to confirm what a signal "
          "means and what the right fix and its cost are.",
          {"query": {"type": "string"}, "k": {"type": "integer", "description": "Results (1-5, default 3)."}},
          ["query"]),
]


def _coerce(value, schema_type: str):
    if value is None:
        return None
    if schema_type == "integer" and not isinstance(value, bool):
        return int(value)
    if schema_type == "boolean":
        if isinstance(value, str):
            if value.lower() in ("true", "1", "yes"):
                return True
            if value.lower() in ("false", "0", "no"):
                return False
            raise ValueError(f"not a boolean: {value!r}")
        return bool(value)
    if schema_type == "string":
        return str(value)
    return value


_SPEC_PROPS = {s["function"]["name"]: s["function"]["parameters"] for s in TOOL_SPECS}


def run_tool(name: str, args: dict | str | None, ctx: ToolContext) -> dict:
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}; available: {sorted(TOOLS)}"}
    try:
        if isinstance(args, str):
            args = json.loads(args) if args.strip() else {}
        args = dict(args or {})
    except (ValueError, TypeError) as e:
        return {"error": f"arguments must be a JSON object: {e}"}
    params = _SPEC_PROPS[name]
    unknown = set(args) - set(params["properties"])
    if unknown:
        return {"error": f"unexpected argument(s) {sorted(unknown)} for {name}; allowed: {sorted(params['properties'])}"}
    missing = [r for r in params["required"] if args.get(r) in (None, "")]
    if missing:
        return {"error": f"missing required argument(s) {missing} for {name}"}
    try:
        kw = {k: _coerce(v, params["properties"][k].get("type", "string")) for k, v in args.items()}
        inspect.signature(fn).bind(ctx, **kw)
        return fn(ctx, **kw)
    except (ValueError, TypeError, KeyError) as e:
        return {"error": f"{name}: {e}"}
