"""Investigator tools: signatures visible, compact JSON, fast, bad args handled."""
import json
import time
from datetime import datetime, timedelta

import pytest

from app.fleet import FleetTraffic
from app.tools import TOOL_SPECS, TOOLS, ToolContext, run_tool

T0 = datetime(2026, 11, 19, 10, 0, 0)
NOW_S = 900  # 15 minutes of history; fault starts at 14:00


def _ctx(aid=None, fault=None) -> ToolContext:
    f = FleetTraffic(T0)
    f.start(T0)
    if fault:
        f.calls_through(NOW_S - 60)
        f.set_fault(aid, fault, T0 + timedelta(seconds=NOW_S - 60))
    now = T0 + timedelta(seconds=NOW_S)
    f.calls_through(NOW_S)
    return ToolContext(f, now)


def _run(ctx, name, **args):
    out = run_tool(name, args, ctx)
    assert "error" not in out, out
    s = json.dumps(out)
    assert len(s) < 4000
    return out


def test_specs_match_tools():
    assert {s["function"]["name"] for s in TOOL_SPECS} == set(TOOLS)
    for s in TOOL_SPECS:
        assert s["type"] == "function"
        fn = s["function"]
        assert len(fn["description"]) > 30
        assert fn["parameters"]["type"] == "object"
        assert set(fn["parameters"]["required"]) <= set(fn["parameters"]["properties"])
    json.dumps(TOOL_SPECS)


def test_carrier_reject_visible():
    ctx = _ctx("acct_northwind", "carrier_reject")
    ov = _run(ctx, "overview", account_id="acct_northwind")
    assert ov["fail_rate"] > 0.3 and ov["previous_window"]["fail_rate"] < 0.06
    assert ov["top_hangup_causes"][0]["cause"].startswith("5350")
    bd = _run(ctx, "breakdown", account_id="acct_northwind", by="carrier")
    top = max(bd["groups"], key=lambda g: g["fail_rate"])
    assert top["key"] == "TATA" and top["fail_rate"] > 0.9
    prof = _run(ctx, "account_profile", account_id="acct_northwind")
    assert prof["ha_enabled"] is False
    kb = _run(ctx, "search_kb", query="HA numbers carrier failover India backup number")
    assert any(r["source"] == "ha_numbers.md" for r in kb["results"])


def test_cps_burst_visible():
    ctx = _ctx("acct_zylo", "cps_burst")
    cp = _run(ctx, "cps_profile", account_id="acct_zylo")
    assert cp["peak_cps"] >= 3 * cp["cps_limit"] and cp["count_5180"] > 50
    assert "bursts" in cp["burst_pattern"] and len(cp["last_30s_per_second"]) == 30


def test_silent_route_visible():
    ctx = _ctx("acct_medley", "silent_route")
    ov = _run(ctx, "overview", account_id="acct_medley")
    assert ov["fail_rate"] < 0.05 and ov["silent_rate"] > 0.15
    ar = _run(ctx, "audio_by_route", account_id="acct_medley")
    worst = ar["routes"][0]
    assert (worst["carrier"], worst["to_carrier"]) == ("TATA", "Vi-Delhi")
    sc = _run(ctx, "sample_calls", account_id="acct_medley", silent=True, n=3)
    assert sc["calls"] and all(c["audio_energy"] <= 5 for c in sc["calls"])


def test_pbx_timeout_visible():
    ctx = _ctx("acct_brightline", "pbx_timeout")
    bd = _run(ctx, "breakdown", account_id="acct_brightline", by="hangup_cause", only_failures=True)
    assert bd["groups"][0]["key"].startswith("4170")
    el = _run(ctx, "edge_log", account_id="acct_brightline")
    assert el["groups"][0]["event"] == "egress_timeout"


def test_anchoring_visible():
    ctx = _ctx("acct_medley", "anchoring_4590")
    bd = _run(ctx, "breakdown", account_id="acct_medley", by="to_carrier")
    intl = [g for g in bd["groups"] if g["key"] in ("US-Mobile", "UK-Mobile")]
    dom = [g for g in bd["groups"] if g["key"] not in ("US-Mobile", "UK-Mobile")]
    assert all(g["fail_rate"] < 0.2 for g in intl) and all(g["fail_rate"] > 0.8 for g in dom)
    prof = _run(ctx, "account_profile", account_id="acct_medley")
    assert prof["media_region"] == "us-east-1"
    assert any("us-east-1" in c["change"] for c in prof["change_log_24h"])


def test_acl_drop_visible():
    ctx = _ctx("acct_zylo", "acl_drop")
    vt = _run(ctx, "volume_trend", account_id="acct_zylo", minutes=5)
    assert vt["minutes"][-1]["ratio"] < 0.3 and vt["minutes"][0]["ratio"] > 0.8
    el = _run(ctx, "edge_log", account_id="acct_zylo")
    g = el["groups"][0]
    assert g["peer_ip"].startswith("34.93.") and g["count"] > 50 and g["in_trunk_acl"] is False
    assert len(el["groups"]) > 1  # scanner noise is present as a decoy


def test_all_tools_fast_on_15_minutes():
    ctx = _ctx("acct_northwind", "carrier_reject")
    assert len(ctx.fleet.calls(None, T0, ctx.now)) > 6500
    calls = [("overview", {}), ("breakdown", {"by": "to_carrier"}), ("cps_profile", {"window_s": 900}),
             ("audio_by_route", {"window_s": 900}), ("volume_trend", {"minutes": 15}),
             ("sample_calls", {"carrier": "Jio", "n": 10}), ("edge_log", {"window_s": 900}),
             ("account_profile", {}), ("past_incidents", {})]
    for aid in ctx.fleet.accounts:
        for name, args in calls:
            t = time.perf_counter()
            _run(ctx, name, account_id=aid, **args)
            assert (time.perf_counter() - t) < 0.05, name
    t = time.perf_counter()
    _run(ctx, "search_kb", query="silent calls Vi-Delhi")
    assert (time.perf_counter() - t) < 0.05


@pytest.mark.parametrize("name,args", [
    ("nope", {}),
    ("overview", {}),
    ("overview", {"account_id": "acct_ghost"}),
    ("overview", {"account_id": "acct_zylo", "window_s": 99999}),
    ("overview", {"account_id": "acct_zylo", "window_s": "abc"}),
    ("breakdown", {"account_id": "acct_zylo", "by": "colour"}),
    ("breakdown", {"account_id": "acct_zylo", "by": "carrier", "bogus": 1}),
    ("volume_trend", {"account_id": "acct_zylo", "minutes": 99}),
    ("sample_calls", {"account_id": "acct_zylo", "silent": "maybe"}),
    ("search_kb", {"query": ""}),
    ("overview", "{not json"),
])
def test_run_tool_bad_args(name, args):
    out = run_tool(name, args, _ctx())
    assert set(out) == {"error"} and out["error"]


def test_run_tool_accepts_json_string_and_coerces():
    ctx = _ctx()
    out = run_tool("breakdown", '{"account_id": "acct_zylo", "by": "carrier", "window_s": "30", "only_failures": "false"}', ctx)
    assert "error" not in out and out["window_s"] == 30
