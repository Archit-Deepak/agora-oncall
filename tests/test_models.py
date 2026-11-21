"""Part 1 test: every shared model builds, nests, and round-trips through JSON.

This is the contract the whole build depends on, so it just proves the shapes
are real and survive serialization (the store snapshots to JSON, the dashboard
reads JSON, the proxy reads JSON).
"""
from datetime import datetime

from app.models import (
    ActionRecord,
    Alert,
    CallAttempt,
    CallRecord,
    CaseFile,
    Evidence,
    Hypothesis,
    IncidentState,
    Milestones,
)

T0 = datetime(2026, 11, 19, 10, 42, 0)


def test_call_record_mirrors_a_plivo_cdr():
    rec = CallRecord(
        call_uuid="5e65d61c-8ad3-4e68",
        from_number="+19085930363",
        to_number="+918277056814",
        initiation_time=T0,
        end_time=T0,
        hangup_cause_name="domestic_anchored_terms_not_met",
        hangup_cause_code=4590,
        hangup_source="zentrunk",
        sip_response_code=503,
        carrier_route="carrier_x",
        account_id="acct_northwind",
        api_auth_ok=True,
    )
    assert rec.call_direction == "outbound"  # default
    assert rec.hangup_cause_code == 4590


def test_defaults_are_sane():
    cf = CaseFile(incident_id="INC-20261119-1")
    assert cf.state is IncidentState.IDLE
    assert cf.verdict == "unsure"
    assert cf.top_confidence == 0.0
    assert cf.version == 0
    assert cf.hypotheses == [] and cf.evidence == [] and cf.calls == []
    assert isinstance(cf.milestones, Milestones)


def test_full_case_file_round_trips_through_json():
    cf = CaseFile(
        incident_id="INC-20261119-1",
        state=IncidentState.IN_CALL,
        alert=Alert(
            incident_id="INC-20261119-1",
            customer_name="Northwind Health",
            account_id="acct_northwind",
            failure_rate=0.34,
            baseline_rate=0.02,
            since=T0,
            window_calls=240,
            fired_at=T0,
        ),
        hypotheses=[
            Hypothesis(id="carrier_rejection", title="Carrier X rejecting", status="leading",
                       confidence=0.55, match_share=0.55),
            Hypothesis(id="number_formatting", title="Bad number formatting", status="ruled_out",
                       reason="failed numbers are valid E.164"),
            Hypothesis(id="auth_config", title="Auth/config failure", status="ruled_out",
                       reason="0 x 401/407"),
        ],
        evidence=[
            Evidence(id="ev1", at=T0, check="carrier_breakdown",
                     summary="Carrier X is failing on 82% of its calls.",
                     data={"carrier_x": 0.82}, supports=["carrier_rejection"]),
        ],
        leading="carrier_rejection",
        verdict="leaning",
        top_confidence=0.55,
        calls=[CallAttempt(attempt_id="a1", purpose="initial", route="agora_phone", placed_at=T0)],
        actions=[ActionRecord(kind="ticket", payload={"title": "Northwind: carrier X"})],
        transcript=[{"role": "user", "content": "what's going on?"}],
    )

    dumped = cf.model_dump_json()
    restored = CaseFile.model_validate_json(dumped)

    assert restored == cf
    assert restored.state is IncidentState.IN_CALL
    assert restored.hypotheses[0].id == "carrier_rejection"
    assert restored.calls[0].verdict_told == "none"  # default survived
