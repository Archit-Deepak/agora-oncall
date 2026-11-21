"""Read-only health check for every key in .env. Never prints secrets.

Usage: python3 scripts/check_env.py
Only side effect: fills TELEGRAM_CHAT_ID in .env if it is empty and you have messaged the bot.
"""
import base64
import json
import pathlib
import re
import urllib.error
import urllib.request

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"


def load():
    out = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


E = load()
results = []


def record(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))


def http(url, headers=None, data=None, method=None):
    headers = {"User-Agent": "incident-agent-check/1.0", **(headers or {})}  # default Python UA is blocked by Cloudflare (Groq 1010)
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:  # network/DNS
        return 0, str(e)[:200]


def basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


# ---- format checks -------------------------------------------------------
for k, v in E.items():
    if v != v.strip():
        record(f"format {k}", False, "leading/trailing whitespace")
for k in ("PLIVO_NUMBER", "ENGINEER_PHONE"):
    record(f"format {k} is E.164", bool(re.fullmatch(r"\+[1-9]\d{6,14}", E.get(k, ""))),
           "expected like +919876543210")
host = E.get("ZENTRUNK_TERMINATION_HOST", "")
record("format ZENTRUNK_TERMINATION_HOST", bool(host) and "://" not in host and ":" not in host and "/" not in host,
       "hostname only: no sip:, port or slash")
record("format PUBLIC_BASE_URL", E.get("PUBLIC_BASE_URL", "").startswith("https://") and not E["PUBLIC_BASE_URL"].endswith("/"),
       "https, no trailing slash")

# ---- Agora REST ----------------------------------------------------------
s, b = http(f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{E['AGORA_APP_ID']}/agents?limit=1",
            {"Authorization": basic(E["AGORA_CUSTOMER_ID"], E["AGORA_CUSTOMER_SECRET"])})
record("Agora REST auth (Customer ID/Secret + App ID)", s == 200, f"HTTP {s}" + ("" if s == 200 else f" {b[:120]}"))

# ---- Murf ----------------------------------------------------------------
s, b = http("https://api.murf.ai/v1/speech/voices?model=falcon-2.1", {"api-key": E["MURF_API_KEY"]})
ok = s == 200
detail = f"HTTP {s}"
if ok:
    try:
        voices = json.loads(b)
        ids = {v.get("voiceId") for v in voices} if isinstance(voices, list) else set()
        vid = E.get("MURF_VOICE_ID", "")
        detail += f"; {len(ids)} voices; MURF_VOICE_ID {'found' if vid in ids else 'NOT in list (check spelling)'}"
    except Exception:
        pass
else:
    detail += f" {b[:120]}"
record("Murf API key", ok, detail)

# ---- Groq ----------------------------------------------------------------
s, b = http("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {E['GROQ_API_KEY']}"})
ok = s == 200
detail = f"HTTP {s}"
if ok:
    ids = {m["id"] for m in json.loads(b)["data"]}
    for k in ("GROQ_MODEL", "GROQ_FALLBACK_MODEL"):
        detail += f"; {E[k]} {'available' if E[k] in ids else 'NOT available'}"
else:
    detail += f" {b[:120]}"
record("Groq API key", ok, detail)

# ---- Plivo ---------------------------------------------------------------
auth = {"Authorization": basic(E["PLIVO_AUTH_ID"], E["PLIVO_AUTH_TOKEN"])}
s, b = http(f"https://api.plivo.com/v1/Account/{E['PLIVO_AUTH_ID']}/", auth)
record("Plivo Auth ID/Token", s == 200, f"HTTP {s}" + ("" if s == 200 else f" {b[:120]}"))
if s == 200:
    num = E["PLIVO_NUMBER"].lstrip("+")
    s2, _ = http(f"https://api.plivo.com/v1/Account/{E['PLIVO_AUTH_ID']}/Number/{num}/", auth)
    record("Plivo number belongs to this account", s2 == 200, f"HTTP {s2} (needed so caller ID isn't rejected with 4190)")
    s3, b3 = http(f"https://api.plivo.com/v1/Account/{E['PLIVO_AUTH_ID']}/Zentrunk/Trunk/", auth)
    if s3 == 200:
        domains = [t.get("trunk_domain") for t in json.loads(b3).get("objects", [])]
        record("Zentrunk termination host exists on account", host in domains,
               "trunk domains found: " + str(len(domains)))
    else:
        record("Zentrunk trunk list", False, f"HTTP {s3}")

# ---- Linear --------------------------------------------------------------
q = json.dumps({"query": "{ viewer { name } team(id: \"%s\") { key name } }" % E["LINEAR_TEAM_ID"]}).encode()
s, b = http("https://api.linear.app/graphql", {"Content-Type": "application/json", "Authorization": E["LINEAR_API_KEY"]}, q)
ok = s == 200 and '"team":{' in b.replace(" ", "")
record("Linear API key + team ID", ok, f"HTTP {s}")

# ---- Telegram ------------------------------------------------------------
tok = E.get("TELEGRAM_BOT_TOKEN", "")
if not tok:
    record("Telegram bot token", False, "empty")
else:
    s, b = http(f"https://api.telegram.org/bot{tok}/getMe")
    ok = s == 200
    record("Telegram bot token", ok, f"HTTP {s}" + (f"; bot @{json.loads(b)['result']['username']}" if ok else ""))
    if ok and not E.get("TELEGRAM_CHAT_ID"):
        s, b = http(f"https://api.telegram.org/bot{tok}/getUpdates")
        chats = [u["message"]["chat"]["id"] for u in json.loads(b).get("result", []) if "message" in u]
        if chats:
            cid = str(chats[-1])
            lines = ENV.read_text().splitlines()
            lines = [f"TELEGRAM_CHAT_ID={cid}" if l.startswith("TELEGRAM_CHAT_ID=") else l for l in lines]
            ENV.write_text("\n".join(lines) + "\n")
            record("Telegram chat ID", True, "found and saved to .env")
        else:
            record("Telegram chat ID", False, "no messages seen; open your bot in Telegram and send /start, then re-run")
    elif ok:
        record("Telegram chat ID", True, "already set")

# ---- Tunnel --------------------------------------------------------------
s, _ = http(E["PUBLIC_BASE_URL"] + "/state")
record("Tunnel reachable", s in (200, 404, 502), f"HTTP {s} (502/404 is fine until our app runs; 0 means tunnel is down)")

import sys

if "--details" in sys.argv:
    print("\n--- Murf English voices (voiceId | name | locale | gender)")
    s, b = http("https://api.murf.ai/v1/speech/voices?model=falcon-2.1", {"api-key": E["MURF_API_KEY"]})
    if s == 200:
        en = [v for v in json.loads(b) if str(v.get("locale", "")).startswith("en")]
        print(f"configured MURF_VOICE_ID = {E['MURF_VOICE_ID']!r}; {len(en)} English voices")
        for v in en[:40]:
            print(f"{v.get('voiceId')} | {v.get('displayName')} | {v.get('locale')} | {v.get('gender')}")
    print("\n--- Groq models (llama / gpt-oss)")
    s, b = http("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {E['GROQ_API_KEY']}"})
    if s == 200:
        for m in json.loads(b)["data"]:
            if "llama" in m["id"] or "gpt-oss" in m["id"]:
                print(m["id"])

print(f"\n{sum(results)}/{len(results)} checks passed")
