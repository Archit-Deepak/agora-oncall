"""OnCall — the v2 orchestrator: spend-aware, multi-account, LLM investigator in the loop.

  MONITORING ──AccountWatch fires──► SUSPECTED  (investigator agent starts immediately:
                                                  ring time and confirm time are free compute)
  SUSPECTED ──recovers in last 20 s──► AUTO_RECOVERED (+ Telegram note with what it saw)
  SUSPECTED ──still bad after confirm_s──► high spend: ALERTED → CALLING (phone)
                                         low spend:  TELEGRAM_SENT (findings + "Call me" button)
  IN_CALL: investigator answers + "found it" are spoken into the call via /speak;
           "found it" proposes the fix with its cost → CONFIRM_ACTIONS
  CONFIRM_ACTIONS ──"yes"──► EXECUTING (apply the fix) → VERIFYING (watch recovery live)
                  ──hang up──► TELEGRAM_SENT ("Approve fix" / "Call me" buttons)
  VERIFYING ──recovered──► RESOLVED (+ ticket, customer email, memory, Telegram summary)
            ──no effect──► back to IN_CALL, investigator reopened with that fact

Every mutation goes through the store; transitions are whitelisted in app/states.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

from app import email_draft, states, verify
from app.detector import is_failure
from app.models import ActionRecord, Alert, CallAttempt, CaseFile, IncidentState as S

log = logging.getLogger("oncall")

FIX_PHRASES = {
    "enable_ha": "enable a high-availability backup number on another carrier",
    "raise_cps": "raise their CPS limit by adding concurrency",
    "pace_dialer": "ask them to pace their dialer under the CPS limit",
    "reroute_destination": "move those calls onto a healthy route",
    "notify_customer": "email their tech team with the evidence",
    "switch_caller_id": "switch them to a non-Indian caller ID",
    "move_media_to_india": "move their media back to an Indian region",
    "add_ip_to_acl": "add their new IP to the trunk allow-list",
}
def done_line(fix: dict, res: dict) -> str:
    """What the agent says after applying a fix (plain words, no system strings)."""
    import re as _re
    p = fix.get("params") or {}
    msg = res.get("message", "")
    m = _re.search(r"raised (\d+) → (\d+)", msg)
    return {
        "enable_ha": "High availability is on, so calls the carrier rejects now retry from a backup number on another carrier.",
        "raise_cps": f"Their CPS limit is now {m.group(2)}, up from {m.group(1)}." if m else "I've raised their CPS limit.",
        "pace_dialer": "I've asked them to pace their dialer under the limit.",
        "reroute_destination": f"Calls to {p.get('to_carrier', 'that network')} now go via {p.get('via', 'a healthy carrier')}.",
        "switch_caller_id": "Their outbound calls now use a non-Indian caller ID.",
        "move_media_to_india": "Their media is back in an Indian region.",
        "add_ip_to_acl": "Their new IP is on the trunk allow-list.",
        "notify_customer": "I've emailed their team with the evidence.",
    }.get(fix.get("fix"), "The fix is applied.")


SIGNAL_LABEL = {"failure_rate": "failing", "silent_rate": "silent", "volume_drop": "missing"}


def _pct(x: float | None) -> int:
    return int(round((x or 0) * 100))


def _inr(x: float) -> str:
    return f"₹{int(round(x)):,}"


class OnCall:
    v2 = True

    def __init__(self, store, clock, fleet, watch, voice, ticket, notifier, trigger: datetime, *,
                 agent_factory=None, summarizer=None, confirm_s: float = 25.0,
                 call_min_spend_inr: float = 20000, found_it_min_s: float = 12.0,
                 verify_timeout_s: float = 75.0, retry_wait_s: float = 15.0, engineer: str = "there"):
        self.store, self.clock, self.fleet, self.watch = store, clock, fleet, watch
        self.voice, self.ticket, self.notifier = voice, ticket, notifier
        self.trigger = trigger
        self.agent_factory = agent_factory  # (account_id) -> InvestigatorAgent | None
        self.summarizer = summarizer
        self.confirm_s = confirm_s
        self.call_min_spend_inr = call_min_spend_inr
        self.found_it_min_s = found_it_min_s
        self.verify_timeout_s = verify_timeout_s
        self.retry_wait_s = retry_wait_s
        self.engineer = engineer
        self.agent = None
        self.agent_task: asyncio.Task | None = None
        self.summary_notifier = None  # where "send me a summary on Telegram" goes (defaults to notifier)
        self.bg: set[asyncio.Task] = set()
        self._reset_incident()

    def _reset_incident(self) -> None:
        self._suspected_at: float | None = None
        self._peak = 0.0
        self._found_pushed = False
        self._recovered_pushed = False
        self._spoken_answers: set[str] = set()
        self._retry_at: float | None = None
        self._dial_failures = 0
        self._last_greeting = ""
        self._verify_started: float | None = None
        self._verify_before: float | None = None

    # ── helpers ────────────────────────────────────────────────────────────
    def now(self) -> datetime:
        return self.trigger + timedelta(seconds=self.clock.now())

    @staticmethod
    def _set(c: CaseFile, new: S) -> None:
        states.check(c.state, new)
        c.state = new

    @staticmethod
    def active(c: CaseFile) -> CallAttempt | None:
        return next((a for a in reversed(c.calls) if a.state in ("placing", "ringing", "answered")), None)

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self.bg.add(t)

        def done(task: asyncio.Task) -> None:
            self.bg.discard(task)
            if not task.cancelled() and task.exception():
                log.warning("background task failed: %r", task.exception())
                asyncio.ensure_future(self._note_error(f"background: {task.exception()!r}"[:200]))
        t.add_done_callback(done)

    async def _note_error(self, text: str) -> None:
        await self.store.update(lambda c: c.investigator_log.append(
            {"at": self.now().strftime("%H:%M:%S"), "kind": "error", "text": text}))

    def account(self, aid: str | None = None):
        aid = aid or self.store.get().account_id
        return self.fleet.accounts.get(aid) if aid else None

    def signal_value(self, aid: str, signal: str, seconds: float) -> float:
        now = self.now()
        calls = self.fleet.calls(aid, now - timedelta(seconds=seconds), now)
        if signal == "volume_drop":
            exp = self.fleet.expected_rate(aid) * seconds
            return len(calls) / exp if exp else 1.0
        if signal == "silent_rate":
            ans = [c for c in calls if c.audio_energy is not None]
            return sum(c.audio_energy <= 5 for c in ans) / len(ans) if ans else 0.0
        return sum(map(is_failure, calls)) / len(calls) if calls else 0.0

    def is_bad(self, signal: str, value: float) -> bool:
        if signal == "volume_drop":
            return value <= self.watch.volume_ratio
        if signal == "silent_rate":
            return value >= self.watch.silent_rate
        return value >= self.watch.fail_rate

    def is_recovered(self, signal: str, value: float) -> bool:
        if signal == "volume_drop":
            return value >= 0.85
        return value < 0.06

    def at_risk_per_hour(self, aid: str, share: float) -> float:
        acct = self.account(aid)
        return (acct.spend_per_day_inr / 24.0) * share if acct else 0.0

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def start(self) -> None:
        await self.store.update(lambda c: self._set(c, S.MONITORING))

    async def tick(self) -> None:
        cf = self.store.get()
        st = cf.state
        if st in (S.MONITORING, S.AUTO_RECOVERED, S.RESOLVED):
            if st is not S.MONITORING and cf.account_id and cf.alert:
                v = self.signal_value(cf.account_id, cf.alert.signal, self.watch.window_s)
                if not self.is_bad(cf.alert.signal, v):
                    self.watch.rearm(cf.account_id)
            alert = self.watch.check(self.fleet, self.now())
            if alert is not None:
                await self._on_suspected(alert)
            return

        aid, sig = cf.account_id, cf.alert.signal if cf.alert else "failure_rate"
        if st is S.SUSPECTED:
            v20 = self.signal_value(aid, sig, 20)
            self._peak = max(self._peak, self.signal_value(aid, sig, self.watch.window_s)) \
                if sig != "volume_drop" else min(self._peak or 1.0, self.signal_value(aid, sig, 60))
            if self.is_recovered(sig, v20):
                await self._auto_recover(during_call=False)
            elif self.clock.now() - (self._suspected_at or 0) >= self.confirm_s \
                    and self.is_bad(sig, self.signal_value(aid, sig, 6)):
                await self._confirmed()
            return

        if st is S.RETRY and self._retry_at is not None and time.monotonic() >= self._retry_at:
            self._retry_at = None
            await self._place_call("retry", self._last_greeting)
            return

        await self._live_call_updates(self.store.get())

        cf = self.store.get()
        if cf.state is S.VERIFYING:
            await self._check_verification(cf)
        elif cf.state is S.WAITING_FOR_EVIDENCE and cf.verdict == "confident":
            await self._place_call("callback", self.found_it_line(cf, opener=True))

    # ── suspicion / confirmation ───────────────────────────────────────────
    async def _on_suspected(self, alert: Alert) -> None:
        self._reset_incident()
        self._suspected_at = self.clock.now()
        self._peak = alert.signal_value or alert.failure_rate
        aid = alert.account_id
        old_calls = len(self.store.get().calls)

        def apply(c: CaseFile) -> None:
            c.incident_id = alert.incident_id
            c.account_id = aid
            c.alert = alert
            c.hypotheses, c.evidence, c.fixes, c.investigator_log = [], [], [], []
            c.pending_questions, c.transcript, c.actions = [], [], []
            c.verdict, c.top_confidence, c.leading, c.email_draft = "unsure", 0.0, None, None
            c.milestones = type(c.milestones)(manual_baseline_seconds=c.milestones.manual_baseline_seconds)
            c.milestones.alert_at = self.now()
            self._set(c, S.SUSPECTED)
        await self.store.update(apply)
        self._call_offset = old_calls
        await self._start_agent(aid, alert)

    async def _start_agent(self, aid: str, alert: Alert) -> None:
        await self._stop_agent()
        if not self.agent_factory:
            return
        self.agent = self.agent_factory(aid)
        acct = self.account(aid)
        kickoff = (f"ALERT {alert.incident_id}: account {aid} ({acct.name if acct else aid}, "
                   f"{acct.use_case if acct else ''}, spend ₹{int(acct.spend_per_day_inr) if acct else 0:,}/day). "
                   f"Signal {alert.signal} = {alert.signal_value if alert.signal_value is not None else alert.failure_rate:.3f} "
                   f"over the last minute. Find the root cause.")
        self.agent_task = asyncio.ensure_future(self.agent.run(kickoff))

    async def _stop_agent(self) -> None:
        if self.agent:
            self.agent.stop()
        if self.agent_task and not self.agent_task.done():
            self.agent_task.cancel()
            try:
                await self.agent_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.agent, self.agent_task = None, None

    async def _confirmed(self) -> None:
        cf = self.store.get()
        aid, sig = cf.account_id, cf.alert.signal
        v = self.signal_value(aid, sig, self.watch.window_s)

        def apply(c: CaseFile) -> None:
            if c.alert:
                c.alert.signal_value = round(v, 4)
                if sig == "failure_rate":
                    c.alert.failure_rate = round(v, 4)
            self._set(c, S.ALERTED if self._high_value(aid) else S.TELEGRAM_SENT)
        await self.store.update(apply)
        if self._high_value(aid):
            await self._place_call("initial", self.opening_line(self.store.get()))
        else:
            self._spawn(self._low_tier_notice())

    def _high_value(self, aid: str) -> bool:
        acct = self.account(aid)
        return bool(acct and acct.spend_per_day_inr >= self.call_min_spend_inr)

    async def _low_tier_notice(self) -> None:
        # Give the investigator a moment so the note has substance.
        for _ in range(40):
            if self.agent is None or self.agent.concluded:
                break
            await asyncio.sleep(0.5)
        cf = self.store.get()
        fix = self.proposed_fix(cf)
        acct = self.account()
        text = (f"🟠 {acct.name if acct else cf.account_id}: {self._signal_sentence(cf)}\n"
                f"(₹{int(acct.spend_per_day_inr) if acct else 0:,}/day account, below the phone-call tier)\n")
        if fix:
            text += f"Cause: {fix.get('evidence')}\nProposed fix: {FIX_PHRASES.get(fix['fix'], fix['fix'])}"
            if fix.get("cost"):
                text += f" (cost: {fix['cost']})"
        buttons = [("✅ Approve fix", "approve_fix"), ("📞 Call me", "call_me_now")] if fix else [("📞 Call me", "call_me_now")]
        await self._send(text, buttons)

    async def _auto_recover(self, during_call: bool) -> None:
        cf = self.store.get()
        dur = int(self.clock.now() - (self._suspected_at or self.clock.now()))
        await self.store.update(lambda c: self._set(c, S.AUTO_RECOVERED))
        await self._stop_agent()
        acct = self.account()
        lead = next((h for h in cf.hypotheses if h.status in ("leading", "confirmed")), None)
        label = SIGNAL_LABEL.get(cf.alert.signal if cf.alert else "", "failing")
        text = (f"⚠️ Mild incident — {acct.name if acct else cf.account_id}\n"
                f"Up to {_pct(self._peak)}% of calls {label} for about {dur} s; recovered on its own.\n"
                + (f"What it looked like: {lead.reason}\n" if lead and lead.reason else "")
                + "No action needed; still watching.")
        if self.summarizer:
            try:
                text = await self.summarizer(cf, self._peak, dur, during_call)
            except Exception:  # noqa: BLE001
                pass
        await self._send(text, None, kind="telegram_notice")

    # ── calls ──────────────────────────────────────────────────────────────
    def _signal_sentence(self, cf: CaseFile) -> str:
        a = cf.alert
        if not a:
            return "calls are failing"
        v = a.signal_value if a.signal_value is not None else a.failure_rate
        if a.signal == "silent_rate":
            return f"calls are connecting but about {_pct(v)} percent are silent"
        if a.signal == "volume_drop":
            return f"their call volume has dropped to about {_pct(v)} percent of normal"
        return f"about {_pct(v)} percent of their calls are failing"

    def opening_line(self, cf: CaseFile) -> str:
        acct = self.account(cf.account_id)
        risk = self.at_risk_per_hour(cf.account_id, cf.alert.signal_value or 0) if cf.alert else 0
        spend = f", about {int(acct.spend_per_day_inr / 1000)} thousand rupees a day" if acct else ""
        return (f"Hi {self.engineer}, this is your incident agent. {acct.name if acct else 'A customer'}{spend}, "
                f"is in trouble: {self._signal_sentence(cf)} over the last minute, roughly "
                f"{int(risk):,} rupees an hour at risk. I'm debugging it as we speak and I'll tell you "
                f"the moment I find it. What do you want to know?")

    def proposed_fix(self, cf: CaseFile) -> dict | None:
        return next((f for f in reversed(cf.fixes) if f.get("status") == "proposed"), None)

    def found_it_line(self, cf: CaseFile, opener: bool = False) -> str:
        fix = self.proposed_fix(cf) or {}
        pre = "Hey, it's me again. I found it. " if opener else "Okay, I've got it. "
        cost = fix.get("spoken_cost")
        ask = f"I can {FIX_PHRASES.get(fix.get('fix'), fix.get('fix') or 'apply the fix')}"
        if cost:
            ask += f", which costs {cost}"
        owner = fix.get("owner")
        owner_txt = {"customer": " This one's on their side, not ours.",
                     "carrier": " It's the carrier, not us."}.get(owner, "")
        return f"{pre}{fix.get('spoken_summary') or fix.get('evidence', '')}{owner_txt} {ask}. Want me to go ahead?"

    async def _place_call(self, purpose: str, greeting: str) -> None:
        self._last_greeting = greeting
        attempt = CallAttempt(attempt_id=f"att-{uuid.uuid4().hex[:8]}", purpose=purpose,
                              route=getattr(self.voice, "route", "mock"), placed_at=self.now(), state="placing")
        try:
            attempt.agora_agent_id = await self.voice.place_call(attempt, greeting)
            attempt.state = "ringing"
        except Exception as e:  # noqa: BLE001 - a failed dial must never freeze the incident
            attempt.state, attempt.hangup_reason = "failed", f"dial error: {e!r}"[:200]
            self._dial_failures += 1
        dial_failed = attempt.state == "failed"

        def apply(c: CaseFile) -> None:
            c.calls.append(attempt)
            if c.milestones.call_placed_at is None and not dial_failed:
                c.milestones.call_placed_at = self.now()
            if dial_failed:
                target = (S.RETRY if self._dial_failures < 3 and c.state in (S.ALERTED, S.RETRY)
                          else S.TELEGRAM_SENT)
                if c.state is not target:
                    self._set(c, target)
            else:
                self._set(c, S.CALLING)
        if dial_failed and self.store.get().state is S.WAITING_FOR_EVIDENCE:
            # callback dial failed: just try again later from WAITING_FOR_EVIDENCE
            await self.store.update(lambda c: c.calls.append(attempt))
            await self._note_error(attempt.hangup_reason)
            return
        await self.store.update(apply)
        if dial_failed:
            await self._note_error(attempt.hangup_reason)
            if self._dial_failures < 3:
                self._retry_at = time.monotonic() + 5
            else:
                await self._send(f"🚨 I couldn't place the call ({attempt.hangup_reason}). "
                                 f"{self._signal_sentence(self.store.get())}.", [("📞 Call me", "call_me_now")])

    async def on_answered(self) -> None:
        now = self.now()

        def apply(c: CaseFile) -> None:
            a = self.active(c)
            if a and a.state != "answered":
                a.state, a.answered_at = "answered", now
            if c.milestones.acknowledged_at is None:
                c.milestones.acknowledged_at = now
            if c.state is S.CALLING:
                self._set(c, S.IN_CALL)
        await self.store.update(apply)

    async def on_no_answer(self, reason: str = "no_answer") -> None:
        cf = self.store.get()
        a = self.active(cf)
        first = a is not None and a.purpose != "retry"

        def apply(c: CaseFile) -> None:
            x = self.active(c)
            if x:
                x.state, x.hangup_reason, x.ended_at = "failed", reason, self.now()
            self._set(c, S.RETRY if first else S.TELEGRAM_SENT)
        await self.store.update(apply)
        if first:
            self._retry_at = time.monotonic() + self.retry_wait_s
        else:
            cf = self.store.get()
            fix = self.proposed_fix(cf)
            text = f"🚨 Couldn't reach you twice. {self.account().name if self.account() else ''}: {self._signal_sentence(cf)}."
            if fix:
                text += f"\nCause: {fix.get('evidence')}\nFix: {FIX_PHRASES.get(fix['fix'], fix['fix'])}"
            await self._send(text, [("✅ Approve fix", "approve_fix"), ("📞 Call me", "call_me_now")] if fix
                             else [("📞 Call me", "call_me_now")])

    async def on_call_ended(self) -> None:
        def apply(c: CaseFile) -> None:
            a = self.active(c)
            if a:
                a.state, a.ended_at = "ended", self.now()
            if c.state in (S.IN_CALL, S.UNSURE_TOLD) and c.verdict != "confident":
                self._set(c, S.WAITING_FOR_EVIDENCE)
            elif c.state in (S.IN_CALL, S.CONFIRM_ACTIONS) and c.verdict == "confident":
                self._set(c, S.TELEGRAM_SENT)
        before = self.store.get().state
        await self.store.update(apply)
        after = self.store.get().state
        if after is S.TELEGRAM_SENT and before is not S.TELEGRAM_SENT:
            cf = self.store.get()
            fix = self.proposed_fix(cf)
            await self._send(f"📋 You hung up before deciding. {fix.get('evidence') if fix else ''}\n"
                             f"Proposed: {FIX_PHRASES.get(fix['fix'], fix['fix']) if fix else 'n/a'}",
                             [("✅ Approve fix", "approve_fix"), ("📞 Call me", "call_me_now")])

    async def on_demand_call(self) -> None:
        cf = self.store.get()
        if cf.state is not S.TELEGRAM_SENT:
            return
        line = self.found_it_line(cf, opener=True) if self.proposed_fix(cf) else \
            "You asked me to call. " + self.opening_line(cf)
        if self.proposed_fix(cf):
            self._found_pushed = True  # the call opens with the proposal; answered → CONFIRM_ACTIONS
        await self._place_call("on_demand", line)

    # ── during the call ────────────────────────────────────────────────────
    async def ask(self, text: str) -> str:
        """Queue an engineer question for the investigator (voice proxy calls this)."""
        qid = f"q-{uuid.uuid4().hex[:5]}"
        await self.store.update(lambda c: c.pending_questions.append(
            {"id": qid, "text": text, "status": "queued", "at": self.now().strftime("%H:%M:%S")}))
        if self.agent is None and self.agent_factory and self.store.get().account_id:
            await self._start_agent(self.store.get().account_id, self.store.get().alert)
        return qid

    async def _live_call_updates(self, cf: CaseFile) -> None:
        a = self.active(cf)
        on_call = a is not None and a.state == "answered"
        if not on_call:
            return
        # 1) answers to the engineer's questions
        for q in cf.pending_questions:
            if q.get("status") == "answered" and q["id"] not in self._spoken_answers:
                self._spoken_answers.add(q["id"])
                await self._speak(a, f"On your question: {self._spoken(q['answer'])}")
        # 2) "found it" — never on top of the greeting
        answered_for = (self.now() - a.answered_at).total_seconds() if a.answered_at else 0
        if (cf.verdict == "confident" and not self._found_pushed and self.proposed_fix(cf)
                and cf.state in (S.IN_CALL, S.UNSURE_TOLD) and answered_for >= self.found_it_min_s):
            self._found_pushed = True

            def apply(c: CaseFile) -> None:
                if c.state is S.UNSURE_TOLD:
                    self._set(c, S.IN_CALL)
                self._set(c, S.CONFIRM_ACTIONS)
                x = self.active(c)
                if x:
                    x.verdict_told = "confident"
                if c.milestones.diagnosed_at is None:
                    c.milestones.diagnosed_at = self.now()
            await self.store.update(apply)
            await self._speak(a, self.found_it_line(self.store.get()))
        elif cf.state is S.IN_CALL and a.purpose in ("on_demand", "callback") and self.proposed_fix(cf) \
                and self._found_pushed and cf.verdict == "confident":
            # the call opened with the proposal already
            await self.store.update(lambda c: self._set(c, S.CONFIRM_ACTIONS))

    def _spoken(self, text: str) -> str:
        """Investigator prose → phone words: account names instead of ids, no UTC stamps."""
        import re as _re

        def name(m):
            acct = self.fleet.accounts.get(m.group(0))
            return acct.name if acct else "the account"
        text = _re.sub(r"\bacct_\w+\s*\(([^)]+)\)", r"\1", text or "")
        text = _re.sub(r"\bacct_\w+", name, text)
        text = _re.sub(r"\s*\b\d{1,2}:\d{2}(:\d{2})?([–-]\d{1,2}:\d{2}(:\d{2})?)?\s*UTC\b", "", text)
        return text.strip()

    async def _speak(self, attempt: CallAttempt, text: str) -> None:
        await self.store.update(lambda c: c.transcript.append(
            {"role": "agent", "text": text, "at": self.now().isoformat(), "via": "speak"}))
        try:
            await self.voice.speak(attempt, text, "APPEND")
        except Exception as e:  # noqa: BLE001 - narration must never break the flow
            await self._note_error(f"speak failed: {e!r}"[:200])

    async def add_turn(self, role: str, text: str) -> None:
        await self.store.update(lambda c: c.transcript.append(
            {"role": role, "text": text, "at": self.now().isoformat()}))

    # ── decision → fix → verification ──────────────────────────────────────
    async def approve_now(self) -> bool:
        """The engineer approved on the call. Works from IN_CALL / UNSURE_TOLD too (the yes can
        come before the "found it" break-in, or after an earlier decline). Returns False if
        there is nothing to approve."""
        cf = self.store.get()
        if not (cf.verdict == "confident" and self.proposed_fix(cf)):
            return False
        if cf.state in (S.IN_CALL, S.UNSURE_TOLD):
            self._found_pushed = True  # don't announce "found it" after they already said yes

            def apply(c: CaseFile) -> None:
                if c.state is S.UNSURE_TOLD:
                    self._set(c, S.IN_CALL)
                self._set(c, S.CONFIRM_ACTIONS)
                if c.milestones.diagnosed_at is None:
                    c.milestones.diagnosed_at = self.now()
            await self.store.update(apply)
        return self.store.get().state is S.CONFIRM_ACTIONS

    async def decline(self) -> None:
        if self.store.get().state is S.CONFIRM_ACTIONS:
            await self.store.update(lambda c: self._set(c, S.IN_CALL))

    async def on_confirm_yes(self) -> None:
        cf = self.store.get()
        if cf.state not in (S.CONFIRM_ACTIONS, S.TELEGRAM_SENT):
            return
        fix = self.proposed_fix(cf)
        if not fix:
            return
        await self.store.update(lambda c: self._set(c, S.EXECUTING))
        res = self.fleet.apply_fix(cf.account_id, fix["fix"], fix.get("params") or {})
        ok = bool(res.get("ok"))
        a = self.active(self.store.get())

        def apply(c: CaseFile) -> None:
            for f in c.fixes:
                if f is not None and f.get("status") == "proposed":
                    f["status"] = "applied" if ok else "rejected"
                    f["result"] = res
                    f["applied_at"] = self.now().strftime("%H:%M:%S")
            if ok:
                self._set(c, S.VERIFYING)
                c.milestones.fix_applied_at = self.now()
            else:
                self._set(c, S.IN_CALL if self.active(c) else S.TELEGRAM_SENT)
        await self.store.update(apply)
        if ok:
            sig = cf.alert.signal if cf.alert else "failure_rate"
            self._verify_started = self.clock.now()
            self._verify_before = self.signal_value(cf.account_id, sig, 20)
            if a:
                await self._speak(a, f"Done. {done_line(fix, res)} Watching the traffic now.")
            self._spawn(self._ticket_and_email())
        else:
            if a:
                await self._speak(a, f"That didn't go through: {res.get('message', 'unknown error')}.")
            if self.agent:
                self.agent.concluded = False
                self.agent.messages.append({"role": "user", "content":
                                            f"The fix {fix['fix']} was rejected: {res.get('message')}. Propose another."})

    async def _check_verification(self, cf: CaseFile) -> None:
        sig = cf.alert.signal if cf.alert else "failure_rate"
        v = self.signal_value(cf.account_id, sig, 15)
        elapsed = self.clock.now() - (self._verify_started or self.clock.now())
        fix = next((f for f in reversed(cf.fixes) if f.get("status") == "applied"), {})
        a = self.active(cf)
        recovered = self.is_recovered(sig, v) or (
            sig == "failure_rate" and v < 0.12 and fix.get("signature")
            and verify.signature_cleared(fix["signature"], self.fleet, cf.account_id, self.now()))
        if recovered and elapsed >= 5:
            before = self._verify_before or 0
            what = "Volume's back" if sig == "volume_drop" else "And it's recovered"
            line = (f"{what}: {'from' if sig == 'volume_drop' else 'failures went from'} {_pct(before)} to "
                    f"{_pct(v)} percent in {int(elapsed)} seconds. The ticket and the customer note are done. "
                    "Anything else?") if sig != "silent_rate" else (
                    f"And it's recovered: silent calls went from {_pct(before)} to {_pct(v)} percent in "
                    f"{int(elapsed)} seconds. The ticket and the customer note are done. Anything else?")

            def apply(c: CaseFile) -> None:
                for f in c.fixes:
                    if f.get("status") == "applied":
                        f["status"] = "verified"
                        f["verification"] = {"before": round(before, 3), "after": round(v, 3), "seconds": int(elapsed)}
                self._set(c, S.RESOLVED)
                c.milestones.resolved_at = self.now()
            await self.store.update(apply)
            if a and a.state == "answered":
                await self._speak(a, line)
            self._spawn(self._follow_up(before, v, int(elapsed)))
            await self._stop_agent()
        elif elapsed >= self.verify_timeout_s and fix.get("fix") != "notify_customer":
            def apply(c: CaseFile) -> None:
                for f in c.fixes:
                    if f.get("status") == "applied":
                        f["status"] = "no_effect"
                c.verdict = "leaning"
                self._set(c, S.IN_CALL if self.active(c) else S.TELEGRAM_SENT)
            await self.store.update(apply)
            self._found_pushed = False
            if a and a.state == "answered":
                await self._speak(a, f"The fix didn't take: still {_pct(v)} percent. I'm digging again.")
            if self.agent:
                self.agent.concluded = False
                self.agent.messages.append({"role": "user", "content":
                                            f"Fix {fix.get('fix')} was applied {int(elapsed)}s ago but the signal is still {v:.2f}. Re-investigate."})

    # ── follow-ups ─────────────────────────────────────────────────────────
    async def _notify_page(self, kind: str, title: str, body: str, ref: str | None = None) -> None:
        """Pop-up on the console page (voice → screen bus)."""
        try:
            from app import screen
            await screen.bus.publish({"type": "notify", "kind": kind, "title": title, "body": body, "ref": ref})
        except Exception:  # noqa: BLE001 - a pop-up must never break the incident
            pass

    def summary_text(self, cf: CaseFile | None = None) -> str:
        cf = cf or self.store.get()
        acct = self.account(cf.account_id)
        fix = (next((f for f in reversed(cf.fixes) if f.get("status") in ("verified", "applied")), None)
               or next((f for f in reversed(cf.fixes) if f.get("status") == "proposed"), None))
        t = self.ticket_ref(cf)
        risk = self.at_risk_per_hour(cf.account_id, cf.alert.signal_value or 0) if cf.alert else 0
        m = cf.milestones
        hhmm = lambda d: d.strftime("%H:%M:%S") if d else "–"  # noqa: E731
        lines = [f"Incident {cf.incident_id} · {acct.name if acct else cf.account_id}",
                 f"Impact: {self._signal_sentence(cf)} (about ₹{int(risk):,}/hour at risk)"]
        if fix:
            v = fix.get("verification")
            lines += [f"Cause: {fix.get('evidence')}", f"Owner: {fix.get('owner')}",
                      f"Fix: {FIX_PHRASES.get(fix.get('fix'), fix.get('fix'))} · {fix.get('status')}"
                      + (f" ({_pct(v['before'])}% → {_pct(v['after'])}% in {v['seconds']}s)" if v else "")]
        else:
            lead = next((h for h in cf.hypotheses if h.status == "leading"), None)
            lines.append(f"Cause: still investigating" + (f" (leading: {lead.title})" if lead else ""))
        if t is not None and t.external_id:
            lines.append(f"Ticket: {t.external_id}")
        lines.append(f"Timeline: alert {hhmm(m.alert_at)} · call {hhmm(m.call_placed_at)} · diagnosed {hhmm(m.diagnosed_at)}")
        return "\n".join(lines)

    async def send_summary(self) -> bool:
        """Engineer asked on the call: send the incident summary to Telegram (and pop it on the page)."""
        text = self.summary_text()
        sink = self.summary_notifier or self.notifier
        ok, err = True, None
        try:
            if hasattr(sink, "send"):
                await sink.send("📋 " + text, None)
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)[:200]
        await self.store.update(lambda c: c.actions.append(ActionRecord(
            kind="incident_summary", status="done" if ok else "failed", error=err,
            payload={"text": text, "via": "telegram"})))
        await self._notify_page("telegram", "Summary sent to Telegram" if ok else "Telegram send failed",
                                text if ok else (err or ""))
        return ok

    def ticket_ref(self, cf: CaseFile | None = None):
        cf = cf or self.store.get()
        return next((x for x in cf.actions if x.kind == "ticket" and x.status in ("running", "done")), None)

    async def file_ticket(self) -> str:
        """Engineer asked for a ticket on the call: file it now, independent of the fix decision."""
        t = self.ticket_ref()
        if t is not None:
            return "exists"
        self._spawn(self._ticket_and_email())
        return "filing"

    async def _ticket_and_email(self) -> None:
        cf = self.store.get()
        fix = (next((f for f in reversed(cf.fixes) if f.get("status") in ("applied", "verified")), None)
               or next((f for f in reversed(cf.fixes) if f.get("status") == "proposed"), None) or {})
        existing = self.ticket_ref(cf)
        if existing is not None:
            # Already filed earlier on the call: note the fix on the same ticket instead of a duplicate.
            if existing.status == "done" and existing.payload.get("ref") and fix.get("status") in ("applied", "verified"):
                try:
                    await self.ticket.append(existing.payload["ref"], f"Fix applied: {fix.get('fix')} {fix.get('params') or ''}")
                except Exception as e:  # noqa: BLE001
                    await self._note_error(f"ticket comment failed: {e!r}"[:200])
            email = fix.get("customer_message") or email_draft.render(cf)
            await self.store.update(lambda c: setattr(c, "email_draft", email))
            return
        acct = self.account()
        trail = [e for e in cf.investigator_log if e.get("kind") in ("tool", "hypothesis", "verify", "finding")][-12:]
        lead = next((h for h in cf.hypotheses if h.status in ("confirmed", "leading")), None)
        desc = (f"**Account:** {acct.name if acct else cf.account_id} (₹{int(acct.spend_per_day_inr) if acct else 0:,}/day)\n"
                f"**Signal:** {self._signal_sentence(cf)}\n**Cause:** {fix.get('evidence') or 'under investigation' + (f' (leading: {lead.title})' if lead else '')}\n"
                f"**Owner:** {fix.get('owner')}\n**Fix:** {fix.get('fix')} {fix.get('params') or ''} "
                f"(cost: {fix.get('cost')})\n\n**Investigation trail (agent):**\n"
                + "\n".join(f"- {e['at']} {e['kind']}: {e.get('tool') or e.get('id') or e.get('signature') or ''} "
                            f"{(e.get('evidence') or e.get('reason') or e.get('summary') or '')[:140]}" for e in trail))
        what = (fix.get("signature") or (lead.id if lead else "") or "").replace("_", " ")
        what = what or f"{self._signal_sentence(cf)} (under investigation)"
        payload = {"title": f"{acct.name if acct else cf.account_id}: {what}",
                   "description": desc, "priority": "Urgent"}
        await self.store.update(lambda c: c.actions.append(ActionRecord(kind="ticket", status="running", payload=payload)))
        ref, err, attempts = None, None, 0
        for i in range(3):
            attempts += 1
            try:
                ref = await self.ticket.create(payload)
                break
            except Exception as e:  # noqa: BLE001
                err = str(e)[:200]
                a = self.active(self.store.get())
                if a and a.state == "answered" and i == 0:
                    await self._speak(a, "Heads up, Linear just threw an error. Retrying.")
                await asyncio.sleep(1.0)
        email = fix.get("customer_message") or email_draft.render(cf)

        def apply(c: CaseFile) -> None:
            for x in c.actions:
                if x.kind == "ticket" and x.status == "running":
                    x.attempts = attempts
                    x.status = "done" if ref else "failed"
                    x.error = None if ref else err
                    if ref:
                        x.external_id, x.external_url = ref["id"], ref["url"]
                        x.payload = {**x.payload, "ref": ref}
            c.email_draft = email
            c.actions.append(ActionRecord(kind="email_draft", status="done", payload={"body": email}))
            if ref:
                c.milestones.ticket_at = self.now()
        await self.store.update(apply)
        if ref:
            await self._notify_page("ticket", f"Linear ticket {ref['id']} created",
                                    f"{payload['title']} · priority Urgent · investigation trail attached"
                                    + (f" · created on attempt {attempts} (first attempt failed, retried)" if attempts > 1 else ""),
                                    ref.get("url"))
        else:
            await self._notify_page("error", "Ticket creation failed", err or "")

    async def _follow_up(self, before: float, after: float, seconds: int) -> None:
        for _ in range(20):  # let the ticket land first
            if any(x.kind == "ticket" and x.status in ("done", "failed") for x in self.store.get().actions):
                break
            await asyncio.sleep(0.5)
        cf = self.store.get()
        fix = next((f for f in reversed(cf.fixes) if f.get("status") == "verified"), {})
        t = next((x for x in cf.actions if x.kind == "ticket" and x.status == "done"), None)
        summary = (f"Resolved {cf.incident_id}: {fix.get('evidence')}. Fix {fix.get('fix')} applied and verified "
                   f"({_pct(before)}% → {_pct(after)}% in {seconds}s).")
        if t and t.payload.get("ref"):
            try:
                await self.ticket.append(t.payload["ref"], summary)
            except Exception as e:  # noqa: BLE001
                await self._note_error(f"ticket comment failed: {e!r}"[:200])
        acct = self.account()
        if acct is not None and hasattr(acct, "past_incidents"):
            acct.past_incidents.append({"date": self.now().strftime("%d %b %H:%M"), "summary": fix.get("evidence"),
                                        "fix": f"{fix.get('fix')} — recovered {_pct(before)}%→{_pct(after)}% in {seconds}s"})
        await self._send("✅ " + summary + (f"\nTicket: {t.external_url}" if t else ""), None, kind="incident_summary")

    async def _send(self, text: str, buttons, kind: str = "telegram_notice") -> None:
        status, err = "done", None
        try:
            if hasattr(self.notifier, "send"):
                await self.notifier.send(text, buttons)
            elif hasattr(self.notifier, "mild_outage"):
                await self.notifier.mild_outage(self.store.get(), text)
        except Exception as e:  # noqa: BLE001
            status, err = "failed", str(e)[:200]
        await self.store.update(lambda c: c.actions.append(
            ActionRecord(kind=kind, status=status, error=err, payload={"text": text, "buttons": buttons or []})))

    async def hangup_active(self) -> None:
        cf = self.store.get()
        a = self.active(cf)
        if a is None:
            return
        try:
            await self.voice.hangup(a)
        except Exception as e:  # noqa: BLE001
            await self._note_error(f"hangup failed: {e!r}"[:200])
        if a.state == "answered":
            await self.on_call_ended()
        else:
            def apply(c: CaseFile) -> None:
                x = self.active(c)
                if x:
                    x.state, x.hangup_reason, x.ended_at = "ended", "hung up by operator", self.now()
                if c.state is S.CALLING:
                    self._set(c, S.TELEGRAM_SENT)
            await self.store.update(apply)

    async def shutdown(self) -> None:
        await self._stop_agent()
        for t in list(self.bg):
            t.cancel()
