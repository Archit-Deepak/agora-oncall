"""Voice-driven screen: gate, call detail (SIP ladder + story), resolving actions on real fleet data."""
import asyncio
from datetime import datetime, timedelta

from app import screen
from app.fleet import FleetTraffic


def test_gate():
    assert screen.wants_screen("Hey, can you open up one of those call logs and show me what's going on?")
    assert screen.wants_screen("go back")
    assert screen.wants_screen("compare the carriers")
    assert not screen.wants_screen("whose fault is it?")
    assert not screen.wants_screen("yes please")


def _fleet(aid, fault):
    t0 = datetime(2026, 11, 21, 10)
    fl = FleetTraffic(anchor=t0, seed=7)
    fl.start(t0)
    now = t0 + timedelta(seconds=60)
    fl.calls(aid, now - timedelta(seconds=1), now)
    fl.set_fault(aid, fault, now=now)
    return fl, now + timedelta(seconds=40)


def test_open_failed_call_shows_carrier_503_ladder():
    fl, now = _fleet("acct_northwind", "carrier_reject")
    ev, facts = screen.resolve({"name": "open_call", "args": {"kind": "failed", "carrier": "TATA"}},
                               fl, "acct_northwind", now, "failure_rate", "open one of those")
    d = ev["call"]
    assert ev["type"] == "open_call" and d["carrier"] == "TATA" and d["code"] == 5350
    msgs = [r["msg"] for r in d["ladder"]]
    assert msgs[0] == "INVITE" and any(m.startswith("503") for m in msgs)
    assert "refused" in d["story"] and "TATA" in facts
    # "another one" gives a different call
    ev2, _ = screen.resolve({"name": "open_call", "args": {"kind": "failed", "which": "another"}},
                            fl, "acct_northwind", now, "failure_rate", "another one")
    assert ev2["call"]["call_uuid"] != d["call_uuid"]


def test_silent_incident_opens_a_silent_call_by_default():
    fl, now = _fleet("acct_medley", "silent_route")
    ev, _ = screen.resolve({"name": "open_call", "args": {}}, fl, "acct_medley", now, "silent_rate", "open a call")
    d = ev["call"]
    assert d["silent"] and d["answered"] and any("silence" in r["msg"] for r in d["ladder"])


def test_cps_and_4590_never_reach_a_carrier():
    for aid, fault, code in (("acct_zylo", "cps_burst", 5180), ("acct_medley", "anchoring_4590", 4590)):
        fl, now = _fleet(aid, fault)
        ev, _ = screen.resolve({"name": "open_call", "args": {"kind": "failed", "code": code}}, fl, aid, now,
                               "failure_rate", "x")
        assert ev["call"]["code"] == code
        assert not any(r["to"] in ("TATA", "Airtel", "Jio") for r in ev["call"]["ladder"])


def test_chart_and_filter():
    fl, now = _fleet("acct_northwind", "carrier_reject")
    ev, facts = screen.resolve({"name": "show_chart", "args": {"by": "carrier"}}, fl, "acct_northwind", now,
                               "failure_rate", "compare carriers")
    assert ev["bars"][0]["label"] == "TATA" and ev["bars"][0]["rate"] > 0.5 and "TATA" in facts
    ev, facts = screen.resolve({"name": "filter_logs", "args": {"carrier": "TATA", "status": "failed"}}, fl,
                               "acct_northwind", now, "failure_rate", "only tata")
    assert ev["filter"] == {"carrier": "TATA", "status": "failed"} and "failed" in facts


def test_bus_publish_and_wait():
    async def go():
        b = screen.ScreenBus()
        waiter = asyncio.create_task(b.wait_after(0, timeout=2))
        await asyncio.sleep(0.01)
        await b.publish({"type": "close"})
        return await waiter
    evs = asyncio.run(go())
    assert evs and evs[0]["type"] == "close" and evs[0]["seq"] == 1


def test_quick_path_covers_common_phrasings():
    q = screen.quick
    assert q("Hey, can you open up one of those call logs and show me what's going on?")["name"] == "open_call"
    assert q("show me another one")["args"]["which"] == "another"
    assert q("open a TATA call")["args"]["carrier"] == "TATA"
    assert q("compare the carriers")["name"] == "show_chart"
    assert q("show me only TATA")["name"] == "filter_logs"
    assert q("okay go back")["name"] == "close_panel"
    assert q("can you show me the investigator") is None  # unusual → LLM planner


def test_chart_says_not_a_carrier_problem_when_all_equal():
    fl, now = _fleet("acct_zylo", "cps_burst")
    _, facts = screen.resolve({"name": "show_chart", "args": {"by": "carrier"}}, fl, "acct_zylo", now,
                              "failure_rate", "compare carriers")
    assert "NOT a carrier problem" in facts
    _, facts = screen.resolve({"name": "filter_logs", "args": {"carrier": "TATA"}}, fl, "acct_zylo", now,
                              "failure_rate", "only tata")
    assert "cps limit reached (raised by Zentrunk)" in facts


def test_chart_conclusions_by_signature():
    for aid, fault, expect in (("acct_northwind", "carrier_reject", "TATA stands out"),
                               ("acct_medley", "anchoring_4590", "NOT a carrier problem"),
                               ("acct_zylo", "cps_burst", "NOT a carrier problem")):
        fl, now = _fleet(aid, fault)
        _, facts = screen.resolve({"name": "show_chart", "args": {"by": "carrier"}}, fl, aid, now, "failure_rate", "x")
        assert expect in facts, (fault, facts)


def test_volume_incident_chart_shows_received_vs_normal():
    fl, now = _fleet("acct_zylo", "acl_drop")
    ev, facts = screen.resolve({"name": "show_chart", "args": {"by": "carrier"}}, fl, "acct_zylo", now,
                               "volume_drop", "compare carriers")
    assert ev["metric"] == "volume" and all(b["rate"] < 0.4 for b in ev["bars"])
    assert "before they reach Plivo" in ev["conclusion"]


def test_quick_filter_phrasing_from_the_real_call():
    q = screen.quick
    a = q("I'm a bit away from my screen. Can you, like, show me the. Call logs filter with only the failed calls?")
    assert a["name"] == "filter_logs" and a["args"]["status"] == "failed"
    a = q("can you just show all the routes from Jio?")
    assert a["name"] == "filter_logs" and a["args"]["carrier"] == "Jio"


def test_flow_map_locates_each_failure():
    cases = [("acct_northwind", "carrier_reject", "carrier"), ("acct_zylo", "cps_burst", "trunk"),
             ("acct_medley", "anchoring_4590", "trunk"), ("acct_zylo", "acl_drop", "acl"),
             ("acct_medley", "silent_route", "media")]
    for aid, fault, where in cases:
        fl, now = _fleet(aid, fault)
        m = screen.flow_map(fl, aid, now)
        assert m["where"] == where, (fault, m["story"])
    assert screen.quick("Okay, can you just show me their call flow, how it is looking?")["name"] == "show_flow"
    fl, now = _fleet("acct_northwind", "carrier_reject")
    ev, facts = screen.resolve({"name": "show_flow", "args": {}}, fl, "acct_northwind", now, "failure_rate", "x")
    assert ev["type"] == "flow" and "Leg B breaks at TATA" in facts


def test_report_quick_path():
    assert screen.quick("show me the incident report")["name"] == "show_report"
    assert screen.quick("can you pull up the postmortem")["name"] == "show_report"


def test_gate_lets_every_quick_phrase_through():
    for t in ["compair the carriers", "show me there call flow", "open won of those call logs", "go back"]:
        assert screen.wants_screen(t), t


def test_open_call_on_acl_incident_shows_flow_instead():
    fl, now = _fleet("acct_zylo", "acl_drop")
    ev, facts = screen.resolve({"name": "open_call", "args": {"kind": "failed"}}, fl, "acct_zylo", now, "volume_drop", "x")
    assert ev["type"] in ("flow", "open_call")
    if ev["type"] == "flow":
        assert "no call log" in facts
