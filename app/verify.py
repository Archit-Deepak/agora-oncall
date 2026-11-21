"""Code-side verification of a diagnosis: "the LLM proposes, code verifies".

Each signature has a check computed straight from the raw CDRs / edge logs (not from the
LLM's words). The agent may only say "found it" once its conclusion passes here. The same
checks double as a rules-only fallback diagnosis if the LLM is down (`best_guess`).

Codes are Plivo Zentrunk hangup codes (see app/kb/*.md).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

C_CARRIER_REJECT = 5350
C_CPS = 5180
C_PBX_TIMEOUT = 4170
C_ANCHORING = 4590
SILENT_ENERGY = 5.0


@dataclass
class Check:
    ok: bool
    score: float  # 0..1 strength of the signature (shown as the evidence score)
    reason: str
    detail: dict = field(default_factory=dict)


def _is_failure(c) -> bool:
    from app.detector import is_failure
    return is_failure(c)


def _window(fleet, aid: str, now: datetime, window_s: int):
    return fleet.calls(aid, now - timedelta(seconds=window_s), now)


def carrier_reject(fleet, aid, now, window_s=30) -> Check:
    calls = _window(fleet, aid, now, window_s)
    fails = [c for c in calls if _is_failure(c)]
    if len(fails) < 6:
        return Check(False, 0.0, f"only {len(fails)} failures in the window")
    rej = [c for c in fails if c.hangup_cause_code == C_CARRIER_REJECT]
    by_carrier = Counter(c.carrier_route for c in rej)
    if not by_carrier:
        return Check(False, 0.0, "no 5350 carrier rejections")
    carrier, n = by_carrier.most_common(1)[0]
    on_carrier = [c for c in calls if c.carrier_route == carrier]
    carrier_fail = sum(map(_is_failure, on_carrier)) / max(1, len(on_carrier))
    share = n / len(fails)
    ok = share >= 0.6 and carrier_fail >= 0.5
    return Check(ok, round(share, 3),
                 f"{int(share*100)}% of failures are 5350 rejects on {carrier}; {carrier} failing {int(carrier_fail*100)}%",
                 {"carrier": carrier, "share_of_failures": round(share, 3), "carrier_fail_rate": round(carrier_fail, 3)})


def cps_burst(fleet, aid, now, window_s=30) -> Check:
    calls = _window(fleet, aid, now, window_s)
    fails = [c for c in calls if _is_failure(c)]
    n = sum(c.hangup_cause_code == C_CPS for c in calls)
    if not calls or not fails:
        return Check(False, 0.0, "no failures")
    share_f = n / len(fails)
    share_all = n / len(calls)
    ok = share_f >= 0.6 and share_all >= 0.08
    return Check(ok, round(share_f, 3), f"{n} calls rejected 5180 cps_limit_reached ({int(share_f*100)}% of failures)",
                 {"count_5180": n, "share_of_failures": round(share_f, 3)})


def silent_route(fleet, aid, now, window_s=60) -> Check:
    calls = _window(fleet, aid, now, window_s)
    answered = [c for c in calls if c.audio_energy is not None]
    if len(answered) < 15:
        return Check(False, 0.0, "not enough answered calls with audio metrics")
    routes: dict[tuple, list] = {}
    for c in answered:
        routes.setdefault((c.carrier_route, c.to_carrier), []).append(c)
    best, best_rate = None, 0.0
    for key, cs in routes.items():
        if len(cs) < 8:
            continue
        rate = sum(c.audio_energy <= SILENT_ENERGY for c in cs) / len(cs)
        if rate > best_rate:
            best, best_rate = key, rate
    fail_rate = sum(map(_is_failure, calls)) / max(1, len(calls))
    ok = best is not None and best_rate >= 0.5 and fail_rate < 0.15
    route = f"{best[0]} → {best[1]}" if best else "none"
    return Check(ok, round(best_rate, 3),
                 f"{int(best_rate*100)}% of answered calls on {route} are silent; overall failure rate {int(fail_rate*100)}%",
                 {"carrier": best[0] if best else None, "to_carrier": best[1] if best else None,
                  "silent_rate": round(best_rate, 3), "fail_rate": round(fail_rate, 3)})


def pbx_timeout(fleet, aid, now, window_s=30) -> Check:
    calls = _window(fleet, aid, now, window_s)
    fails = [c for c in calls if _is_failure(c)]
    if len(fails) < 8:
        return Check(False, 0.0, f"only {len(fails)} failures")
    n = sum(c.hangup_cause_code == C_PBX_TIMEOUT for c in fails)
    share = n / len(fails)
    return Check(share >= 0.6, round(share, 3), f"{int(share*100)}% of failures are 4170 request_timeout_customer",
                 {"count_4170": n, "share_of_failures": round(share, 3)})


def anchoring_4590(fleet, aid, now, window_s=30) -> Check:
    calls = _window(fleet, aid, now, window_s)
    fails = [c for c in calls if _is_failure(c)]
    if len(fails) < 8:
        return Check(False, 0.0, f"only {len(fails)} failures")
    hits = [c for c in fails if c.hangup_cause_code == C_ANCHORING]
    share = len(hits) / len(fails)
    domestic = sum(c.from_number.startswith("+91") and c.to_number.startswith("+91") for c in hits)
    ok = share >= 0.6 and domestic >= 0.9 * max(1, len(hits))
    return Check(ok, round(share, 3), f"{int(share*100)}% of failures are 4590 domestic_anchored_terms_not_met (+91 → +91)",
                 {"count_4590": len(hits), "share_of_failures": round(share, 3)})


def acl_drop(fleet, aid, now, window_s=30) -> Check:
    calls = _window(fleet, aid, now, window_s)
    expected = fleet.expected_rate(aid) * window_s
    ratio = len(calls) / expected if expected else 1.0
    rejects = fleet.edge_log(aid, now - timedelta(seconds=window_s), now)
    ok = ratio <= 0.4 and len(rejects) >= 10
    return Check(ok, round(1 - min(1.0, ratio), 3),
                 f"volume at {int(ratio*100)}% of expected; {len(rejects)} INVITEs rejected at the trunk edge",
                 {"volume_ratio": round(ratio, 3), "edge_rejects": len(rejects)})


CHECKS = {
    "carrier_reject": carrier_reject,
    "cps_burst": cps_burst,
    "silent_route": silent_route,
    "pbx_timeout": pbx_timeout,
    "anchoring_4590": anchoring_4590,
    "acl_drop": acl_drop,
}

# Canonical owner + fix per signature (the agent may pick an alternative fix in `conclude`).
PLAYBOOK = {
    "carrier_reject": {"owner": "carrier", "fix": "enable_ha",
                       "cost": "one extra India local number rental (HA backup number)",
                       "spoken_cost": "one extra local number rental a month"},
    "cps_burst": {"owner": "customer", "fix": "raise_cps",
                  "cost": "+25 concurrency (+1 CPS) ≈ ₹1,200/month per CPS; or pace the dialer for free",
                  "spoken_cost": "about twelve hundred rupees a month per extra CPS, or they can pace the dialer for free"},
    "silent_route": {"owner": "carrier", "fix": "reroute_destination", "cost": "none (routing change)"},
    "pbx_timeout": {"owner": "customer", "fix": "notify_customer", "cost": "none (customer action)"},
    "anchoring_4590": {"owner": "customer", "fix": "switch_caller_id", "cost": "none"},
    "acl_drop": {"owner": "customer", "fix": "add_ip_to_acl", "cost": "none"},
}
# Spoken cost for the non-default alternatives the agent may pick.
FIX_SPOKEN_COST = {"pace_dialer": "nothing, it's a setting on their dialer",
                   "move_media_to_india": "nothing on our side; they run their media in Mumbai"}


def phone_ready(text: str | None, max_words: int = 32) -> bool:
    """Is an LLM-written sentence fit to be spoken as-is? Short, no code-ish tokens."""
    if not text:
        return False
    t = text.strip()
    return (len(t.split()) <= max_words and not any(ch in t for ch in "_=[]{}<>|~→")
            and not any(w.isupper() and len(w) > 4 for w in t.replace(",", " ").split()))


# Fixes that genuinely address each signature (the agent may choose among them).
VALID_FIXES = {
    "carrier_reject": {"enable_ha"},
    "cps_burst": {"raise_cps", "pace_dialer"},
    "silent_route": {"reroute_destination"},
    "pbx_timeout": {"notify_customer"},
    "anchoring_4590": {"switch_caller_id", "move_media_to_india"},
    "acl_drop": {"add_ip_to_acl"},
}

# The hangup code that marks each failure signature (None: not a failure-code signature).
SIGNATURE_CODE = {"carrier_reject": C_CARRIER_REJECT, "cps_burst": C_CPS,
                  "pbx_timeout": C_PBX_TIMEOUT, "anchoring_4590": C_ANCHORING}


def top_rejected_ip(fleet, aid: str, now: datetime, window_s: int = 60) -> str | None:
    """The source IP with the most trunk-ACL rejections recently (the customer's new egress IP)."""
    ips = [e.get("peer_ip") for e in fleet.edge_log(aid, now - timedelta(seconds=window_s), now)
           if "ACL" in str(e.get("reason", "")) and e.get("peer_ip")]
    return max(set(ips), key=ips.count) if ips else None


def signature_cleared(signature: str, fleet, aid: str, now: datetime, window_s: int = 15) -> bool:
    """True when the signature's own hangup code has (almost) stopped appearing.

    Recovery is judged on the signature, not only on the overall failure rate: on a low-volume
    account a 15 s window holds ~30 calls, so two ordinary background failures already read as
    6-7% and a fixed fail-rate bar would call a real recovery "no effect"."""
    code = SIGNATURE_CODE.get(signature)
    if code is None:
        return False
    calls = _window(fleet, aid, now, window_s)
    if not calls:
        return False
    return sum(c.hangup_cause_code == code for c in calls) <= max(1, int(0.02 * len(calls)))


def check(signature: str, fleet, aid: str, now: datetime) -> Check:
    fn = CHECKS.get(signature)
    if fn is None:
        return Check(False, 0.0, f"unknown signature {signature!r}; use one of {sorted(CHECKS)}")
    return fn(fleet, aid, now)


def best_guess(fleet, aid: str, now: datetime) -> tuple[str, Check] | None:
    """Rules-only diagnosis (fallback if the LLM is unavailable)."""
    results = [(sig, fn(fleet, aid, now)) for sig, fn in CHECKS.items()]
    passing = [r for r in results if r[1].ok]
    if not passing:
        return None
    return max(passing, key=lambda r: r[1].score)


def spoken(signature: str, chk: Check, account_name: str) -> str:
    """Plain-words line for the phone (used when the rules engine concludes, or the LLM gave none)."""
    d = chk.detail
    pct = lambda x: f"{int(round((x or 0) * 100))} percent"  # noqa: E731
    return {
        "carrier_reject": f"{d.get('carrier')} is refusing {account_name}'s calls: {pct(d.get('share_of_failures'))} "
                          f"of the failures are {d.get('carrier')} service-unavailable rejects, and the other carriers are fine.",
        "cps_burst": f"{account_name}'s dialer is bursting past their calls-per-second limit; "
                     f"{d.get('count_5180')} calls were rejected for CPS in the last half minute.",
        "silent_route": f"Calls from {d.get('carrier')} to {d.get('to_carrier')} are connecting but silent: "
                        f"{pct(d.get('silent_rate'))} of them have no audio, while the failure rate looks normal.",
        "pbx_timeout": f"{account_name}'s own PBX isn't answering: {pct(d.get('share_of_failures'))} of the failures "
                       "are request timeouts on their side.",
        "anchoring_4590": f"India-to-India calls are being blocked by the domestic anchoring rule: "
                          f"{pct(d.get('share_of_failures'))} of failures are 4590s, right after their media moved out of India.",
        "acl_drop": f"{account_name}'s calls are vanishing before they reach us: volume is at "
                    f"{pct(d.get('volume_ratio'))} of normal and the trunk is rejecting INVITEs from an unknown IP.",
    }.get(signature, chk.reason)
