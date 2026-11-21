"""Incident dashboard app — press Trigger, watch the whole incident play out.

Wires the brain (clock, simulator, detector, investigator, orchestrator) to MOCK
adapters so the full arc runs with no phone and no network, and streams the live
case file to a one-page dashboard over SSE.

Runs as its own FastAPI app (own port) so the working voice server is untouched:

    .venv/bin/uvicorn app.incident_app:app --port 8001

Then open http://localhost:8001 and click Trigger.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import agora, brain_proxy, config, summaries
from app.adapters import MockNotifier, MockTicketSink, MockVoiceCaller
from app.clock import ScenarioClock
from app.detector import Detector
from app.investigator import Investigator
from app.models import IncidentState
from app.orchestrator import Orchestrator
from app.simulator import CallSimulator
from app.store import IncidentStore
from app.traffic import LiveTraffic

WEB = Path(__file__).resolve().parent.parent / "web"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"

# Auto-play delays, in scenario seconds.
ANSWER_AFTER = 4
VERDICT_AFTER = 7
HANGUP_AFTER = 12
PROPOSE_AFTER = 5
CONFIRM_AFTER = 9

# Live mode (INCIDENT_LIVE=1): real adapters per .env, events come from the phone, not a script.
LIVE = os.getenv("INCIDENT_LIVE", "0") == "1"
LIVE_CLOCK_SPEED = float(os.getenv("LIVE_CLOCK_SPEED", "6"))
RING_TIMEOUT_S = float(os.getenv("RING_TIMEOUT_S", "60"))  # no answer within this → missed
POLL_S = 2.0
CONFIRM_S = float(os.getenv("CONFIRM_S", "25"))  # failures must persist this long before we call
log = logging.getLogger("live")


def _mode(key: str) -> str:
    if config.env("DEMO_MOCK_FALLBACK") == "1" and key != "VOICE_MODE":
        return "mock"
    return config.env(key, "mock").split("#")[0].strip()


def _build_adapters(live: bool):
    from app import live_adapters as L
    if not live:
        return MockVoiceCaller(), MockTicketSink(fail_first=True), MockNotifier()
    voice = L.AgoraPhoneCaller() if _mode("VOICE_MODE") == "agora_phone" else MockVoiceCaller()
    fail_first = config.env("FAIL_FIRST_TICKET", "true").split("#")[0].strip().lower() == "true"
    ticket = (L.FaultInjectingSink(L.LinearTicketSink(), fail_first) if _mode("TICKET_MODE") == "linear"
              else MockTicketSink(fail_first=fail_first))
    notifier = L.TelegramNotifier() if _mode("NOTIFY_MODE") == "telegram" else MockNotifier()
    return voice, ticket, notifier


class Demo:
    def __init__(self, speed: float = 10.0, live: bool = False):
        self.live = live
        self.speed = LIVE_CLOCK_SPEED if live else speed
        self.play_task: asyncio.Task | None = None
        self.reset()

    def reset(self, traffic: bool = False) -> None:
        """traffic=False: the scripted Northwind timeline (tests, Trigger button).
        traffic=True: real-time operator-driven CDRs (Start / All pass / All fail)."""
        self.trigger = datetime.now()
        self.store = IncidentStore(STATE_DIR / "incident.json")
        self.voice, self.ticket, self.notifier = _build_adapters(self.live)
        if traffic:
            self.clock = ScenarioClock(speed=1.0)
            self.traffic: LiveTraffic | None = LiveTraffic(self.trigger)
            self.orch = Orchestrator(self.store, self.clock, self.traffic,
                                     Detector(window_s=60, min_calls=100),
                                     Investigator(window_s=60, recovery_rate=0.06),
                                     self.voice, self.ticket, self.notifier, trigger=self.trigger,
                                     single_call=True, confirm_s=CONFIRM_S,
                                     summarizer=summaries.groq_summary if self.live else None)
        else:
            self.trigger = self.trigger.replace(microsecond=0)
            self.clock = ScenarioClock(speed=self.speed)
            self.traffic = None
            self.orch = Orchestrator(self.store, self.clock, CallSimulator(self.trigger),
                                     Detector(), Investigator(), self.voice, self.ticket,
                                     self.notifier, trigger=self.trigger,
                                     freeze_clock_during_calls=self.live)
        self._marks: dict[str, float] = {}
        self._placed_mono: dict[str, float] = {}
        self._last_poll = 0.0
        self.last_error: str | None = None

    # Auto-drive the call events that a human/phone would normally trigger.
    async def _auto_events(self) -> None:
        cf = self.store.get()
        now_s = self.clock.now()
        active = next((a for a in reversed(cf.calls) if a.state in ("ringing", "answered")), None)

        if cf.state is IncidentState.CALLING and active:
            placed = (active.placed_at - self.trigger).total_seconds()
            if now_s - placed >= ANSWER_AFTER:
                await self.orch.on_answered()
        elif cf.state is IncidentState.IN_CALL and active and self.orch.single_call:
            pass  # the orchestrator breaks in with "found it" itself once confident
        elif cf.state is IncidentState.IN_CALL and active:
            answered = (active.answered_at - self.trigger).total_seconds() if active.answered_at else now_s
            if active.purpose == "initial":
                if active.verdict_told == "none" and now_s - answered >= VERDICT_AFTER:
                    await self.orch.on_verdict_told()
            else:  # callback
                if not any(x.kind == "ticket" for x in cf.actions) and now_s - answered >= PROPOSE_AFTER:
                    await self.orch.propose_actions()
        elif cf.state is IncidentState.UNSURE_TOLD and active:
            answered = (active.answered_at - self.trigger).total_seconds() if active.answered_at else now_s
            if now_s - answered >= HANGUP_AFTER:
                await self.orch.on_call_ended()
        elif cf.state is IncidentState.CONFIRM_ACTIONS:
            self._marks.setdefault("confirm_at", now_s)
            if now_s - self._marks["confirm_at"] >= CONFIRM_AFTER:
                await self.orch.on_confirm_yes()

    # ── live: watch the real call (answered / ended / rang out) ──────────────
    async def watch_call(self) -> None:
        cf = self.store.get()
        a = next((x for x in reversed(cf.calls) if x.state in ("ringing", "answered")), None)
        if a is None or not a.agora_agent_id or a.route == "mock":
            return
        now = time.monotonic()
        placed = self._placed_mono.setdefault(a.attempt_id, now)
        if now - self._last_poll < POLL_S:
            return
        self._last_poll = now
        st = await agora.agent_status(a.agora_agent_id)
        status = str(st.get("status") or "").upper()
        gone = st.get("status_code") == 404 or status in ("STOPPED", "FAILED", "STOPPING")
        if a.state == "ringing" and not gone:
            hist = await agora.agent_history(a.agora_agent_id)
            contents = (hist.get("contents") or hist.get("data", {}).get("contents") or [])
            if any(c.get("role") == "user" for c in contents if isinstance(c, dict)):
                await self.orch.on_answered()
                return
        if gone:
            log.warning("call %s agent %s gone (%s %s)", a.attempt_id, a.agora_agent_id,
                        st.get("status_code"), status)
            if a.state == "answered":
                await self.orch.on_call_ended()
            else:
                await self.orch.on_no_answer(reason=f"agent {status or st.get('status_code')}")
        elif a.state == "ringing" and now - placed > RING_TIMEOUT_S:
            await self.voice.hangup(a)
            await self.orch.on_no_answer(reason="ring_timeout")

    async def hangup_active(self) -> None:
        cf = self.store.get()
        a = next((x for x in reversed(cf.calls) if x.state in ("ringing", "answered")), None)
        if a is None:
            return
        await self.voice.hangup(a)
        if a.state == "answered":
            await self.orch.on_call_ended()

    async def live_loop(self) -> None:
        while True:
            try:
                await self.orch.tick()
                await self.watch_call()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep the incident alive, show the error
                self.last_error = repr(e)[:300]
                log.warning("live loop error: %s", self.last_error)
                await asyncio.sleep(2)
            if self.store.get().state is IncidentState.RESOLVED and not any(
                    x.state in ("ringing", "answered") for x in self.store.get().calls):
                break
            await asyncio.sleep(0.5)

    async def loop(self) -> None:
        try:
            while True:
                if self.clock.running:
                    await self.orch.tick()
                    await self._auto_events()
                    if self.store.get().state is IncidentState.RESOLVED:
                        break
                await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            pass


demo = Demo(live=LIVE)
app = FastAPI()
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
app.include_router(brain_proxy.router)
brain_proxy.bind(lambda: demo.orch, lambda: demo.hangup_active())
_tg_task: asyncio.Task | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _tg_task
    from app.live_adapters import TelegramNotifier
    if LIVE and isinstance(demo.notifier, TelegramNotifier):
        async def call_me() -> None:
            await demo.orch.on_demand_call()
        _tg_task = asyncio.create_task(demo.notifier.poll_buttons(call_me))


@app.get("/api/live")
async def live_info() -> dict:
    return {"live": demo.live, "voice": type(demo.voice).__name__, "ticket": type(demo.ticket).__name__,
            "notifier": type(demo.notifier).__name__, "speed": demo.speed, "last_error": demo.last_error,
            "turns": list(brain_proxy.turns)[-10:]}


# ── live traffic controls (the logs dashboard) ───────────────────────────────
@app.post("/sim/start")
async def sim_start() -> dict:
    cf = demo.store.get()
    fresh = demo.traffic is None or cf.state in (IncidentState.RESOLVED, IncidentState.IDLE)
    if fresh:
        await _stop_play()
        demo.reset(traffic=True)
        demo.clock.start()
        demo.traffic.start(demo.trigger)
        await demo.orch.start()
        demo.play_task = asyncio.create_task(demo.live_loop() if demo.live else demo.loop())
    else:
        demo.traffic.start()
    return demo.traffic.stats()


@app.post("/sim/stop")
async def sim_stop() -> dict:
    if demo.traffic:
        demo.traffic.stop()
    return demo.traffic.stats() if demo.traffic else {"running": False}


@app.post("/sim/mode")
async def sim_mode(m: str) -> dict:
    if m not in ("pass", "fail"):
        return {"error": "mode must be pass or fail"}
    if demo.traffic is None or not demo.traffic.running:
        await sim_start()
    demo.traffic.set_mode(m)
    return demo.traffic.stats()


@app.get("/cdr")
async def cdr(limit: int = 60) -> dict:
    if demo.traffic is None:
        return {"rows": [], "stats": {"running": False, "mode": "pass", "calls": 0, "failed": 0,
                                      "fail_rate": 0.0, "per_carrier": {}, "causes": {}, "spark": [],
                                      "window_s": 60},
                "threshold": 0.20}
    rows = [{"call_uuid": c.call_uuid, "initiation_time": c.initiation_time.strftime("%H:%M:%S"),
             "from": c.from_number, "to": c.to_number, "carrier": c.carrier_route,
             "duration": c.call_duration, "sip": c.sip_response_code,
             "code": c.hangup_cause_code, "cause": c.hangup_cause_name, "source": c.hangup_source}
            for c in demo.traffic.recent(limit)]
    return {"rows": rows, "stats": demo.traffic.stats(), "threshold": demo.orch.detector.rate_threshold}


@app.post("/demo/hangup")
async def hangup() -> dict:
    await demo.hangup_active()
    return demo.store.get().model_dump(mode="json")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(WEB / "dashboard.html"))


@app.get("/state")
async def state() -> dict:
    return demo.store.get().model_dump(mode="json")


@app.get("/state/stream")
async def state_stream(request: Request) -> StreamingResponse:
    async def gen():
        async for cf in demo.store.subscribe():
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(cf.model_dump(mode='json'))}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/demo/trigger")
async def trigger(auto: int = 1) -> dict:
    await _stop_play()
    demo.reset()
    demo.clock.start()
    await demo.orch.start()
    if demo.live:
        demo.play_task = asyncio.create_task(demo.live_loop())
    elif auto:
        demo.play_task = asyncio.create_task(demo.loop())
    return demo.store.get().model_dump(mode="json")


@app.post("/demo/tick")
async def tick(auto: int = 1) -> dict:
    await demo.orch.tick()
    if auto:
        await demo._auto_events()
    return demo.store.get().model_dump(mode="json")


@app.post("/demo/advance")
async def advance(seconds: float = 30.0, auto: int = 1) -> dict:
    demo.clock.advance(seconds)
    await demo.orch.tick()
    if auto:
        await demo._auto_events()
    return demo.store.get().model_dump(mode="json")


@app.post("/demo/pause")
async def pause() -> dict:
    demo.clock.pause()
    return {"running": demo.clock.running}


@app.post("/demo/resume")
async def resume() -> dict:
    demo.clock.resume()
    return {"running": demo.clock.running}


@app.post("/demo/reset")
async def reset() -> dict:
    await _stop_play()
    demo.reset()
    return demo.store.get().model_dump(mode="json")


async def _stop_play() -> None:
    if demo.play_task and not demo.play_task.done():
        demo.play_task.cancel()
        try:
            await demo.play_task
        except asyncio.CancelledError:
            pass
    demo.play_task = None
