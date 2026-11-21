"""The real per-turn LLM proxy (build-plan.md §5.3), driven by the live incident.

Agora POSTs /v1/chat/completions on every turn. For each turn we:
  1. auth-check, snapshot the case file
  2. router.classify(last user text) → FAST | SLOW(check) | CONFIRM_YES | CONFIRM_NO | END
  3. move the state machine where the conversation implies it (answered, verdict told,
     actions proposed / confirmed / declined, hang-up)
  4. build OUR system prompt from the case file + RAG + code-decided verdict policy
  5. stream Groq's answer back as OpenAI-style SSE (content deltas only; no reasoning)

The proxy never decides the diagnosis. It only narrates the case file.

`bind(get_orch)` is called by the app so this module doesn't import the app (no cycles).
"""
from __future__ import annotations

import asyncio
import re
import json
import time
from collections import deque
from typing import Callable

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import checks, config, context, llm, router as intent_router, screen
from app.models import IncidentState as S
from app.rag import KnowledgeBase

router = APIRouter()
_rag = KnowledgeBase()
turns: deque[dict] = deque(maxlen=100)

_get_orch: Callable | None = None
_hangup: Callable | None = None  # async fn(attempt) — app-provided
SLOW_CHECK_PAUSE_S = 1.5  # a beat between the filler and the answer so it sounds like work happened
CONFIRM_DELAY_S = 4.0  # let "On it..." finish speaking before the ticket error narration
HANGUP_DELAY_S = 6.0  # let the goodbye finish before we drop the call

MAX_SENTENCES = 2  # persona says two; enforced here (real call 21 Nov: the model wrote the engineer's
                   # "Yes, please enable HA." and then "HA is now enabled" as its own third sentence)


class SentenceCap:
    """Pass streamed text through until N complete sentences, then stop.

    A sentence end is . ! or ? followed by whitespace, a capital letter or a quote (models
    sometimes drop the space: "change.Yes, please"). A terminator at the very end of the
    buffer isn't counted until the next chunk shows what follows, so "5.3" is never cut."""
    _END = __import__("re").compile(r"[.!?](?=[\s\"\u201c\u2018A-Z])")

    def __init__(self, n: int):
        self.n, self.buf, self.emitted, self.done = n, "", 0, False

    def feed(self, text: str) -> str:
        if self.done:
            return ""
        self.buf += text
        ends = [m.end() for m in self._END.finditer(self.buf)]
        if len(ends) >= self.n:
            self.done = True
            out = self.buf[self.emitted:ends[self.n - 1]]
        else:
            out = self.buf[self.emitted:]
        self.emitted += len(out)
        return out


_HEDGES = ("leading", "not confirmed", "unconfirmed", "not verified", "possib", "might", "may be", "maybe", "likely",
           "suspect", "checking", "looks like", "seems", "still", "verify", "verified", "not sure", "idea",
           "hypothes", "investigat", "narrow", "could be", "pulling", "looking into")
_SIG_WORDS = {"carrier_reject": ("carrier", "tata", "503", "5350", "reject"),
              "cps_burst": ("cps", "calls per second", "5180", "burst", "dialer", "rate limit", "rate-limit"),
              "silent_route": ("silent", "no audio", "dead air"),
              "anchoring_4590": ("anchoring", "4590", "media"),
              "acl_drop": ("acl", "allow-list", "allow list", "allowlist", " ip ", "blocked"),
              "pbx_timeout": ("pbx", "4170", "timeout")}


def states_unverified_cause(text: str, cf) -> bool:
    """True if the reply talks about a live hypothesis without hedging it (code guard, any phrasing)."""
    t = f" {text.lower()} "
    if any(h in t for h in _HEDGES):
        return False
    live = [h.id for h in cf.hypotheses if h.status in ("leading", "open", "confirmed")] or list(_SIG_WORDS)
    return any(w in t for hid in live for w in _SIG_WORDS.get(hid, ()))


def unverified_line(cf) -> str:
    lead = next((h for h in cf.hypotheses if h.status == "leading"), None)
    return ((f"Not confirmed yet: the leading idea is {lead.title.lower()}, and I'm still checking it."
             if lead else "Not confirmed yet, I'm still narrowing it down.")
            + " I'll tell you on this call the moment it's verified.")


FILLERS = {
    "compare_carriers": "Give me a few seconds, I'm checking the other carriers.",
    "sample_failed_call": "One sec, pulling a failed call.",
    "recent_config_changes": "Let me check for recent config changes.",
}


def bind(get_orch: Callable, hangup: Callable | None = None) -> None:
    global _get_orch, _hangup
    _get_orch, _hangup = get_orch, hangup


def _chunk(cid: str, content: str | None = None, finish: str | None = None) -> str:
    delta = {"content": content} if content is not None else {}
    return "data: " + json.dumps({
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": "incident-proxy",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }) + "\n\n"


async def _groq_stream(messages: list[dict], turn: dict, t0: float):
    """Voice answers: streamed, content only, with the fallback model (app/llm.py)."""
    async for text in llm.stream_text(messages, turn, t0):
        yield text


def _active(cf):
    return next((a for a in reversed(cf.calls) if a.state in ("placing", "ringing", "answered")), None)


async def plan_turn(user_text: str) -> dict:
    """Advance the state machine for this turn and decide what to say. Pure orchestration,
    no LLM, so it is unit-testable. Returns {fixed, filler, instruction, check, hangup}."""
    orch = _get_orch()
    cf = orch.store.get()
    out = {"fixed": None, "filler": None, "instruction": "", "check": None, "hangup": False,
           "intent": None}

    # A user turn on a ringing call means they picked up (backup for the status poller).
    if cf.state is S.CALLING:
        await orch.on_answered()
        cf = orch.store.get()

    intent, check_id = intent_router.classify(user_text)
    out["intent"] = intent
    active = _active(cf)
    purpose = active.purpose if active else "initial"

    if intent == intent_router.END:
        out["hangup"] = True
        out["fixed"] = ("Okay. I'll keep digging and call you back as soon as I'm sure."
                        if cf.verdict != "confident" and cf.state is not S.RESOLVED
                        else "Okay, talk soon.")
        return out

    if cf.state is S.CONFIRM_ACTIONS and intent == intent_router.CONFIRM_YES:
        out["fixed"] = "On it. Raising the ticket and drafting the customer email now."
        asyncio.get_running_loop().call_later(CONFIRM_DELAY_S, lambda: asyncio.ensure_future(orch.on_confirm_yes()))
        return out

    if cf.state is S.CONFIRM_ACTIONS and intent == intent_router.CONFIRM_NO:
        await orch.decline_actions()
        out["instruction"] = "The engineer declined the ticket for now. Acknowledge briefly and ask what else they need."
        return out

    if intent == intent_router.SLOW:
        out["check"] = check_id
        out["filler"] = FILLERS.get(check_id, "Give me a few seconds, I'm checking.")

    # Confident callback: as soon as the engineer engages, propose the actions.
    if (cf.state is S.IN_CALL and cf.verdict == "confident"
            and purpose in ("callback", "on_demand", "retry") and intent != intent_router.SLOW):
        if intent == intent_router.CONFIRM_YES:
            await orch.propose_actions()
            out["fixed"] = "On it. Raising the ticket and drafting the customer email now."
            asyncio.get_running_loop().call_later(CONFIRM_DELAY_S, lambda: asyncio.ensure_future(orch.on_confirm_yes()))
            return out
        await orch.propose_actions()
        out["instruction"] = ("Give the cause, the key evidence, and the fix in two or three short sentences, "
                              "then end by asking exactly: should I raise the ticket and draft the customer email?")
        return out

    # Unsure call: being asked for the cause is what sets up the honest callback.
    if cf.state is S.IN_CALL and cf.verdict == "recovered" and intent_router.asks_cause(user_text):
        out["instruction"] = ("They asked for the cause. The calls have recovered; say so, say it pointed at "
                              "carrier X but was never confirmed, and that you'll keep watching it.")
    elif cf.state is S.IN_CALL and cf.verdict != "confident" and intent_router.asks_cause(user_text):
        if getattr(orch, "single_call", False):
            # Single-call flow: the investigation is still running in the background on this call.
            out["instruction"] = ("They asked for the cause. Say plainly you don't know yet, say what you have "
                                  "already ruled out, and that you're still checking and will tell them on this "
                                  "call the moment you find it.")
        else:
            await orch.on_verdict_told()
            out["instruction"] = ("They asked for the cause. Say plainly you are not sure yet, say what you have "
                                  "ruled out, and offer to call back as soon as you are confident.")
    return out


async def plan_turn_v2(user_text: str) -> dict:
    """v2 (OnCall): questions that need digging go to the background investigator; a "yes"
    applies the proposed fix. The voice reply itself never waits on the investigator."""
    orch = _get_orch()
    cf = orch.store.get()
    out = {"fixed": None, "filler": None, "instruction": "", "check": None, "hangup": False, "intent": None}
    if cf.state is S.CALLING:
        await orch.on_answered()
        cf = orch.store.get()
    if not re.search(r"[A-Za-z0-9\u0900-\u097F]", user_text or ""):
        # empty / noise-only turn (sim 21 Nov: "   " and "..." got full answers)
        out["intent"] = "EMPTY"
        out["fixed"] = "Sorry, I didn't catch that."
        return out
    intent, _check = intent_router.classify(user_text)
    out["intent"] = intent
    if intent == intent_router.END:
        out["hangup"] = True
        out["fixed"] = ("Okay. I'll keep digging and text you the moment I have it."
                        if cf.verdict != "confident" else
                        "Okay. I'll send the details to Telegram so you can approve the fix from there.")
        return out
    # Action confirmations are written by code, not the LLM: instant, and they can't misstate what
    # happened (real calls 21 Nov: "HA is now enabled" / "Open Tickets tab" were invented).
    fix_open = (cf.verdict == "confident" and orch.proposed_fix(cf) is not None
                and cf.state in (S.IN_CALL, S.CONFIRM_ACTIONS, S.UNSURE_TOLD))
    still = " The fix is still waiting for your yes." if fix_open else ""
    if intent != intent_router.CONFIRM_NO and intent_router.asks_to_loop_in(user_text) and cf.alert is not None:
        from app import escalation
        fix = orch.proposed_fix(cf) or next((f for f in reversed(cf.fixes) if f.get("owner")), {})
        people = escalation.pick(user_text, fix.get("owner"))
        already = {n for a in cf.actions if a.kind == "loop_in" and a.status in ("done", "running")
                   for n in (a.payload or {}).get("people", [])}
        t = orch.ticket_ref()
        tid = t.external_id if t is not None and t.external_id else None
        out["intent"] = "LOOP_IN"
        if people and all(p["name"] in already for p in people):
            # repeat request: don't spam the ticket with the same loop-in (sim 21 Nov)
            out["fixed"] = f"{escalation.names(people).capitalize()} {'is' if len(people) == 1 else 'are'} already on {tid or 'the ticket'}."
            return out
        people = [p for p in people if p["name"] not in already]
        orch._spawn(escalation.loop_in(orch, people, user_text[:160]))
        who = escalation.names(people)
        out["fixed"] = (f"Looping in {who} on {tid} with the evidence." if tid else
                        f"Filing the ticket and looping in {who} with the evidence.") + still
        return out
    if intent != intent_router.CONFIRM_NO and intent_router.asks_for_summary(user_text) and cf.alert is not None:
        out["intent"] = "SUMMARY"
        last = next((a for a in reversed(cf.actions) if a.kind == "incident_summary"
                     and (a.payload or {}).get("via") == "telegram" and a.status == "done"), None)
        if last is not None and (last.payload or {}).get("text") == orch.summary_text(cf):
            out["fixed"] = "That summary is already on your Telegram, and nothing has changed since."
            return out
        ok = await orch.send_summary()
        out["fixed"] = ("Done, the summary is on your Telegram and on the screen." + still if ok else
                        "Sorry, Telegram didn't accept it. Want me to try again?")
        return out
    if intent != intent_router.CONFIRM_NO and intent_router.asks_for_ticket(user_text) and cf.alert is not None:
        await orch.file_ticket()
        t = orch.ticket_ref()
        out["intent"] = "TICKET"
        tid = t.external_id if t is not None and t.external_id else None
        out["fixed"] = ((f"It's already in Linear as {' '.join(tid.split('-'))}." if tid else
                         "Filing it in Linear now with the investigation trail; the number will pop up on your screen.")
                        + still)
        return out
    pending = (cf.verdict == "confident" and orch.proposed_fix(cf) is not None
               and cf.state in (S.IN_CALL, S.UNSURE_TOLD, S.CONFIRM_ACTIONS))
    wants = intent == intent_router.CONFIRM_YES or (intent != intent_router.CONFIRM_NO
                                                    and intent_router.asks_to_act(user_text))
    if pending and wants and await orch.approve_now():
        fix = orch.proposed_fix(orch.store.get()) or {}
        from app.oncall import FIX_PHRASES
        out["intent"] = "APPROVE"
        out["fixed"] = f"On it. Going to {FIX_PHRASES.get(fix.get('fix'), 'apply the fix')} now."
        inc_id = orch.store.get().incident_id

        def _approve_same_incident():
            # bound to this incident: never approve a later incident's fix (sim 21 Nov: delayed actions
            # leaked into the next session)
            if _get_orch() is orch and orch.store.get().incident_id == inc_id:
                orch._spawn(orch.on_confirm_yes())
        asyncio.get_running_loop().call_later(CONFIRM_DELAY_S, _approve_same_incident)
        return out
    if cf.state is S.CONFIRM_ACTIONS and intent == intent_router.CONFIRM_NO:
        await orch.decline()
        out["instruction"] = ("They said no to the fix for now. Acknowledge in a few words, mention the free "
                              "alternative if there is one, and that they can just say yes any time.")
        return out
    if screen.wants_screen(user_text):
        action = await screen.plan(user_text, screen.incident_hint(cf))
        if action:
            event, facts = screen.resolve(action, orch.fleet, cf.account_id, orch.now(),
                                          cf.alert.signal if cf.alert else None, screen.clean_said(user_text))
            await screen.bus.publish(event)
            await orch.store.update(lambda c: c.investigator_log.append(
                {"at": orch.now().strftime("%H:%M:%S"), "kind": "screen", "action": action["name"],
                 "args": action.get("args"), "said": event.get("said")}))
            out["intent"] = f"SCREEN:{action['name']}"
            if facts:
                out["instruction"] = (f"You just changed what's on their screen. {facts} In one or two short spoken "
                                      "sentences, walk them through what they're looking at: point at the key "
                                      "line (e.g. where the 503 comes back from the carrier). No IDs or UUIDs.")
            return out
    if intent == intent_router.SLOW or intent_router.needs_investigation(user_text):
        qid = await orch.ask(user_text)
        out["intent"] = "ASK"
        out["instruction"] = (f"They asked something the investigator is now checking (question {qid}). If the "
                              "case file already answers it, answer briefly; otherwise say you're checking that "
                              "right now and will tell them in a few seconds. Do not guess.")
        return out
    if cf.verdict != "confident" and cf.alert is not None:
        # Always, not only on recognised cause questions (sim 21 Nov: Hinglish "kya ho raha hai?" got the
        # unverified lead stated as fact).
        out["instruction"] = ("Nothing is verified yet. If you mention a cause at all, call it the leading idea, "
                              "not confirmed, and never state it as fact.")
    if intent_router.asks_cause(user_text) and cf.verdict != "confident" and cf.alert is not None:
        # Code-written while unverified (sim 21 Nov: the LLM kept stating the lead as fact).
        lead = next((h for h in cf.hypotheses if h.status == "leading"), None)
        out["intent"] = "CAUSE_UNVERIFIED"
        out["fixed"] = unverified_line(cf)
        return out
    if intent_router.asks_cause(user_text) and cf.verdict != "confident":
        out["instruction"] = ("They asked for the cause. Not verified yet. In at most two short sentences: say you "
                              "don't have it confirmed, name the leading idea in plain words, and that you'll tell "
                              "them on this call. Mention at most one ruled-out item, only if it helps.")
    return out


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request):
    if request.headers.get("authorization", "") != f"Bearer {config.PROXY_SHARED_SECRET}":
        raise HTTPException(status_code=401, detail="bad proxy key")
    if _get_orch is None:
        raise HTTPException(status_code=503, detail="brain not bound")

    body = await request.json()
    t0 = time.perf_counter()
    incoming = body.get("messages", [])
    history = [{"role": m["role"], "content": m.get("content") or ""}
               for m in incoming if m.get("role") in ("user", "assistant")]
    user_text = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    turn = {"turn_id": body.get("turn_id"), "user": user_text, "agent": "", "intent": None,
            "ttft_ms": None, "total_ms": None, "error": None}
    turns.append(turn)
    cid = f"chatcmpl-{body.get('turn_id', int(t0 * 1000))}"
    orch = _get_orch()

    async def stream():
        try:
            if user_text:
                await orch.add_turn("engineer", user_text)
            v2 = getattr(orch, "v2", False)
            plan = await (plan_turn_v2(user_text) if v2 else plan_turn(user_text))
            turn["intent"] = plan["intent"]
            if plan["filler"]:
                turn["agent"] += plan["filler"] + " "
                turn["ttft_ms"] = round((time.perf_counter() - t0) * 1000)
                yield _chunk(cid, plan["filler"] + " ")
            if plan["check"]:
                o = _get_orch()
                calls = o.sim.calls_through(o.clock.now())
                ev = checks.REGISTRY[plan["check"]](calls, o._now_dt())
                await o.add_evidence(ev)
                await asyncio.sleep(SLOW_CHECK_PAUSE_S)
                plan["instruction"] = (f"You just ran a check. Result: {ev.summary} "
                                       "Tell the engineer the result in one or two sentences.")
            if plan["fixed"]:
                turn["agent"] += plan["fixed"]
                turn["ttft_ms"] = turn["ttft_ms"] or round((time.perf_counter() - t0) * 1000)
                yield _chunk(cid, llm.clean_for_tts(plan["fixed"]))
            else:
                cf = _get_orch().store.get()
                active = _active(cf)
                if v2:
                    acct = orch.account(cf.account_id)
                    risk = orch.at_risk_per_hour(cf.account_id, cf.alert.signal_value or 0) if cf.alert else 0
                    sys_prompt = context.build_system_prompt_v2(
                        cf, acct, _rag.top_k(user_text or "incident", k=2), plan["instruction"], risk,
                        engineer=getattr(orch, "engineer", None))
                else:
                    sys_prompt = context.build_system_prompt(
                        cf, _rag.top_k(user_text or "incident", k=3),
                        call_purpose=active.purpose if active else "initial",
                        instruction=plan["instruction"])
                capper = SentenceCap(MAX_SENTENCES)
                # While nothing is verified, hold the (short) reply and check it before it's spoken: the
                # LLM must not state an unverified hypothesis as fact (sims 21 Nov, any phrasing).
                hold = cf.verdict != "confident" and cf.alert is not None
                held = ""
                async for text in _groq_stream([{"role": "system", "content": sys_prompt}, *history], turn, t0):
                    keep = capper.feed(text)
                    if keep:
                        if hold:
                            held += keep
                        else:
                            turn["agent"] += keep
                            yield _chunk(cid, keep)
                    if capper.done:
                        turn["capped"] = True
                        break
                if hold and held:
                    if states_unverified_cause(held, cf):
                        turn["guarded"] = held[:200]
                        held = unverified_line(cf)
                    turn["agent"] += held
                    held = llm.clean_for_tts(held)
                    yield _chunk(cid, held)
            if turn["agent"].strip():
                await orch.add_turn("agent", turn["agent"].strip())
            if plan["hangup"] and _hangup is not None:
                o = _get_orch()
                cur = _active(o.store.get())
                att = cur.attempt_id if cur else None

                def _hangup_same_call():
                    # only hang up the call this goodbye was said on (a new session may have started)
                    now_active = _active(_get_orch().store.get())
                    if att and now_active is not None and now_active.attempt_id == att:
                        asyncio.ensure_future(_hangup())
                asyncio.get_running_loop().call_later(HANGUP_DELAY_S, _hangup_same_call)
        except Exception as e:  # noqa: BLE001 - say something rather than go silent
            turn["error"] = repr(e)[:200]
            yield _chunk(cid, "Sorry, I lost my train of thought for a second.")
        turn["total_ms"] = round((time.perf_counter() - t0) * 1000)
        print(f"[turn {turn['turn_id']}] {turn['intent']} ttft={turn['ttft_ms']}ms total={turn['total_ms']}ms "
              f"user={turn['user']!r} agent={turn['agent']!r} err={turn['error']}", flush=True)
        yield _chunk(cid, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
