"""Part 8 test: the full incident, end to end, with mock adapters.

Walks the exact demo arc and asserts the state machine visits the right states,
the agent is honest early and confident late, the callback fires on its own, and
the ticket (deliberate first failure, then retry) + email land. This is the test
that protects everything built after it.
"""
import asyncio
from datetime import datetime

from app.adapters import MockNotifier, MockTicketSink, MockVoiceCaller
from app.clock import ScenarioClock
from app.detector import Detector
from app.investigator import Investigator
from app.models import IncidentState
from app.orchestrator import Orchestrator
from app.simulator import CallSimulator
from app.store import IncidentStore

TRIGGER = datetime(2026, 11, 19, 10, 42, 0)


class Manual:
    """A clock we drive by hand so the replay is instant and deterministic."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def build(tmp_path):
    mt = Manual()
    clock = ScenarioClock(speed=1.0, time_fn=mt)
    store = IncidentStore(tmp_path / "incident.json")
    voice, ticket, notifier = MockVoiceCaller(), MockTicketSink(fail_first=True), MockNotifier()
    orch = Orchestrator(store, clock, CallSimulator(TRIGGER), Detector(), Investigator(),
                        voice, ticket, notifier, trigger=TRIGGER, fail_first_ticket=True)
    return mt, clock, store, voice, ticket, orch


def test_full_incident_arc(tmp_path):
    mt, clock, store, voice, ticket, orch = build(tmp_path)
    seen: list[IncidentState] = []

    async def go():
        clock.start()
        await orch.start()
        seen.append(store.get().state)

        # Time passes until the detector fires and call #1 goes out.
        mt.t = 60
        await orch.tick()
        s = store.get()
        seen.append(s.state)
        assert s.state is IncidentState.CALLING
        assert s.alert is not None
        assert voice.placed and voice.placed[0][1] == "initial"

        # Engineer answers. It's early, so the agent is honestly unsure.
        await orch.on_answered()
        assert store.get().state is IncidentState.IN_CALL
        assert store.get().verdict == "unsure"
        await orch.on_verdict_told()
        assert store.get().state is IncidentState.UNSURE_TOLD

        # Engineer hangs up; we wait for more evidence.
        await orch.on_call_ended()
        assert store.get().state is IncidentState.WAITING_FOR_EVIDENCE

        # Later, the window fills and confidence crosses the threshold.
        mt.t = 300
        await orch.tick()  # refreshes investigation, then fires the callback
        s = store.get()
        seen.append(s.state)
        assert s.verdict == "confident"
        assert s.top_confidence >= 0.85
        assert s.state is IncidentState.CALLING
        assert voice.placed[-1][1] == "callback"

        # Callback answered; agent proposes actions; engineer says yes.
        await orch.on_answered()
        await orch.propose_actions()
        assert store.get().state is IncidentState.CONFIRM_ACTIONS
        await orch.on_confirm_yes()

        return store.get()

    final = asyncio.run(go())

    # Ticket: first attempt failed on purpose, retry succeeded.
    assert ticket.create_calls == 2
    assert final.state is IncidentState.RESOLVED
    tickets = [a for a in final.actions if a.kind == "ticket"]
    assert tickets and tickets[0].status == "done"
    assert tickets[0].external_id == "VPS-42"
    assert final.email_draft and "carrier X" in final.email_draft
    assert ticket.appended, "incident summary should be appended as a comment"
    # Milestones recorded along the way.
    assert final.milestones.alert_at and final.milestones.diagnosed_at and final.milestones.ticket_at


def test_illegal_transition_raises():
    from app import states
    from app.models import IncidentState as S
    import pytest
    with pytest.raises(ValueError):
        states.check(S.IDLE, S.RESOLVED)
