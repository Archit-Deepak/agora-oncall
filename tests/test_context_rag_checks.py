"""Parts 10 + 11b test: RAG retrieval, the context builder's verdict policy, and
the voice-triggered checks.
"""
from datetime import datetime

from app import checks, scenario
from app.context import build_system_prompt
from app.models import Alert, CaseFile, Hypothesis, IncidentState
from app.rag import KnowledgeBase
from app.simulator import CallSimulator

TRIGGER = datetime(2026, 11, 19, 10, 42, 0)


def _case(verdict: str, conf: float) -> CaseFile:
    return CaseFile(
        incident_id="INC-20261119-1",
        state=IncidentState.IN_CALL,
        alert=Alert(incident_id="INC-20261119-1", customer_name=scenario.CUSTOMER_NAME,
                    account_id=scenario.ACCOUNT_ID, failure_rate=0.34, baseline_rate=0.02,
                    since=TRIGGER, window_calls=200, fired_at=TRIGGER),
        hypotheses=[
            Hypothesis(id="carrier_rejection", title="Carrier X rejecting calls",
                       status="confirmed" if verdict == "confident" else "leading", confidence=conf),
            Hypothesis(id="number_formatting", title="Bad E.164", status="ruled_out",
                       reason="valid E.164"),
            Hypothesis(id="auth_config", title="Auth failure", status="ruled_out", reason="no 401"),
        ],
        verdict=verdict, top_confidence=conf,
    )


def test_rag_retrieves_relevant_chunk():
    kb = KnowledgeBase()
    hits = kb.top_k("carrier rejecting calls with 503", k=3)
    assert hits
    joined = " ".join(c.text.lower() for c in hits)
    assert "503" in joined or "carrier" in joined


def test_context_unsure_policy_forbids_naming_cause():
    cf = _case("unsure", 0.55)
    kb = KnowledgeBase()
    prompt = build_system_prompt(cf, kb.top_k("carrier failure", 3), call_purpose="initial")
    assert "UNSURE" in prompt
    assert "not sure yet" in prompt
    assert "ruled out" in prompt.lower()
    assert "binding" in prompt.lower()


def test_context_confident_policy_allows_cause_and_actions():
    cf = _case("confident", 0.92)
    kb = KnowledgeBase()
    prompt = build_system_prompt(cf, kb.top_k("carrier failure fix", 3), call_purpose="callback")
    assert "CONFIDENT" in prompt
    assert "carrier X" in prompt
    assert "purpose=callback" in prompt


def test_checks_over_cdrs():
    sim = CallSimulator(TRIGGER)
    now = TRIGGER.replace(minute=47)  # 5 minutes in
    calls = sim.calls_through(300)

    cc = checks.compare_carriers(calls, now)
    assert cc.data[scenario.CARRIER_X] > cc.data[scenario.CARRIER_Y]
    assert "carrier_rejection" in cc.supports

    sample = checks.sample_failed_call(calls, now)
    assert sample.data.get("sip") == 503
    assert sample.data.get("carrier") == scenario.CARRIER_X

    cfg = checks.recent_config_changes(calls, now)
    assert cfg.data["changes_24h"] == 0
