"""Phase C, part 12: the voice conversation drives the brain.

Plays the demo through the real /v1/chat/completions endpoint (Groq stubbed out), with
mock voice/ticket/notifier adapters and the clock frozen during calls like live mode:

  alert → call #1 → "what's the cause?" (honest unsure) → "bye" → evidence fills →
  automatic confident callback → "go straight to the fix" (actions proposed) →
  "yes" → ticket fails once, retries → RESOLVED

Plus the missed-call path: no answer → retry → no answer → Telegram → "Call me now".
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import brain_proxy, config, incident_app
from app.models import IncidentState as S


@pytest.fixture()
def client(monkeypatch):
    async def fake_groq(messages, turn, t0):
        turn["ttft_ms"] = 1
        fake_groq.prompts.append(messages[0]["content"])
        yield "Stub answer."
    fake_groq.prompts = []
    monkeypatch.setattr(brain_proxy, "_groq_stream", fake_groq)
    monkeypatch.setattr(brain_proxy, "CONFIRM_DELAY_S", 0.0)
    monkeypatch.setattr(brain_proxy, "HANGUP_DELAY_S", 0.0)
    monkeypatch.setattr(brain_proxy, "SLOW_CHECK_PAUSE_S", 0.0)
    # Both apps bind the proxy at import time; whichever was imported last wins. Re-bind to v1.
    brain_proxy.bind(lambda: incident_app.demo.orch, lambda: incident_app.demo.hangup_active())
    with TestClient(incident_app.app) as c:
        c.post("/demo/trigger?auto=0")
        incident_app.demo.orch.freeze_clock_during_calls = True
        incident_app.demo.orch.retry_wait_s = 0.0
        c.prompts = fake_groq.prompts
        yield c


def say(client, text):
    r = client.post("/v1/chat/completions",
                    headers={"Authorization": f"Bearer {config.PROXY_SHARED_SECRET}"},
                    json={"stream": True, "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200
    spoken = "".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                     for line in r.text.splitlines()
                     if line.startswith("data: {"))
    assert r.text.rstrip().endswith("data: [DONE]")
    return spoken


def state():
    return incident_app.demo.store.get()


def until(client, cond, step=5, limit=300):
    """Advance scenario time (no scripted call events) until cond(case file) holds."""
    for _ in range(limit):
        if cond(state()):
            return
        client.post(f"/demo/advance?seconds={step}&auto=0")
    raise AssertionError(f"condition never met; state={state().state}")


def run(client, fn, *a):
    return client.portal.call(fn, *a)


def test_rejects_bad_key(client):
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer nope"}, json={"messages": []})
    assert r.status_code == 401


def test_conversation_drives_full_arc(client):
    until(client, lambda c: c.state is S.CALLING)

    # Engineer picks up and asks for the cause while the evidence is still thin.
    say(client, "hey, what's going on, do you know the cause?")
    cf = state()
    assert cf.state is S.UNSURE_TOLD
    assert cf.verdict != "confident"
    assert "not sure yet" in client.prompts[-1]  # instruction to be honest
    assert not incident_app.demo.clock.running  # time is frozen while on the call

    # Slow-path check: filler first, evidence appended.
    n_ev = len(cf.evidence)
    spoken = say(client, "are the other carriers okay?")
    assert spoken.startswith("Give me a few seconds")
    assert len(state().evidence) == n_ev + 1

    # Hang up → clock resumes → evidence fills → automatic confident callback.
    spoken = say(client, "okay bye")
    assert "call you back" in spoken
    until(client, lambda c: c.state is S.CALLING and c.calls[-1].purpose == "callback")
    assert state().verdict == "confident"

    say(client, "go straight to the fix")
    assert state().state is S.CONFIRM_ACTIONS
    assert "raise the ticket" in client.prompts[-1]

    spoken = say(client, "yes, go ahead")
    assert spoken.startswith("On it")
    for _ in range(50):  # the confirm runs as a background task on the app loop
        if state().state is S.RESOLVED:
            break
        client.post("/demo/tick?auto=0")
    assert state().state is S.RESOLVED
    assert incident_app.demo.ticket.create_calls == 2
    assert incident_app.demo.voice.spoken[0].startswith("The ticket system just returned an error")
    roles = [t["role"] for t in state().transcript]
    assert roles.count("engineer") == 5 and "agent" in roles


def test_missed_call_retries_then_telegram_then_call_me_now(client):
    until(client, lambda c: c.state is S.CALLING)
    orch = incident_app.demo.orch
    run(client, orch.on_no_answer)
    assert state().state is S.RETRY
    run(client, orch.tick)
    assert state().state is S.CALLING and state().calls[-1].purpose == "retry"
    run(client, orch.on_no_answer)
    assert state().state is S.TELEGRAM_SENT
    assert incident_app.demo.notifier.missed == [state().incident_id]
    run(client, orch.on_demand_call)
    cf = state()
    assert cf.state is S.CALLING and cf.calls[-1].purpose == "on_demand"
    assert incident_app.demo.voice.placed[-1][2].startswith("You asked me to call")
