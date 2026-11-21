"""Temporarily detach the IP ACL from the Zentrunk trunk, run a test call, then ALWAYS re-attach it.
Credentials stay attached the whole time.

  .venv/bin/python scripts/acl_off_livekit_test.py          # LiveKit test call
  .venv/bin/python scripts/acl_off_livekit_test.py agora    # Agora v2 test call
"""
import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
E = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
AUTH = "Basic " + base64.b64encode(f"{E['PLIVO_AUTH_ID']}:{E['PLIVO_AUTH_TOKEN']}".encode()).decode()
BASE = f"https://api.plivo.com/v1/Account/{E['PLIVO_AUTH_ID']}/Zentrunk"


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json", "User-Agent": "acl/1.0"})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def trunk():
    s, d = call("GET", "/Trunk/")
    return next(t for t in d["objects"] if t["trunk_domain"] == E["ZENTRUNK_TERMINATION_HOST"])


t = trunk()
tid, acl = t["trunk_id"], t["ipacl_uuid"]
print("trunk:", t["name"], "| ACL attached:", bool(acl))
keep = {"name": t["name"], "trunk_direction": t["trunk_direction"], "credential_uuid": t["credential_uuid"],
        "secure": t["secure"]}
try:
    s, d = call("POST", f"/Trunk/{tid}/", {**keep, "ipacl_uuid": None})
    print("detach ACL ->", s, str(d)[:200])
    now = trunk()
    print("ACL now attached:", bool(now["ipacl_uuid"]), "| credentials attached:", bool(now["credential_uuid"]))
    if now["ipacl_uuid"]:
        print("ACL still attached; skipping the test call.")
    else:
        time.sleep(3)
        if "agora" in sys.argv:
            subprocess.run([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "spike_s1_v2.py"), "call"], cwd=ROOT)
            print("waiting 40s for Agora to dial before re-attaching the ACL...", flush=True)
            time.sleep(40)
        else:
            subprocess.run([str(ROOT / "scripts" / "livekit_trunk_test.sh")], cwd=ROOT)
finally:
    s, d = call("POST", f"/Trunk/{tid}/", {**keep, "ipacl_uuid": acl})
    print("\nre-attach ACL ->", s, str(d)[:200])
    print("ACL attached again:", trunk()["ipacl_uuid"] == acl)
