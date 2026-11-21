"""Read-only: which Agora API surface can see the phone number you imported in the console?

Prints status codes and number IDs only (no credentials).
"""
import base64
import json
import pathlib
import urllib.error
import urllib.request

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"
E = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l and not l.startswith("#"))
auth = "Basic " + base64.b64encode(f"{E['AGORA_CUSTOMER_ID']}:{E['AGORA_CUSTOMER_SECRET']}".encode()).decode()
app = E["AGORA_APP_ID"]


def get(url):
    req = urllib.request.Request(url, headers={"Authorization": auth, "User-Agent": "probe/1.0"})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, str(e)[:200]


candidates = {
    "Platform API v1 (preview): numbers": f"https://preview.ai.agora.io/conversational-ai/v1/projects/{app}/numbers",
    "Platform API v1 (preview): agents": f"https://preview.ai.agora.io/conversational-ai/v1/projects/{app}/agents",
    "v2 telephony: numbers": f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{app}/phone-numbers",
}
for label, url in candidates.items():
    s, b = get(url)
    print(f"\n[{s}] {label}")
    try:
        data = json.loads(b)
        items = data.get("data") or data.get("numbers") or data.get("agents") or data.get("items") or data
        if isinstance(items, list):
            for it in items:
                keep = {k: it[k] for k in it if k in ("number_id", "agent_id", "number", "id", "name", "phone_number", "description", "inbound")}
                print("   ", keep)
            if not items:
                print("    (empty list)")
        else:
            print("   ", str(items)[:300])
    except Exception:
        print("   ", b[:200])
