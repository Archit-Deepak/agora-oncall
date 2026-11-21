"""Part 9 test: the dashboard app wiring drives the brain to RESOLVED.

Uses auto=0 and /demo/advance (which ticks + auto-drives one event per call) so the
run is deterministic without a live background loop.
"""
from fastapi.testclient import TestClient

from app import incident_app


def test_dashboard_serves_and_state_is_json():
    with TestClient(incident_app.app) as client:
        assert client.get("/").status_code == 200
        s = client.get("/state").json()
        assert "incident_id" in s and "state" in s


def test_trigger_and_advance_runs_to_resolved():
    with TestClient(incident_app.app) as client:
        client.post("/demo/trigger?auto=0")

        state = None
        saw_calling = saw_unsure = False
        for _ in range(200):
            s = client.post("/demo/advance?seconds=5").json()
            state = s["state"]
            if state == "CALLING":
                saw_calling = True
            if s["verdict"] == "unsure" and s["top_confidence"] < 0.60:
                saw_unsure = True
            if state == "RESOLVED":
                break

        assert saw_calling, "should have placed a call"
        assert saw_unsure, "should have been honestly unsure early"
        assert state == "RESOLVED"
        # Ticket failed once then succeeded; callback happened.
        assert incident_app.demo.ticket.create_calls == 2
        purposes = [p[1] for p in incident_app.demo.voice.placed]
        assert "initial" in purposes and "callback" in purposes
