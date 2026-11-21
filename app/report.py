"""Auto-generated incident report (postmortem), computed from the case file and raw CDRs.

Every number here comes from code (milestones, CDR counts, verification), never from an LLM,
so the report is safe to show and to paste into a ticket.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.detector import is_failure

PREVENTION = {
    "carrier_reject": [
        "Enable HA numbers on every high-spend outbound account that still has them off, so a single carrier can't take traffic down.",
        "Alert on per-carrier rejection share, not only the account-wide failure rate.",
        "Raise the carrier fault with the carrier's NOC and track their RFO.",
    ],
    "cps_burst": [
        "Agree a pacing ratio with the customer for new campaigns; cap attempts at the CPS limit.",
        "Watch per-second peaks, not per-minute averages: bursts hide in averages.",
        "Offer the Voice API for bursty campaigns (over-limit calls queue instead of failing).",
    ],
    "silent_route": [
        "Track silent-call rate (answered with no audio) per carrier and destination; the failure rate never shows it.",
        "Open a ticket with the affected carrier for the one-way audio on that interconnect.",
        "Keep the routing override until the carrier confirms the fix, then re-test with sample calls.",
    ],
    "pbx_timeout": [
        "Share the evidence with the customer: 4170 counts, PBX IP and port, OPTIONS timeouts.",
        "Ask the customer to allow-list Plivo's signalling IPs and monitor SIP OPTIONS from their side.",
    ],
    "anchoring_4590": [
        "Before moving media or agent infrastructure out of India, switch India-to-India traffic to compliant routing.",
        "Add a pre-deploy check for domestic anchoring on accounts that call +91 numbers from +91 caller IDs.",
    ],
    "acl_drop": [
        "Ask customers to add the new egress IP to the trunk allow-list before migrating, and keep both during the move.",
        "Alert on call volume vs expected, not only failures: blocked INVITEs never create a CDR.",
    ],
}
TITLE = {"carrier_reject": "Carrier rejecting outbound calls", "cps_burst": "Dialer exceeding the CPS limit",
         "silent_route": "Silent calls on one carrier route", "pbx_timeout": "Customer PBX not answering",
         "anchoring_4590": "India domestic-anchoring rejections", "acl_drop": "Customer traffic blocked at the trunk IP allow-list"}
FIX_DONE = {"enable_ha": "Enabled HA (backup number on another carrier)", "raise_cps": "Raised the CPS limit",
            "pace_dialer": "Customer paced their dialer", "reroute_destination": "Rerouted the destination via a healthy carrier",
            "notify_customer": "Notified the customer with evidence", "switch_caller_id": "Switched to a non-Indian caller ID",
            "move_media_to_india": "Moved media back to an Indian region", "add_ip_to_acl": "Added the new IP to the trunk allow-list"}


def _fmt(sec: float | None) -> str:
    if sec is None:
        return "–"
    sec = int(round(sec))
    return f"{sec}s" if sec < 90 else f"{sec // 60}m {sec % 60:02d}s"


def build(cf, fleet, oncall, fault_at: datetime | None, now: datetime) -> dict:
    aid = cf.account_id
    acct = fleet.accounts.get(aid) if aid else None
    fix = next((f for f in reversed(cf.fixes) if f.get("status") in ("verified", "applied")), None) or \
        next((f for f in reversed(cf.fixes) if f.get("status") == "proposed"), None) or {}
    m = cf.milestones
    fault_at = fault_at or m.alert_at or now
    end = m.resolved_at or now
    rel = lambda d: (d - fault_at).total_seconds() if d else None  # noqa: E731
    sig = fix.get("signature") or (cf.leading or "")
    signal = cf.alert.signal if cf.alert else "failure_rate"

    # impact from raw CDRs over the incident window
    calls = fleet.calls(aid, fault_at, end) if aid else []
    if signal == "silent_rate":
        affected = sum(c.audio_energy is not None and c.audio_energy <= 5 for c in calls)
        what = "silent (answered, no audio)"
    elif signal == "volume_drop":
        expected = fleet.expected_rate(aid) * (end - fault_at).total_seconds() if aid else 0
        affected = max(0, int(expected - len(calls)))
        what = "never reached Plivo (blocked before a CDR existed)"
    else:
        affected = sum(map(is_failure, calls))
        what = "failed"
    dur_s = (end - fault_at).total_seconds()
    peak = (cf.alert.signal_value or cf.alert.failure_rate) if cf.alert else 0
    v0 = (fix.get("verification") or {}).get("before")
    if v0 is not None and signal != "volume_drop":
        peak = max(peak, v0)
    elif v0 is not None:
        peak = min(peak, v0)  # for volume, the worst point is the lowest
    risk_h = oncall.at_risk_per_hour(aid, peak) if aid and cf.alert else 0
    risk_total = risk_h * dur_s / 3600

    ticket = next((x for x in cf.actions if x.kind == "ticket" and x.status == "done"), None)
    tg = [x for x in cf.actions if x.kind == "incident_summary" and (x.payload or {}).get("via") == "telegram"]
    v = fix.get("verification")
    resolved = cf.state.value == "RESOLVED"
    ruled = [h.title for h in cf.hypotheses if h.status == "ruled_out"]
    queries = sum(1 for e in cf.investigator_log if e.get("kind") == "tool")
    rejected = [e for e in cf.investigator_log if e.get("kind") == "verify" and not e.get("ok")]
    models = sorted({str(e.get("model")).replace("azure:", "") for e in cf.investigator_log if e.get("model")})
    title = TITLE.get(sig, "Incident")

    summary = (f"{acct.name if acct else aid}: {title.lower()}. "
               f"{affected} calls {what} over {_fmt(dur_s)} (peak {int(round(peak * 100))}%"
               f"{' of calls ' + ('silent' if signal == 'silent_rate' else 'failing') if signal != 'volume_drop' else ' of normal volume'}). "
               f"The agent detected it {_fmt(rel(m.alert_at))} after it started, phoned the on-call engineer at "
               f"{_fmt(rel(m.call_placed_at))}, and had a code-verified diagnosis at {_fmt(rel(m.diagnosed_at))}. ")
    if fix and m.fix_applied_at:
        summary += f"After the engineer approved, it applied the fix at {_fmt(rel(m.fix_applied_at))}"
        summary += (f" and traffic recovered from {round(v['before'] * 100)}% to {round(v['after'] * 100)}% within "
                    f"{v['seconds']}s." if v else ".")
    elif fix:
        summary += "The fix was proposed and is waiting for approval."

    timeline = [(l, d) for l, d in [("Fault starts", fault_at), ("Detected (suspected)", m.alert_at),
                                     ("Engineer phoned", m.call_placed_at), ("Engineer answered", m.acknowledged_at),
                                     ("Verified diagnosis", m.diagnosed_at), ("Ticket filed", m.ticket_at),
                                     ("Fix applied", m.fix_applied_at), ("Recovery confirmed", m.resolved_at)] if d]
    timeline.sort(key=lambda x: x[1])
    return {
        "incident_id": cf.incident_id, "account": acct.name if acct else aid, "account_id": aid,
        "title": title, "status": "Resolved" if resolved else cf.state.value.replace("_", " ").title(),
        "summary": summary,
        "impact": {"duration": _fmt(dur_s), "affected_calls": affected, "affected_what": what,
                   "peak": peak, "signal": signal, "revenue_at_risk_per_hour": round(risk_h),
                   "revenue_at_risk_total": round(risk_total), "spend_per_day": acct.spend_per_day_inr if acct else 0},
        "timeline": [{"label": l, "at": d.strftime("%H:%M:%S"), "t": _fmt(rel(d))} for l, d in timeline],
        "metrics": {"detect": _fmt(rel(m.alert_at)), "call": _fmt(rel(m.call_placed_at)),
                    "diagnose": _fmt(rel(m.diagnosed_at)), "fix": _fmt(rel(m.fix_applied_at)),
                    "recover": _fmt(rel(m.resolved_at))},
        "root_cause": {"signature": sig, "evidence": fix.get("evidence"), "owner": fix.get("owner"),
                       "check": (next((e.get("reason") for e in reversed(cf.investigator_log)
                                       if e.get("kind") == "verify" and e.get("ok")), None)),
                       "found_by": "LLM investigator" if fix.get("source") == "llm" else "Rules fallback" if fix else None},
        "investigation": {"queries": queries, "ruled_out": ruled, "rejected_conclusions": len(rejected),
                          "models": models},
        "resolution": {"fix": FIX_DONE.get(fix.get("fix"), fix.get("fix")), "params": fix.get("params") or {},
                       "cost": fix.get("cost"), "status": fix.get("status"), "verification": v},
        "follow_ups": {"ticket": ticket.external_id if ticket else None,
                       "ticket_url": (ticket.external_url if ticket else None),
                       "telegram_summary": bool(tg), "email": cf.email_draft},
        "prevention": PREVENTION.get(sig, []),
        "transcript": cf.transcript,
    }


def markdown(r: dict) -> str:
    i, rc, rs = r["impact"], r["root_cause"], r["resolution"]
    lines = [f"# Incident report: {r['incident_id']} · {r['account']}", "",
             f"**{r['title']}** · status: {r['status']}", "", r["summary"], "",
             "## Impact", f"- Duration: {i['duration']}", f"- Calls {i['affected_what']}: {i['affected_calls']}",
             f"- Peak: {int(round(i['peak'] * 100))}%", f"- Revenue at risk: about ₹{i['revenue_at_risk_per_hour']:,}/hour "
             f"(≈ ₹{i['revenue_at_risk_total']:,} over the incident)", "",
             "## Timeline", *[f"- {t['at']} (+{t['t']}): {t['label']}" for t in r["timeline"]], "",
             "## Root cause", f"- {rc['evidence']}", f"- Owner: {rc['owner']}",
             f"- Verified by code: {rc['check']}", f"- Found by: {rc['found_by']}", "",
             "## Resolution", f"- {rs['fix']} {rs['params'] or ''}".rstrip(), f"- Cost: {rs['cost'] or 'none'}"]
    if rs["verification"]:
        v = rs["verification"]
        lines.append(f"- Recovery: {round(v['before'] * 100)}% → {round(v['after'] * 100)}% in {v['seconds']}s")
    lines += ["", "## Follow-ups", f"- Ticket: {r['follow_ups']['ticket'] or 'not filed'}",
              f"- Telegram summary: {'sent' if r['follow_ups']['telegram_summary'] else 'not sent'}",
              "", "## Prevention", *[f"- {p}" for p in r["prevention"]]]
    if r["investigation"]["ruled_out"]:
        lines += ["", "## Ruled out", *[f"- {x}" for x in r["investigation"]["ruled_out"]]]
    return "\n".join(lines) + "\n"
