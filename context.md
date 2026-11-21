Project Context

Read this first. It explains who you're working with, what we're building, why, and how it will be judged. The detailed build plan, schedule, risks and fallback tiers live in hackathon-plan.md; treat that as the source of truth for scope and order of work.

1. Who I am
Archit Agarkhed. Forward Deployed Engineer (intern) at Plivo, a cloud communications platform (voice, SMS, SIP trunking), since September 2026. Also in college alongside work.
Day job: I answer inbound leads, run discovery calls, own customer accounts after onboarding, and debug customer issues, especially failing calls. The scenario in this project is my actual job.
What I know well (don't over-explain these):
SIP signaling, call flows, response codes, hangup causes
RTP media and WebRTC
SIP trunking (origination/termination, trunk config, carrier routing)
WebSockets, including real-time audio streaming
Pipecat: I've built a Pipecat voice AI agent with RAG over Plivo's voice API docs
Plivo's voice APIs (Make a Call, answer/hangup callbacks, XML), call detail records, and how calls fail in practice
Tools: Claude Code + Cursor (team plan). Comfortable with Notion and Slack.
Working solo, with 2 days to build.

How to work with me:

Skip telecom basics; go deep on Agora specifics, integration details and anything new to me.
When something is unverified (an Agora capability, a config option), say so and propose a quick test rather than assuming.
Optimize for a reliable demo, not production completeness. If a choice trades demo reliability for elegance, pick reliability and flag it.
Keep every external dependency behind a small interface with a mock and a real implementation, switchable by config.
Never use real customer data. All data is simulated, using real field names and real hangup causes.
2. The hackathon
An external voice AI hackathon built around Agora Conversational AI and Murf AI Falcon TTS. Both are required in the demo.
Track chosen: Workplace Assistance — "From Workplace Assistance to Intelligent Action" (ps31).
Track problem statement: how can real-time voice AI improve workplace productivity by letting employees access information, resolve issues, automate repetitive tasks and complete business workflows through natural, hands-free conversation?
What the track asks for: a voice-first, agentic workplace assistant combining enterprise knowledge, contextual intelligence and tool calling to execute real business tasks across connected systems.
Tech focus listed: Agora Conversational AI, Agora RTC and/or Agora Chat, Murf Falcon TTS, LLMs, RAG, tool calling, MCP-compatible integrations, enterprise APIs.

Required demo flow (all five must be visible):

Initiate: employee interacts with the assistant by natural voice using Agora Conversational AI + Murf Falcon TTS.
Understand: the AI interprets the request, retrieves relevant enterprise information, identifies the action.
Execute: the AI uses tool calling / external APIs to perform meaningful actions (e.g. updating tickets).
Confirm: the AI communicates the outcome, handles errors, and asks for confirmation before critical actions.
Follow up: the AI updates systems, generates a summary, or initiates follow-ups.

Expected deliverable: a working voice assistant with natural conversation, enterprise knowledge integration, and at least one complete workplace workflow with meaningful actions across connected systems. The brief also encourages multi-step workflows and measurable improvements in operational efficiency.

3. Evaluation rubric, and how we win each criterion
3.1 Problem–solution fit (does the solution match the chosen track?)
The track's own example is a field technician copilot that diagnoses issues by voice, escalates incidents and updates tickets. Our project is that, for telecom incident response.
Every required flow step maps to a concrete feature (see section 5). Make the mapping explicit in the pitch.
3.2 Innovation
The agent calls the engineer first. Most entries are assistants you talk to; this one is proactive.
Investigation starts at alert time. Ring time is free compute, so a first diagnosis is ready at pickup.
Confidence-gated honesty. It says "I'm not sure, here's what I've ruled out" and calls back once evidence crosses a threshold. The confidence score is computed from evidence, not invented by the LLM.
Case-file architecture for speed. The voice agent narrates a continuously updated case file instead of running tools live, so most answers are near-instant.
3.3 Use of Agora voice AI
Agora Conversational AI is the conversation layer, not a bolt-on: interruption handling (barge-in), turn-taking and the full voice loop.
Telephony through Agora (Agent Studio SIP trunk inbound/outbound) if the spike passes; otherwise the Agora in-app/RTC path.
Custom LLM endpoint: Agora calls our OpenAI-compatible proxy, which injects the case file + RAG on every turn.
Murf Falcon is the agent's voice everywhere it speaks.
Note for this criterion: where two routes work equally well, prefer the one that uses more of Agora natively. For example, Agora placing the outbound call through the SIP trunk (Route B) shows more Agora than Plivo placing the call and bridging in (Route A). Weigh this against reliability.
3.4 Product functionality
One scenario that works end to end, every time: alert → call → diagnosis → follow-ups → "not sure" → callback → confirmed ticket + email draft → summary.
A deterministic scripted evidence timeline, so the demo behaves identically on every run.
A one-command reset, a mock fallback flag, and a backup video (fallback tiers A–D in hackathon-plan.md).
3.5 Future scope, scalability and real-world use
Real-world: every CPaaS/telecom provider and every company running voice at scale has this exact incident-response problem.
Production path: swap the simulator for real call-records APIs; the adapters are built for this.
Scale-out: on-call rotations and escalation chains, multi-customer monitoring, baseline-aware anomaly detection, learning from past incidents, approved auto-remediation (e.g. carrier failover with voice approval), a fast decision model for routing.
Beyond telecom: the same pattern (detect → investigate in the background → call the human → confidence-gated answer → act with approval) applies to payments failures, infra outages and logistics incidents.
4. What we're building

One line: an AI on-call engineer that calls you when a big customer's calls start failing, already knows what's going on when you pick up, admits when it isn't sure, and calls back once it is.

The flow:

A simulator emits call records for a big customer; a detector fires when the failure rate crosses a threshold.
The investigator (plain code, fixed checks) starts immediately and writes evidence into a case file.
The agent rings the engineer (phone via Plivo/SIP into Agora, or in the browser as a fallback).
On pickup, it opens with alert-time facts, never waiting on the investigator.
The engineer asks follow-ups. Fast path: answered from the case file (~2–3 seconds). Slow path: "give me five seconds" + one targeted check (under 10 seconds).
"So what's the issue?" → confidence score. Below threshold: honest "not sure" + what's ruled out + callback. Above: diagnosis + the fix from the runbook (RAG).
With voice confirmation: raise a real ticket (Notion or Slack) and draft the customer email. One action fails on purpose and is retried.
Missed call → retry → Telegram message with a "Call me now" button.
The dashboard shows the confidence meter, hypotheses, ruled-out list and a stage timer, compared against a manually timed baseline (measurable efficiency).

Demo scenario: carrier rejection is the true cause. Number formatting and auth/config get ruled out.

5. Required flow → feature mapping
Required step	Feature
Initiate	The agent calls; the conversation runs on Agora Conversational AI with a Murf Falcon voice
Understand	Case file + RAG over docs, runbooks and the customer profile
Execute	Real ticket (Notion/Slack) + customer email draft via tool calls
Confirm	Voice confirmation before actions; deliberate failure + retry; honest low-confidence answer
Follow up	Callback when confident; incident summary on the ticket; Telegram on a missed call
6. Stack and key decisions
Voice: Agora Conversational AI (required) + Murf Falcon TTS (required)
LLM: Groq openai/gpt-oss-120b (primary), llama-3.3-70b-versatile (fallback); reasoning effort low; paid Developer tier for rehearsals
Backend: one FastAPI app. Endpoints: /plivo/answer, /plivo/status, /v1/chat/completions (LLM proxy), /telegram/webhook (optional), /state (dashboard), /demo/trigger, /demo/reset, optional /mock/call-records
Shared state: case file in Redis or SQLite
Calling: Plivo Make a Call / SIP trunking, or Agora outbound via SIP trunk (decide after spikes)
Alerts: Telegram Bot API (bots can message but can't place calls; the bot must be started once beforehand)
Tickets: Notion database or Slack (pick one), optionally through MCP
Public URL: ngrok / Cloudflare Tunnel, or deploy to Railway/Render

Design rules:

The investigator is deterministic code; the LLM only narrates.
Confidence is computed from evidence, deterministically.
Actions run after voice confirmation, in the background.
Mock-first adapters for every external system, switchable by config.
7. Constraints
Solo, 2 days. Feature freeze by mid-afternoon Day 2.
Worst-case version end to end by end of Day 1, then upgrade.
Cut order if behind: Telegram button → incident summary → deliberate error demo → phone path (switch to browser). Never cut the callback climax or the ticket action.
Confirm hackathon rules allow extra providers (Plivo, Groq, Telegram) alongside the required stack.
Personal Plivo account (or approved work access). No real customer data.