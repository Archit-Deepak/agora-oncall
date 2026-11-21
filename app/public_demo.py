"""Public demo: a judge enters an access code and waits.

One session at a time. "call" saves an Indian mobile and warm-starts 60 s of healthy traffic.
"browser" does the same with no phone: when a failure holds, a window joins the agent over
Agora RTC. Neither breaks anything. The phone rings, or the window connects, only after the
operator injects a fault and it stays bad through the confirm window. "watch" still injects
a hidden fault immediately and uses a mock phone (no call).

Guards (all server-side):
  - access code (constant-time compare), wrong-code rate limit per IP
  - Indian mobile numbers only (+91 6-9xxxxxxxxx)
  - one call per number, ever (exempt: ENGINEER_PHONE and PUBLIC_EXEMPT_NUMBERS)
  - daily caps (calls / watch sessions), one session at a time
  - call capped at PUBLIC_MAX_CALL_S once answered; whole session capped at PUBLIC_SESSION_MAX_S
Sessions are recorded in SQLite (state/public.db); numbers are stored only as salted hashes.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import random
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from app import config
from app.models import IncidentState as S

log = logging.getLogger("public")

ACCESS_CODE = os.getenv("PUBLIC_ACCESS_CODE", "AGORA26")
DAILY_CALL_CAP = int(os.getenv("PUBLIC_DAILY_CALL_CAP", "20"))
DAILY_WATCH_CAP = int(os.getenv("PUBLIC_DAILY_WATCH_CAP", "200"))
MAX_CALL_S = float(os.getenv("PUBLIC_MAX_CALL_S", "300"))
SESSION_MAX_S = float(os.getenv("PUBLIC_SESSION_MAX_S", "540"))
END_GRACE_S = 25.0          # keep the session (and the call) up briefly after RESOLVED
WARM_S = 60                 # seconds of healthy history before the fault
BAD_CODE_LIMIT = 8          # wrong codes per IP per hour

# Hidden faults the public demo can inject (phone-tier accounts only).
FAULT_POOL = [("acct_northwind", "carrier_reject"), ("acct_zylo", "cps_burst"), ("acct_zylo", "acl_drop"),
              ("acct_medley", "silent_route"), ("acct_medley", "anchoring_4590")]

_MOBILE = re.compile(r"^[6-9]\d{9}$")


def normalize_india_mobile(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "").lstrip("0")   # 0 / 091 trunk prefixes
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return f"+91{digits}" if _MOBILE.match(digits) else None


def mask(number: str) -> str:
    return f"{number[:5]}•••••{number[-2:]}" if number else ""


class PublicDemo:
    def __init__(self, console_getter, db_path: Path, live_allowed: bool):
        self._console = console_getter          # () -> Console (it is replaced on reset)
        self.db_path = db_path
        self.live_allowed = live_allowed        # server runs with INCIDENT_LIVE=1
        self.lock = asyncio.Lock()
        self.session: dict | None = None
        self.task: asyncio.Task | None = None
        self._bad_codes: dict[str, list[float]] = {}
        self._last_fault: tuple[str, str] | None = None
        self._salt = (config.env("PROXY_SHARED_SECRET") or "salt").encode()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, mode TEXT, number_hash TEXT, ip TEXT, account TEXT, fault TEXT,
                started_at REAL, ended_at REAL, outcome TEXT)""")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _hash(self, number: str) -> str:
        return hmac.new(self._salt, number.encode(), hashlib.sha256).hexdigest()[:32]

    def _exempt(self, number: str) -> bool:
        raw = [config.env("ENGINEER_PHONE")] + config.env("PUBLIC_EXEMPT_NUMBERS").split(",")
        return number in {normalize_india_mobile(x) or x.strip() for x in raw if x and x.strip()}

    def _count_since(self, mode: str, since: float) -> int:
        # An armed session that never rang does not use up the daily call budget.
        extra = " AND (outcome IS NULL OR outcome NOT LIKE 'no-call:%')" if mode == "call" else ""
        with self._db() as db:
            return db.execute(f"SELECT COUNT(*) FROM sessions WHERE mode=? AND started_at>=?{extra}",
                              (mode, since)).fetchone()[0]

    def _called_before(self, number: str) -> bool:
        with self._db() as db:
            return db.execute(
                "SELECT 1 FROM sessions WHERE mode='call' AND number_hash=? "
                "AND (outcome IS NULL OR outcome NOT LIKE 'no-call:%') LIMIT 1",
                (self._hash(number),)).fetchone() is not None

    def _bad_code(self, ip: str) -> bool:
        now = time.time()
        hits = [t for t in self._bad_codes.get(ip, []) if now - t < 3600]
        self._bad_codes[ip] = hits
        return len(hits) >= BAD_CODE_LIMIT

    # ── start ──────────────────────────────────────────────────────────────
    async def start(self, mode: str, phone: str, code: str, name: str, ip: str,
                    force: str | None = None) -> tuple[int, dict]:
        if mode not in ("call", "browser", "watch"):
            return 400, {"error": "mode must be call, browser, or watch"}
        if self._bad_code(ip):
            return 429, {"error": "Too many wrong codes from your network. Try again in an hour."}
        if not hmac.compare_digest((code or "").strip().upper().encode(), ACCESS_CODE.upper().encode()):
            self._bad_codes.setdefault(ip, []).append(time.time())
            return 403, {"error": "That access code isn't right. It's in the submission notes."}
        number = None
        if mode == "call":
            if not self.live_allowed:
                return 503, {"error": "Live calls are switched off right now."}
            number = normalize_india_mobile(phone)
            if not number:
                return 400, {"error": "Please enter an Indian mobile number (10 digits, starting 6-9)."}
            if not self._exempt(number):
                if self._called_before(number):
                    return 409, {"error": "This number has already had its demo call."}
                day_ago = time.time() - 86400
                if self._count_since("call", day_ago) >= DAILY_CALL_CAP:
                    return 429, {"error": "Today's demo-call budget is used up."}
        elif mode == "browser":
            if not self.live_allowed:
                return 503, {"error": "Live calls are switched off right now."}
            if self._count_since("browser", time.time() - 86400) >= DAILY_CALL_CAP:
                return 429, {"error": "Today's browser-demo budget is used up."}
        elif self._count_since("watch", time.time() - 86400) >= DAILY_WATCH_CAP:
            return 429, {"error": "Daily demo limit reached. Please try again tomorrow."}
        name = re.sub(r"[^A-Za-z .'-]", "", name or "").strip()[:24] or "there"

        async with self.lock:
            if self.session and not self.session.get("ended_at"):
                left = max(0, int(self.session["started_at"] + SESSION_MAX_S - time.time()))
                return 409, {"error": f"Someone else's demo is running. Try again in about {max(30, left)} seconds.",
                             "busy": True}
            sid = uuid.uuid4().hex[:10]
            aid, fault = None, None
            # Watch mode still breaks an account immediately. A real call waits for the operator's Inject.
            if mode == "watch":
                pool = [p for p in FAULT_POOL if p != self._last_fault] or FAULT_POOL
                aid, fault = random.choice(pool)
                if force and ":" in force and tuple(force.split(":", 1)) in FAULT_POOL + [("acct_brightline", "pbx_timeout")]:
                    aid, fault = force.split(":", 1)
                self._last_fault = (aid, fault)
            waiting = mode in ("call", "browser")
            self.session = {"id": sid, "mode": mode, "number": number, "name": name, "account": aid,
                            "fault": fault, "started_at": time.time(), "ended_at": None, "outcome": None,
                            "answered_at": None, "notes": [], "dialed": False, "waiting": waiting}
            with self._db() as db:
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                           (sid, mode, self._hash(number) if number else None, ip, aid, fault,
                            self.session["started_at"], None, None))
            await self._boot(mode, number, name, aid, fault)
            self.task = asyncio.create_task(self._watchdog(sid))
        return 200, {"session": sid, "mode": mode, "number": mask(number or ""),
                     "waiting": waiting,
                     "eta_s": None if waiting else (40 if mode == "call" else 35)}

    async def _boot(self, mode: str, number: str | None, name: str, aid: str, fault: str) -> None:
        from app import adapters, console_app, screen
        screen.bus.reset()
        con = self._console()
        await console_app.reset()                      # hang up anything, stop loops, fresh state
        con = self._console()
        con.live = mode in ("call", "browser")
        con.reset()                                    # rebuild adapters for this mode
        if mode == "call" and hasattr(con.voice, "to_number"):
            con.voice.to_number = number
        if mode == "browser":
            from app import live_adapters
            con.voice = live_adapters.AgoraBrowserCaller()
            con.oncall.voice = con.voice
        # Public sessions never ping the operator's Telegram; notices are shown on the page instead.
        con.notifier = adapters.MockNotifier()
        con.oncall.notifier = con.notifier
        # "Send me a summary on Telegram": real Telegram only for the owner's own number (judges see it on the page).
        if mode == "call" and number and self._exempt(number) and config.env("NOTIFY_MODE").split("#")[0].strip() == "telegram":
            from app import live_adapters
            con.oncall.summary_notifier = live_adapters.TelegramNotifier()
        con.oncall.engineer = name
        # Warm start: scenario "now" == wall clock, with WARM_S of healthy history already behind it.
        con.trigger = datetime.now() - timedelta(seconds=WARM_S)
        con.oncall.trigger = con.trigger
        con.fleet = type(con.fleet)(con.trigger)
        con.oncall.fleet = con.fleet
        con.clock.start()
        con.clock.advance(WARM_S)
        await con.oncall.start()
        con.fleet.start(con.trigger)
        if fault:
            con.fleet.set_fault(aid, fault, now=con.oncall.now())
            self.session["fault_at"] = con.oncall.now()
        con.task = asyncio.create_task(con.loop())

    def note_fault(self, account: str, fault: str | None) -> None:
        """Operator pressed Inject during an armed call. Remember what broke, for the report."""
        s = self.session
        if not s or s.get("ended_at") or s.get("mode") not in ("call", "browser"):
            return
        if not fault or fault in ("none", "healthy"):
            return
        if not s.get("fault_at"):
            s["fault_at"] = self._console().oncall.now()
        s["account"] = account
        s["fault"] = fault
        s["waiting"] = False

    # ── watchdog: caps and clean endings ───────────────────────────────────
    async def _watchdog(self, sid: str) -> None:
        resolved_at = None
        warned = False
        try:
            while self.session and self.session["id"] == sid and not self.session.get("ended_at"):
                await asyncio.sleep(1.0)
                con = self._console()
                cf = con.store.get()
                a = con.oncall.active(cf)
                now = time.time()
                if cf.state in (S.ALERTED, S.CALLING, S.RETRY, S.IN_CALL, S.CONFIRM_ACTIONS,
                                S.EXECUTING, S.VERIFYING, S.RESOLVED, S.WAITING_FOR_EVIDENCE):
                    self.session["dialed"] = True
                if a and a.state == "answered" and not self.session["answered_at"]:
                    self.session["answered_at"] = now
                answered = self.session["answered_at"]
                if answered and a and a.state == "answered" and now - answered > MAX_CALL_S:
                    if not warned:
                        warned = True
                        await con.oncall._speak(a, "We're at the five minute limit for this demo, so I'll "
                                                   "hang up now. Thanks for trying it!")
                        await asyncio.sleep(7)
                        await con.oncall.hangup_active()
                        await self._end("call time limit")
                        return
                if cf.state is S.RESOLVED:
                    resolved_at = resolved_at or now
                    if a is None or now - resolved_at > END_GRACE_S:
                        if a is not None:
                            await con.oncall._speak(a, "That's the whole loop. Thanks for being my on-call "
                                                       "engineer today, goodbye!")
                            await asyncio.sleep(6)
                            await con.oncall.hangup_active()
                        await self._end("resolved")
                        return
                if cf.state is S.TELEGRAM_SENT and self.session["mode"] in ("call", "browser"):
                    await self._end("call ended before the fix was approved")
                    return
                if now - self.session["started_at"] > SESSION_MAX_S:
                    if a is not None:
                        await con.oncall.hangup_active()
                    await self._end("session time limit")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never leave a call hanging on a bug
            log.exception("public watchdog failed")
            try:
                await self._console().oncall.hangup_active()
            except Exception:  # noqa: BLE001
                pass
            await self._end(f"error: {e!r}"[:120])

    async def _end(self, outcome: str) -> None:
        if not self.session or self.session.get("ended_at"):
            return
        self.session["ended_at"] = time.time()
        if not self.session.get("dialed"):
            outcome = f"no-call: {outcome}"
        self.session["outcome"] = outcome
        con = self._console()
        con.fleet.stop()
        if con.task and not con.task.done() and con.task is not asyncio.current_task():
            con.task.cancel()
        await con.oncall._stop_agent()
        with self._db() as db:
            db.execute("UPDATE sessions SET ended_at=?, outcome=? WHERE id=?",
                       (self.session["ended_at"], outcome, self.session["id"]))

    async def stop(self) -> None:
        """Operator abort."""
        if self.session and not self.session.get("ended_at"):
            try:
                await self._console().oncall.hangup_active()
            except Exception:  # noqa: BLE001
                pass
            await self._end("stopped by operator")

    # ── status for the page ────────────────────────────────────────────────
    def status(self) -> dict:
        con = self._console()
        cf = con.store.get()
        s = self.session
        busy = bool(s and not s.get("ended_at"))
        out: dict = {"busy": busy, "live_calls": self.live_allowed, "caps": {"max_call_s": MAX_CALL_S}}
        if not s:
            return out
        aid = cf.account_id or None
        revealed = cf.verdict == "confident" or bool(s.get("ended_at"))
        fix = cf.fixes[-1] if cf.fixes else None
        call = cf.calls[-1] if cf.calls else None
        out["session"] = {
            "id": s["id"], "mode": s["mode"], "number": mask(s["number"] or ""), "name": s["name"],
            "elapsed_s": int((s.get("ended_at") or time.time()) - s["started_at"]),
            "ended": bool(s.get("ended_at")), "outcome": s.get("outcome"),
            "hidden_fault": s["fault"] if revealed else None,
            "waiting": bool(s.get("waiting")) and not s.get("ended_at"),
            "hidden_account": s["account"] if revealed or cf.state is not S.MONITORING else None,
        }
        out["state"] = cf.state.value
        out["incident"] = {
            "id": cf.incident_id, "account_id": aid,
            "account": con.oncall.account(aid).name if aid and con.oncall.account(aid) else None,
            "signal": cf.alert.signal if cf.alert else None,
            "signal_value": cf.alert.signal_value if cf.alert else None,
            "verdict": cf.verdict, "confidence": cf.top_confidence,
            "hypotheses": [{"id": h.id, "title": h.title, "status": h.status, "confidence": h.confidence,
                            "reason": h.reason} for h in cf.hypotheses],
            "log": cf.investigator_log[-60:],
            "transcript": cf.transcript[-40:],
            "fix": ({k: fix.get(k) for k in ("signature", "fix", "params", "owner", "status", "spoken_summary",
                                              "evidence", "cost", "verification", "source")} if fix else None),
            "actions": [{"kind": x.kind, "status": x.status, "id": x.external_id, "attempts": x.attempts,
                         "text": (x.payload or {}).get("text") or (x.payload or {}).get("body")} for x in cf.actions],
            "email": cf.email_draft,
            "call": ({"state": call.state, "purpose": call.purpose, "reason": call.hangup_reason} if call else None),
            "milestones": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                           for k, v in cf.milestones.model_dump().items()},
        }
        if (s.get("mode") == "browser" and not s.get("ended_at") and call
                and call.state in ("ringing", "answered") and hasattr(con.voice, "join_info")):
            info = con.voice.join_info()
            if info:
                out["voice"] = info
        st = con.fleet.stats(con.oncall.now(), window_s=60) if con.fleet.running or s.get("ended_at") else {"accounts": {}}
        out["accounts"] = [{"id": k, "name": v.get("name"), "spend": v.get("spend_per_day_inr"),
                            "fail_rate": v.get("fail_rate"), "silent_rate": v.get("silent_rate"),
                            "volume_ratio": v.get("volume_ratio"), "calls": v.get("calls"),
                            "at_risk": v.get("at_risk_inr_per_hour"), "spark": v.get("spark")}
                           for k, v in (st.get("accounts") or {}).items()]
        rows = con.fleet.recent(25, account_id=aid) if aid else con.fleet.recent(25)
        out["cdr"] = [{"uuid": c.call_uuid, "t": c.initiation_time.strftime("%H:%M:%S"), "acct": c.account_id, "from": c.from_number,
                       "to": c.to_number[:6] + "…", "carrier": c.carrier_route, "to_carrier": c.to_carrier,
                       "code": c.hangup_cause_code, "cause": c.hangup_cause_name, "sip": c.sip_response_code,
                       "energy": c.audio_energy, "dur": c.call_duration} for c in rows]
        return out
