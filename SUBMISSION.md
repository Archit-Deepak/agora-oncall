# Submission notes (draft — Archit to edit)

**Link:** https://52-231-69-243.sslip.io
**Access code:** `AGORA26`

## Try it (2 minutes)
1. Open the link, enter your **Indian mobile number** and the access code, press **📞 Call me**.
2. A high-spend customer starts failing. Your phone rings in about 15 seconds from a US (+1) number.
3. Pick up: you're the on-call engineer. Ask it anything ("what's going on?", "is it only one carrier?", "whose fault is it?").
4. When it finds the cause it proposes a fix and its cost. Say **"yes"**. It applies the fix, watches traffic and tells you the before → after.
5. The page shows everything live: CDRs, the investigator's tool calls, code verification, the call transcript, the ticket and the customer email.

Limits: 5-minute calls, one call per number, Indian mobiles only.

## What it is
An AI on-call engineer for a voice platform (CPaaS). It watches every customer account weighted by spend,
catches failures that dashboards miss (silent calls, volume drops), works out who owns the problem, phones the
engineer, debugs in the background during the call, proposes the fix with its cost, applies it on a spoken
"yes", and verifies recovery, then files the ticket and drafts the customer email.

## Track mapping (Workplace Assistance)
- **Initiate:** the agent calls the engineer; conversation on Agora Conversational AI with a Murf Falcon voice.
- **Understand:** live case file + knowledge base + an LLM investigator with tool calls over CDRs, edge logs and account config.
- **Execute:** applies the fix (HA backup number, CPS increase, reroute, ACL entry, caller ID), raises a ticket, drafts the email.
- **Confirm:** asks before acting; "found it" only after code re-verifies the LLM's conclusion; honest "not sure yet".
- **Follow up:** verified recovery (before → after), ticket with the investigation trail, customer email, account memory.

## Stack
Agora Conversational AI (outbound telephony `/call`, custom LLM endpoint, `/speak` break-ins) · Murf Falcon TTS ·
Plivo Zentrunk SIP trunk · Groq gpt-oss-120b (voice) · Azure OpenAI gpt-5-mini / gpt-4.1-mini (investigator) ·
FastAPI on an Azure VM. All customers and traffic are simulated with real Plivo/Zentrunk field names and hangup codes.
