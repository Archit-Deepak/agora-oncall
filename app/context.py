"""Per-turn system prompt builder (build-plan.md §5.4).

Renders the persona, the binding rules, the code-decided verdict policy, a compact
case-file view, the retrieved knowledge, and the action state into one system
message that replaces whatever Agora sent. The LLM narrates this; it never decides
the verdict or invents facts.
"""
from __future__ import annotations

from app import config
from app.models import CaseFile
from app.rag import Chunk

PERSONA = (
    f"You are an on-call incident agent on a phone call with {config.ENGINEER_NAME}, a telecom engineer. "
    "Speak like a calm senior engineer. At most two short sentences, under forty words. Answer only what was asked. No lists, no markdown, no symbols. "
    "Say numbers as words a person would say. Skip telecom basics."
)

RULES = (
    "Facts come ONLY from the CASE FILE and KNOWLEDGE below. If it is not there, say you do not know yet. "
    "The VERDICT POLICY is binding: never state a root cause unless the policy says CONFIDENT. "
    "Never claim an action happened unless ACTIONS shows it done."
)


def _verdict_policy(cf: CaseFile) -> str:
    pct = int(round(cf.top_confidence * 100))
    ruled = [h.title for h in cf.hypotheses if h.status == "ruled_out"]
    ruled_txt = "; ".join(ruled) if ruled else "none yet"
    if cf.verdict == "recovered":
        rate = f"{int(round((cf.recent_fail_rate or 0) * 100))}%"
        return (f"RECOVERED. The customer's calls have stopped failing (failure rate back to {rate}). "
                f"Do NOT claim an ongoing cause. Say the calls have recovered on their own; the earlier "
                f"failures pointed at carrier X but that was never confirmed. Suggest keeping an eye on it. "
                f"Ruled out: {ruled_txt}.")
    if cf.verdict == "confident":
        return (f"CONFIDENT ({pct}%). Cause: carrier X rejecting the customer's calls. "
                f"You may state the cause, the evidence, and the fix, and propose raising a ticket "
                f"and drafting the customer email. Ruled out: {ruled_txt}.")
    label = "LEANING" if cf.verdict == "leaning" else "UNSURE"
    return (f"{label} ({pct}%). Leading: carrier rejection on carrier X, NOT confirmed. "
            f"If asked for the cause: say you are not sure yet, give what you've ruled out "
            f"({ruled_txt}), and offer to call back when you know more.")


def _case_file_view(cf: CaseFile) -> str:
    a = cf.alert
    lines = [f"Incident {cf.incident_id}, state {cf.state.value}."]
    if a:
        lines.append(f"{a.customer_name}: outbound failures ~{int(round(a.failure_rate*100))}% "
                     f"since {a.since:%-I:%M %p} (say it exactly like that) (baseline {int(round(a.baseline_rate*100))}%).")
    for h in cf.hypotheses:
        lines.append(f"- {h.title}: {h.status}"
                     + (f" ({int(round(h.confidence*100))}%)" if h.status in ("leading", "confirmed") else "")
                     + (f" — {h.reason}" if h.reason else ""))
    for ev in cf.evidence[-4:]:
        lines.append(f"- evidence: {ev.summary}")
    return "\n".join(lines)


def _knowledge_view(chunks: list[Chunk]) -> str:
    if not chunks:
        return "(none)"
    return "\n".join(f"({c.source}: {c.heading}) {c.text}" for c in chunks)


def _actions_view(cf: CaseFile) -> str:
    if not cf.actions:
        return "none proposed yet"
    return "; ".join(f"{a.kind}={a.status}" + (f" ({a.external_id})" if a.external_id else "")
                     for a in cf.actions)


def build_system_prompt(cf: CaseFile, chunks: list[Chunk], call_purpose: str = "initial",
                        elapsed_s: int = 0, instruction: str = "") -> str:
    extra = f"\n\n[THIS TURN]\n{instruction}" if instruction else ""
    return (
        f"[PERSONA]\n{PERSONA}\n\n"
        f"[RULES]\n{RULES}\n\n"
        f"[VERDICT POLICY]\n{_verdict_policy(cf)}\n\n"
        f"[CASE FILE v{cf.version}]\n{_case_file_view(cf)}\n\n"
        f"[KNOWLEDGE]\n{_knowledge_view(chunks)}\n\n"
        f"[ACTIONS]\n{_actions_view(cf)}\n\n"
        f"[CALL] purpose={call_purpose}, elapsed={elapsed_s}s"
        + extra
    )


# ── v2: multi-account, LLM investigator, fixes ──────────────────────────────
PERSONA_V2 = (
    "You are an on-call incident agent on a live phone call with {name}, a forward deployed "
    "engineer at a voice platform who owns high-spend customer accounts. Calm senior-engineer tone. "
    "HARD LIMIT: two short sentences, under thirty-five words total; answer only what was asked, don't "
    "repeat the fix unless asked. Plain spoken words: no lists, markdown, symbols or slashes; say numbers "
    "the way a person would. Skip telecom basics. The engineer works at the platform; the affected "
    "account is THEIR customer, so say 'their traffic', 'their media', never 'your'."
)
RULES_V2 = (
    "Facts come ONLY from the CASE FILE, INVESTIGATION and KNOWLEDGE below. A separate investigator is "
    "running queries in the background right now; you narrate what it has found. If something isn't known "
    "yet, say you're checking it and will tell them on this call. Never state a root cause unless the "
    "VERDICT POLICY says CONFIRMED. Never claim a fix or ticket happened unless ACTIONS shows it, and never "
    "say you WILL apply a fix or open a ticket: that only happens when the engineer says yes and the system "
    "confirms it. If a fix is proposed and they seem to want it, ask them to just say yes. Tickets are filed "
    "in Linear: quote the ticket ID from ACTIONS; never invent where something is shown. Never write the "
    "engineer's words or answer on their behalf."
)


def _v2_policy(cf: CaseFile, fix: dict | None) -> str:
    if cf.verdict == "recovered":
        return "RECOVERED. The account's traffic is back to normal; say so and don't claim a live cause."
    if cf.verdict == "confident" and fix:
        return (f"CONFIRMED (code-verified). Cause: {fix.get('evidence')}. Owner: {fix.get('owner')}. "
                f"Proposed fix: {fix.get('fix')} {fix.get('params') or ''}, cost: {fix.get('spoken_cost') or fix.get('cost') or 'none'}. "
                "You may state the cause, the owner and the fix, and ask whether to go ahead.")
    lead = next((h for h in cf.hypotheses if h.status == "leading"), None)
    ruled = [h.title for h in cf.hypotheses if h.status == "ruled_out"]
    return ("NOT CONFIRMED YET. "
            + (f"Leading idea: {lead.title} ({int(lead.confidence*100)}%), unverified. " if lead else "No lead yet. ")
            + (f"Ruled out so far: {'; '.join(ruled)}. " if ruled else "")
            + "If asked for the cause: say you don't know yet, share the lead as a possibility and what's ruled out.")


def build_system_prompt_v2(cf: CaseFile, account, chunks: list[Chunk], instruction: str = "",
                           at_risk_inr_h: float = 0.0, engineer: str | None = None) -> str:
    fix = next((f for f in reversed(cf.fixes) if f.get("status") in ("proposed", "applied", "verified")), None)
    a = cf.alert
    acct = (f"{account.name}: {account.use_case}; spend about {int(account.spend_per_day_inr):,} rupees a day; "
            f"plan {getattr(account, 'plan', '')}; CPS limit {getattr(account, 'cps_limit', '?')}; "
            f"HA numbers {'on' if getattr(account, 'ha_enabled', False) else 'off'}.") if account else "unknown account"
    sig = (f"Signal {a.signal} = {round((a.signal_value if a.signal_value is not None else a.failure_rate)*100)}% "
           f"(alert {a.fired_at:%I:%M %p}); about {int(at_risk_inr_h):,} rupees an hour at risk.") if a else ""
    hyps = "\n".join(f"- {h.title}: {h.status}" + (f" {int(h.confidence*100)}%" if h.status in ('leading', 'confirmed') else "")
                     + (f" — {h.reason}" if h.reason else "") for h in cf.hypotheses) or "(none yet)"
    trail = [e for e in cf.investigator_log if e.get("kind") in ("tool", "finding", "verify", "answer")][-6:]
    trail_txt = "\n".join(f"- {e['at']} {e['kind']} {e.get('tool') or e.get('signature') or ''}: "
                          f"{(e.get('result') or e.get('reason') or e.get('summary') or e.get('answer') or '')[:220]}"
                          for e in trail) or "(just started)"
    qs = "\n".join(f"- {q['text']} → {q.get('answer') or 'still checking'}" for q in cf.pending_questions[-4:]) or "(none)"
    actions = "; ".join(f"{x.kind}={x.status}" + (f" {x.external_id}" if x.external_id else "") for x in cf.actions) or "none"
    fixes = "; ".join(f"{f.get('fix')}={f.get('status')}" + (f" ({f['verification']})" if f.get('verification') else "")
                      for f in cf.fixes) or "none"
    extra = f"\n\n[THIS TURN]\n{instruction}" if instruction else ""
    name = engineer if engineer and engineer != "there" else (config.ENGINEER_NAME if engineer is None else "the engineer")
    return (f"[PERSONA]\n{PERSONA_V2.format(name=name)}\n\n[RULES]\n{RULES_V2}\n\n[VERDICT POLICY]\n{_v2_policy(cf, fix)}\n\n"
            f"[ACCOUNT]\n{acct}\n{sig}\n\n[HYPOTHESES]\n{hyps}\n\n[INVESTIGATION — latest]\n{trail_txt}\n\n"
            f"[ENGINEER QUESTIONS]\n{qs}\n\n[KNOWLEDGE]\n{_knowledge_view(chunks)}\n\n"
            f"[ACTIONS] {actions}\n[FIXES] {fixes}\n[STATE] {cf.state.value}{extra}")
