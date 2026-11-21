"""Live traffic (the logs dashboard): realistic CDRs, operator controls, honest arc."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app import incident_app
from app.detector import Detector, is_failure
from app.investigator import Investigator
from app.traffic import LiveTraffic

T0 = datetime(2026, 11, 19, 23, 0, 0)
ZENTRUNK_CODES = {3000, 3010, 5350, 4340, 4410, 6030, 4550, 4160}


def _run(pass_s=60, fail_s=0):
    tr = LiveTraffic(T0)
    tr.start(T0)
    tr.calls_through(pass_s)
    if fail_s:
        tr.set_mode("fail", T0 + timedelta(seconds=pass_s))
        tr.calls_through(pass_s + fail_s)
    return tr


def test_healthy_traffic_looks_like_a_real_log():
    tr = _run(pass_s=120)
    calls = tr.calls
    assert 400 < len(calls) < 560  # ~4 calls/s
    assert {c.hangup_cause_code for c in calls} <= ZENTRUNK_CODES
    # busy / no answer happen but are not failures; real failures stay near baseline
    assert any(c.hangup_cause_code == 4410 for c in calls)
    rate = sum(map(is_failure, calls)) / len(calls)
    assert rate < 0.05
    assert Detector(window_s=60, min_calls=100).check(calls, T0 + timedelta(seconds=120)) is None


def test_fail_mode_timing_suspect_call_then_found_it():
    """'All calls fail' → suspected < 30 s → call 30-60 s (cause not known yet) → 85% within ~60 s of the call."""
    for seed in (1, 7, 23):
        tr = LiveTraffic(T0, seed=seed)
        tr.start(T0)
        tr.calls_through(45)
        tr.set_mode("fail", T0 + timedelta(seconds=45))
        det, inv = Detector(window_s=60, min_calls=100), Investigator(window_s=60, recovery_rate=0.06)
        sus = conf = None
        for s in range(46, 300):
            now = T0 + timedelta(seconds=s)
            calls = tr.calls_through(s)
            if sus is None and det.check(calls, now):
                sus = s - 45
            if conf is None and inv.assess(calls, now)["verdict"] == "confident":
                conf = s - 45
        call = sus + 25
        assert sus <= 30
        assert 30 <= call <= 60
        assert conf > call, "cause must not be known when the phone rings"
        assert conf - call <= 60


def test_stop_halts_and_pass_recovers():
    tr = _run(pass_s=30, fail_s=30)
    n = len(tr.calls)
    tr.stop(T0 + timedelta(seconds=60))
    tr.calls_through(120)
    assert len(tr.calls) == n  # stopped: nothing new
    tr.start(T0 + timedelta(seconds=120))
    tr.set_mode("pass", T0 + timedelta(seconds=120))
    recent = [c for c in tr.calls_through(240) if c.initiation_time > T0 + timedelta(seconds=120)]
    assert sum(map(is_failure, recent)) / len(recent) < 0.05


def test_controls_endpoints():
    with TestClient(incident_app.app) as c:
        assert c.get("/cdr").json()["rows"] == []
        st = c.post("/sim/start").json()
        assert st["running"] and st["mode"] == "pass"
        assert incident_app.demo.store.get().state.value == "MONITORING"
        assert c.post("/sim/mode?m=fail").json()["mode"] == "fail"
        out = c.get("/cdr?limit=20").json()
        assert out["threshold"] == 0.20
        assert all(r["code"] in ZENTRUNK_CODES for r in out["rows"])
        assert c.post("/sim/stop").json()["running"] is False
        c.post("/demo/reset")


def test_switching_back_to_pass_reads_as_recovered_quickly():
    """Regression: the share-of-failures confidence used to stay high (~75%) for a full
    minute after 'All calls pass'. Now recovery is detected from the last 20 s."""
    tr = _run(pass_s=60, fail_s=150)
    inv = Investigator(window_s=60, recovery_rate=0.06)
    during = inv.assess(tr.calls, T0 + timedelta(seconds=210))
    assert during["verdict"] == "confident"
    tr.set_mode("pass", T0 + timedelta(seconds=210))
    after = inv.assess(tr.calls_through(235), T0 + timedelta(seconds=235))
    assert after["verdict"] == "recovered"
    assert after["recent_fail_rate"] < 0.10


# ── single-call flow, driven tick by tick (mock adapters, no background loop) ─────────
def _setup(c):
    c.post("/sim/start")
    c.portal.call(incident_app._stop_play)
    demo = incident_app.demo
    return demo, demo.orch, demo.traffic


def _at(demo):
    return demo.trigger + timedelta(seconds=demo.clock.now())


def _step(c, demo, seconds, n=1):
    for _ in range(n):
        demo.clock.advance(seconds)
        c.portal.call(demo.orch.tick)
    return demo.store.get()


def test_real_outage_one_call_that_finds_it_live():
    with TestClient(incident_app.app) as c:
        demo, o, tr = _setup(c)
        _step(c, demo, 30)                       # healthy history
        tr.set_mode("fail", _at(demo))
        for _ in range(30):
            cf = _step(c, demo, 1)
            if cf.state.value == "SUSPECTED":
                break
        assert cf.state.value == "SUSPECTED" and not cf.calls
        for _ in range(40):
            cf = _step(c, demo, 1)
            if cf.state.value == "CALLING":
                break
        assert cf.state.value == "CALLING" and cf.verdict != "confident"
        assert demo.voice.placed[-1][2].count("debugging it as we speak") == 1
        c.portal.call(o.on_answered)
        for _ in range(120):
            cf = _step(c, demo, 1)
            if cf.state.value == "CONFIRM_ACTIONS":
                break
        assert cf.state.value == "CONFIRM_ACTIONS"
        assert any(t.startswith("Update: found it") for t in demo.voice.spoken)
        c.portal.call(o.on_confirm_yes)
        cf = demo.store.get()
        assert cf.state.value == "RESOLVED"
        assert [a.purpose for a in cf.calls] == ["initial"]  # one call, no callback
        c.post("/demo/reset")


def test_blip_gets_telegram_not_a_call_and_no_spam():
    with TestClient(incident_app.app) as c:
        demo, o, tr = _setup(c)
        _step(c, demo, 30)
        tr.set_mode("fail", _at(demo))
        for _ in range(30):
            cf = _step(c, demo, 1)
            if cf.state.value == "SUSPECTED":
                break
        assert cf.state.value == "SUSPECTED"
        tr.set_mode("pass", _at(demo))           # fixed before we'd call
        for _ in range(30):
            cf = _step(c, demo, 1)
            if cf.state.value == "AUTO_RECOVERED":
                break
        assert cf.state.value == "AUTO_RECOVERED"
        assert not cf.calls and not demo.voice.placed
        assert len(demo.notifier.notices) == 1 and "Recovered" in demo.notifier.notices[0]
        cf = _step(c, demo, 2, n=60)             # the lagging 1-min window must not re-fire
        assert cf.state.value == "AUTO_RECOVERED" and len(demo.notifier.notices) == 1
        tr.set_mode("fail", _at(demo))           # a new spike → a new suspicion
        for _ in range(40):
            cf = _step(c, demo, 1)
            if cf.state.value == "SUSPECTED":
                break
        assert cf.state.value == "SUSPECTED"
        c.post("/demo/reset")


def test_recovery_during_the_call_is_spoken_then_telegram():
    with TestClient(incident_app.app) as c:
        demo, o, tr = _setup(c)
        _step(c, demo, 30)
        tr.set_mode("fail", _at(demo))
        for _ in range(70):
            cf = _step(c, demo, 1)
            if cf.state.value == "CALLING":
                break
        assert cf.state.value == "CALLING"
        c.portal.call(o.on_answered)
        tr.set_mode("pass", _at(demo))
        for _ in range(40):
            _step(c, demo, 1)
            if any(t.startswith("Update:") for t in demo.voice.spoken):
                break
        assert any("recovered on their own" in t for t in demo.voice.spoken)
        assert not any(t.startswith("Update: found it") for t in demo.voice.spoken)
        c.portal.call(o.on_call_ended)
        cf = demo.store.get()
        assert cf.state.value == "AUTO_RECOVERED"
        assert len(demo.notifier.notices) == 1
        cf = _step(c, demo, 2, n=60)
        assert [a.purpose for a in cf.calls] == ["initial"]  # no callback
        c.post("/demo/reset")


def test_pass_clicked_just_before_the_call_holds_the_call():
    """Race: 'All pass' a few seconds before the confirm deadline must not ring the phone."""
    with TestClient(incident_app.app) as c:
        demo, o, tr = _setup(c)
        _step(c, demo, 30)
        tr.set_mode("fail", _at(demo))
        for _ in range(30):
            cf = _step(c, demo, 1)
            if cf.state.value == "SUSPECTED":
                break
        _step(c, demo, 1, n=20)                  # 5 s before the 25 s confirm deadline
        assert demo.store.get().state.value == "SUSPECTED"
        tr.set_mode("pass", _at(demo))
        for _ in range(40):
            cf = _step(c, demo, 1)
            if cf.state.value != "SUSPECTED":
                break
        assert cf.state.value == "AUTO_RECOVERED"
        assert not demo.voice.placed
        assert len(demo.notifier.notices) == 1
        c.post("/demo/reset")
