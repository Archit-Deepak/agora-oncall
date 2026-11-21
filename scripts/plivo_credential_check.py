"""Read-only: compare the Zentrunk credential username attached to the trunk with ZENTRUNK_USERNAME in .env.
Plivo never returns passwords, so only the username can be checked."""
import base64
import json
import pathlib
import urllib.request

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"
E = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l and not l.startswith("#"))
AUTH = "Basic " + base64.b64encode(f"{E['PLIVO_AUTH_ID']}:{E['PLIVO_AUTH_TOKEN']}".encode()).decode()
BASE = f"https://api.plivo.com/v1/Account/{E['PLIVO_AUTH_ID']}/Zentrunk"


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": AUTH, "User-Agent": "check/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())


trunk = next(t for t in get("/Trunk/")["objects"] if t["trunk_domain"] == E["ZENTRUNK_TERMINATION_HOST"])
cred = get(f"/Credential/{trunk['credential_uuid']}/")
obj = cred.get("object", cred)
username = obj.get("username", "")
print("credential name:", obj.get("name"))
print("username matches .env:", username == E["ZENTRUNK_USERNAME"],
      f"(trunk has {len(username)} chars, .env has {len(E['ZENTRUNK_USERNAME'])} chars)")
pw = E["ZENTRUNK_PASSWORD"]
print("password in .env: length", len(pw), "| has required special char:", any(c in "~!@#$%^&*()_+" for c in pw))
