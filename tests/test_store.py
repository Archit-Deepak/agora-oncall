"""Part 2 test: the IncidentStore holds state, snapshots it, and notifies.

Async methods are driven with asyncio.run() so we don't need pytest-asyncio.
"""
import asyncio
from datetime import datetime

from app.models import CaseFile, Evidence, IncidentState
from app.store import IncidentStore


def test_get_returns_a_deep_copy(tmp_path):
    store = IncidentStore(tmp_path / "incident.json", CaseFile(incident_id="INC-1"))

    leaked = store.get()
    leaked.state = IncidentState.ALERTED
    leaked.evidence.append(Evidence(id="x", at=datetime(2026, 11, 19), check="c", summary="s"))

    # The live state is untouched by mutating the copy.
    assert store.get().state is IncidentState.IDLE
    assert store.get().evidence == []


def test_update_bumps_version_mutates_and_persists(tmp_path):
    path = tmp_path / "incident.json"
    store = IncidentStore(path, CaseFile(incident_id="INC-1"))

    async def go():
        await store.update(lambda c: setattr(c, "state", IncidentState.ALERTED))
        await store.update(lambda c: c.evidence.append(
            Evidence(id="e1", at=datetime(2026, 11, 19, 10, 42), check="carrier_breakdown",
                     summary="Carrier X failing on 82% of calls.")))

    asyncio.run(go())

    live = store.get()
    assert live.version == 2
    assert live.state is IncidentState.ALERTED
    assert live.evidence[0].check == "carrier_breakdown"

    # Snapshot survives a reload from disk.
    assert path.exists()
    reloaded = IncidentStore.load(path).get()
    assert reloaded.version == 2
    assert reloaded.state is IncidentState.ALERTED
    assert reloaded.evidence[0].summary == "Carrier X failing on 82% of calls."


def test_reset_returns_to_a_fresh_case(tmp_path):
    store = IncidentStore(tmp_path / "incident.json", CaseFile(incident_id="INC-1"))

    async def go():
        await store.update(lambda c: setattr(c, "state", IncidentState.RESOLVED))
        await store.reset(CaseFile(incident_id="INC-2"))

    asyncio.run(go())
    fresh = store.get()
    assert fresh.incident_id == "INC-2"
    assert fresh.state is IncidentState.IDLE
    assert fresh.version == 0


def test_subscribe_gets_current_state_then_updates(tmp_path):
    store = IncidentStore(tmp_path / "incident.json", CaseFile(incident_id="INC-1"))

    async def go():
        sub = store.subscribe()
        first = await sub.__anext__()  # current state, delivered immediately
        await store.update(lambda c: setattr(c, "state", IncidentState.CALLING))
        nxt = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        await sub.aclose()
        return first, nxt

    first, nxt = asyncio.run(go())
    assert first.state is IncidentState.IDLE
    assert nxt.state is IncidentState.CALLING
    assert nxt.version == 1
