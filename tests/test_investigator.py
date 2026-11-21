"""Parts 5 + 7 test: detector fires on the spike; investigator's verdict rises
honestly from 'unsure' early to 'confident' once the window fills.
"""
from datetime import datetime, timedelta

from app.detector import Detector
from app.investigator import Investigator
from app.simulator import CallSimulator

TRIGGER = datetime(2026, 11, 19, 10, 42, 0)


def test_detector_does_not_fire_on_healthy_baseline():
    sim = CallSimulator(TRIGGER)
    det = Detector()
    # Evaluate just before the trigger — only healthy traffic exists.
    assert det.check(sim.calls_through(-1), TRIGGER - timedelta(seconds=1)) is None


def test_detector_fires_once_during_incident():
    sim = CallSimulator(TRIGGER)
    det = Detector()
    now = TRIGGER + timedelta(seconds=60)
    alert = det.check(sim.calls_through(60), now)
    assert alert is not None
    assert alert.failure_rate >= 0.20
    assert alert.account_id == "acct_northwind"
    # Latches: a second check returns nothing.
    assert det.check(sim.calls_through(90), TRIGGER + timedelta(seconds=90)) is None


def test_verdict_is_unsure_early():
    sim = CallSimulator(TRIGGER)
    now = TRIGGER + timedelta(seconds=40)
    result = Investigator().assess(sim.calls_through(40), now)
    assert result["verdict"] == "unsure"
    assert result["top_confidence"] < 0.60
    # Decoys are already ruled out.
    ruled = {h.id: h.status for h in result["hypotheses"]}
    assert ruled["auth_config"] == "ruled_out"
    assert ruled["number_formatting"] == "ruled_out"


def test_verdict_becomes_confident_when_window_fills():
    sim = CallSimulator(TRIGGER)
    now = TRIGGER + timedelta(seconds=300)  # 5 minutes in, steady state
    result = Investigator().assess(sim.calls_through(300), now)
    assert result["verdict"] == "confident"
    assert result["top_confidence"] >= 0.85
    carrier = next(h for h in result["hypotheses"] if h.id == "carrier_rejection")
    assert carrier.status == "confirmed"
    # Carrier X is clearly the worst route.
    assert result["breakdown"]["carrier_x"] > result["breakdown"]["carrier_y"]
