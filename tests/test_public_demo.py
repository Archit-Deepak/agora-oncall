"""Public demo guards: code, India-only numbers, one call per number, caps, one session at a time."""
import asyncio

import pytest

from app import public_demo as P


def test_normalize_india_mobile():
    for raw in ("9876543210", "+91 98765 43210", "091-9876543210", "919876543210", "09876543210"):
        assert P.normalize_india_mobile(raw) == "+919876543210", raw
    for bad in ("", "12345", "5876543210", "+14155550100", "98765432101", "abcdefghij"):
        assert P.normalize_india_mobile(bad) is None, bad
    assert P.mask("+919876543210") == "+9198•••••10"


@pytest.fixture()
def demo(tmp_path, monkeypatch):
    booted = []

    async def fake_boot(self, mode, number, name, aid, fault):
        booted.append((mode, number, name, aid, fault))

    async def fake_watchdog(self, sid):
        return None
    monkeypatch.setattr(P.PublicDemo, "_boot", fake_boot)
    monkeypatch.setattr(P.PublicDemo, "_watchdog", fake_watchdog)
    monkeypatch.setattr(P, "ACCESS_CODE", "AGORA26")
    monkeypatch.setattr(P.config, "env", lambda k, d="": {"ENGINEER_PHONE": "+919000000001"}.get(k, d))
    d = P.PublicDemo(lambda: None, tmp_path / "p.db", live_allowed=True)
    d.booted = booted
    return d


def start(d, mode="call", phone="9876543210", code="agora26", ip="1.1.1.1", name="Judge"):
    return asyncio.run(d.start(mode, phone, code, name, ip))


def finish(d):
    d.session["ended_at"] = 1.0


def test_wrong_code_then_rate_limited(demo):
    for _ in range(P.BAD_CODE_LIMIT):
        assert start(demo, code="nope")[0] == 403
    assert start(demo, code="agora26")[0] == 429  # this IP is locked out for the hour
    assert start(demo, code="agora26", ip="2.2.2.2")[0] == 200


def test_call_happy_path_then_one_call_per_number(demo):
    code, body = start(demo)
    assert code == 200 and body["number"].endswith("10") and "•" in body["number"]
    mode, number, name, aid, fault = demo.booted[-1]
    assert number == "+919876543210" and name == "Judge" and aid is None and fault is None
    assert body["waiting"] is True and body["eta_s"] is None
    assert start(demo, phone="9123456789")[0] == 409          # busy: one session at a time
    finish(demo)
    assert start(demo)[0] == 409                               # same number again
    assert start(demo, phone="9123456789")[0] == 200


def test_browser_arms_without_a_phone(demo):
    code, body = start(demo, mode="browser", phone="")
    assert code == 200 and body["waiting"] is True and body["mode"] == "browser"
    mode, number, name, aid, fault = demo.booted[-1]
    assert mode == "browser" and number is None and name == "Judge" and aid is None and fault is None
    finish(demo)
    assert start(demo, mode="browser", phone="")[0] == 200  # no one-number limit


def test_browser_off_when_live_calls_are_off(demo):
    demo.live_allowed = False
    assert start(demo, mode="browser", phone="")[0] == 503


def test_browser_caller_joins_a_channel_and_does_not_dial(monkeypatch):
    from datetime import datetime

    from app import live_adapters as L
    from app.models import CallAttempt

    seen = {}

    async def fake_join(channel, greeting, idle_timeout=30):
        seen["channel"] = channel
        seen["idle"] = idle_timeout
        return {"status_code": 200, "agent_id": "agent-1"}

    async def fake_phone(*_a, **_k):
        raise AssertionError("browser mode dialled a phone")

    monkeypatch.setattr(L.agora, "start_agent", fake_join)
    monkeypatch.setattr(L.agora, "place_phone_call", fake_phone)
    monkeypatch.setattr(L.agora, "rtc_token", lambda channel, uid, ttl_s=3600: "tok")
    monkeypatch.setattr(L.config, "AGORA_APP_ID", "app")
    monkeypatch.setattr(L.config, "USER_UID", 1002)
    caller = L.AgoraBrowserCaller()
    attempt = CallAttempt(attempt_id="att-abc", purpose="initial", route="agora_rtc", placed_at=datetime.now())
    agent_id = asyncio.run(caller.place_call(attempt, "hi"))
    assert agent_id == "agent-1"
    assert seen == {"channel": "brw-att-abc", "idle": 90}
    info = caller.join_info()
    assert info == {"app_id": "app", "channel": "brw-att-abc", "uid": 1002, "token": "tok"}


def test_watch_still_injects_a_fault(demo):
    assert start(demo, mode="watch", phone="")[0] == 200
    _mode, _number, _name, aid, fault = demo.booted[-1]
    assert (aid, fault) in P.FAULT_POOL


def test_non_indian_number_rejected_and_watch_needs_no_number(demo):
    assert start(demo, phone="+14155550100")[0] == 400
    assert start(demo, mode="watch", phone="")[0] == 200


def test_exempt_number_can_call_repeatedly(demo):
    assert start(demo, phone="9000000001")[0] == 200
    finish(demo)
    assert start(demo, phone="9000000001")[0] == 200


def test_daily_cap(demo, monkeypatch):
    monkeypatch.setattr(P, "DAILY_CALL_CAP", 2)
    for ph in ("9111111111", "9222222222"):
        assert start(demo, phone=ph)[0] == 200
        finish(demo)
    assert start(demo, phone="9333333333")[0] == 429
    assert start(demo, mode="watch")[0] == 200  # watch still allowed


def test_calls_disabled_when_server_not_live(tmp_path, demo):
    demo.live_allowed = False
    assert start(demo)[0] == 503
    assert start(demo, mode="watch")[0] == 200
