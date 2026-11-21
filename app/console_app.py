"""v2 console app: the fleet of accounts + OnCall orchestrator + LLM investigator + voice proxy.

    .venv/bin/uvicorn app.console_app:app --port 8002                 # mock phone, real Groq investigator
    INCIDENT_LIVE=1 .venv/bin/uvicorn app.console_app:app --port 8000 # real phone/Linear/Telegram per .env

Open /  → the console (web/console.html). The old single-account app (app.incident_app) is untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import hmac

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import agora, brain_proxy, config, summaries
from app.clock import ScenarioClock
from app.incident_app import _build_adapters
from app.models import IncidentState as S
from app.oncall import OnCall
from app.store import IncidentStore

WEB = Path(__file__).resolve().parent.parent / "web"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
LIVE = os.getenv("INCIDENT_LIVE", "0") == "1"
CONFIRM_S = float(os.getenv("CONFIRM_S", "5"))
CALL_MIN_SPEND = float(os.getenv("CALL_MIN_SPEND_INR", "20000"))
RING_TIMEOUT_S = float(os.getenv("RING_TIMEOUT_S", "120"))
STATUS_404_GRACE_S = 10.0
MOCK_ANSWER_AFTER_S = 3.0
MOCK_APPROVE_AFTER_S = float(os.getenv("MOCK_APPROVE_AFTER_S", "8"))  # mock mode: auto-"yes" (0 = never)
log = logging.getLogger("console")


class Console:
    def __init__(self, live: bool = LIVE, chat=None):
        self.live = live
        self.chat = chat  # override the investigator's LLM (tests)
        self.task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.reset()

    def reset(self) -> None:
        from app.fleet import FleetTraffic
        from app.fleet_detect import AccountWatch
        from app.investigator_agent import InvestigatorAgent
        self.trigger = datetime.now()
        self.clock = ScenarioClock(speed=1.0)
        self.store = IncidentStore(STATE_DIR / "console.json")
        self.fleet = FleetTraffic(self.trigger)
        self.watch = AccountWatch()
        self.voice, self.ticket, self.notifier = _build_adapters(self.live)
        self.oncall = OnCall(
            self.store, self.clock, self.fleet, self.watch, self.voice, self.ticket, self.notifier,
            self.trigger, confirm_s=CONFIRM_S, call_min_spend_inr=CALL_MIN_SPEND,
            engineer=config.ENGINEER_NAME, summarizer=summaries.groq_summary if self.live else None,
            agent_factory=lambda aid: InvestigatorAgent(self.store, self.fleet, self.oncall.now, aid,
                                                        **({"chat": self.chat} if self.chat else {})))
        self._placed: dict[str, float] = {}
        self._marks: dict[str, float] = {}
        self._last_poll = 0.0

    # ── real phone: answered / ended / rang out ────────────────────────────
    async def watch_call(self) -> None:
        cf = self.store.get()
        a = self.oncall.active(cf)
        if a is None or not a.agora_agent_id or a.route == "mock":
            return
        now = time.monotonic()
        placed = self._placed.setdefault(a.attempt_id, now)
        if now - self._last_poll < 2.0:
            return
        self._last_poll = now
        st = await agora.agent_status(a.agora_agent_id)
        status = str(st.get("status") or "").upper()
        code = st.get("status_code")
        gone = status in ("STOPPED", "FAILED") or (code == 404 and now - placed > STATUS_404_GRACE_S)
        if a.state == "ringing" and not gone:
            hist = await agora.agent_history(a.agora_agent_id)
            contents = hist.get("contents") or (hist.get("data") or {}).get("contents") or []
            if any(isinstance(c, dict) and c.get("role") == "user" for c in contents):
                await self.oncall.on_answered()
                return
        if gone:
            log.warning("call %s agent gone (%s %s)", a.attempt_id, code, status)
            if a.state == "answered":
                await self.oncall.on_call_ended()
            else:
                await self.oncall.on_no_answer(reason=f"agent {status or code}")
        elif a.state == "ringing" and now - placed > RING_TIMEOUT_S:
            await self.voice.hangup(a)
            await self.oncall.on_no_answer(reason="ring_timeout")

    # ── mock phone: auto-answer, optional auto-approve (dashboard-only demo) ──
    async def mock_events(self) -> None:
        cf = self.store.get()
        a = self.oncall.active(cf)
        now = time.monotonic()
        if cf.state is S.CALLING and a:
            if now - self._marks.setdefault(f"ring-{a.attempt_id}", now) >= MOCK_ANSWER_AFTER_S:
                await self.oncall.on_answered()
        if cf.state is S.CONFIRM_ACTIONS and MOCK_APPROVE_AFTER_S > 0:
            if now - self._marks.setdefault(f"confirm-{cf.incident_id}-{len(cf.fixes)}", now) >= MOCK_APPROVE_AFTER_S:
                await self.oncall.on_confirm_yes()

    async def loop(self) -> None:
        while True:
            try:
                await self.oncall.tick()
                await (self.watch_call() if self.live else self.mock_events())
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep running; show the error
                self.last_error = repr(e)[:300]
                log.warning("loop error: %s", self.last_error)
                await self.oncall._note_error(f"loop: {self.last_error}")
                await asyncio.sleep(1.5)
            await asyncio.sleep(0.5)


console = Console()
app = FastAPI()
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
app.include_router(brain_proxy.router)
brain_proxy.bind(lambda: console.oncall, lambda: console.oncall.hangup_active())

from app.public_demo import PublicDemo  # noqa: E402

public = PublicDemo(lambda: console, STATE_DIR / "public.db", live_allowed=LIVE)

# ── operator auth ──────────────────────────────────────────────────────────
# Anything that can place a call or change the incident is operator-only once ADMIN_TOKEN is
# set (always on the VM). Open the console once with /console?token=… to get the cookie.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
ADMIN_POSTS = ("/fleet/", "/incident/", "/demo/", "/public/stop")
ADMIN_GETS = ("/console", "/api/live")


def _is_admin(request: Request) -> bool:
    if not ADMIN_TOKEN:
        return not LIVE  # local dev/tests: open; a live server without a token: locked
    got = request.cookies.get("admin") or request.headers.get("x-admin-token") or request.query_params.get("token") or ""
    return hmac.compare_digest(got.encode(), ADMIN_TOKEN.encode())


@app.middleware("http")
async def _admin_guard(request: Request, call_next):
    path = request.url.path
    needs = ((request.method == "POST" and path.startswith(ADMIN_POSTS))
             or (request.method == "GET" and path.startswith(ADMIN_GETS)))
    if needs and not _is_admin(request):
        return JSONResponse({"error": "operator only"}, status_code=401)
    return await call_next(request)
_tg_task: asyncio.Task | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _tg_task
    if console.live and hasattr(console.notifier, "poll_actions"):
        _tg_task = asyncio.create_task(console.notifier.poll_actions({
            "call_me_now": lambda: console.oncall.on_demand_call(),
            "approve_fix": lambda: console.oncall.on_confirm_yes(),
        }))


async def _stop_loop() -> None:
    if console.task and not console.task.done():
        console.task.cancel()
        try:
            await console.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    console.task = None


@app.get("/")
async def index():
    """The console. CSS/JS URLs carry the file mtime so a deploy is never hidden by a browser cache."""
    from fastapi.responses import HTMLResponse
    html = (WEB / "oncall.html").read_text()
    for name in ("oncall.css", "oncall.js"):
        v = int((WEB / name).stat().st_mtime)
        html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/console")
async def console_page(request: Request):
    tok = request.query_params.get("token")
    if tok and ADMIN_TOKEN:  # first visit with ?token=… → remember it in a cookie, drop it from the URL
        r = RedirectResponse("/console", status_code=303)
        r.set_cookie("admin", tok, httponly=True, secure=True, samesite="strict", max_age=7 * 86400)
        return r
    return FileResponse(str(WEB / "console.html"))


# ── public demo ────────────────────────────────────────────────────────────
@app.post("/public/start")
async def public_start(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    # Operators (simulations, rehearsals) may pick the hidden fault: {"fault": "acct_zylo:cps_burst"}.
    force = str(body.get("fault") or "") if (body.get("fault") and _is_admin(request)) else None
    code, payload = await public.start(str(body.get("mode", "call")), str(body.get("phone", "")),
                                       str(body.get("code", "")), str(body.get("name", "")), ip, force=force)
    return JSONResponse(payload, status_code=code)


@app.get("/talk")
async def talk_page():
    """The browser-call window. Opened by a click so the browser allows it."""
    from fastapi.responses import HTMLResponse
    html = (WEB / "talk.html").read_text()
    v = int((WEB / "talk.js").stat().st_mtime)
    html = html.replace("/static/talk.js", f"/static/talk.js?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.post("/public/voice/hangup")
async def public_voice_hangup():
    """The talk window closed the mic. Only ends a live browser session."""
    s = public.session
    if not s or s.get("ended_at") or s.get("mode") != "browser":
        return JSONResponse({"ok": False}, status_code=409)
    await public._console().oncall.hangup_active()
    return {"ok": True}


@app.get("/public/status")
async def public_status() -> dict:
    return _jsonable(public.status())


@app.get("/public/ui/stream")
async def public_ui_stream(request: Request, after: int = 0) -> StreamingResponse:
    """Voice → screen events (open a call, filter, chart, scroll, close) as SSE."""
    from app import screen

    async def gen():
        seq = after if after else screen.bus.seq  # new listeners start from "now"
        yield f"event: hello\ndata: {json.dumps({'seq': seq})}\n\n"
        while not await request.is_disconnected():
            events = await screen.bus.wait_after(seq, timeout=15)
            if not events:
                yield ": keepalive\n\n"
                continue
            for e in events:
                seq = e["seq"]
                yield f"data: {json.dumps(e, default=str)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/public/call/{call_uuid}")
async def public_call(call_uuid: str):
    from app import screen
    c = next((x for x in console.fleet.recent(3000) if x.call_uuid == call_uuid), None)
    if c is None:
        return JSONResponse({"error": "call not found (older than the live window)"}, status_code=404)
    acct = console.fleet.accounts.get(c.account_id)
    return _jsonable(screen.call_detail(c, acct.name if acct else ""))


@app.get("/public/cdr")
async def public_cdr(status: str = "all", carrier: str = "", to_network: str = "", code: int = 0,
                     account: str = "", q: str = "", limit: int = 50):
    """Call logs for the console page: newest first, filterable like a CDR search."""
    from app import screen
    now = console.oncall.now()
    acct = account or ""
    window = (console.fleet.calls(acct, now - timedelta(seconds=180), now) if acct
              else console.fleet.recent(1500))
    f = {"kind": status or "all", "carrier": carrier or None, "to_network": to_network or None, "code": code or None}
    ql = (q or "").strip().lower()
    rows = sorted((c for c in window if screen._match(c, f)
                   and (not ql or ql in c.call_uuid.lower() or ql in c.to_number or ql in c.from_number)),
                  key=lambda c: c.initiation_time, reverse=True)
    rows = rows[:max(1, min(limit, 200))]
    names = {k: v.name for k, v in console.fleet.accounts.items()}
    return {"running": console.fleet.running, "rows": [{
        "uuid": c.call_uuid, "account_id": c.account_id, "account": names.get(c.account_id, c.account_id),
        "direction": c.call_direction, "from": c.from_number, "to": c.to_number,
        "carrier": c.carrier_route, "to_carrier": c.to_carrier,
        "start": c.initiation_time.strftime("%H:%M:%S"), "date": c.initiation_time.strftime("%d %b %Y"),
        "duration": c.call_duration, "code": c.hangup_cause_code, "cause": c.hangup_cause_name,
        "source": c.hangup_source, "sip": c.sip_response_code, "energy": c.audio_energy,
        "answered": c.answer_time is not None} for c in rows]}


@app.get("/public/flowmap")
async def public_flowmap(account: str = ""):
    from app import screen
    aid = account or console.store.get().account_id
    if not aid or aid not in console.fleet.accounts:
        return {"account_id": None}
    return _jsonable(screen.flow_map(console.fleet, aid, console.oncall.now()))


def _report() -> dict | None:
    from app import report
    cf = console.store.get()
    if not cf.account_id or not cf.alert:
        return None
    return report.build(cf, console.fleet, console.oncall, (public.session or {}).get("fault_at"), console.oncall.now())


@app.get("/public/report")
async def public_report():
    r = _report()
    return _jsonable(r) if r else {"incident_id": None}


@app.get("/public/report.md")
async def public_report_md():
    from fastapi.responses import PlainTextResponse
    from app import report
    r = _report()
    if not r:
        return PlainTextResponse("No incident yet.\n", status_code=404)
    return PlainTextResponse(report.markdown(r), headers={
        "Content-Disposition": f'attachment; filename="{r["incident_id"]}.md"'})


@app.get("/public/timeline")
async def public_timeline(bucket_s: int = 5):
    """Incident signal over time + stage markers + response metrics (time to detect / call / diagnose / fix)."""
    from app.detector import is_failure
    cf = console.store.get()
    aid = cf.account_id
    if not aid or not cf.alert:
        return {"account_id": None}
    now = console.oncall.now()
    sess = public.session or {}
    fault_at = sess.get("fault_at") or (console.fleet.faults().get(aid) or {}).get("since") or cf.milestones.alert_at
    start = max(fault_at - timedelta(seconds=30), now - timedelta(seconds=420))
    sig = cf.alert.signal
    exp = console.fleet.expected_rate(aid) * bucket_s
    pts, t = [], start
    while t + timedelta(seconds=bucket_s) <= now:
        b = console.fleet.calls(aid, t, t + timedelta(seconds=bucket_s))
        if sig == "silent_rate":
            ans = [c for c in b if c.audio_energy is not None]
            v = sum(c.audio_energy <= 5 for c in ans) / len(ans) if ans else None
        elif sig == "volume_drop":
            v = len(b) / exp if exp else None
        else:
            v = sum(map(is_failure, b)) / len(b) if b else None
        pts.append({"t": round((t - fault_at).total_seconds() + bucket_s / 2, 1), "v": None if v is None else round(v, 3)})
        t += timedelta(seconds=bucket_s)
    m = cf.milestones
    rel = lambda d: round((d - fault_at).total_seconds(), 1) if d else None  # noqa: E731
    marks = [("Fault starts", fault_at), ("Detected", m.alert_at), ("Call placed", m.call_placed_at),
             ("Engineer answers", m.acknowledged_at), ("Diagnosed", m.diagnosed_at), ("Ticket filed", m.ticket_at),
             ("Fix applied", m.fix_applied_at), ("Recovered", m.resolved_at)]
    baseline = int(config.env("MANUAL_BASELINE_SECONDS", "0").split("#")[0].strip() or 0)
    return _jsonable({
        "account_id": aid, "signal": sig, "now_t": rel(now), "points": pts,
        "threshold": {"failure_rate": console.watch.fail_rate, "silent_rate": console.watch.silent_rate,
                      "volume_drop": console.watch.volume_ratio}.get(sig),
        "marks": [{"label": l, "t": rel(d)} for l, d in marks if d],
        "metrics": {"detect_s": rel(m.alert_at), "call_s": rel(m.call_placed_at), "diagnose_s": rel(m.diagnosed_at),
                    "fix_s": rel(m.fix_applied_at), "recover_s": rel(m.resolved_at)},
        "manual_baseline_s": baseline or None,
    })


@app.get("/public/breakdown")
async def public_breakdown(by: str = "carrier", metric: str = "", account: str = ""):
    """Insights page: failure / silent breakdown computed from the raw CDRs (same code the voice uses)."""
    from app import screen
    cf = console.store.get()
    aid = account or cf.account_id
    if by not in ("carrier", "to_network", "hangup_cause"):
        by = "carrier"
    args = {"by": by, **({"metric": metric} if metric in ("failures", "silent") else {})}
    ev, facts = screen.resolve({"name": "show_chart", "args": args}, console.fleet, aid, console.oncall.now(),
                               cf.alert.signal if cf.alert and aid == cf.account_id else None, "")
    return _jsonable({**ev, "account_id": aid})


@app.post("/public/stop")
async def public_stop() -> dict:
    await public.stop()
    return {"ok": True}


@app.get("/state")
async def state() -> dict:
    return console.store.get().model_dump(mode="json")


@app.get("/state/stream")
async def state_stream(request: Request) -> StreamingResponse:
    async def gen():
        async for cf in console.store.subscribe():
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(cf.model_dump(mode='json'))}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/fleet/start")
async def fleet_start() -> dict:
    if console.task is None or console.task.done():
        if console.store.get().state is S.IDLE:
            console.clock.start()
            await console.oncall.start()
        console.task = asyncio.create_task(console.loop())
    console.fleet.start()
    return {"running": True}


@app.post("/fleet/stop")
async def fleet_stop() -> dict:
    console.fleet.stop()
    return {"running": False}


@app.post("/fleet/fault")
async def fleet_fault(account: str, fault: str = "none") -> dict:
    if not console.fleet.running:
        await fleet_start()
    chosen = None if fault in ("none", "", "healthy") else fault
    console.fleet.set_fault(account, chosen)
    if chosen:
        public.note_fault(account, chosen)
    return {"ok": True, "faults": _jsonable(console.fleet.faults())}


@app.post("/fleet/allpass")
async def fleet_allpass() -> dict:
    console.fleet.clear_all()
    return {"ok": True}


def _jsonable(x):
    return json.loads(json.dumps(x, default=str))


@app.get("/fleet")
async def fleet() -> dict:
    st = console.fleet.stats()
    faults = console.fleet.faults()
    rows = []
    for aid, acct in console.fleet.accounts.items():
        s = (st.get("accounts") or {}).get(aid, {})
        f = faults.get(aid) or {}
        since = f.get("since")
        rows.append({
            "id": aid, "name": acct.name, "use_case": acct.use_case,
            "spend_per_day_inr": acct.spend_per_day_inr, "plan": getattr(acct, "plan", ""),
            "cps_limit": getattr(acct, "cps_limit", None), "ha_enabled": getattr(acct, "ha_enabled", False),
            "fault": f.get("fault"), "fault_since": since.strftime("%H:%M:%S") if hasattr(since, "strftime") else since,
            **{k: s.get(k) for k in ("calls", "failed", "fail_rate", "answered", "silent", "silent_rate",
                                     "volume_ratio", "at_risk_inr_per_hour", "top_causes", "spark")},
        })
    rows.sort(key=lambda r: -(r["spend_per_day_inr"] or 0))
    w = console.watch
    return _jsonable({"running": console.fleet.running, "accounts": rows,
                      "faults": ["carrier_reject", "cps_burst", "silent_route", "pbx_timeout", "anchoring_4590", "acl_drop"],
                      "thresholds": {"fail_rate": w.fail_rate, "silent_rate": w.silent_rate, "volume_ratio": w.volume_ratio},
                      "call_min_spend_inr": CALL_MIN_SPEND, "live": console.live, "last_error": console.last_error})


@app.get("/cdr")
async def cdr(account: str | None = None, limit: int = 60) -> dict:
    rows = [{"call_uuid": c.call_uuid, "initiation_time": c.initiation_time.strftime("%H:%M:%S"),
             "direction": c.call_direction, "from": c.from_number, "to": c.to_number,
             "carrier": c.carrier_route, "to_carrier": c.to_carrier, "duration": c.call_duration,
             "sip": c.sip_response_code, "code": c.hangup_cause_code, "cause": c.hangup_cause_name,
             "source": c.hangup_source, "audio_energy": c.audio_energy}
            for c in console.fleet.recent(limit, account_id=account)]
    return {"rows": rows}


@app.post("/incident/approve")
async def approve() -> dict:
    await console.oncall.on_confirm_yes()
    return console.store.get().model_dump(mode="json")


@app.post("/incident/ask")
async def ask(q: str) -> dict:
    qid = await console.oncall.ask(q)
    return {"id": qid}


@app.post("/demo/hangup")
async def hangup() -> dict:
    await console.oncall.hangup_active()
    return console.store.get().model_dump(mode="json")


@app.post("/demo/reset")
async def reset() -> dict:
    if public.session and not public.session.get("ended_at"):
        return JSONResponse(
            {"error": "A mentor is waiting on the public page. Inject a fault, or POST /public/stop first."},
            status_code=409)
    try:
        a = console.oncall.active(console.store.get())
        if a is not None:
            await console.voice.hangup(a)  # never leave a live agent ringing someone
    except Exception:  # noqa: BLE001
        pass
    await _stop_loop()
    await console.oncall.shutdown()
    console.reset()
    return console.store.get().model_dump(mode="json")


@app.get("/api/live")
async def live_info() -> dict:
    return {"live": console.live, "voice": type(console.voice).__name__, "ticket": type(console.ticket).__name__,
            "notifier": type(console.notifier).__name__, "last_error": console.last_error,
            "turns": list(brain_proxy.turns)[-10:]}
