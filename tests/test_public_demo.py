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
