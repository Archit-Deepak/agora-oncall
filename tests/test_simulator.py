"""Part 4 test: the simulator emits healthy baseline, then carrier-X failures.

Checks the shape of the generated CDRs, not exact confidence numbers (those are
asserted later): baseline ~2%, incident dominated by carrier X 503s, and
determinism.
"""
from datetime import datetime

from app import scenario
from app.simulator import CallSimulator

TRIGGER = datetime(2026, 11, 19, 10, 42, 0)


def _rate(calls, pred):
    bad = [c for c in calls if pred(c)]
    return len(bad) / len(calls) if calls else 0.0


def test_baseline_is_about_two_percent():
    sim = CallSimulator(TRIGGER)
    # Just the 5 minutes before the trigger.
    baseline = [c for c in sim.calls_through(-1) ]
    assert len(baseline) > 200
    fail_rate = _rate(baseline, lambda c: c.sip_response_code != scenario.SIP_OK)
    assert 0.0 < fail_rate <= 0.04
    # No carrier-X rejections during baseline.
    assert not any(c.carrier_route == scenario.CARRIER_X and c.sip_response_code == 503 for c in baseline)


def test_incident_is_dominated_by_carrier_x_rejections():
    sim = CallSimulator(TRIGGER)
    calls = sim.calls_through(60 * 5)  # 5 minutes into the incident
    incident = [c for c in calls if c.initiation_time >= TRIGGER]
    fails = [c for c in incident if c.sip_response_code != scenario.SIP_OK]
    assert fails, "expected failures during the incident"
    x_sig = [c for c in fails if scenario.matches_carrier_rejection(c.carrier_route, c.sip_response_code)]
    # Carrier X 503s are the clear majority of failures once the window fills.
    assert len(x_sig) / len(fails) > 0.5


def test_no_auth_or_numbering_failures_ever():
    # auth_config and number_formatting are decoys: they must be cleanly ruled out,
    # so the simulator never actually emits 401/407 or 484, and auth is always OK.
    sim = CallSimulator(TRIGGER)
    calls = sim.calls_through(60 * 6)
    assert all(c.api_auth_ok for c in calls)
    assert not any(c.sip_response_code in (401, 407, 484) for c in calls)


def test_deterministic():
    a = CallSimulator(TRIGGER).calls_through(200)
    b = CallSimulator(TRIGGER).calls_through(200)
    assert [c.call_uuid for c in a] == [c.call_uuid for c in b]
    assert [c.sip_response_code for c in a] == [c.sip_response_code for c in b]
