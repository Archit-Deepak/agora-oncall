"""InvestigatorAgent — the background LLM that actually debugs.

Runs from the moment an outage is suspected until the incident closes, as a Groq
tool-calling loop over deterministic query tools (app/tools.py). It never talks to the
engineer directly; it writes to the shared case file:

  - investigator_log : every tool call (+ compact result) and every short thought — the
                       dashboard shows this live as "the agent thinking"
  - hypotheses       : via report_hypothesis (open / leading / ruled_out / confirmed)
  - conclusion       : via conclude → code verification (app/verify.py) must pass, else the
                       agent is told why and keeps digging
  - answers          : engineer questions arrive in case.pending_questions (queued by the
                       voice proxy); the agent answers them first via answer_question

The voice LLM reads the case file on every turn and never waits on this loop; the
orchestrator speaks new answers / "found it" into the live call.

If the LLM fails (HTTP errors, timeouts, max steps), a rules-only diagnosis
(verify.best_guess) takes over so the incident still progresses.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Awaitable, Callable

from app import llm, verify
from app.models import CaseFile, Evidence, Hypothesis

log = logging.getLogger("investigator")

SIGNATURES = sorted(verify.CHECKS)

SYSTEM = f"""You are the investigator half of an AI on-call engineer at a CPaaS (Plivo-style, India). An account
tripped an alert; a TRIAGE PACK of first queries is in the first message. Find the root cause using ONLY tool data.
Another agent narrates your findings on a live call with the engineer.
Steps: read the triage; call report_hypothesis for the lead (status leading) and for 1-2 decoys you can rule out
(status ruled_out); use more tools only if needed; then conclude (code verifies it; if rejected, read why).
"ENGINEER ASKS [q-…]" messages come first: answer via answer_question in 1-2 short spoken sentences, the way
you'd say it on a phone: account names (not ids), plain times like "about a minute ago" (no UTC stamps), no symbols.
Be brief. Never invent numbers. Busy/no-answer/cancel are normal, not failures.

Known failure signatures (signature id → what it looks like → owner → usual fix):
- carrier_reject → one outbound carrier returns 5350 service_unavailable_by_carrier (SIP 503), others fine →
  carrier → enable_ha (India has no automatic carrier failover; HA rents a backup number on another carrier)
- cps_burst → 5180 cps_limit_reached in waves, per-second peaks above the account CPS limit → customer's dialer →
  raise_cps (buy concurrency) or pace_dialer (free)
- silent_route → calls answer and bill normally but audio energy ≤ 5 on one carrier→destination route; failure
  rate looks healthy → carrier route → reroute_destination (params: to_carrier, via)
- pbx_timeout → 4170 request_timeout_customer on inbound calls to the customer's PBX → customer → notify_customer
- anchoring_4590 → 4590 domestic_anchored_terms_not_met on +91→+91 calls, often after media/infra moved out of
  India (check change_log) → customer config → switch_caller_id or move_media_to_india
- acl_drop → call volume collapses with few failures; edge_log shows INVITEs rejected "not in trunk IP ACL",
  often after an SBC/egress IP change → customer config → add_ip_to_acl (params: ip)
Signature ids: {", ".join(SIGNATURES)} (or "other")."""

DEADLINE_S = 45.0
REJECT_COOLDOWN_S = 3.0    # after code rejects a conclusion, wait for more data before retrying
FALLBACK_RETRY_S = 2.0     # how often the rules-only diagnosis is retried while unconcluded
HISTORY_TAIL = 8           # messages kept after system + triage (free-tier token budget)
RESULT_CAP = 900           # chars of each tool result sent back to the model
TRIAGE = [("overview", {"window_s": 30}), ("account_profile", {}),
          ("breakdown", {"by": "carrier", "only_failures": True, "window_s": 30}),
          ("breakdown", {"by": "hangup_cause", "only_failures": True, "window_s": 30}),
          ("cps_profile", {"window_s": 30}), ("audio_by_route", {"window_s": 60}),
          ("volume_trend", {"minutes": 3}), ("edge_log", {"window_s": 60})]


def compact_specs(specs: list[dict]) -> list[dict]:
    """Same tools, far fewer tokens: first sentence of each description, no per-param prose."""
    out = []
    for t in specs:
        f = t["function"]
        desc = (f.get("description") or "").split(". ")[0][:140]
        params = json.loads(json.dumps(f.get("parameters") or {"type": "object", "properties": {}}))
        for prop in (params.get("properties") or {}).values():
            prop.pop("description", None)
        out.append({"type": "function", "function": {"name": f["name"], "description": desc, "parameters": params}})
    return out

AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "report_hypothesis",
        "description": "Record or update a hypothesis on the shared case file (shown to the engineer). Use as soon as you have a lead, and to rule things out.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "signature id, e.g. carrier_reject"},
            "title": {"type": "string", "description": "short plain-English title"},
            "status": {"type": "string", "enum": ["open", "leading", "ruled_out"]},
            "confidence": {"type": "number", "description": "0..1, honest"},
            "evidence": {"type": "string", "description": "one sentence quoting tool numbers"},
            "owner": {"type": "string", "enum": ["carrier", "customer", "plivo", "unknown"]}},
            "required": ["id", "title", "status", "evidence"]}}},
    {"type": "function", "function": {
        "name": "conclude",
        "description": "Declare the root cause. Verified by code against raw data before it is accepted.",
        "parameters": {"type": "object", "properties": {
            "signature": {"type": "string", "enum": SIGNATURES},
            "spoken_summary": {"type": "string", "description": "the CAUSE only (not the fix), one or two short plain sentences for the phone, with the key number"},
            "evidence": {"type": "string", "description": "the key numbers"},
            "owner": {"type": "string", "enum": ["carrier", "customer", "plivo"]},
            "fix": {"type": "string", "description": "fix id, e.g. enable_ha, raise_cps, pace_dialer, reroute_destination, notify_customer, switch_caller_id, move_media_to_india, add_ip_to_acl"},
            "fix_params": {"type": "object", "description": "e.g. {\"to_carrier\":\"Vi-Delhi\",\"via\":\"Jio\"} or {\"ip\":\"34.93.1.2\"}"},
            "fix_cost": {"type": "string"},
            "customer_message": {"type": "string", "description": "2-4 sentence status note to the customer"}},
            "required": ["signature", "spoken_summary", "evidence", "owner", "fix"]}}},
    {"type": "function", "function": {
        "name": "answer_question",
        "description": "Answer a queued engineer question (ENGINEER ASKS [id]). 1-2 short spoken sentences.",
        "parameters": {"type": "object", "properties": {
            "question_id": {"type": "string"}, "answer": {"type": "string"}},
            "required": ["question_id", "answer"]}}},
]


def _short(obj, limit: int = 600) -> str:
    s = json.dumps(obj, default=str, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + "…"


class InvestigatorAgent:
    def __init__(self, store, fleet, now_fn: Callable[[], datetime], account_id: str, *,
                 chat: Callable[..., Awaitable[dict]] | None = None, max_steps: int = 28,
                 idle_poll_s: float = 1.0, tool_runner=None, tool_specs: list[dict] | None = None):
        self.store = store
        self.fleet = fleet
        self.now_fn = now_fn
        self.account_id = account_id
        self.chat = chat or llm.chat
        self.max_steps = max_steps
        self.idle_poll_s = idle_poll_s
        if tool_runner is None or tool_specs is None:
            from app import tools as T
            tool_runner = tool_runner or (lambda name, args: T.run_tool(name, args, T.ToolContext(fleet, now_fn())))
            tool_specs = tool_specs or compact_specs(T.TOOL_SPECS)
        self.run_tool = tool_runner
        self.tool_specs = tool_specs
        self.concluded = False
        self.steps = 0
        self._stop = False
        self._sent_questions: set[str] = set()
        self.messages: list[dict] = []
        self._model: str | None = None  # which LLM made the current step (shown on the dashboard)

    def stop(self) -> None:
        self._stop = True

    # ── case-file writes ───────────────────────────────────────────────────
    async def _log(self, kind: str, **data) -> None:
        entry = {"at": self.now_fn().strftime("%H:%M:%S"), "kind": kind, **data}
        await self.store.update(lambda c: c.investigator_log.append(entry))

    async def _upsert_hypothesis(self, args: dict, status: str | None = None, conf: float | None = None) -> None:
        hid = str(args.get("id") or args.get("signature") or "other")
        st = status or args.get("status", "open")
        confidence = float(conf if conf is not None else args.get("confidence") or 0.0)
        if st != "confirmed":
            confidence = min(confidence, 0.84)  # only verified conclusions may cross the 85% bar

        def apply(c: CaseFile) -> None:
            h = next((x for x in c.hypotheses if x.id == hid), None)
            if h is None:
                h = Hypothesis(id=hid, title=args.get("title") or hid)
                c.hypotheses.append(h)
            h.title = args.get("title") or h.title
            h.status = st if st in ("open", "leading", "ruled_out", "confirmed") else "open"
            h.confidence = 0.0 if h.status == "ruled_out" else round(confidence, 3)
            h.reason = args.get("evidence") or h.reason
            live = [x for x in c.hypotheses if x.status in ("leading", "confirmed")]
            top = max((x.confidence for x in live), default=0.0)
            if c.verdict != "confident":
                c.top_confidence = top
                c.leading = max(live, key=lambda x: x.confidence).id if live else None
                c.verdict = "leaning" if top >= 0.6 else "unsure"
        await self.store.update(apply)

    # ── tool dispatch ──────────────────────────────────────────────────────
    async def _dispatch(self, name: str, args: dict) -> dict:
        if name == "report_hypothesis":
            await self._upsert_hypothesis(args)
            await self._log("hypothesis", id=args.get("id"), status=args.get("status"),
                            confidence=args.get("confidence"), evidence=args.get("evidence"), model=self._model)
            return {"ok": True}
        if name == "conclude":
            return await self._conclude(args, source="llm")
        if name == "answer_question":
            qid, ans = args.get("question_id"), (args.get("answer") or "").strip()

            def apply(c: CaseFile) -> None:
                for q in c.pending_questions:
                    if q.get("id") == qid:
                        q["answer"], q["status"] = ans, "answered"
            await self.store.update(apply)
            await self._log("answer", question_id=qid, answer=ans)
            return {"ok": True}
        res = self.run_tool(name, args)
        if asyncio.iscoroutine(res):
            res = await res
        await self._log("tool", tool=name, args=args, result=_short(res, 400), model=self._model)
        return res

    async def _conclude(self, args: dict, source: str) -> dict:
        sig = args.get("signature")
        chk = verify.check(sig, self.fleet, self.account_id, self.now_fn())
        await self._log("verify", signature=sig, ok=chk.ok, reason=chk.reason, source=source)
        if not chk.ok:
            return {"verified": False, "reason": chk.reason,
                    "hint": "Gather more evidence or consider another signature."}
        pb = verify.PLAYBOOK.get(sig, {})
        acct = self.fleet.accounts.get(self.account_id)
        # The phone line always uses code-computed numbers (live run: the model said "about 22%" while
        # the verified share was 53%). The LLM's own wording is kept as the detailed summary.
        if args.get("spoken_summary"):
            args["detailed_summary"] = args["spoken_summary"]
        args["spoken_summary"] = verify.spoken(sig, chk, acct.name if acct else "")
        # Code owns the facts: who owns a signature, and which fixes can address it. The LLM may
        # pick among valid fixes (e.g. pace_dialer vs raise_cps) but can't invent one.
        valid_fixes = verify.VALID_FIXES.get(sig, {pb.get("fix")})
        chosen = args.get("fix") if args.get("fix") in valid_fixes else pb.get("fix")
        llm_owner = args.get("owner")
        is_default = chosen == pb.get("fix")
        fix = {"signature": sig, "fix": chosen,
               "params": (args.get("fix_params") or {}) if chosen == args.get("fix") else {},
               # costs are facts from the playbook, never LLM prose
               "cost": pb.get("cost") if is_default else None,
               "spoken_cost": pb.get("spoken_cost") if is_default else verify.FIX_SPOKEN_COST.get(chosen),
               "owner": pb.get("owner") or llm_owner, "status": "proposed",
               "spoken_summary": args.get("spoken_summary"), "detailed_summary": args.get("detailed_summary"),
               "evidence": args.get("evidence") or chk.reason,
               "customer_message": args.get("customer_message"), "check": chk.detail,
               "evidence_score": chk.score, "source": source}
        # Only pass parameters the fix actually takes (models like to invent extras, e.g. a backup number).
        allowed = {"raise_cps": {"cps_limit", "add"}, "pace_dialer": {"max_cps"},
                   "reroute_destination": {"to_carrier", "via"}, "switch_caller_id": {"caller_id"},
                   "move_media_to_india": {"region"}, "add_ip_to_acl": {"ip"}, "notify_customer": {"message"}}
        fix["params"] = {k: v for k, v in (fix["params"] or {}).items()
                         if k in allowed.get(fix["fix"], set()) and v not in (None, "", "None", "null")}
        # Fill route/ip params from the verified detail when the agent left them out (live run 21 Nov:
        # the model sent via=null and the reroute was rejected, so the incident could never resolve).
        if fix["fix"] == "reroute_destination":
            bad = chk.detail.get("carrier")
            carriers = [c for c, _ in (acct.carriers if acct else [])]
            healthy = [c for c in carriers if c != bad] or ["Jio"]
            # The affected destination is a verified fact (live run: the model put the via-carrier here).
            to = chk.detail.get("to_carrier") or fix["params"].get("to_carrier")
            via = fix["params"].get("via")
            if via not in carriers or via == bad:
                via = "Jio" if "Jio" in healthy else healthy[0]
            fix["params"] = {"to_carrier": to, "via": via}
        # Caller ID must be a non-Indian number the account already owns (models invent numbers).
        if fix["fix"] == "switch_caller_id":
            owned = [c for c in (acct.caller_ids if acct else []) if not c.startswith("+91")]
            if fix["params"].get("caller_id") not in owned:
                fix["params"] = {"caller_id": owned[0]} if owned else {}
        # The ACL fix needs the IP that is actually being rejected at the edge, not a guessed one.
        if fix["fix"] == "add_ip_to_acl":
            rejected = verify.top_rejected_ip(self.fleet, self.account_id, self.now_fn())
            if rejected and fix["params"].get("ip") != rejected:
                fix["params"] = {"ip": rejected}
        if llm_owner and llm_owner != fix["owner"]:
            await self._log("thought", text=f"Owner corrected by code: {llm_owner} → {fix['owner']} for {sig}")
        conf = max(0.9, min(0.99, 0.75 + 0.25 * chk.score))
        await self._upsert_hypothesis({"id": sig, "title": args.get("title") or sig.replace("_", " "),
                                       "evidence": chk.reason}, status="confirmed", conf=conf)

        def apply(c: CaseFile) -> None:
            c.verdict = "confident"
            c.top_confidence = round(conf, 3)
            c.leading = sig
            c.fixes = [f for f in c.fixes if f.get("status") != "proposed"] + [fix]
            c.evidence.append(Evidence(id=f"verify_{sig}", at=self.now_fn(), check="code_verification",
                                       summary=chk.reason, data=chk.detail, supports=[sig]))
        await self.store.update(apply)
        self.concluded = True
        await self._log("finding", signature=sig, summary=fix["spoken_summary"], fix=fix["fix"], owner=fix["owner"])
        return {"verified": True, "evidence_score": chk.score}

    async def _fallback(self, why: str) -> None:
        if self.concluded:
            return
        now = asyncio.get_running_loop().time()
        if now - getattr(self, "_last_fallback", -1e9) < FALLBACK_RETRY_S:
            return
        self._last_fallback = now
        guess = verify.best_guess(self.fleet, self.account_id, self.now_fn())
        if not getattr(self, "_fallback_logged", False):
            await self._log("thought", text=f"Rules-only diagnosis ({why})")
            self._fallback_logged = True
        if guess:
            sig, chk = guess
            pb = verify.PLAYBOOK[sig]
            acct = self.fleet.accounts.get(self.account_id)
            fix_params = {}
            if pb["fix"] == "add_ip_to_acl":
                ip = verify.top_rejected_ip(self.fleet, self.account_id, self.now_fn())
                if ip:
                    fix_params = {"ip": ip}
            await self._conclude({"signature": sig, "spoken_summary": verify.spoken(sig, chk, acct.name if acct else ""),
                                  "evidence": chk.reason, "owner": pb["owner"], "fix": pb["fix"],
                                  "fix_params": fix_params}, source="rules")

    # ── main loop ──────────────────────────────────────────────────────────
    def _new_questions(self, cf: CaseFile) -> list[dict]:
        return [q for q in cf.pending_questions
                if q.get("status") == "queued" and q.get("id") not in self._sent_questions]

    async def _triage(self) -> str:
        """The first queries any FDE runs, done in code at zero LLM tokens."""
        parts = []
        for name, args in TRIAGE:
            a = {"account_id": self.account_id, **args}
            try:
                res = self.run_tool(name, a)
                if asyncio.iscoroutine(res):
                    res = await res
            except Exception as e:  # noqa: BLE001
                res = {"error": repr(e)[:120]}
            await self._log("tool", tool=name, args=a, result=_short(res, 400), triage=True)
            parts.append(f"## {name}({', '.join(f'{k}={v}' for k, v in args.items())})\n{_short(res, 700)}")
        return "\n".join(parts)

    def _window_messages(self) -> list[dict]:
        head, tail = self.messages[:2], self.messages[2:]
        if len(tail) > HISTORY_TAIL:
            tail = tail[-HISTORY_TAIL:]
            while tail and tail[0].get("role") == "tool":  # never start on an orphaned tool result
                tail = tail[1:]
        return head + tail

    async def run(self, kickoff: str) -> None:
        t_start = asyncio.get_running_loop().time()
        await self._log("thought", text="Investigation started: running the triage pack")
        triage = await self._triage()
        self.messages = [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"{kickoff}\n\nTRIAGE PACK:\n{triage}"}]
        tools = self.tool_specs + AGENT_TOOLS
        llm_down_logged = False
        while not self._stop:
            if (not self.concluded and
                    asyncio.get_running_loop().time() - t_start > DEADLINE_S):
                await self._fallback("no verified conclusion within the deadline")
            cf = self.store.get()
            qs = self._new_questions(cf)
            for q in qs:
                self._sent_questions.add(q["id"])
                self.messages.append({"role": "user", "content": f"ENGINEER ASKS [{q['id']}]: {q['text']}"})
            if self.concluded and not qs and not self._awaiting_answers(cf):
                await asyncio.sleep(self.idle_poll_s)
                continue
            if self.steps >= self.max_steps:
                # Out of LLM steps: keep trying the (verified) rules diagnosis as data accumulates.
                # Only a passing verification marks the investigation concluded.
                await self._fallback("max steps")
                await asyncio.sleep(FALLBACK_RETRY_S)
                continue
            try:
                msg = await self.chat(self._window_messages(), tools)
                llm_down_logged = False
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if not llm_down_logged:
                    await self._log("error", text=f"LLM unavailable: {repr(e)[:140]}")
                    llm_down_logged = True
                # Every provider in the chain is down: don't sit out the deadline, let the
                # (code-verified) rules diagnosis take over as soon as the data supports it.
                await self._fallback("LLM unavailable")
                await asyncio.sleep(FALLBACK_RETRY_S)
                continue
            self.steps += 1
            self._model = msg.get("_model")
            calls = msg.get("tool_calls") or []
            clean = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                clean["tool_calls"] = calls
            self.messages.append(clean)
            if msg.get("content"):
                await self._log("thought", text=msg["content"].strip()[:300], model=msg.get("_model"))
            if not calls:
                if not self.concluded:
                    self.messages.append({"role": "user", "content":
                                          "Continue: report hypotheses, then conclude when the evidence is clear."})
                continue
            for tc in calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = await self._dispatch(name, args)
                except Exception as e:  # noqa: BLE001
                    result = {"error": repr(e)[:200]}
                self.messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                      "content": json.dumps(result, default=str)[:RESULT_CAP]})
                if name == "conclude" and not result.get("verified"):
                    await asyncio.sleep(REJECT_COOLDOWN_S)  # let more evidence accumulate first

    def _awaiting_answers(self, cf: CaseFile) -> bool:
        return any(q.get("status") == "queued" and q.get("id") in self._sent_questions
                   for q in cf.pending_questions)
