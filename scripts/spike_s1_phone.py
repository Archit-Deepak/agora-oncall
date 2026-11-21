"""Spike S1: Agora dials your phone through the Plivo Zentrunk trunk, speaking in a Murf Falcon voice.

Uses Agora's Platform API (preview): https://preview.ai.agora.io/conversational-ai/v1/...

  python3 scripts/spike_s1_phone.py register        # idempotent: import the Plivo number via API
  python3 scripts/spike_s1_phone.py numbers         # list registered numbers (ids only)
  python3 scripts/spike_s1_phone.py call            # place the test call to ENGINEER_PHONE
  python3 scripts/spike_s1_phone.py status <id>     # session status
  python3 scripts/spike_s1_phone.py stop <id>       # hang up / stop session

For this spike the LLM is Groq called directly (OpenAI-compatible URL), so the voice path is
tested without our proxy. Spike S2 swaps llm.url for our proxy. Secrets are never printed.
"""
import base64
import json
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"
E = {}
for line in ENV.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        E[k.strip()] = v.strip()

BASE = f"https://preview.ai.agora.io/conversational-ai/v1/projects/{E['AGORA_APP_ID']}"
AUTH = "Basic " + base64.b64encode(f"{E['AGORA_CUSTOMER_ID']}:{E['AGORA_CUSTOMER_SECRET']}".encode()).decode()


def api(method, path, body=None, headers=None):
    h = {"Authorization": AUTH, "Content-Type": "application/json", "User-Agent": "incident-agent/1.0", **(headers or {})}
    req = urllib.request.Request(BASE + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


def find_number():
    s, d = api("GET", "/numbers")
    for n in d.get("data", {}).get("list", []):
        if n.get("number") == E["PLIVO_NUMBER"]:
            return n
    return None


def register():
    existing = find_number()
    if existing:
        print("Already registered. number_id =", existing.get("number_id"))
        return existing.get("number_id")
    body = {
        "number": E["PLIVO_NUMBER"],
        "description": "agora-hackathon demo caller ID (Plivo Zentrunk)",
        "sip_trunk": {
            "name": "agora-hackathon-demo",
            "hostname": E["ZENTRUNK_TERMINATION_HOST"],
            "transport": E.get("ZENTRUNK_TRANSPORT", "udp"),
            "auth": {"username": E["ZENTRUNK_USERNAME"], "password": E["ZENTRUNK_PASSWORD"]},
        },
        "inbound": {"agent": None},
    }
    s, d = api("POST", "/numbers", body)
    print("HTTP", s, json.dumps({k: v for k, v in d.items() if k != "sip_trunk"})[:400])
    return d.get("number_id")


def call():
    n = find_number()
    if not n:
        sys.exit("Number not registered. Run: python3 scripts/spike_s1_phone.py register")
    channel = f"s1-{uuid.uuid4().hex[:8]}"
    # NOTE: telephony transport rejects channel/token ("transport.channel: Field is not supported").
    body = {
        "agent": {
            "instructions": ("You are a calm on-call incident agent talking to an engineer on the phone. "
                             "Keep every reply to one or two short sentences. This is a voice test."),
            "greeting": {"mode": "text", "on": "first_join", "text":
                         f"Hi {E.get('ENGINEER_NAME', 'there')}, this is your incident agent. "
                         "If you can hear this voice, the phone path works. Say something and I will reply."},
            "pipeline": {
                "mode": "cascaded",
                "asr": {"credential": {"mode": "managed"}, "vendor": "deepgram", "model": "nova-3", "language": "en-US"},
                "llm": {
                    "credential": {"mode": "byok", "fields": {"api_key": E["GROQ_API_KEY"]}},
                    "vendor": "groq", "style": "openai",
                    "model": E.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                    "failure_message": "Sorry, give me a second.",
                },
                "tts": {
                    "credential": {"mode": "byok", "fields": {"api_key": E["MURF_API_KEY"]}},
                    "vendor": "murf",
                    "params": {
                        "base_url": "wss://global.api.murf.ai/v1/speech/stream-input",
                        "voiceId": E.get("MURF_VOICE_ID", "en-US-daniel"),
                        "locale": "en-US", "rate": 0, "pitch": 0,
                        "model": E.get("MURF_MODEL", "falcon-2.1"), "sample_rate": 24000,
                    },
                },
            },
        },
        "transport": {
            "type": "telephony",
            "from": n["number_id"],
            "to": E["ENGINEER_PHONE"],
            "call_policy": {"max_ring_duration_ms": 45000, "max_call_duration_seconds": 180,
                            "end_call": {"on_user_request": True}},
        },
        "lifecycle": {"idle_timeout_ms": 20000, "max_duration_ms": 240000},
        "labels": {"spike": "s1"},
    }
    s, d = api("POST", "/sessions/ephemeral", body, {"Idempotency-Key": uuid.uuid4().hex})
    print("HTTP", s, json.dumps(d)[:600])
    if s == 200:
        print("\nYour phone should ring within ~10 seconds.")
        print("Session:", d.get("agent_session_id"))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "register":
        register()
    elif cmd == "numbers":
        s, d = api("GET", "/numbers")
        print("HTTP", s, [{"number_id": n.get("number_id"), "number": n.get("number")} for n in d.get("data", {}).get("list", [])])
    elif cmd == "call":
        call()
    elif cmd == "status":
        print(api("GET", f"/sessions/{sys.argv[2]}"))
    elif cmd == "stop":
        print(api("POST", f"/sessions/{sys.argv[2]}/stop"))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
