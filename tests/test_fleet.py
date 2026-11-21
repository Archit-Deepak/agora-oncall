"""FleetTraffic + AccountWatch: each fault's signature, alert timing/targeting, fixes and recovery."""
from datetime import datetime, timedelta

import pytest

from app.detector import is_failure
from app.fleet import FleetTraffic, is_silent
from app.fleet_detect import AccountWatch

T0 = datetime(2026, 11, 19, 10, 0, 0)
FAULT_AT = 60   # s of healthy history before the fault
FIX_AT = 120

# (account, fault, fix, params, expected signal)
CASES = [
    ("acct_northwind", "carrier_reject", "enable_ha", {}, "failure_rate"),
    ("acct_zylo", "cps_burst", "pace_dialer", {}, "failure_rate"),
    ("acct_zylo", "cps_burst", "raise_cps", {}, "failure_rate"),
    ("acct_medley", "silent_route", "reroute_destination", {"to_carrier": "Vi-Delhi", "via": "Jio"}, "silent_rate"),
    ("acct_medley", "anchoring_4590", "switch_caller_id", {}, "failure_rate"),
    ("acct_medley", "anchoring_4590", "move_media_to_india", {}, "failure_rate"),
    ("acct_zylo", "acl_drop", "add_ip_to_acl", {"ip": "34.93.172.19"}, "volume_drop"),
    ("acct_brightline", "pbx_timeout", None, {}, "failure_rate"),
]


def _t(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def _fleet(seed=7) -> FleetTraffic:
    f = FleetTraffic(T0, seed=seed)
    f.start(T0)
    return f


def _healthy(m: dict) -> bool:
    return m["fail_rate"] < 0.08 and m["silent_rate"] < 0.08 and m["volume_ratio"] > 0.8


def test_healthy_fleet_looks_real_and_never_alerts():
    f = _fleet()
    w = AccountWatch()
    for s in range(1, 301, 2):
        assert w.check(f, _t(s)) is None
    calls = f.calls(None, T0, _t(300))
    assert 2100 < len(calls) < 2700  # ~8.2 calls/s across 4 accounts
    assert sum(map(is_failure, calls)) / len(calls) < 0.04
    codes = {c.hangup_cause_code for c in calls}
    assert {3000, 3010, 4410, 6030}.issubset(codes)
    answered = [c for c in calls if c.answer_time is not None]
    assert 0.75 < len(answered) / len(calls) < 0.92
    assert all(c.audio_energy is not None for c in answered)
    assert all(c.audio_energy is None for c in calls if c.answer_time is None)
    assert sum(map(is_silent, answered)) / len(answered) < 0.03
    assert {c.account_id for c in calls} == set(f.accounts)
    assert all(c.trunk_id and c.to_carrier for c in calls)
    assert f.peak_cps("acct_zylo", _t(300), 300) <= f.accounts["acct_zylo"].cps_limit


def test_deterministic_per_seed():
    a, b = _fleet(3), _fleet(3)
    ca, cb = a.calls(None, T0, _t(30)), b.calls(None, T0, _t(30))
    assert [c.call_uuid for c in ca] == [c.call_uuid for c in cb]


@pytest.mark.parametrize("seed", [1, 7, 23])
@pytest.mark.parametrize("aid,fault,fix,params,signal", CASES)
def test_fault_alerts_right_account_then_fix_recovers(seed, aid, fault, fix, params, signal):
    f = _fleet(seed)
    w = AccountWatch()
    fired, others = None, []
    for s in range(1, FIX_AT + 1):
        if s == FAULT_AT:
            f.set_fault(aid, fault, _t(s))
        a = w.check(f, _t(s))
        while a is not None:
            if a.account_id == aid and fired is None:
                fired = (s - FAULT_AT, a)
            elif a.account_id != aid:
                others.append(a.account_id)
            a = w.check(f, _t(s))
    assert others == []
    assert fired is not None, "no alert for the faulted account"
    secs, alert = fired
    assert 0 <= secs <= 30
    assert alert.signal == signal
    assert alert.customer_name == f.accounts[aid].name
    assert alert.signal_value is not None
    assert f.faults()[aid]["fault"] == fault

    if fix:
        res = f.apply_fix(aid, fix, params, _t(FIX_AT))
        assert res["ok"], res
        assert res["cost"]
        assert f.accounts[aid].change_log[-1]["by"] == "plivo-oncall-agent"
        assert f.faults()[aid]["mitigated"]
    else:
        res = f.apply_fix(aid, "notify_customer", {}, _t(FIX_AT))
        assert res["ok"]
        assert not _healthy(f.window_metrics(aid, _t(FIX_AT + 30), 30))  # nothing Plivo-side changed
        f.set_fault(aid, None, _t(FIX_AT + 30))  # operator clicks "all pass"
    start = FIX_AT if fix else FIX_AT + 30
    assert _healthy(f.window_metrics(aid, _t(start + 30), 30))  # first 30 s after the fix is clean
    # and after the full 60 s window the watch would be quiet again for that account
    w.rearm(aid)
    assert w.signals(f, aid, _t(start + 61)) == []


def test_carrier_reject_signature_and_ha_backup_number():
    f = _fleet()
    f.set_fault("acct_northwind", "carrier_reject", _t(60))
    calls = f.calls("acct_northwind", _t(100), _t(150))
    tata = [c for c in calls if c.carrier_route == "TATA"]
    rest = [c for c in calls if c.carrier_route != "TATA"]
    assert sum(map(is_failure, tata)) / len(tata) > 0.95
    assert sum(c.hangup_cause_code == 5350 for c in tata) / len(tata) > 0.75
    assert sum(map(is_failure, rest)) / len(rest) < 0.15  # retry-overflow blips decayed
    assert {c.hangup_source for c in tata if c.hangup_cause_code == 5350} == {"Carrier"}
    assert f.apply_fix("acct_northwind", "enable_ha", None, _t(150))["ok"]
    after = f.calls("acct_northwind", _t(151), _t(200))
    backup = f.accounts["acct_northwind"].ha_backup_number
    assert any(c.from_number == backup for c in after)
    assert sum(map(is_failure, after)) / len(after) < 0.06


def test_cps_burst_rejects_over_limit_and_raise_cps_cost():
    f = _fleet()
    f.set_fault("acct_zylo", "cps_burst", _t(60))
    calls = f.calls("acct_zylo", _t(60), _t(120))
    n5180 = [c for c in calls if c.hangup_cause_code == 5180]
    assert len(n5180) / len(calls) > 0.4
    assert {c.sip_response_code for c in n5180} == {503}
    assert {c.hangup_source for c in n5180} == {"Zentrunk"}
    assert f.peak_cps("acct_zylo", _t(120), 60) >= 3 * 4
    res = f.apply_fix("acct_zylo", "raise_cps", None, _t(120))
    assert res["ok"] and "₹" in res["cost"] and "concurrency" in res["cost"]
    assert f.accounts["acct_zylo"].cps_limit >= 12


def test_silent_route_keeps_fail_rate_healthy():
    f = _fleet()
    f.set_fault("acct_medley", "silent_route", _t(60))
    m = f.window_metrics("acct_medley", _t(120), 60)
    assert m["fail_rate"] < 0.05
    assert m["silent_rate"] > 0.15
    silent = [c for c in m["_calls"] if c.answer_time is not None and is_silent(c)]
    route = [c for c in silent if c.carrier_route == "TATA" and c.to_carrier == "Vi-Delhi"]
    assert len(route) / len(silent) > 0.85
    assert all(8 <= c.call_duration <= 20 for c in route)
    assert all(c.hangup_cause_code in (3000, 3010) for c in route)


def test_acl_drop_volume_and_edge_log():
    f = _fleet()
    f.set_fault("acct_zylo", "acl_drop", _t(60))
    m = f.window_metrics("acct_zylo", _t(120), 60)
    assert m["volume_ratio"] < 0.3
    assert m["fail_rate"] < 0.1
    edge = f.edge_log("acct_zylo", _t(60), _t(120))
    new_ip = [e for e in edge if e["peer_ip"] == "34.93.172.19"]
    assert len(new_ip) > 80
    assert all("not in trunk IP ACL" in e["msg"] for e in new_ip)
    assert any("migrated SBC/egress" in c["change"] for c in f.accounts["acct_zylo"].change_log)
    # background scanner noise exists on healthy trunks too
    assert f.edge_log("acct_northwind", T0, _t(120))


def test_anchoring_only_india_to_india_fails():
    f = _fleet()
    f.set_fault("acct_medley", "anchoring_4590", _t(60))
    calls = f.calls("acct_medley", _t(61), _t(150))
    dom = [c for c in calls if c.to_number.startswith("+91")]
    intl = [c for c in calls if not c.to_number.startswith("+91")]
    assert sum(c.hangup_cause_code == 4590 for c in dom) / len(dom) > 0.95
    assert intl and not any(c.hangup_cause_code == 4590 for c in intl)
    assert f.accounts["acct_medley"].media_region == "us-east-1"
    f.set_fault("acct_medley", None, _t(150))
    assert f.accounts["acct_medley"].media_region == "ap-south-1"  # clearing restores


def test_pbx_timeout_signature():
    f = _fleet()
    f.set_fault("acct_brightline", "pbx_timeout", _t(60))
    calls = f.calls("acct_brightline", _t(61), _t(120))
    t = [c for c in calls if c.hangup_cause_code == 4170]
    assert len(t) / len(calls) > 0.75
    assert {c.sip_response_code for c in t} == {408} and {c.call_direction for c in calls} == {"inbound"}
    assert any(e["method"] == "OPTIONS" for e in f.edge_log("acct_brightline", _t(60), _t(120)))


def test_invalid_fixes_rejected_with_reason():
    f = _fleet()
    f.calls_through(30)
    now = _t(30)
    bad = [
        ("acct_zylo", "enable_ha", {}),                       # already enabled
        ("acct_brightline", "enable_ha", {}),                 # inbound
        ("acct_zylo", "raise_cps", {"cps_limit": 3}),         # not higher
        ("acct_zylo", "raise_cps", {"cps_limit": 500}),       # beyond self-serve
        ("acct_medley", "switch_caller_id", {"caller_id": "+912240088120"}),  # Indian caller ID
        ("acct_medley", "switch_caller_id", {"caller_id": "12345"}),          # not E.164
        ("acct_medley", "move_media_to_india", {}),           # already in India
        ("acct_zylo", "add_ip_to_acl", {"ip": "34.93.88.10"}),  # already allowed
        ("acct_zylo", "add_ip_to_acl", {"ip": "not-an-ip"}),
        ("acct_zylo", "add_ip_to_acl", {}),
        ("acct_medley", "reroute_destination", {"to_carrier": "Mars"}),
        ("acct_medley", "reroute_destination", {"via": "Verizon"}),
        ("acct_zylo", "pace_dialer", {"max_cps": 9}),          # above CPS limit
        ("acct_zylo", "reboot_carrier", {}),                  # unknown fix
        ("acct_nobody", "enable_ha", {}),
    ]
    n_changes = {a: len(p.change_log) for a, p in f.accounts.items()}
    for aid, fix, params in bad:
        res = f.apply_fix(aid, fix, params, now)
        assert res["ok"] is False, (aid, fix, res)
        assert res["message"]
    assert {a: len(p.change_log) for a, p in f.accounts.items()} == n_changes


def test_mismatched_fix_applies_but_does_not_recover():
    f = _fleet()
    f.set_fault("acct_northwind", "carrier_reject", _t(60))
    res = f.apply_fix("acct_northwind", "raise_cps", {"add": 2}, _t(90))
    assert res["ok"]
    assert f.window_metrics("acct_northwind", _t(130), 30)["fail_rate"] > 0.3


def test_set_fault_validation():
    f = _fleet()
    with pytest.raises(ValueError):
        f.set_fault("acct_brightline", "cps_burst", _t(1))
    with pytest.raises(ValueError):
        f.set_fault("acct_northwind", "pbx_timeout", _t(1))
    with pytest.raises(ValueError):
        f.set_fault("acct_northwind", "meteor", _t(1))


def test_stats_and_compat_views():
    f = _fleet()
    f.set_fault("acct_zylo", "acl_drop", _t(60))
    st = f.stats(_t(120))
    assert st["running"] is True and set(st["accounts"]) == set(f.accounts)
    z = st["accounts"]["acct_zylo"]
    assert z["fault"] == "acl_drop" and z["volume_ratio"] < 0.3 and z["at_risk_inr_per_hour"] > 0
    assert len(z["spark"]) == 36
    assert st["accounts"]["acct_northwind"]["fault"] is None
    allc = f.calls_through(120)
    assert allc == sorted(allc, key=lambda c: c.initiation_time)
    f.clear_all(_t(120))
    assert f.faults() == {}
