"""v2 end-to-end: fleet → AccountWatch → OnCall → InvestigatorAgent (scripted LLM) → voice/fix/verify.

The LLM is replaced by a scripted `chat` so these run fast and offline; the live-Groq path
is exercised by the e2e script. Scenario time is driven with clock.advance(); the agent
runs as a real background task on the app loop.
"""
import time

import pytest
from fastapi.testclient import TestClient

from app import console_app, investigator_agent
from app.models import IncidentState as S


def scripted(signature, fix, params=None, bad_first=None, owner="carrier"):
    """A fake LLM: optional wrong conclusion first, then the right one; answers questions."""
    state = {"n": 0}

    async def chat(messages, tools=None, **kw):
        state["n"] += 1
        last = messages[-1]
        if last.get("role") == "user" and str(last.get("content", "")).startswith("ENGINEER ASKS ["):
            qid = last["content"].split("[", 1)[1].split("]", 1)[0]
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"t{state['n']}", "type": "function", "function": {
                    "name": "answer_question",
                    "arguments": f'{{"question_id": "{qid}", "answer": "TATA is the only carrier failing."}}'}}]}
        if state["n"] == 1:
            return {"role": "assistant", "content": "Lead looks clear.", "tool_calls": [
                {"id": "t1", "type": "function", "function": {
                    "name": "report_hypothesis",
                    "arguments": f'{{"id": "{signature}", "title": "lead", "status": "leading", "confidence": 0.7, "evidence": "triage"}}'}}]}
        sig = bad_first if (bad_first and state["n"] == 2) else signature
        import json
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"t{state['n']}", "type": "function", "function": {
                "name": "conclude", "arguments": json.dumps({
                    "signature": sig, "spoken_summary": "Cause found.", "evidence": "numbers",
                    "owner": owner, "fix": fix, "fix_params": params or {}})}}]}
    return chat


async def broken_chat(messages, tools=None, **kw):
    raise RuntimeError("groq down")


@pytest.fixture()
def run(monkeypatch):
    monkeypatch.setattr(console_app, "MOCK_ANSWER_AFTER_S", 0.0)
    monkeypatch.setattr(console_app, "MOCK_APPROVE_AFTER_S", 0.0)
    # tests compress scenario time ~100x; shrink the agent's real-time waits to match
    monkeypatch.setattr(investigator_agent, "REJECT_COOLDOWN_S", 0.02)
    monkeypatch.setattr(investigator_agent, "FALLBACK_RETRY_S", 0.02)

    def make(chat):
        con = console_app.Console(live=False, chat=chat)
        monkeypatch.setattr(console_app, "console", con)
        # Both apps bind the voice proxy at import time; make sure it talks to this console.
        from app import brain_proxy
        brain_proxy.bind(lambda: console_app.console.oncall, lambda: console_app.console.oncall.hangup_active())
        c = TestClient(console_app.app)
        c.__enter__()
        con.clock.start()
        c.portal.call(con.oncall.start)
        con.fleet.start(con.oncall.now())
        step(c, con, 50)  # healthy history
        return c, con
    yield make


def step(c, con, seconds, until=None):
    for _ in range(int(seconds)):
        con.clock.advance(1)
        c.portal.call(con.oncall.tick)
        c.portal.call(con.mock_events)
        time.sleep(0.02)  # let the investigator task run
        if until and until(con.store.get()):
            return True
    return until is None


def fault(con, aid, f):
    con.fleet.set_fault(aid, f, now=con.oncall.now())


def test_high_spend_account_gets_a_call_that_finds_and_fixes_it(run):
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 40, until=lambda cf: cf.state is S.SUSPECTED)
    assert con.store.get().account_id == "acct_northwind"
    assert step(c, con, 60, until=lambda cf: cf.state in (S.CALLING, S.IN_CALL))
    assert "Northwind Health" in con.voice.placed[-1][2]
    assert step(c, con, 60, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    assert any(t.startswith("Okay, I've got it") for t in con.voice.spoken)
    cf = con.store.get()
    assert cf.verdict == "confident" and cf.fixes[-1]["fix"] == "enable_ha"
    c.post("/incident/approve")
    assert step(c, con, 90, until=lambda cf: cf.state is S.RESOLVED)
    time.sleep(1.6)  # ticket fail-first + retry
    cf = con.store.get()
    f = cf.fixes[-1]
    assert f["status"] == "verified" and f["verification"]["after"] < 0.06
    assert any("And it's recovered" in t for t in con.voice.spoken)
    assert con.fleet.accounts["acct_northwind"].past_incidents[-1]["fix"].startswith("enable_ha")
    assert [a.purpose for a in cf.calls] == ["initial"]
    c.post("/demo/reset")


def test_low_spend_account_gets_telegram_not_a_call(run):
    c, con = run(scripted("pbx_timeout", "notify_customer"))
    fault(con, "acct_brightline", "pbx_timeout")
    assert step(c, con, 40, until=lambda cf: cf.state is S.SUSPECTED)
    assert step(c, con, 40, until=lambda cf: cf.state is S.TELEGRAM_SENT)
    time.sleep(1.5)
    assert not con.voice.placed
    assert con.notifier.notices and "Brightline" in con.notifier.notices[-1]
    c.post("/demo/reset")


def test_wrong_conclusion_is_rejected_by_code(run):
    c, con = run(scripted("carrier_reject", "enable_ha", bad_first="cps_burst"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 70, until=lambda cf: cf.verdict == "confident")
    log = con.store.get().investigator_log
    verifies = [e for e in log if e["kind"] == "verify"]
    assert verifies[0]["signature"] == "cps_burst" and verifies[0]["ok"] is False
    assert any(e["ok"] and e["signature"] == "carrier_reject" for e in verifies)
    c.post("/demo/reset")


def test_llm_down_falls_back_to_rules(run, monkeypatch):
    monkeypatch.setattr(investigator_agent, "DEADLINE_S", 1.0)
    c, con = run(broken_chat)
    fault(con, "acct_zylo", "cps_burst")
    assert step(c, con, 70, until=lambda cf: cf.verdict == "confident")
    cf = con.store.get()
    assert cf.fixes[-1]["signature"] == "cps_burst" and cf.fixes[-1]["source"] == "rules"
    c.post("/demo/reset")


def test_hang_up_before_deciding_then_approve_from_telegram(run):
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 120, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    c.portal.call(con.oncall.on_call_ended)
    cf = con.store.get()
    assert cf.state is S.TELEGRAM_SENT
    assert ("✅ Approve fix", "approve_fix") in con.notifier.buttons
    c.portal.call(con.oncall.on_confirm_yes)        # what the Telegram button does
    assert step(c, con, 90, until=lambda cf: cf.state is S.RESOLVED)
    c.post("/demo/reset")


def test_engineer_question_is_answered_on_the_call(run):
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 80, until=lambda cf: cf.state is S.IN_CALL)
    c.post("/incident/ask?q=is it only TATA?")
    assert step(c, con, 30, until=lambda cf: any(q.get("status") == "answered" for q in cf.pending_questions))
    step(c, con, 2)
    assert any(t.startswith("On your question") for t in con.voice.spoken)
    c.post("/demo/reset")


def test_blip_auto_recovers_with_a_note(run):
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 40, until=lambda cf: cf.state is S.SUSPECTED)
    con.fleet.clear_all(now=con.oncall.now())
    assert step(c, con, 40, until=lambda cf: cf.state is S.AUTO_RECOVERED)
    assert not con.voice.placed
    assert con.notifier.notices and "Mild incident" in con.notifier.notices[-1]
    c.post("/demo/reset")


def test_code_corrects_owner_and_invalid_fix(run):
    # LLM blames Plivo and proposes a fix that can't address a CPS burst.
    c, con = run(scripted("cps_burst", "enable_ha", owner="plivo"))
    fault(con, "acct_zylo", "cps_burst")
    assert step(c, con, 70, until=lambda cf: cf.verdict == "confident")
    f = con.store.get().fixes[-1]
    assert f["owner"] == "customer" and f["fix"] == "raise_cps"
    assert any("Owner corrected" in (e.get("text") or "") for e in con.store.get().investigator_log)
    c.post("/demo/reset")


def test_anchoring_4590_with_invented_caller_id_resolves(run):
    # LLM invents a caller ID the account doesn't own; code swaps in the account's own +1 number,
    # and verification judges recovery on the 4590s stopping, not on raw noise.
    c, con = run(scripted("anchoring_4590", "switch_caller_id", params={"caller_id": "+15550000000"}, owner="customer"))
    fault(con, "acct_medley", "anchoring_4590")
    assert step(c, con, 120, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    f = con.store.get().fixes[-1]
    owned = [n for n in con.fleet.accounts["acct_medley"].caller_ids if not n.startswith("+91")]
    assert f["params"]["caller_id"] in owned
    c.post("/incident/approve")
    assert step(c, con, 60, until=lambda cf: cf.state is S.RESOLVED)
    assert con.store.get().fixes[-1]["status"] == "verified"
    c.post("/demo/reset")


def _say(c, text):
    from app import config
    r = c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {config.PROXY_SHARED_SECRET}"},
               json={"stream": True, "messages": [{"role": "user", "content": text}]})
    assert r.status_code == 200
    return r.text


def test_spoken_request_after_decline_applies_the_fix(run, monkeypatch):
    # Real call 21 Nov: "wait…" was taken as a decline and "can you make a ticket?" then did nothing
    # while the voice LLM promised it would. Now: decline keeps the proposal; a request applies it.
    from app import brain_proxy

    async def fake_stream(messages, turn, t0):
        yield "ok."
    monkeypatch.setattr(brain_proxy, "_groq_stream", fake_stream)
    monkeypatch.setattr(brain_proxy, "CONFIRM_DELAY_S", 0.0)
    monkeypatch.setattr(console_app, "MOCK_APPROVE_AFTER_S", 10_000)  # no auto-yes: approval must come by voice
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 120, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    _say(c, "wait, start again, what is going on?")
    assert con.store.get().state is S.CONFIRM_ACTIONS          # a pause, not a decline
    _say(c, "no, not now")
    assert con.store.get().state is S.IN_CALL                  # declined, proposal kept
    assert con.oncall.proposed_fix(con.store.get())
    txt = _say(c, "okay, go ahead and apply the fix")
    assert "On it" in txt
    assert step(c, con, 60, until=lambda cf: cf.state in (S.VERIFYING, S.RESOLVED))
    c.post("/demo/reset")


def test_ticket_request_files_ticket_and_keeps_fix_pending(run, monkeypatch):
    # Real call 21 Nov: "make a linear ticket" applied the fix instead; tickets are their own action.
    from app import brain_proxy

    async def fake_stream(messages, turn, t0):
        yield "ok."
    monkeypatch.setattr(brain_proxy, "_groq_stream", fake_stream)
    monkeypatch.setattr(console_app, "MOCK_APPROVE_AFTER_S", 10_000)
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 120, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    _say(c, "I think we might have to make a ticket for this")
    time.sleep(1.6)  # fail-first + retry
    cf = con.store.get()
    assert cf.state is S.CONFIRM_ACTIONS and con.oncall.proposed_fix(cf)       # fix still waiting for a yes
    t = [a for a in cf.actions if a.kind == "ticket"]
    assert len(t) == 1 and t[0].status == "done" and t[0].external_id
    c.post("/incident/approve")
    assert step(c, con, 60, until=lambda cf: cf.state is S.RESOLVED)
    time.sleep(1.0)
    assert len([a for a in con.store.get().actions if a.kind == "ticket"]) == 1  # no duplicate ticket
    c.post("/demo/reset")


def test_voice_summary_goes_to_telegram_and_page(run, monkeypatch):
    from app import brain_proxy, screen

    async def fake_stream(messages, turn, t0):
        yield "Sent."
    monkeypatch.setattr(brain_proxy, "_groq_stream", fake_stream)
    monkeypatch.setattr(console_app, "MOCK_APPROVE_AFTER_S", 10_000)
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 120, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    seq = screen.bus.seq
    _say(c, "can you send my telegram a summary of the issue?")
    assert con.notifier.notices and "Northwind Health" in con.notifier.notices[-1] and "Cause:" in con.notifier.notices[-1]
    assert any(e["type"] == "notify" and e["kind"] == "telegram" for e in screen.bus.events if e["seq"] > seq)
    c.post("/demo/reset")


def test_reroute_with_null_via_is_repaired_and_resolves(run):
    # Live run 21 Nov: the model proposed reroute_destination with via=null → fix rejected → never resolved.
    c, con = run(scripted("silent_route", "reroute_destination", params={"to_carrier": "Vi-Delhi", "via": None}))
    fault(con, "acct_medley", "silent_route")
    assert step(c, con, 150, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    f = con.store.get().fixes[-1]
    assert f["params"] == {"to_carrier": "Vi-Delhi", "via": "Jio"}
    c.post("/incident/approve")
    assert step(c, con, 90, until=lambda cf: cf.state is S.RESOLVED)
    c.post("/demo/reset")


def test_incident_report_after_resolution(run):
    from app import report
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 120, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    c.post("/incident/approve")
    assert step(c, con, 90, until=lambda cf: cf.state is S.RESOLVED)
    time.sleep(1.6)
    r = c.get("/public/report").json()
    assert r["status"] == "Resolved" and r["root_cause"]["signature"] == "carrier_reject"
    assert r["impact"]["affected_calls"] > 0 and r["resolution"]["verification"]
    labels = [t["label"] for t in r["timeline"]]
    assert "Detected (suspected)" in labels and "Fix applied" in labels and "Recovery confirmed" in labels
    assert r["prevention"] and r["follow_ups"]["ticket"]
    md = c.get("/public/report.md")
    assert md.status_code == 200 and md.text.startswith("# Incident report") and "## Root cause" in md.text
    assert report.markdown(r).count("## ") >= 5
    c.post("/demo/reset")


def test_voice_loop_in_files_ticket_then_loops_people_in(run, monkeypatch):
    from app import brain_proxy, screen

    async def fake_stream(messages, turn, t0):
        yield "Done."
    monkeypatch.setattr(brain_proxy, "_groq_stream", fake_stream)
    monkeypatch.setattr(console_app, "MOCK_APPROVE_AFTER_S", 10_000)
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 120, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    seq = screen.bus.seq
    _say(c, "can you loop in the carrier team and the account manager?")
    time.sleep(2.5)  # ticket (fail-first + retry) then loop-in
    cf = con.store.get()
    tickets = [a for a in cf.actions if a.kind == "ticket"]
    loops = [a for a in cf.actions if a.kind == "loop_in"]
    assert len(tickets) == 1 and tickets[0].status == "done"
    assert loops and loops[0].status == "done" and loops[0].payload["people"] == ["Rahul Menon", "Neha Kapoor"]
    assert con.ticket.looped == ["Rahul Menon", "Neha Kapoor"]
    assert any(e["type"] == "notify" and "Looped in" in e["title"] for e in screen.bus.events if e["seq"] > seq)
    assert cf.state is S.CONFIRM_ACTIONS  # looping people in never applies the fix
    c.post("/demo/reset")


def test_reroute_destination_comes_from_verification(run):
    # Live run 21 Nov: the model sent to_carrier="Jio" (the via carrier) → "Calls to Jio now go via Jio".
    c, con = run(scripted("silent_route", "reroute_destination", params={"to_carrier": "Jio", "via": "Jio"}))
    fault(con, "acct_medley", "silent_route")
    assert step(c, con, 150, until=lambda cf: cf.state is S.CONFIRM_ACTIONS)
    f = con.store.get().fixes[-1]
    assert f["params"]["to_carrier"] == "Vi-Delhi" and f["params"]["via"] != "TATA"
    assert "percent" in f["spoken_summary"] and f["detailed_summary"] == "Cause found."
    c.post("/demo/reset")


def test_goodbye_hangup_never_hits_a_newer_call(run, monkeypatch):
    # sim 21 Nov: "bye" schedules a hang-up 6 s later; if a new session/call starts in between,
    # the delayed hang-up must not kill the new call.
    from app import brain_proxy

    async def fake_stream(messages, turn, t0):
        yield "Okay."
    monkeypatch.setattr(brain_proxy, "_groq_stream", fake_stream)
    monkeypatch.setattr(brain_proxy, "HANGUP_DELAY_S", 0.3)
    c, con = run(scripted("carrier_reject", "enable_ha"))
    fault(con, "acct_northwind", "carrier_reject")
    assert step(c, con, 80, until=lambda cf: cf.state is S.IN_CALL)
    _say(c, "okay bye, call me later")
    c.portal.call(con.oncall.on_call_ended)                 # that call ends…
    c.portal.call(con.oncall._place_call, "on_demand", "hi")  # …and a new one starts before the delay fires
    time.sleep(0.8)
    a = con.oncall.active(con.store.get())
    assert a is not None and a.purpose == "on_demand" and a.state == "ringing"
    c.post("/demo/reset")


def test_empty_turns_get_a_short_reprompt(run, monkeypatch):
    from app import brain_proxy

    async def fake_stream(messages, turn, t0):
        yield "LLM should not be called."
    monkeypatch.setattr(brain_proxy, "_groq_stream", fake_stream)
    c, con = run(scripted("carrier_reject", "enable_ha"))
    for t in ("   ", "...", ""):
        assert "didn't catch that" in _say(c, t)
    c.post("/demo/reset")
