"""Orchestrator — the state machine that ties everything together.

It reacts to two kinds of input:
  - time passing (tick): monitor -> detect -> place call #1 -> keep the case file's
    investigation fresh -> fire the callback once confident and no call is active
  - call events (answered / verdict told / ended / confirm): driven in real life by
    the voice proxy, and by the test here directly

Every mutation goes through the store (lock + snapshot + publish). Transitions are
validated against states.ALLOWED.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta

from app import email_draft, greetings, states
from app.clock import ScenarioClock
from app.detector import Detector
from app.interfaces import Notifier, TicketSink, VoiceCaller
from app.investigator import Investigator
from app.models import ActionRecord, Alert, CallAttempt, CaseFile, IncidentState
from app.simulator import CallSimulator
from app.store import IncidentStore

HOLD_CHECK_S = 5  # "is it failing right now?" window used just before placing the call


class Orchestrator:
    def __init__(self, store: IncidentStore, clock: ScenarioClock, sim: CallSimulator,
                 detector: Detector, investigator: Investigator, voice: VoiceCaller,
                 ticket: TicketSink, notifier: Notifier, trigger: datetime,
                 fail_first_ticket: bool = True, ticket_retries: int = 2,
                 freeze_clock_during_calls: bool = False, retry_wait_s: float = 15.0,
                 freeze_investigation_during_calls: bool = False,
                 single_call: bool = False, confirm_s: float = 25.0, summarizer=None):
        self.store = store
        self.clock = clock
        self.sim = sim
        self.detector = detector
        self.investigator = investigator
        self.voice = voice
        self.ticket = ticket
        self.notifier = notifier
        self.trigger = trigger
        self.fail_first_ticket = fail_first_ticket
        self.ticket_retries = ticket_retries
        # Live mode: hold scenario time still while the engineer is on the phone, so
        # call #1 stays honestly "unsure" and the evidence lands after hang-up (GATE_MODE=call_end).
        self.freeze_clock_during_calls = freeze_clock_during_calls
        self.retry_wait_s = retry_wait_s
        # Live-traffic mode: time keeps flowing (the logs keep streaming), but the verdict the
        # agent speaks from is held still while a call is up; it catches up after hang-up.
        self.freeze_investigation_during_calls = freeze_investigation_during_calls
        # Single-call flow (live traffic): suspect → confirm it's sustained (confirm_s) → ONE call
        # that debugs live and breaks in with "found it"; a blip that recovers first → Telegram.
        self.single_call = single_call
        self.confirm_s = confirm_s
        self.summarizer = summarizer  # async (cf, peak, duration_s, during_call) -> str
        self._suspected_at: float | None = None
        self._peak_rate = 0.0
        self._update_pushed = False
        self._recovery_pushed = False
        self._retry_at: float | None = None
        self._retry_greeting: str = ""
        self._retry_purpose: str = "initial"
        self._last_greeting: str = ""

    # ── helpers ──────────────────────────────────────────────────────────────
    def _now_dt(self) -> datetime:
        return self.trigger + timedelta(seconds=self.clock.now())

    @staticmethod
    def _set_state(c: CaseFile, new: IncidentState) -> None:
        states.check(c.state, new)
        c.state = new

    def _active_attempt(self, c: CaseFile) -> CallAttempt | None:
        for a in reversed(c.calls):
            if a.state in ("placing", "ringing", "answered"):
                return a
        return None

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        await self.store.update(lambda c: self._set_state(c, IncidentState.MONITORING))

    async def tick(self) -> None:
        now_dt = self._now_dt()
        calls = self.sim.calls_through(self.clock.now())
        state = self.store.get().state

        if state in (IncidentState.MONITORING, IncidentState.AUTO_RECOVERED):
            if (state is IncidentState.AUTO_RECOVERED and self.detector.fired
                    and self._window_rate(calls, now_dt) < self.detector.rate_threshold):
                self.detector.fired = False  # re-arm only once the 1-minute window is clean again
            alert = self.detector.check(calls, now_dt)
            if alert is not None:
                if self.single_call:
                    await self._on_suspected(alert, now_dt)
                else:
                    await self._on_alert(alert, now_dt)
            return

        if state is IncidentState.SUSPECTED:
            await self._refresh_investigation(calls, now_dt)
            self._peak_rate = max(self._peak_rate, self._window_rate(calls, now_dt))
            if self.store.get().verdict == "recovered":
                await self._auto_recover(during_call=False)
            elif self.clock.now() - (self._suspected_at or 0) >= self.confirm_s:
                # Only call if it's still failing *now*. If the last ~20 s are already back under the
                # alert threshold, hold the call: it's probably recovering, and a phone call for an
                # outage that just ended is noise. (Recovery then sends the Telegram note.)
                if self._window_rate(calls, now_dt, seconds=HOLD_CHECK_S) >= self.detector.rate_threshold:
                    await self._confirm_outage(calls, now_dt)
            return

        # Keep the investigation current while the incident is live.
        if state not in (IncidentState.IDLE, IncidentState.RESOLVED):
            frozen = (self.freeze_investigation_during_calls
                      and self._active_attempt(self.store.get()) is not None)
            if not frozen:
                await self._refresh_investigation(calls, now_dt)
            else:
                # The spoken verdict is held during a call, except recovery: never let the
                # agent claim a live cause after the calls have stopped failing.
                res = self.investigator.assess(calls, now_dt)
                if res["verdict"] == "recovered" or self.store.get().verdict == "recovered":
                    await self._refresh_investigation(calls, now_dt)

        cf = self.store.get()
        if self.single_call:
            if await self._live_call_updates(cf, calls, now_dt):
                return
            cf = self.store.get()
        if cf.state is IncidentState.RETRY and self._retry_at is not None and time.monotonic() >= self._retry_at:
            self._retry_at = None
            await self._place_call("retry", self._retry_greeting)
            return
        # Callback climax: confident, nothing on the phone, last verdict was "unsure".
        if cf.state is IncidentState.WAITING_FOR_EVIDENCE and cf.verdict == "confident":
            await self._place_call("callback", greetings.callback_greeting(cf))

    # ── internal steps ───────────────────────────────────────────────────────
    async def _on_alert(self, alert: Alert, now_dt: datetime) -> None:
        def apply(c: CaseFile) -> None:
            c.incident_id = alert.incident_id
            c.alert = alert
            c.milestones.alert_at = now_dt
            self._set_state(c, IncidentState.ALERTED)
        await self.store.update(apply)
        await self._refresh_investigation(self.sim.calls_through(self.clock.now()), now_dt)
        cf = self.store.get()
        await self._place_call("initial", greetings.initial_greeting(cf))

    # ── single-call flow ─────────────────────────────────────────────────────
    def _window_rate(self, calls, now_dt: datetime, seconds: float | None = None) -> float:
        from app.detector import is_failure
        lo = now_dt - timedelta(seconds=seconds or self.detector.window_s)
        win = [c for c in calls if lo < c.initiation_time <= now_dt]
        return sum(map(is_failure, win)) / len(win) if win else 0.0

    async def _on_suspected(self, alert: Alert, now_dt: datetime) -> None:
        self._suspected_at = self.clock.now()
        self._peak_rate = alert.failure_rate
        self._update_pushed = self._recovery_pushed = False

        def apply(c: CaseFile) -> None:
            c.incident_id = alert.incident_id
            c.alert = alert
            c.milestones = type(c.milestones)(manual_baseline_seconds=c.milestones.manual_baseline_seconds)
            c.milestones.alert_at = now_dt
            self._set_state(c, IncidentState.SUSPECTED)
        await self.store.update(apply)

    async def _confirm_outage(self, calls, now_dt: datetime) -> None:
        rate = round(self._window_rate(calls, now_dt), 4)

        def apply(c: CaseFile) -> None:
            if c.alert:
                c.alert.failure_rate = rate  # speak the current rate, not the one at first crossing
            self._set_state(c, IncidentState.ALERTED)
        await self.store.update(apply)
        await self._place_call("initial", greetings.debugging_greeting(self.store.get()))

    async def _auto_recover(self, during_call: bool) -> None:
        cf = self.store.get()
        duration = int(self.clock.now() - (self._suspected_at or self.clock.now()))
        peak = max(self._peak_rate, cf.alert.failure_rate if cf.alert else 0.0)

        def apply(c: CaseFile) -> None:
            self._set_state(c, IncidentState.AUTO_RECOVERED)
        await self.store.update(apply)
        # Stay latched until the 1-minute window drops under the threshold (see tick), so the
        # lagging window can't instantly re-fire and spam Telegram.

        from app import summaries
        try:
            text = await (self.summarizer or _template_async)(self.store.get(), peak, duration, during_call)
        except Exception:  # noqa: BLE001
            text = summaries.template_summary(self.store.get(), peak, duration, during_call)
        status, err = "done", None
        try:
            if hasattr(self.notifier, "mild_outage"):
                await self.notifier.mild_outage(self.store.get(), text)
        except Exception as e:  # noqa: BLE001
            status, err = "failed", str(e)[:200]
        await self.store.update(lambda c: c.actions.append(
            ActionRecord(kind="telegram_notice", status=status, error=err, payload={"text": text})))

    async def _live_call_updates(self, cf: CaseFile, calls, now_dt: datetime) -> bool:
        """While the engineer is on the line, break in with news from the background investigation."""
        active = self._active_attempt(cf)
        on_call = active is not None and active.state == "answered"
        if (on_call and cf.verdict == "confident" and not self._update_pushed
                and cf.state in (IncidentState.IN_CALL, IncidentState.UNSURE_TOLD)):
            self._update_pushed = True

            def apply(c: CaseFile) -> None:
                if c.state is IncidentState.UNSURE_TOLD:
                    self._set_state(c, IncidentState.IN_CALL)
                a = self._active_attempt(c)
                if a:
                    a.verdict_told = "confident"
            await self.store.update(apply)
            await self.propose_actions()
            await self.voice.speak(active, greetings.found_it_update(self.store.get()), "APPEND")
            return True
        if on_call and cf.verdict == "recovered" and not self._recovery_pushed and cf.state in (
                IncidentState.IN_CALL, IncidentState.UNSURE_TOLD, IncidentState.CONFIRM_ACTIONS):
            self._recovery_pushed = True
            await self.voice.speak(active, greetings.recovered_update(cf), "APPEND")
            return True
        if cf.state is IncidentState.WAITING_FOR_EVIDENCE and cf.verdict == "recovered":
            await self._auto_recover(during_call=True)
            return True
        return False

    async def _refresh_investigation(self, calls, now_dt: datetime) -> None:
        res = self.investigator.assess(calls, now_dt)

        def apply(c: CaseFile) -> None:
            c.hypotheses = res["hypotheses"]
            c.leading = res["leading"]
            c.verdict = res["verdict"]
            c.top_confidence = res["top_confidence"]
            c.recent_fail_rate = res.get("recent_fail_rate")
            for ev in res["evidence"]:
                idx = next((i for i, e in enumerate(c.evidence) if e.id == ev.id), None)
                if idx is None:
                    c.evidence.append(ev)
                else:
                    c.evidence[idx] = ev
            if res["verdict"] == "confident" and c.milestones.diagnosed_at is None:
                c.milestones.diagnosed_at = now_dt
        await self.store.update(apply)

    async def _place_call(self, purpose: str, greeting: str) -> CallAttempt:
        now_dt = self._now_dt()
        attempt = CallAttempt(attempt_id=f"att-{uuid.uuid4().hex[:8]}", purpose=purpose,
                              route=getattr(self.voice, "route", "mock"), placed_at=now_dt, state="placing")
        self._last_greeting = greeting
        agent_id = await self.voice.place_call(attempt, greeting)
        attempt.agora_agent_id = agent_id
        attempt.state = "ringing"

        def apply(c: CaseFile) -> None:
            c.calls.append(attempt)
            if c.milestones.call_placed_at is None:
                c.milestones.call_placed_at = now_dt
            self._set_state(c, IncidentState.CALLING)
        await self.store.update(apply)
        return attempt

    # ── call events (driven by the voice proxy in real life) ─────────────────
    async def on_answered(self) -> None:
        now_dt = self._now_dt()

        def apply(c: CaseFile) -> None:
            a = self._active_attempt(c)
            if a:
                a.state = "answered"
                a.answered_at = now_dt
            if c.milestones.acknowledged_at is None:
                c.milestones.acknowledged_at = now_dt
            self._set_state(c, IncidentState.IN_CALL)
        await self.store.update(apply)
        if self.freeze_clock_during_calls:
            self.clock.pause()

    async def on_verdict_told(self) -> None:
        def apply(c: CaseFile) -> None:
            a = self._active_attempt(c)
            verdict = "confident" if c.verdict == "confident" else "unsure"
            if a:
                a.verdict_told = verdict
            if verdict == "unsure":
                self._set_state(c, IncidentState.UNSURE_TOLD)
        await self.store.update(apply)

    async def propose_actions(self) -> None:
        """Agent has stated a confident cause and asks to raise the ticket + draft email."""
        def apply(c: CaseFile) -> None:
            self._set_state(c, IncidentState.CONFIRM_ACTIONS)
            if not any(x.kind == "ticket" for x in c.actions):
                c.actions.append(ActionRecord(kind="ticket", status="proposed",
                                              payload=email_draft.ticket_payload(c)))
        await self.store.update(apply)

    async def on_call_ended(self) -> None:
        def apply(c: CaseFile) -> None:
            a = self._active_attempt(c)
            if a:
                a.state = "ended"
                a.ended_at = self._now_dt()
            if c.state is IncidentState.UNSURE_TOLD:
                self._set_state(c, IncidentState.WAITING_FOR_EVIDENCE)
            elif c.state is IncidentState.IN_CALL and c.verdict != "confident":
                # Hung up before asking for the cause: still a "wait for evidence, call back" case.
                self._set_state(c, IncidentState.WAITING_FOR_EVIDENCE)
        await self.store.update(apply)
        if self.freeze_clock_during_calls:
            self.clock.resume()
        cf = self.store.get()
        if self.single_call and cf.verdict == "recovered" and cf.state in (
                IncidentState.IN_CALL, IncidentState.UNSURE_TOLD, IncidentState.CONFIRM_ACTIONS,
                IncidentState.WAITING_FOR_EVIDENCE):
            await self._auto_recover(during_call=True)

    async def on_no_answer(self, reason: str = "no_answer") -> None:
        """The phone rang out / was declined. Retry once, then fall back to Telegram."""
        cf = self.store.get()
        active = self._active_attempt(cf)
        first_miss = active is not None and active.purpose != "retry"
        if first_miss:
            self._retry_purpose = active.purpose
            self._retry_greeting = self._last_greeting

        def apply(c: CaseFile) -> None:
            a = self._active_attempt(c)
            if a:
                a.state = "failed"
                a.hangup_reason = reason
                a.ended_at = self._now_dt()
            self._set_state(c, IncidentState.RETRY if first_miss else IncidentState.TELEGRAM_SENT)
        await self.store.update(apply)
        if first_miss:
            self._retry_at = time.monotonic() + self.retry_wait_s
        else:
            await self.notifier.missed_call(self.store.get())

    async def on_demand_call(self) -> None:
        """Engineer tapped "Call me now" in Telegram."""
        cf = self.store.get()
        if cf.state is not IncidentState.TELEGRAM_SENT:
            return
        greeting = (greetings.callback_greeting(cf) if cf.verdict == "confident"
                    else greetings.telegram_greeting(cf))
        await self._place_call("on_demand", greeting)

    async def decline_actions(self) -> None:
        """Engineer said "not yet" to the ticket + email."""
        await self.store.update(lambda c: self._set_state(c, IncidentState.IN_CALL))

    async def add_evidence(self, ev) -> None:
        await self.store.update(lambda c: c.evidence.append(ev))

    async def add_turn(self, role: str, text: str) -> None:
        now = self._now_dt()
        await self.store.update(lambda c: c.transcript.append({"role": role, "text": text,
                                                               "at": now.isoformat()}))

    async def on_confirm_yes(self) -> None:
        if self.store.get().state is not IncidentState.CONFIRM_ACTIONS:
            return
        await self.store.update(lambda c: self._set_state(c, IncidentState.EXECUTING))
        await self._execute_actions()

    # ── actions ──────────────────────────────────────────────────────────────
    async def _execute_actions(self) -> None:
        cf = self.store.get()
        payload = email_draft.ticket_payload(cf)
        ref, error, attempts = None, None, 0
        for _ in range(self.ticket_retries + 1):
            attempts += 1
            try:
                ref = await self.ticket.create(payload)
                error = None
                break
            except Exception as e:  # noqa: BLE001 - deliberate first failure, then retry
                error = str(e)
                active = self._active_attempt(self.store.get())
                if active:
                    await self.voice.speak(active, "The ticket system just returned an error. Retrying.")

        email = email_draft.render(cf)
        now_dt = self._now_dt()

        def apply(c: CaseFile) -> None:
            for x in c.actions:
                if x.kind == "ticket":
                    x.attempts = attempts
                    if ref:
                        x.status = "done"
                        x.external_id = ref["id"]
                        x.external_url = ref["url"]
                    else:
                        x.status = "failed"
                        x.error = error
            c.email_draft = email
            c.actions.append(ActionRecord(kind="email_draft", status="done",
                                          payload={"body": email}))
            if ref:
                c.milestones.ticket_at = now_dt
                self._set_state(c, IncidentState.RESOLVED)
        await self.store.update(apply)

        if ref:
            active = self._active_attempt(self.store.get())
            if active:
                await self.voice.speak(active, "Done. The ticket is up in Linear, and the customer email draft is on your screen.")
            # Follow-up: append the incident summary as a ticket comment.
            await self.ticket.append(ref, f"Incident {cf.incident_id} summary: carrier X rejection, "
                                          f"{int(round(cf.top_confidence * 100))}% confidence, email draft sent.")


async def _template_async(cf, peak, duration_s, during_call):
    from app import summaries
    return summaries.template_summary(cf, peak, duration_s, during_call)
