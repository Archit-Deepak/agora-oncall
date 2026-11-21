"""Voice-driven screen: the engineer talks on the phone, the logs page reacts.

  "can you open one of those call logs?"  → a real failed call opens with its SIP ladder
  "show me only TATA"                     → the CDR table filters
  "compare the carriers"                  → a per-carrier chart
  "go back" / "close that"                → panel closes

Flow (per voice turn, inside the LLM proxy):
  wants_screen(text)  cheap keyword gate, so ordinary questions pay nothing
  plan(text)          a fast LLM picks ONE action from a fixed whitelist (tool calling)
  resolve(action)     code turns it into concrete data from the fleet (a real call_uuid, counts)
  bus.publish(event)  the page receives it over SSE (/public/ui/stream) and acts within ~100 ms
  → the voice reply is told exactly what is now on screen and narrates it.

The LLM never touches the page directly: it can only choose among these actions, and every
value shown is computed by code from the CDRs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter, deque
from datetime import datetime, timedelta

from app import llm

# ── gate ───────────────────────────────────────────────────────────────────
_SCREEN_WORDS = ["open", "show", "pull up", "bring up", "display", "filter", "only the", "just the", "zoom",
                 "close", "go back", "back to", "graph", "chart", "compare", "scroll", "highlight", "click",
                 "look at", "expand", "details of", "detail", "one of those", "that call", "this call", "screen",
                 "log", "ladder", "trace", "next one", "another one", "different one"]


def wants_screen(text: str) -> bool:
    t = (text or "").lower()
    # the quick parser is part of the gate (sim 21 Nov: "compair the carriers" passed the parser but not the gate)
    return any(w in t for w in _SCREEN_WORDS) or quick(text) is not None


# ── actions the LLM may choose (tool calling) ─────────────────────────────
CARRIERS = ["TATA", "Airtel", "Jio", "Vodafone"]
ACTIONS = [
    {"type": "function", "function": {
        "name": "open_call", "description": "Open ONE call's full log (SIP ladder, timeline, cause) on screen.",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["failed", "silent", "answered", "any"],
                     "description": "which kind of call; 'failed' if they say 'one of those' while failures are shown"},
            "carrier": {"type": "string", "description": "outbound carrier, e.g. TATA, Airtel, Jio"},
            "to_network": {"type": "string", "description": "destination network, e.g. Vi-Delhi, Jio, BSNL"},
            "code": {"type": "integer", "description": "hangup code, e.g. 5350, 5180, 4590, 4170"},
            "which": {"type": "string", "enum": ["latest", "another"], "description": "'another' = a different one than last time"}},
            "required": ["kind"]}}},
    {"type": "function", "function": {
        "name": "filter_logs", "description": "Filter the live CDR table.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["failed", "silent", "answered", "all"]},
            "carrier": {"type": "string"}, "to_network": {"type": "string"}, "code": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "show_chart", "description": "Show a breakdown chart of the incident account's last minute.",
        "parameters": {"type": "object", "properties": {
            "by": {"type": "string", "enum": ["carrier", "to_network", "hangup_cause"]},
            "metric": {"type": "string", "enum": ["failures", "silent"]}}, "required": ["by"]}}},
    {"type": "function", "function": {
        "name": "show_report", "description": "Open the incident report / postmortem.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "show_flow", "description": "Show the account's call flow: the path and legs (customer, trunk, carriers, networks) with what each leg returns.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "scroll_to", "description": "Bring a section of the page into view.",
        "parameters": {"type": "object", "properties": {
            "section": {"type": "string", "enum": ["investigator", "accounts", "logs", "call", "outcome"]}},
            "required": ["section"]}}},
    {"type": "function", "function": {
        "name": "close_panel", "description": "Close the open call/chart panel and clear filters (go back).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "no_screen_action", "description": "The request is not about changing what's on the screen.",
        "parameters": {"type": "object", "properties": {}}}},
]

PLANNER_SYSTEM = (
    "You control a live telecom incident dashboard while the engineer talks on the phone. Pick exactly one "
    "tool for what they want to SEE. 'one of those calls/logs' means a failed call (or a silent one if the "
    "incident is about silent calls). If they're just asking a question, use no_screen_action.")


def _models() -> list[str]:
    fast = [m for m in llm.INVESTIGATOR_MODELS if m.endswith("gpt-4.1-mini")]
    return fast + ["openai/gpt-oss-20b"]


_NETWORKS = ["Vi-Delhi", "BSNL", "Jio", "Airtel", "US-Mobile", "UK-Mobile"]


def quick(text: str) -> dict | None:
    """Zero-latency path for the phrasings people actually use; None → ask the LLM planner."""
    t = (text or "").lower()
    carrier = next((c for c in CARRIERS if c.lower() in t), None)
    if any(p in t for p in ("go back", "close", "back to the", "clear the filter", "never mind the")):
        return {"name": "close_panel", "args": {}}
    if any(p in t for p in ("report", "postmortem", "post-mortem", "post mortem", "rca", "write-up", "write up")):
        return {"name": "show_report", "args": {}}
    if any(p in t for p in ("call flow", "flow", "call path", "topology", "legs", "how the call goes",
                            "how calls go", "routing look", "route look", "the path", "map")):
        return {"name": "show_flow", "args": {}}
    if any(p in t for p in ("compare", "compair", "compere", "chart", "graph", "by carrier", "per carrier", "breakdown")):
        by = "to_network" if any(p in t for p in ("network", "destination")) else \
             "hangup_cause" if any(p in t for p in ("cause", "code")) else "carrier"
        return {"name": "show_chart", "args": {"by": by}}
    if any(p in t for p in ("filter", "only the", "only show", "just the failed", "only failed", "all the failed",
                            "show all", "all routes", "all calls", "list of")):
        args = {"status": "failed" if "fail" in t else "silent" if "silent" in t else "all"}
        if carrier:
            args["carrier"] = carrier
        return {"name": "filter_logs", "args": args}
    wants_call = any(p in t for p in ("call log", "one of those", "one of the", "a call", "that call", "this call",
                                      "another one", "next one", "different one", "another call", "ladder", "trace"))
    if wants_call and any(p in t for p in ("open", "show", "pull up", "bring up", "another", "next", "different",
                                            "look at", "ladder", "trace")):
        kind = "silent" if "silent" in t else "answered" if any(p in t for p in ("good one", "healthy", "working")) \
            else "failed"
        args = {"kind": kind, "which": "another" if any(p in t for p in ("another", "next", "different")) else "latest"}
        if carrier:
            args["carrier"] = carrier
        return {"name": "open_call", "args": args}
    if carrier and any(p in t for p in ("only", "just", "filter")):
        return {"name": "filter_logs", "args": {"carrier": carrier, "status": "failed" if "fail" in t else "all"}}
    return None


async def plan(text: str, incident_hint: str, timeout_s: float = 2.5) -> dict | None:
    """→ {"name": str, "args": dict} or None."""
    fast = quick(text)
    if fast:
        return fast
    msgs = [{"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": f"Incident: {incident_hint}\nEngineer said: {text!r}"}]
    try:
        msg = await asyncio.wait_for(llm.chat(msgs, ACTIONS, models=_models(), max_tokens=120, temperature=0,
                                              max_wait_s=1.0, timeout_s=timeout_s), timeout_s + 0.5)
    except Exception:  # noqa: BLE001 - the screen is a bonus; never break the voice turn
        return None
    calls = msg.get("tool_calls") or []
    if not calls:
        return None
    fn = calls[0].get("function", {})
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    name = fn.get("name")
    return None if name in (None, "no_screen_action") else {"name": name, "args": args}


# ── event bus (page listens over SSE) ──────────────────────────────────────
class ScreenBus:
    def __init__(self) -> None:
        self.events: deque[dict] = deque(maxlen=50)
        self.seq = 0
        self._cond = asyncio.Condition()
        self.last_call_uuid: str | None = None

    def reset(self) -> None:
        self.events.clear()
        self.last_call_uuid = None

    async def publish(self, event: dict) -> dict:
        async with self._cond:
            self.seq += 1
            event = {"seq": self.seq, "at": time.time(), **event}
            self.events.append(event)
            self._cond.notify_all()
        return event

    async def wait_after(self, seq: int, timeout: float = 15.0) -> list[dict]:
        async with self._cond:
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: self.seq > seq), timeout)
            except asyncio.TimeoutError:
                return []
        return [e for e in self.events if e["seq"] > seq]


bus = ScreenBus()


# ── call detail: SIP ladder + plain-English story, computed from the CDR ──
CAUSE_STORY = {
    5350: "{carrier} refused the call with 503 Service Unavailable before it ever rang. With HA off there's no backup number to retry on.",
    5180: "Zentrunk rejected it immediately: the account sent more call attempts that second than its CPS limit. It never reached a carrier.",
    4590: "Plivo blocked it: an Indian caller ID calling an Indian number must have its media anchored in India, and this account's media is outside India.",
    4170: "Zentrunk sent the INVITE to the customer's PBX and got no answer at all for 32 seconds, so the call timed out on their side.",
    4340: "The carrier said the destination is temporarily unavailable (480). Normal background noise at low volume.",
    4160: "The carrier didn't respond in time (408). Normal background noise at low volume.",
    4410: "The callee was busy (486). That's normal, not a failure.",
    6030: "It rang for 30 seconds and nobody picked up. Normal, not a failure.",
    4550: "The caller hung up while it was ringing (CANCEL). Normal, not a failure.",
}


def _jit(uuid: str, k: int, lo: float, hi: float) -> float:
    h = int(hashlib.sha1(f"{uuid}{k}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return round(lo + (hi - lo) * h, 3)


def call_detail(c, account_name: str = "") -> dict:
    u, carrier, net = c.call_uuid, c.carrier_route, c.to_carrier
    code, sip = c.hangup_cause_code, c.sip_response_code
    silent = c.audio_energy is not None and c.audio_energy <= 5
    inbound = c.call_direction == "inbound"
    cust = "Customer PBX" if inbound else "Customer SBC"
    L: list[dict] = []

    def add(t, frm, to, msg, kind="sip"):
        L.append({"t": round(t, 3), "from": frm, "to": to, "msg": msg, "kind": kind})

    if inbound:
        add(0.0, carrier, "Zentrunk", "INVITE")
        add(0.012, "Zentrunk", carrier, "100 Trying")
        add(_jit(u, 1, 0.02, 0.05), "Zentrunk", cust, "INVITE")
        if code == 4170:
            for i, t in enumerate((0.5, 1.5, 3.5, 7.5, 15.5)):
                add(t, "Zentrunk", cust, "INVITE (retransmit)", "warn")
            add(32.0, "Zentrunk", carrier, "408 Request Timeout · 4170 request_timeout_customer", "bad")
        else:
            add(_jit(u, 2, 0.3, 2.0), cust, "Zentrunk", "180 Ringing")
            ans = (c.answer_time - c.initiation_time).total_seconds() if c.answer_time else 3.0
            add(ans, cust, "Zentrunk", "200 OK")
            add(ans + 0.02, "Zentrunk", cust, "ACK")
            add(ans + 0.1, "both", "both", "RTP media flowing", "media")
            add(ans + c.call_duration, cust, "Zentrunk", "BYE")
    else:
        add(0.0, cust, "Zentrunk", "INVITE")
        add(0.008, "Zentrunk", cust, "100 Trying")
        if code == 5180:
            add(_jit(u, 1, 0.01, 0.03), "Zentrunk", cust, "503 Service Unavailable · 5180 cps_limit_reached", "bad")
        elif code == 4590:
            add(_jit(u, 1, 0.02, 0.06), "Plivo", cust, "403 Forbidden · 4590 domestic_anchored_terms_not_met", "bad")
        else:
            t_out = _jit(u, 2, 0.03, 0.08)
            add(t_out, "Zentrunk", carrier, "INVITE")
            add(t_out + 0.02, carrier, "Zentrunk", "100 Trying")
            if code == 5350:
                t = t_out + _jit(u, 3, 0.3, 0.9)
                add(t, carrier, "Zentrunk", "503 Service Unavailable", "bad")
                add(t + 0.01, "Zentrunk", carrier, "ACK")
                add(t + 0.02, "Zentrunk", cust, "503 · 5350 service_unavailable_by_carrier", "bad")
            elif code in (4340, 4160):
                t = t_out + (_jit(u, 3, 0.5, 2.0) if code == 4340 else 8.0)
                add(t, carrier, "Zentrunk", "480 Temporarily Unavailable" if code == 4340 else "408 Request Timeout", "bad")
            else:
                ring = (c.answer_time - c.initiation_time).total_seconds() if c.answer_time else \
                       (c.end_time - c.initiation_time).total_seconds()
                add(t_out + _jit(u, 4, 0.4, 1.5), carrier, "Zentrunk", "180 Ringing")
                add(t_out + _jit(u, 4, 0.45, 1.55), "Zentrunk", cust, "180 Ringing")
                if c.answer_time:
                    add(ring, carrier, "Zentrunk", "200 OK")
                    add(ring + 0.01, "Zentrunk", cust, "200 OK")
                    add(ring + 0.05, cust, "Zentrunk", "ACK")
                    if silent:
                        add(ring + 0.2, carrier, "Zentrunk", f"RTP from callee: energy {c.audio_energy} (silence)", "bad")
                    else:
                        add(ring + 0.2, "both", "both", f"RTP both ways · callee energy {c.audio_energy}", "media")
                    add(ring + c.call_duration, "Callee" if not silent else "Callee (hears nothing)", "Zentrunk", "BYE")
                    add(ring + c.call_duration + 0.02, "Zentrunk", "Callee", "200 OK")
                elif code == 4410:
                    add(ring, carrier, "Zentrunk", "486 Busy Here")
                elif code == 6030:
                    add(30.0, "Zentrunk", carrier, "CANCEL (no answer after 30 s)")
                    add(30.02, carrier, "Zentrunk", "487 Request Terminated")
                elif code == 4550:
                    add(ring, cust, "Zentrunk", "CANCEL")
                    add(ring + 0.02, carrier, "Zentrunk", "487 Request Terminated")
    L.sort(key=lambda x: x["t"])
    if silent:
        story = (f"It connected and billed normally, but the callee leg carried no audio (energy {c.audio_energy}; "
                 f"normal is 40 to 90), so the person heard silence and hung up after {c.call_duration} s. "
                 f"Route: {carrier} to {net}.")
    elif c.answer_time:
        story = f"A healthy call: answered after the ring, audio both ways, {c.call_duration} s long."
    else:
        story = CAUSE_STORY.get(code, f"Ended with {code} {c.hangup_cause_name}.").format(carrier=carrier)
    return {
        "call_uuid": u, "account": account_name, "direction": c.call_direction,
        "from": c.from_number, "to": c.to_number, "carrier": carrier, "to_network": net,
        "initiated": c.initiation_time.strftime("%H:%M:%S"), "answered": c.answer_time is not None,
        "duration_s": c.call_duration, "bill_s": c.bill_duration, "code": code, "cause": c.hangup_cause_name,
        "sip": sip, "source": c.hangup_source, "audio_energy": c.audio_energy, "silent": silent,
        "trunk": c.trunk_id, "ladder": L, "story": story,
    }


# ── resolve an action into concrete data + a sentence for the voice ───────
_FAIL_CODES = {5350, 5180, 4590, 4170, 4340, 4160}


def _match(c, a: dict) -> bool:
    kind = a.get("kind") or a.get("status") or "any"
    silent = c.audio_energy is not None and c.audio_energy <= 5
    if kind == "failed" and c.hangup_cause_code not in _FAIL_CODES:
        return False
    if kind == "silent" and not silent:
        return False
    if kind == "answered" and (c.answer_time is None or silent):
        return False
    if a.get("carrier") and str(a["carrier"]).lower() not in (c.carrier_route or "").lower():
        return False
    if a.get("to_network") and str(a["to_network"]).lower().replace(" ", "") not in (c.to_carrier or "").lower().replace(" ", ""):
        return False
    if a.get("code") and int(a["code"]) != c.hangup_cause_code:
        return False
    return True


def resolve(action: dict, fleet, aid: str | None, now: datetime, signal: str | None, said: str) -> tuple[dict, str]:
    """→ (event for the page, one-line facts for the voice reply)."""
    name, a = action["name"], dict(action.get("args") or {})
    acct = fleet.accounts.get(aid) if aid else None
    acct_name = acct.name if acct else ""
    window = fleet.calls(aid, now - timedelta(seconds=60), now) if aid else fleet.recent(200)
    base = {"said": said}

    if name == "open_call":
        if a.get("kind") in (None, "any") and signal == "silent_rate":
            a["kind"] = "silent"
        pool = [c for c in reversed(window) if _match(c, a)]
        if a.get("kind") == "failed" and not a.get("code") and pool:
            # "one of those" = the dominant failure, not a stray background 480
            top = Counter(c.hangup_cause_code for c in pool).most_common(1)[0][0]
            pool = [c for c in pool if c.hangup_cause_code == top] + [c for c in pool if c.hangup_cause_code != top]
        if a.get("which") == "another" or (pool and pool[0].call_uuid == bus.last_call_uuid):
            pool = [c for c in pool if c.call_uuid != bus.last_call_uuid] or pool
        if not pool:  # relax: drop carrier/network/code, keep the kind
            pool = [c for c in reversed(window) if _match(c, {"kind": a.get("kind", "any")})]
        if not pool and signal == "volume_drop" and aid:
            # Blocked INVITEs never become calls, so there is no call log to open (sim 21 Nov): show where
            # they're blocked instead.
            fm = flow_map(fleet, aid, now)
            return ({**base, "type": "flow", "account_id": aid},
                    "There's no call log for these: the INVITEs are blocked at the trunk before a call exists, so "
                    f"there's no CDR. Showing the call flow instead. {fm['story']}")
        if not pool:
            return ({**base, "type": "toast", "text": "No matching call in the last minute"},
                    "There was no matching call in the last minute to open.")
        c = pool[0]
        bus.last_call_uuid = c.call_uuid
        d = call_detail(c, acct_name)
        cause = (d["cause"] or "").replace("_", " ")
        who = d["source"] if d["source"] not in ("Carrier",) else f"the carrier {d['carrier']}"
        not_carrier = "" if d["source"] == "Carrier" else f" It was raised by {who}, NOT by the carrier."
        facts = (f"Opened one call from {d['carrier']} to {d['to_network']}. It ended with {cause} "
                 f"(code {d['code']}, SIP {d['sip']}).{not_carrier} What happened: {d['story']}")
        return {**base, "type": "open_call", "call": d}, facts

    if name == "filter_logs":
        f = {k: a.get(k) for k in ("status", "carrier", "to_network", "code") if a.get(k) not in (None, "", "all")}
        rows = [c for c in window if _match(c, {**f, "kind": f.get("status", "any")})]
        fails = [c for c in rows if c.hangup_cause_code in _FAIL_CODES]
        desc = ", ".join(f"{k}={v}" for k, v in f.items()) or "everything"
        causes = Counter((c.hangup_cause_name.replace("_", " "), c.hangup_source) for c in fails).most_common(2)
        cause_txt = "; ".join(f"{n} x {name} (raised by {src})" for (name, src), n in causes) or "no failures"
        return ({**base, "type": "filter", "filter": f},
                f"Filtered the log table to {desc}: {len(rows)} calls in the last minute, {len(fails)} failed. "
                f"Failures: {cause_txt}. Only state who raised the error as listed here.")

    if name == "show_chart":
        by = a.get("by", "carrier")
        metric = a.get("metric") or ("silent" if signal == "silent_rate" else
                                     "volume" if signal == "volume_drop" else "failures")
        if metric == "volume" and acct and by in ("carrier", "to_network"):
            # Calls received vs what this account normally sends on each route (volume incidents
            # have no failures to chart: the calls never arrive).
            weights = dict(acct.carriers if by == "carrier" else acct.destinations)
            wsum = sum(weights.values()) or 1.0
            exp_total = fleet.expected_rate(aid) * 30
            recent = [c for c in window if c.initiation_time >= now - timedelta(seconds=30)]
            got = Counter((c.carrier_route if by == "carrier" else c.to_carrier) for c in recent)
            bars = sorted(({"label": k, "total": got.get(k, 0), "expected": round(exp_total * w / wsum),
                            "rate": round(got.get(k, 0) / (exp_total * w / wsum), 3) if exp_total else 1.0}
                           for k, w in weights.items()), key=lambda b: b["rate"])
            worst = max(b["rate"] for b in bars) if bars else 1.0
            what = by.replace("_", " ")
            if bars and worst <= 0.5:
                conclusion = (f"Volume collapsed on every {what} (at most {int(worst * 100)}% of normal): calls are "
                              f"being dropped before they reach Plivo, not by any {what}.")
            elif bars and min(b["rate"] for b in bars) <= 0.4 and sorted(b["rate"] for b in bars)[1] >= 0.7:
                low = min(bars, key=lambda b: b["rate"])
                conclusion = f"{low['label']} carries far less than normal; the others look normal."
            elif bars and worst >= 0.75:
                conclusion = f"Volume is normal on every {what}."
            else:
                conclusion = "Volume is down unevenly; no single route explains it."
            facts = (f"Showing calls received in the last 30 seconds versus normal by {what} for {acct_name}: "
                     + "; ".join(f"{b['label']} {b['total']} of about {b['expected']} expected" for b in bars)
                     + f". Conclusion: {conclusion}")
            return {**base, "type": "chart", "by": by, "metric": "volume", "bars": bars, "account": acct_name,
                    "conclusion": conclusion}, facts
        if metric == "volume":
            metric = "failures"
        key = {"carrier": lambda c: c.carrier_route, "to_network": lambda c: c.to_carrier,
               "hangup_cause": lambda c: f"{c.hangup_cause_code} {c.hangup_cause_name}"}[by]
        total, bad = Counter(), Counter()
        for c in window:
            k = key(c)
            total[k] += 1
            hit = ((c.audio_energy is not None and c.audio_energy <= 5) if metric == "silent"
                   else c.hangup_cause_code in _FAIL_CODES)
            bad[k] += hit
        bars = sorted(({"label": k, "total": total[k], "bad": bad[k],
                        "rate": round(bad[k] / total[k], 3) if total[k] else 0} for k in total),
                      key=lambda b: -b["bad"] if by == "hangup_cause" else -b["rate"])[:8]
        top = bars[0] if bars else None
        facts = (f"Showing {metric} by {by.replace('_', ' ')} for {acct_name or 'all accounts'}: "
                 + "; ".join(f"{b['label']} {int(b['rate'] * 100)}% of {b['total']}" for b in bars[:4]) + ". ")
        rated = [b for b in bars if b["total"] >= 8]
        conclusion = ""
        if by != "hangup_cause" and len(rated) >= 2:
            lead = max(rated, key=lambda b: b["rate"])
            rest = [b for b in rated if b is not lead]
            rest_rate = sum(b["bad"] for b in rest) / max(1, sum(b["total"] for b in rest))
            what = by.replace("_", " ")
            if lead["rate"] >= 0.4 and rest_rate <= max(0.12, lead["rate"] / 3):
                conclusion = f"{lead['label']} stands out; the others are near normal ({int(rest_rate * 100)}%)."
                facts += f"Conclusion: {conclusion}"
            elif min(b["rate"] for b in rated) >= 0.2:
                conclusion = f"Every {what} is failing heavily, so this is not a {what} problem."
                facts += (f"Conclusion: every {what} is failing heavily, so this is NOT a {what} problem; the cause "
                          f"is upstream of routing. Say that; don't call any {what} the outlier.")
            else:
                conclusion = "No clear pattern in this breakdown."
                facts += "Conclusion: no clear pattern by this breakdown; don't name an outlier."
        return {**base, "type": "chart", "by": by, "metric": metric, "bars": bars, "account": acct_name,
                "conclusion": conclusion}, facts

    if name == "show_report":
        return ({**base, "type": "report"},
                "Opened the incident report on their screen: summary, impact, timeline, root cause, fix, follow-ups "
                "and prevention steps, all computed from the data. Tell them it's there and offer to send it to Telegram.")

    if name == "show_flow":
        if not aid:
            return {**base, "type": "toast", "text": "No incident account yet"}, "There's no incident account to map yet."
        fm = flow_map(fleet, aid, now)
        legs = ", ".join(f"{c['name']} {c['calls']} calls ({c['label']})" for c in fm["carriers"] if c["calls"])
        facts = (f"Showing {fm['account']}'s call flow: leg A from their {fm['customer']['sub']} to Plivo Zentrunk "
                 f"(trunk CPS limit {fm['trunk']['cps_limit']}), leg B out to the carriers: {legs}. "
                 f"Reading of the map: {fm['story']}")
        return {**base, "type": "flow", "account_id": aid}, facts

    if name == "scroll_to":
        sec = a.get("section", "logs")
        return {**base, "type": "scroll", "section": sec}, f"Scrolled their screen to the {sec} section."

    if name == "close_panel":
        bus.last_call_uuid = None
        return {**base, "type": "close"}, "Closed the panel and cleared the filters; they're back on the overview."

    return {**base, "type": "toast", "text": "Didn't catch that"}, ""


def incident_hint(cf) -> str:
    a = cf.alert
    if not a:
        return "no active incident"
    lead = next((h.title for h in cf.hypotheses if h.status in ("leading", "confirmed")), "unknown yet")
    return f"account {cf.account_id}, signal {a.signal}, leading cause: {lead}"


_SAFE = re.compile(r"[^A-Za-z0-9 ,.'?-]")


def clean_said(text: str) -> str:
    return _SAFE.sub("", text or "").strip()[:120]


# ── call-flow map: the account's live call path, leg by leg ───────────────
CARRIER_FAIL = {5350, 4340, 4160}
TRUNK_REJECT = {5180: "503 · 5180 CPS limit", 4590: "403 · 4590 anchoring"}


def flow_map(fleet, aid: str, now: datetime, window_s: int = 30) -> dict:
    """Topology + live per-leg outcomes for one account, computed from CDRs and edge logs.

    Outbound: customer SBC → IP allow-list → Zentrunk → carrier → destination network.
    Inbound:  carrier → Zentrunk → customer PBX."""
    p = fleet.accounts[aid]
    calls = fleet.calls(aid, now - timedelta(seconds=window_s), now)
    edge = [e for e in fleet.edge_log(aid, now - timedelta(seconds=window_s), now) if "ACL" in str(e.get("reason", ""))]
    inbound = p.direction == "inbound"
    pct = lambda a, b: round(a / b, 3) if b else 0.0  # noqa: E731
    at_trunk = [c for c in calls if c.hangup_cause_code in TRUNK_REJECT]
    reached = [c for c in calls if c.hangup_cause_code not in TRUNK_REJECT]
    carriers = []
    for name, _w in p.carriers:
        cs = [c for c in reached if c.carrier_route == name]
        rej = sum(c.hangup_cause_code in CARRIER_FAIL for c in cs)
        ans = sum(c.answer_time is not None for c in cs)
        silent = sum(c.audio_energy is not None and c.audio_energy <= 5 for c in cs)
        timeouts = sum(c.hangup_cause_code == 4170 for c in cs)
        if inbound and timeouts and pct(timeouts, len(cs)) >= 0.5:
            label, status = f"408 timeout · {int(pct(timeouts, len(cs)) * 100)}%", "bad"
        elif cs and pct(rej, len(cs)) >= 0.5:
            label, status = f"503 · {int(pct(rej, len(cs)) * 100)}% rejected", "bad"
        elif cs:
            label, status = f"200 OK · {int(pct(ans, len(cs)) * 100)}%", "ok"
        else:
            label, status = "no traffic", "idle"
        carriers.append({"name": name, "calls": len(cs), "rejected": rej, "answered": ans, "silent": silent,
                         "label": label, "status": status})
    nets = []
    for name, _w in p.destinations:
        cs = [c for c in reached if c.to_carrier == name and c.answer_time is not None]
        silent = sum(c.audio_energy is not None and c.audio_energy <= 5 for c in cs)
        by_carrier = Counter(c.carrier_route for c in cs)
        sil_by_carrier = Counter(c.carrier_route for c in cs if c.audio_energy is not None and c.audio_energy <= 5)
        links = [{"carrier": k, "calls": v, "silent": sil_by_carrier.get(k, 0),
                  "status": "warn" if v >= 4 and sil_by_carrier.get(k, 0) / v >= 0.5 else "ok"} for k, v in by_carrier.items()]
        nets.append({"name": name, "answered": len(cs), "silent": silent, "links": links})
    trunk_rejects = Counter(c.hangup_cause_code for c in at_trunk)
    ips = Counter(e.get("peer_ip") for e in edge)
    blocked = len(edge)
    attempts = len(calls) + blocked
    # one representative call for the SIP ladder underneath
    worst = None
    if at_trunk:
        worst = at_trunk[-1]
    else:
        bad_c = next((c for c in carriers if c["status"] == "bad"), None)
        if bad_c:
            worst = next((c for c in reversed(reached) if c.carrier_route == bad_c["name"]
                          and c.hangup_cause_code in CARRIER_FAIL | {4170}), None)
        if worst is None:
            worst = next((c for c in reversed(reached) if c.audio_energy is not None and c.audio_energy <= 5
                          and any(l["status"] == "warn" and l["carrier"] == c.carrier_route
                                  for n in nets if n["name"] == c.to_carrier for l in n["links"])), None)
    # plain-English reading of the map (also what the voice says)
    if blocked >= 5 and len(calls) < 0.5 * fleet.expected_rate(aid) * window_s:
        story = (f"Most INVITEs never get past the trunk's IP allow-list: {blocked} blocked in {window_s} seconds, "
                 f"from {ips.most_common(1)[0][0]}, which isn't on the list. Only {len(calls)} calls got through.")
        where = "acl"
    elif trunk_rejects and sum(trunk_rejects.values()) >= 0.3 * max(1, len(calls)):
        code, n = trunk_rejects.most_common(1)[0]
        why = (f"over the CPS limit of {p.cps_limit}" if code == 5180 else
               f"India to India calls with media in {p.media_region}, breaking domestic anchoring")
        story = (f"Calls are rejected at Zentrunk itself, before any carrier: {n} of {len(calls)} got "
                 f"{TRUNK_REJECT[code]} ({why}). The carriers are healthy.")
        where = "trunk"
    elif any(c["status"] == "bad" for c in carriers):
        b = next(c for c in carriers if c["status"] == "bad")
        others = [c["name"] for c in carriers if c["status"] == "ok"]
        story = (f"Leg B breaks at {b['name']}: it answers 503 Service Unavailable on "
                 f"{int(pct(b['rejected'], b['calls']) * 100)}% of calls. "
                 + (f"{' and '.join(others)} complete normally." if others else "")
                 + (" HA is off, so there's no backup number to retry on." if not p.ha_enabled else ""))
        where = "carrier"
    elif any(l["status"] == "warn" for n in nets for l in n["links"]):
        n = next(n for n in nets if any(l["status"] == "warn" for l in n["links"]))
        l = next(l for l in n["links"] if l["status"] == "warn")
        story = (f"Every leg returns 200 OK, but calls from {l['carrier']} into {n['name']} carry no audio: "
                 f"{l['silent']} of {l['calls']} silent. The signalling is fine, the media path isn't.")
        where = "media"
    else:
        story = "Every leg is healthy: INVITEs reach the carriers and come back 200 OK."
        where = "none"
    return {
        "account_id": aid, "account": p.name, "direction": p.direction, "window_s": window_s,
        "customer": {"label": f"{p.name}", "sub": "customer PBX" if inbound else "dialer / SBC",
                     "caller_ids": len(p.caller_ids)},
        "acl": {"enabled": bool(p.ip_acl) and not inbound, "entries": len(p.ip_acl), "blocked": blocked,
                "noise": blocked < 5,
                "top_ip": ips.most_common(1)[0][0] if ips else None},
        "trunk": {"id": p.trunk_id, "cps_limit": p.cps_limit, "media_region": p.media_region,
                  "ha": p.ha_enabled, "rejects": [{"code": k, "label": TRUNK_REJECT[k], "n": v} for k, v in trunk_rejects.items()]},
        "attempts": attempts, "passed": len(calls), "reached_carriers": len(reached),
        "carriers": carriers, "networks": nets, "story": story, "where": where,
        "sample_uuid": worst.call_uuid if worst else None,
    }
