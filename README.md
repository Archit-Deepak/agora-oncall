# Pagewhisper

An AI on-call engineer for a voice platform. It watches customer accounts by spend, and when a high-spend account's calls start failing it phones the engineer, already investigating. On the call it explains what it has found, proposes a fix and the cost, and acts only after a spoken yes. It then checks that traffic recovered, files a ticket, and drafts the customer email.

Live demo: https://pagewhisper.me
Access code: `AGORA26`

All customers and call traffic are simulated. Field names and hangup codes are the real Plivo / Zentrunk ones. No real customer data.

## Try it

1. Open the site. Enter your name, an Indian mobile number, and the access code, then press **Call me**. That only puts you on the rotation. Nothing rings yet.
2. When a high-spend account starts failing and stays failing, the phone rings about 15 seconds later, from a US number.
3. Pick up. Ask what is going on, whether it is one carrier, and whose fault it is.
4. When it proposes a fix, say **yes**. It applies the fix, watches the traffic, and tells you the before and after.
5. The page shows the call records, the investigator's tool calls, the transcript, the ticket, and the customer email.

Limits: Indian mobiles only, one call per number, five-minute calls.

## Track

Workplace Assistance. The agent calls you (Agora Conversational AI, Murf Falcon voice), reads a live case file plus `app/kb`, investigates with tool calls, asks before it changes anything, and follows up with a verified recovery, a ticket, and a customer email.

The voice model narrates. Code decides the facts: numbers come from tools, and a check in `app/verify.py` has to agree before the agent may say it found the cause.

## Stack

Agora Conversational AI (outbound call, custom LLM endpoint, spoken break-ins) · Murf Falcon · Plivo Zentrunk · Groq for the voice · Azure OpenAI for the investigator · FastAPI.

`V2_SPEC.md` is the product contract: accounts, fault modes, and the state machine. `SUBMISSION.md` is the short judging note.

## Layout

- `app/` — fleet traffic, detection, investigator, voice proxy, orchestrator
- `app/kb/` — the knowledge base the agent retrieves from
- `web/` — the live console
- `tests/` — `pytest`

## Run the tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

A full demo needs the provider credentials in `.env`. That file is gitignored and is not in this repo.
