"""End-to-end run with the LIVE investigator LLM chain and a MOCK phone (no calls, no Plivo cost).

    .venv/bin/python scripts/e2e_live.py acct_medley anchoring_4590
    .venv/bin/python scripts/e2e_live.py all          # all six signatures, in parallel

Warm-starts the fleet with 60 s of healthy history, injects the fault, then runs the real
OnCall loop in real time: suspect → confirm → (mock) call → investigator → found it →
(mock) approve → fix → verify. Prints the state timeline, the investigator's steps, and the
outcome. Exit code 0 only if the incident reached RESOLVED (or TELEGRAM_SENT with a verified
diagnosis for the Telegram-tier account).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("INCIDENT_LIVE", "0")

PAIRS = [("acct_northwind", "carrier_reject"), ("acct_zylo", "cps_burst"), ("acct_medley", "silent_route"),
         ("acct_brightline", "pbx_timeout"), ("acct_medley", "anchoring_4590"), ("acct_zylo", "acl_drop")]
EXPECT = {"carrier_reject": "carrier_reject", "cps_burst": "cps_burst", "silent_route": "silent_route",
          "pbx_timeout": "pbx_timeout", "anchoring_4590": "anchoring_4590", "acl_drop": "acl_drop"}


async def run(aid: str, fault: str, limit_s: float = 200) -> bool:
    from app import console_app
    from app.models import IncidentState as S
    console_app.STATE_DIR = Path(tempfile.mkdtemp(prefix="e2e-"))
    con = console_app.Console(live=False)
    con.trigger = datetime.now() - timedelta(seconds=60)
    con.oncall.trigger = con.trigger
    con.fleet = type(con.fleet)(con.trigger)
    con.oncall.fleet = con.fleet
    con.clock.start()
    con.clock.advance(60)                 # scenario "now" == wall clock, with 60 s of history behind it
    await con.oncall.start()
    con.fleet.start(con.trigger)
    con.fleet.set_fault(aid, fault, now=con.oncall.now())
    t0 = time.monotonic()
    last_state, timeline = None, []
    while time.monotonic() - t0 < limit_s:
        await con.oncall.tick()
        await con.mock_events()
        cf = con.store.get()
        if cf.state is not last_state:
            last_state = cf.state
            timeline.append(f"{time.monotonic() - t0:5.1f}s {cf.state.value}")
        if cf.state is S.RESOLVED or (cf.state is S.TELEGRAM_SENT and cf.verdict == "confident"
                                      and not con.oncall._high_value(aid)):
            break
        await asyncio.sleep(0.5)
    await asyncio.sleep(2.0)  # let ticket/follow-up tasks land
    cf = con.store.get()
    fix = cf.fixes[-1] if cf.fixes else {}
    models = sorted({e.get("model") for e in cf.investigator_log if e.get("model")})
    tools = [e.get("tool") for e in cf.investigator_log if e.get("kind") == "tool" and not e.get("triage")]
    verifies = [(e.get("signature"), e.get("ok"), e.get("source")) for e in cf.investigator_log if e.get("kind") == "verify"]
    errors = [e.get("text") for e in cf.investigator_log if e.get("kind") == "error"]
    ok_diag = fix.get("signature") == EXPECT[fault]
    done = cf.state is S.RESOLVED or (not con.oncall._high_value(aid) and cf.verdict == "confident")
    print(f"\n=== {aid} / {fault}: {'PASS' if ok_diag and done else 'FAIL'} ===")
    print("  timeline:", " → ".join(timeline))
    print(f"  diagnosis: {fix.get('signature')} via {fix.get('source')}  owner={fix.get('owner')}  "
          f"fix={fix.get('fix')} {fix.get('params') or ''}  status={fix.get('status')}  verify={fix.get('verification')}")
    print(f"  models: {models}  extra tools: {tools}")
    print(f"  verify attempts: {verifies}")
    if errors:
        print(f"  errors: {errors[:3]}")
    print(f"  spoken: {[t for t in con.voice.spoken][:4]}")
    await con.oncall.shutdown()
    return ok_diag and done


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "all":
        procs = [subprocess.Popen([sys.executable, __file__, a, f], cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for a, f in PAIRS]
        results = []
        for p, (a, f) in zip(procs, PAIRS):
            out, _ = p.communicate()
            print(out.strip().split("=== ", 1)[-1] if "=== " in out else out[-1500:])
            results.append((a, f, p.returncode == 0))
        print("\nSUMMARY:", ", ".join(f"{f}={'PASS' if ok else 'FAIL'}" for _, f, ok in results))
        return 0 if all(ok for *_, ok in results) else 1
    aid, fault = sys.argv[1], sys.argv[2]
    return 0 if asyncio.run(run(aid, fault)) else 1


if __name__ == "__main__":
    sys.exit(main())
