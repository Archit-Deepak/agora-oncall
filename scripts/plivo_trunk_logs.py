"""Read-only: recent Plivo Zentrunk CDRs and trunk config (no secrets printed)."""
import base64
import json
import pathlib
import urllib.error
import urllib.request

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"
E = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l and not l.startswith("#"))
AUTH = "Basic " + base64.b64encode(f"{E['PLIVO_AUTH_ID']}:{E['PLIVO_AUTH_TOKEN']}".encode()).decode()
BASE = f"https://api.plivo.com/v1/Account/{E['PLIVO_AUTH_ID']}"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": AUTH, "User-Agent": "logs/1.0"})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


for path in ["/Zentrunk/Call/?limit=10", "/Zentrunk/CDR/?limit=10", "/Zentrunk/Calls/?limit=10"]:
    s, d = get(path)
    print(f"\n[{s}] {path}")
    if s == 200:
        print(json.dumps(d, indent=1)[:3000])

s, d = get("/Zentrunk/Trunk/")
print(f"\n[{s}] trunks")
if s == 200:
    for t in d.get("objects", []):
        print({k: t.get(k) for k in ("name", "trunk_direction", "trunk_status", "secure", "trunk_domain",
                                       "ipacl_uuid", "credential_uuid")})
s, d = get("/Zentrunk/IPAccessControlList/")
print(f"\n[{s}] IP ACLs")
if s == 200:
    for a in d.get("objects", []):
        print(a.get("name"), a.get("ip_addresses"))
