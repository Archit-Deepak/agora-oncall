# Build Plan & System Architecture

**Project:** an AI on-call engineer that calls you when a big customer's calls start failing
**Derived from:** `plan.md` (scope, schedule, fallback tiers) and `context.md` (who, why, judging)
**Status:** pre-build. Everything below is the agreed blueprint; deviations get written back here.

Legend used throughout:
- **[VERIFIED]** confirmed in Agora/Murf docs (Nov 2026). Still exercised in a spike, but the design can rely on it.
- **[UNVERIFIED]** plausible but not confirmed. Has a named spike and a fallback.
- **[DECIDE]** a choice you need to make; a recommendation is given.

---

## 0. What changed after checking the docs

Research against current Agora docs changed several assumptions in `plan.md`, mostly for the better:

| Topic | `plan.md` assumed | What the docs say | Effect on the plan |
|---|---|---|---|
| Phone path | Plivo places the call and bridges SIP into Agora Agent Studio (risky) | Agora has an outbound telephony API: `POST /v2/projects/{appid}/call` dials a number through a SIP trunk you register with Agora, and accepts the **full agent `properties`** (custom LLM + Murf) per call **[VERIFIED]** | New primary route: **Agora dials through a Plivo Zentrunk termination trunk.** More Agora-native (better rubric score), fewer moving parts. Plivo is "just the carrier". |
| Custom LLM | Might not be available for phone agents | `llm.vendor: "custom"` + `llm.url`, OpenAI Chat Completions format with SSE streaming **[VERIFIED]**; the `/call` API takes the same `properties` | Spike S2 shrinks to "confirm it works on the phone leg". |
| Murf on phone | Might not be available | `tts.vendor: "murf"`, `model: "FALCON"`, BYOK `api_key`, streaming WebSocket `base_url` **[VERIFIED]** | S1 becomes "hear it on a real phone", not "find out if it exists". |
| Filler-then-answer | Might get cut off | Officially supported: first SSE chunk `object: "chat.completion.custom_metadata"` with `metadata.interruptable: false`, then a filler sentence, then the real answer in the same stream **[VERIFIED]** | S3 is now a latency/timeout check, not a feasibility check. |
| Agent speaking without a user turn | Not considered | `POST /v2/projects/{appid}/agents/{agentId}/speak` with `priority: APPEND/INTERRUPT` (max 512 bytes) **[VERIFIED]** | Background-action results ("ticket failed, retrying… done") can be **spoken mid-call** without waiting for the engineer to talk. |
| Call status | Plivo status callbacks | Agora webhook event `1201 Telephony state` with `state` (`ringing/answered/ended`), `hangup_reason` (`NO_ANSWER`, `BUSY`, `USER_HANGUP`…), `duration_seconds`; `properties.labels` echoed in callbacks **[VERIFIED for the Platform API; UNVERIFIED for v2 /call]** | Missed-call detection comes from Agora, labelled with our incident ID. Polling is the fallback. |
| Opening line | LLM generates it | `llm.greeting_message` is spoken by TTS as soon as the user joins **[VERIFIED]** | Openings are **code-rendered templates**: zero LLM latency, never wrong. |

Also note:
- There are two Agora telephony surfaces. **v2 `/call`** (Beta, free in Beta, accepts inline `properties`) and the newer **Platform API `/v1/.../sessions`** (Preview, uses *saved* agents and `$secrets` references; its docs mention custom LLM headers needing an "approved provider endpoint"). **Use v2 `/call`.** It lets us build the whole agent config in code per call, which we need (different greeting per call).
- gpt-oss models on Groq emit reasoning tokens. The proxy **must not forward reasoning text** to Agora or the agent will read its thinking aloud. Use `reasoning_format: "hidden"` (or strip `reasoning` deltas) **[UNVERIFIED exact Groq param; check in S4]**.

---

## 1. Final architecture

### 1.1 One process, many modules

Everything runs in **one FastAPI process** (Python 3.12, `uv`, `asyncio`). No Redis, no queue, no second service. The case file lives in memory, guarded by an `asyncio.Lock`, snapshotted to `state/incident.json` after each change so a crash or restart doesn't lose the demo. This is the most reliable shape for a 2-day solo build, and it's what makes the one-command reset trivial.

```
                       ┌─────────────────────────── FastAPI process ───────────────────────────┐
                       │                                                                         │
 [Scenario clock] ───► │ Simulator ──CDRs──► Detector ──Alert──► Orchestrator (state machine)   │
                       │                                   │             │        │        │     │
                       │                                   ▼             │        │        │     │
                       │                             Investigator        │        │        │     │
                       │                       (fixed checks, staged)    │        │        │     │
                       │                                   │             │        │        │     │
                       │                                   ▼             ▼        ▼        ▼     │
                       │                         ┌──────────────┐   VoiceCaller  Notifier Actions│
                       │                         │  Case file   │   (adapter)   (adapter)(adapter)
                       │                         │ + event bus  │        │          │        │   │
                       │                         └──────┬───────┘        │          │        │   │
                       │        reads every turn ▲      │ SSE push       │          │        │   │
                       │   LLM proxy ────────────┘      ▼                │          │        │   │
                       │  /v1/chat/completions      Dashboard /state     │          │        │   │
                       │    │  ▲   RAG (BM25)                            │          │        │   │
                       └────┼──┼─────────────────────────────────────────┼──────────┼────────┼───┘
                            │  │                                         │          │        │
                     Groq ◄─┘  └── Agora ConvoAI Engine ◄── /v2/call ────┘     Telegram     Linear
               gpt-oss-120b         (ASR → our proxy → Murf Falcon)                Bot API
                                          │
                                   SIP (Plivo Zentrunk termination)
                                          │
                                   Engineer's phone
```

### 1.2 The three voice routes (same backend, switch by config)

| Route | `VOICE_MODE` | How the engineer is reached | Agora-native score | When we use it |
|---|---|---|---|---|
| **B: Agora dials via SIP** (primary) | `agora_phone` | Backend calls Agora `/v2/projects/{appid}/call`; Agora sends an INVITE through a SIP trunk registered with Agora, which is a **Plivo Zentrunk termination trunk**; the engineer's real phone rings | Highest | S1 passes |
| **A: Plivo dials, bridges to Agora** | `plivo_bridge` | Plivo Make a Call → answer XML `<Dial>` to the Agora inbound SIP URI/number | Medium | Only if B fails for a trunk-specific reason (e.g. Agora can't authenticate to Zentrunk) and A is quick to prove. Timebox 1 hour. |
| **C: Browser** (fallback, Tier B/C) | `agora_rtc` | Dashboard shows an incoming-call screen + ringtone; clicking Answer joins an Agora RTC channel with the Web SDK; backend starts the agent with `/v2/projects/{appid}/join` | Medium (RTC + ConvoAI) | Phone path fails by the Day 1 timebox |
| **Text harness** (dev only) | `mock` | Dashboard chat box posts straight to the proxy | None | Prompt tuning and rehearsing the dialogue without burning calls |

All four call the same `VoiceCaller` interface (section 6.1). The LLM proxy, case file, actions and dashboard are identical across routes, so falling back costs minutes, not hours.

### 1.3 Design rules (non-negotiable)

1. **The investigator is deterministic code.** The LLM never decides what's true; it narrates the case file.
2. **Confidence is computed in code** from evidence (section 4.4). The LLM is told the verdict and isn't allowed to upgrade it.
3. **Openings are templates.** Call #1 and the callback greet from code-rendered text via `greeting_message`.
4. **Critical actions are gated by a code-side state machine**, not by prompt instructions alone (section 5.5).
5. **Actions run in the background**; results are spoken via the Agora `speak` API, so the conversation never stalls.
6. **Every external system sits behind an interface with a mock and a real implementation,** switchable in `.env`.
7. **The scenario is deterministic.** Same inputs, same timeline, same numbers, every run.
8. **No real customer data.** Simulated CDRs with real Plivo field names and real hangup causes.

---

## 2. Repository layout

```
agora_hackathon/
├── README.md                    # setup, run, demo instructions
├── plan.md  context.md  build-plan.md
├── pyproject.toml               # uv-managed; fastapi, uvicorn, httpx, pydantic-settings,
│                                #   rank-bm25, python-telegram-bot (or raw httpx), agora-token-builder
├── .env.example                 # every key, documented; real .env is gitignored
├── app/
│   ├── main.py                  # FastAPI app; lifespan starts clock, simulator, telegram poller
│   ├── config.py                # pydantic-settings: adapter modes, thresholds, timings, keys
│   ├── models.py                # ALL shared Pydantic models (section 3) — written first
│   ├── store.py                 # IncidentStore: in-memory + lock + JSON snapshot + event bus
│   ├── clock.py                 # ScenarioClock: real time × speed factor, pause, manual advance
│   ├── scenario/
│   │   ├── carrier_rejection.yaml   # the scripted timeline (section 4.2)
│   │   └── loader.py
│   ├── sim/simulator.py         # emits CallRecords from the scenario
│   ├── detect/detector.py       # sliding-window failure-rate detector → Alert
│   ├── investigate/
│   │   ├── checks.py            # 3 hypothesis checks + 3 targeted (slow-path) checks
│   │   ├── confidence.py        # the formula, pure function, unit-tested
│   │   └── investigator.py      # runs checks per stage, writes Evidence to the case file
│   ├── orchestrator/
│   │   ├── states.py            # IncidentState enum + allowed transitions
│   │   └── orchestrator.py      # reacts to events: alert, call status, confidence change
│   ├── voice/
│   │   ├── base.py              # VoiceCaller protocol
│   │   ├── agent_properties.py  # builds the Agora `properties` dict (ASR/LLM/TTS/turn detection)
│   │   ├── greetings.py         # opening-line templates (call #1, callback, Telegram-triggered)
│   │   ├── agora_phone.py       # Route B: /v2/call
│   │   ├── agora_rtc.py         # Route C: /v2/join + token minting
│   │   ├── plivo_bridge.py      # Route A (only if needed)
│   │   ├── mock.py              # text harness
│   │   └── speak.py             # wrapper for /agents/{id}/speak
│   ├── llm/
│   │   ├── proxy.py             # POST /v1/chat/completions (SSE), auth check
│   │   ├── context.py           # builds the per-turn system prompt from case file + RAG
│   │   ├── router.py            # deterministic fast/slow/action intent routing
│   │   ├── tools.py             # tool schemas + server-side execution loop
│   │   ├── groq.py              # streaming client, model fallback, reasoning stripping
│   │   └── prompts/             # persona.md, rules.md (versioned text, not code strings)
│   ├── rag/
│   │   ├── index.py             # BM25 over chunked markdown, built at startup
│   │   └── kb/                  # hangup causes, carrier docs, 2 runbooks, customer profile
│   ├── actions/
│   │   ├── base.py              # TicketSink protocol
│   │   ├── linear.py  mock.py      # linear.py = GraphQL issueCreate / commentCreate
│   │   ├── email_draft.py       # template-filled customer email (LLM-free by default)
│   │   └── executor.py          # background runner, deliberate first failure, retry, speak results
│   ├── notify/
│   │   ├── base.py  telegram.py  mock.py
│   └── api/
│       ├── demo.py              # /demo/trigger, /demo/reset, /demo/advance, /demo/pause
│       ├── state.py             # /state (JSON) and /state/stream (SSE)
│       ├── webhooks.py          # /agora/events, /plivo/answer, /plivo/status
│       └── agora_token.py       # /agora/token for the browser route
├── web/                         # dashboard: static HTML + vanilla JS, no build step
│   ├── index.html  app.js  styles.css
│   └── vendor/AgoraRTC_N.js     # pinned Web SDK copy (no CDN dependency on stage)
├── scripts/
│   ├── reset.sh                 # one-command reset
│   ├── spike_s1_phone.py  spike_s2_proxy.py  spike_s3_filler.py  spike_s4_latency.py  spike_s5_missed.py
│   └── replay_scenario.py       # runs the full timeline fast-forward with mock adapters
├── state/                       # runtime snapshots (gitignored)
└── tests/
    ├── test_confidence.py  test_detector.py  test_state_machine.py
    ├── test_proxy_sse.py        # contract test: Agora-compatible SSE chunks
    └── test_scenario_replay.py  # end-to-end with mocks; asserts the exact state sequence
```

**Why static HTML for the dashboard:** no build tooling to break, the Agora Web SDK drops in as one script, SSE from `/state/stream` updates it live. One page, five panels.

---

## 3. Data model (write `app/models.py` first; every module imports from here)

This is the contract between Claude Code (building modules) and Cursor (proxy and voice). Field names on `CallRecord` mirror Plivo CDRs; fill in exact hangup cause names/codes from your own knowledge (marked ✎).

```python
class CallRecord(BaseModel):            # mirrors Plivo CDR fields
    call_uuid: str
    from_number: str                    # E.164
    to_number: str                      # E.164
    call_direction: Literal["outbound", "inbound"]
    initiation_time: datetime
    answer_time: datetime | None
    end_time: datetime
    call_duration: int                  # seconds
    bill_duration: int
    hangup_cause_name: str              # ✎ real Plivo names, e.g. "Normal Hangup", ...
    hangup_cause_code: int              # ✎ real Plivo codes
    hangup_source: str                  # ✎ e.g. "Carrier", "Callee", "Plivo"
    # simulated enrichment (not in the public CDR, but what an FDE would look up):
    sip_response_code: int | None       # e.g. 503, 403, 484, 401
    carrier_route: str                  # "carrier_x", "carrier_y", "carrier_z"
    account_id: str                     # the big customer
    api_auth_ok: bool                   # did the Make Call API request authenticate

class Alert(BaseModel):
    incident_id: str                    # "INC-<yyyymmdd>-<n>"
    customer_name: str
    account_id: str
    failure_rate: float                 # 0.34
    baseline_rate: float                # 0.02
    since: datetime                     # first bad bucket
    direction: str
    window_calls: int
    fired_at: datetime

class Evidence(BaseModel):
    id: str
    at: datetime
    check: str                          # "auth_check", "numbering_check", "carrier_breakdown", ...
    summary: str                        # one human sentence, spoken-friendly
    data: dict                          # numbers backing the summary
    supports: list[str]                 # hypothesis ids
    refutes: list[str]

class Hypothesis(BaseModel):
    id: Literal["carrier_rejection", "number_formatting", "auth_config"]
    title: str
    status: Literal["open", "ruled_out", "leading", "confirmed"]
    confidence: float                   # computed, 0..1
    match_share: float
    stable: bool
    reason: str                         # why ruled out / why leading

class CallAttempt(BaseModel):
    attempt_id: str
    purpose: Literal["initial", "retry", "callback", "on_demand"]
    route: str                          # agora_phone | agora_rtc | plivo_bridge | mock
    agora_agent_id: str | None
    placed_at: datetime
    state: Literal["placing", "ringing", "answered", "ended", "failed"]
    hangup_reason: str | None
    answered_at: datetime | None
    ended_at: datetime | None
    verdict_told: Literal["none", "unsure", "confident"] = "none"

class ActionRecord(BaseModel):
    kind: Literal["ticket", "email_draft", "incident_summary"]
    status: Literal["proposed", "confirmed", "running", "failed", "retrying", "done"]
    attempts: int
    external_id: str | None             # Linear issue identifier, e.g. "INC-42"
    external_url: str | None
    error: str | None
    payload: dict

class Milestones(BaseModel):            # drives the stage timer
    alert_at: datetime | None
    call_placed_at: datetime | None
    acknowledged_at: datetime | None    # first answered call
    diagnosed_at: datetime | None       # confident verdict spoken
    ticket_at: datetime | None
    manual_baseline_seconds: int        # from config; you time this yourself

class CaseFile(BaseModel):
    incident_id: str
    state: IncidentState
    alert: Alert | None
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]            # append-only
    leading: str | None                 # hypothesis id
    verdict: Literal["unsure", "leaning", "confident"]
    top_confidence: float
    calls: list[CallAttempt]
    actions: list[ActionRecord]
    email_draft: str | None
    milestones: Milestones
    transcript: list[dict]              # turns seen by the proxy, for summary + dashboard
    version: int                        # increments on every write; dashboard + proxy use it
```

**Store API** (`store.py`): `get() -> CaseFile` (deep copy), `async update(fn)` (lock, mutate, bump version, snapshot, publish), `subscribe() -> AsyncIterator[CaseFile]`, `reset()`.

---

## 4. Scenario, detection, investigation, confidence

### 4.1 The story (fictional, realistic)

- **Customer:** "Northwind Health" (fictional), outbound appointment-reminder calls, ~1,200 calls/hour, three carrier routes.
- **True cause:** from 10:42, carrier X starts rejecting calls on its route (SIP 503, ✎ matching Plivo hangup cause).
- **Decoys:** number formatting (SIP 484 / invalid destination) and auth/config (401/407, API auth failure) both look plausible at first and get ruled out.
- **Why confidence starts low, honestly:** in the first minutes, the failure window still contains baseline noise (normal no-answers, busies, a few random rejections) plus a burst of retries the customer's dialler fires. Only ~55% of the failures carry the carrier X signature. As the window fills with post-10:42 traffic, the share rises to ~82% and stays steady.

### 4.2 Scripted timeline (`scenario/carrier_rejection.yaml`)

Times are scenario seconds from "trigger". The clock supports a speed factor (dev: 10×), pause, and manual advance (`/demo/advance`, also a dashboard hotkey) as a stage-safety valve.

| T | Event | Case file effect | Confidence (carrier X) |
|---|---|---|---|
| −300…0 | Healthy traffic, 2% failures | baseline established | — |
| 0 | Carrier X starts rejecting; failure rate climbs | — | — |
| +20 | Detector fires (rate 34% vs 2%, ≥200 calls in window) | `ALERTED`, alert facts written | — |
| +21 | Orchestrator places call #1 | `CALLING` | — |
| +25 | `auth_check`: 100% of API requests authenticated, 0 × 401/407 | auth_config → **ruled out** | — |
| +35 | `numbering_check`: failed numbers are valid E.164; same numbers succeeded on carrier Y last week | number_formatting → **ruled out** | — |
| +40 | `carrier_breakdown` (partial window) | carrier_rejection **leading** | **55%** |
| gate | `carrier_breakdown` (full window) + `route_health` (carriers Y/Z healthy) | carrier_rejection **confirmed** | **92%** |

**The gate (important for determinism):** the final evidence stage releases at `max(T+120s, first_call_ended + 30s)`. This is how the simulator decides when the "late" data arrives; it guarantees that call #1 gets the honest "not sure" and the callback gets the diagnosis, however long you chat on call #1. The confidence itself is never faked; only the arrival time of simulated data is scripted. Configurable via `GATE_MODE=call_end|fixed`.

### 4.3 Detector

- Sliding window of 60 s buckets over the last 5 minutes, per `account_id`, outbound.
- Fire when `failure_rate ≥ 20%` **and** `≥ 3×` the trailing-hour baseline **and** `≥ 200` calls in the window. Fires once per incident (latched until reset).
- "Failure" = not answered for a reason other than normal no-answer/busy, i.e. classified by hangup cause (✎ your list).

### 4.4 Confidence formula (pure function, unit-tested)

For each hypothesis *h*, over the failures in the current window:

```
match_share(h)  = failures matching h's signature / all failures in window
stable(h)       = match_share(h) ≥ 0.70 in each of the last 3 one-minute buckets
corroborated(h) = h-specific disconfirming alternative is healthy
                  (carrier_rejection: other carriers' failure rate ≤ 2× baseline)

confidence(h)   = min(0.99, match_share(h) + 0.05·stable(h) + 0.05·corroborated(h))
                  and 0 if h is ruled out
```

Signatures:
- **carrier_rejection:** `carrier_route == leading_route` and `sip_response_code in {503, 403}` (✎ + matching hangup cause names)
- **number_formatting:** `sip_response_code == 484` or the destination fails E.164 validation
- **auth_config:** `api_auth_ok == false` or `sip_response_code in {401, 407}`

Verdict bands (code-side, injected into the prompt):
- `< 0.60` → **unsure**: the agent may name the leading hypothesis only as "leading, not confirmed", must list what's ruled out, must offer a callback.
- `0.60 – 0.85` → **leaning**: same as unsure, but may say "most likely".
- `≥ 0.85` → **confident**: may state the cause, the evidence and the runbook fix, and propose actions.

Designed numbers: stage 3 → 0.55 + 0 + 0 = **55%**; final → 0.82 + 0.05 + 0.05 = **92%**.

### 4.5 Checks

Hypothesis checks run on the staged timeline. **Targeted checks** are the slow path the engineer can trigger by voice; each takes a real 3–5 s (it scans the simulated CDR store, with a minimum duration so the filler moment is visible):

| Targeted check | Spoken trigger examples | Returns |
|---|---|---|
| `compare_carriers` | "are other carriers OK?", "is it just one route?" | per-carrier failure rates |
| `sample_failed_call` | "give me an example call", "a call UUID" | one call UUID + SIP code + hangup cause |
| `recent_config_changes` | "did the customer change anything?" | "no config changes in the last 24 h" (from the profile) |

---

## 5. The voice conversation

### 5.1 Agora agent properties (built in `voice/agent_properties.py`)

```jsonc
{
  "channel": "inc-<incident>-<attempt>",
  "token": "<minted RTC token>",
  "agent_rtc_uid": "1001",
  "remote_rtc_uids": ["1002"],
  "idle_timeout": 30,
  "labels": { "incident_id": "...", "attempt_id": "..." },   // echoed in webhooks
  "asr":  { "vendor": "<ares|deepgram>", "language": "en-US" }, // [DECIDE in S4 on accuracy for SIP/IP terms]
  "llm":  {
    "vendor": "custom",
    "url": "https://<public-host>/v1/chat/completions?attempt=<attempt_id>",
    "api_key": "<PROXY_SHARED_SECRET>",
    "max_history": 20,
    "greeting_message": "<code-rendered opening>",
    "failure_message": "Sorry, give me a second, I lost that. Could you say it again?",
    "params": { "model": "proxy" }
  },
  "tts":  {
    "vendor": "murf",
    "params": { "api_key": "<MURF_KEY>", "base_url": "wss://global.api.murf.ai/v1/speech/stream-input",
                "voiceId": "<pick in S1>", "model": "FALCON", "locale": "en-US", "sample_rate": 24000 }  // 8000/16000 may suit PSTN; test in S1
  },
  "turn_detection": { "interrupt_mode": "adaptive" }   // fewer accidental barge-ins on a noisy stage
}
```

- **`?attempt=` in the LLM URL** tells the proxy which call a turn belongs to, without relying on undocumented request fields **[UNVERIFIED that query strings are preserved; check in S2. Fallback: there's only ever one active call, so the proxy uses the active attempt.]**
- The proxy checks `Authorization: Bearer <PROXY_SHARED_SECRET>` and rejects everything else.

### 5.2 Openings (code templates, `voice/greetings.py`)

- **Call #1:** "Hi Archit, this is your incident agent. Northwind Health's outbound calls started failing at 10:42. About 34 percent are failing right now against a normal 2 percent. I've already started digging. What do you want to know?"
- **Callback (confident):** "It's me again. Found it. Carrier X has been rejecting Northwind's calls since 10:42, 92 percent confident. Want the details, or should I go straight to the fix?"
- **Telegram-triggered:** same as call #1, prefixed with "You asked me to call."

Only alert-time facts or already-computed verdicts, so the first words never wait on anything.

### 5.3 The LLM proxy, per turn

```
Agora POST /v1/chat/completions (stream=true, messages=[system?, ...history, user])
  1. auth check; resolve attempt + case file snapshot (version N)
  2. router.classify(last_user_text) → FAST | SLOW(check_id) | CONFIRM_YES | CONFIRM_NO | END
  3. context.build(case_file, rag.top_k(last_user_text, k=3), verdict_policy, call_purpose)
       → replaces Agora's system message with ours (≈600–900 tokens total)
  4a. FAST:   stream Groq answer, sentence-chunked, straight through as SSE
  4b. SLOW:   emit custom_metadata {interruptable:false}
              emit filler chunk ("Give me five seconds, I'm checking the other carriers.")
              run check (3–5 s) → write Evidence → rebuild context
              stream Groq answer in the SAME response
  4c. CONFIRM_YES (only valid in state CONFIRM_ACTIONS):
              transition → EXECUTE; executor.start() in background
              stream "On it. Raising the ticket and drafting the email now."
  5. append the turn to case_file.transcript; record per-hop timings
```

**Why a deterministic router instead of letting the LLM pick tools:** a tool decision found mid-stream is too late to send the `custom_metadata` first chunk, and a misfire on stage is fatal. Regex + keyword lists over the 10–15 phrasings you'll actually say are fully predictable. The LLM still gets **one tool** (`run_targeted_check`) as a safety net for phrasings the router misses; if it fires, the proxy runs the check and streams a second LLM pass (no filler, slightly slower, still correct). This keeps the "tool calling" requirement honest while keeping the demo deterministic.

**Model call:** Groq `openai/gpt-oss-120b`, `reasoning_effort: "low"`, `temperature: 0.3`, `max_tokens: 160` (spoken answers stay short), reasoning hidden. Fallback to `llama-3.3-70b-versatile` on 429/5xx or TTFT > 1.5 s, one line in config.

### 5.4 System prompt shape (`llm/prompts/` + `context.py`)

```
[PERSONA] You are an on-call incident agent on a phone call with Archit, a telecom engineer.
Speak like a calm senior engineer. 1–3 short sentences. No lists, no markdown, no symbols.
Say numbers as words a person would say. Skip telecom basics.

[RULES]
- Facts come ONLY from CASE FILE and KNOWLEDGE below. If it's not there, say you don't know yet.
- VERDICT POLICY is binding. Never state a root cause unless the policy says CONFIDENT.
- Never claim an action happened unless ACTIONS shows it done.

[VERDICT POLICY]  ← rendered by code
UNSURE (55%). Leading: carrier rejection on carrier X (not confirmed).
Ruled out: number formatting (all failed numbers valid E.164), auth (100% API auth OK).
If asked for the cause: say you're not sure yet, give the two ruled-out items, offer a callback.

[CASE FILE v17]  ← compact rendering, ~300 tokens
[KNOWLEDGE]  ← top-3 BM25 chunks with source names, e.g. (runbook: carrier-rejection.md)
[ACTIONS]  ← proposed/running/done
[CALL] purpose=initial, elapsed=48s
```

### 5.5 Action gating (code, not prompt)

```
CONFIDENT verdict spoken  → state CONFIRM_ACTIONS (agent asks: "Want me to raise a ticket and draft the email?")
  "yes / go ahead / do it" → EXECUTE        (router: CONFIRM_YES, only accepted in CONFIRM_ACTIONS)
  "no / not yet"           → back to IN_CALL, actions stay "proposed"
EXECUTE (background task):
  1. ticket.create()  → first attempt fails on purpose (FAIL_FIRST_TICKET=true): HTTP 503 from a fault-injection wrapper
       speak(APPEND): "The ticket system just returned an error. Retrying."
  2. retry with backoff 1.5 s → success
       speak(APPEND): "Done. The ticket is up in Linear, and the customer email draft is on your screen."
  3. email_draft.render() → stored in case file, shown on dashboard
  4. on call end → incident_summary added to the ticket as a Linear comment
```

The deliberate failure is injected by a wrapper around the real sink (`FaultInjectingSink`), so the real system is genuinely called on the retry.

### 5.6 Latency budget (fast path)

| Hop | Target |
|---|---|
| End-of-speech detection (Agora VAD/turn detection) | 500–800 ms |
| ASR final | inside the above |
| Proxy context build (in-memory + BM25) | < 30 ms |
| Groq TTFT (gpt-oss-120b, low effort, short prompt) | 200–500 ms |
| First sentence complete → Murf Falcon first audio | 150–300 ms |
| PSTN leg | 100–200 ms |
| **Total, end of speech → first audio** | **~1.2–2.0 s** (plan target 2–3 s) |

The proxy logs `t_request, t_first_token, t_first_sentence, t_done` per turn; the dashboard shows the last turn's latency (good material for the pitch).

---

## 6. Interfaces and adapters

### 6.1 Protocols

```python
class VoiceCaller(Protocol):
    async def place_call(self, attempt: CallAttempt, greeting: str) -> str: ...   # returns agora_agent_id
    async def hangup(self, attempt: CallAttempt) -> None: ...
    async def speak(self, attempt: CallAttempt, text: str, priority="APPEND") -> None: ...

class TicketSink(Protocol):
    async def create(self, ticket: TicketPayload) -> TicketRef: ...
    async def append(self, ref: TicketRef, text: str) -> None: ...

class Notifier(Protocol):
    async def missed_call(self, incident: CaseFile) -> None: ...                  # "Call me now" button
    async def on_button(self, handler: Callable[[str], Awaitable[None]]) -> None: ...

class LLMBackend(Protocol):
    def stream(self, messages, tools=None) -> AsyncIterator[str]: ...
```

### 6.2 Config switches (`.env`)

```
VOICE_MODE=agora_phone|agora_rtc|plivo_bridge|mock
LLM_MODE=groq|mock                 # mock = scripted answers keyed by router intent
TICKET_MODE=linear|mock
NOTIFY_MODE=telegram|mock
DEMO_MOCK_FALLBACK=0|1             # 1 forces every adapter except voice to mock
FAIL_FIRST_TICKET=true
GATE_MODE=call_end|fixed
CLOCK_SPEED=1                      # 10 in dev
CONFIDENCE_THRESHOLD=0.85
MANUAL_BASELINE_SECONDS=<you time it>
```

### 6.3 Real implementations, details

- **Agora REST:** Basic auth with Customer ID/Secret; RTC tokens minted server-side with the App Certificate (`agora-token-builder`). Two tokens per phone call (agent UID + SIP UID), one per browser call.
- **SIP trunk (Route B) [UNVERIFIED end-to-end]:** Plivo Zentrunk **outbound (termination) trunk** with **an IP ACL containing Agora's published SIP egress IPs (required by Agora's docs, https://docs.agora.io/en/ai/studio/deploy/sip-trunk) plus a credentials list** (Agora's import form asks for the trunk username/password). Caller ID must be a Plivo number, or Plivo rejects with hangup code 4190. Caller ID = a Plivo number you own. Register the number + trunk with Agora's phone-number management API (v2) or console; pass it as `sip.from_number` in `/call`. Debug checklist from `plan.md`: transport mismatch (UDP/TCP/TLS) fails silently; ~30 s drops mean a session-timer issue.
- **Call status:** Agora webhook `1201` (configure the notification URL in the Agora console) → `/agora/events`, matched by `labels.attempt_id`. **Fallback:** poll the agent status endpoint every 2 s while ringing; "no answer" = not answered within 30 s. Voicemail: turned off on the demo phone; also treat "answered but no user speech for 6 s" as missed.
- **Telegram:** **long polling** in a background task (no public webhook needed, one less failure point). One message: incident title, failure rate, leading hypothesis + confidence, inline button "📞 Call me now" → `callback_query` → orchestrator places an `on_demand` call. Bot must be `/start`ed once beforehand.
- **Tickets: Linear** (decided). A dedicated "Incidents" team in a free Linear workspace, personal API key, GraphQL API (`https://api.linear.app/graphql`):
  - `issueCreate` → title ("Northwind Health: carrier X rejecting outbound calls"), markdown description (impact, start time, evidence, ruled-out hypotheses, runbook fix, customer email draft), priority Urgent, label `incident`, state "In Progress".
  - `commentCreate` → the incident summary at call end (the "follow up" step).
  - Keep the Linear board open on screen during the demo; the issue appears live.
  - **Stretch (Day 2, only if ahead):** route ticket creation through Linear's official MCP server so the pitch can say "tickets via MCP". It needs OAuth, so direct GraphQL stays the default and the fallback.
  - Tier C uses the same Linear call (one HTTP request), so there's no separate worst-case integration.
- **RAG:** BM25 (`rank-bm25`) over ~10 markdown files chunked by heading. Deterministic, zero network, < 5 ms. Reusing your Pipecat embedding setup is an upgrade, not a dependency; BM25 is plenty for 10 files and can't fail on stage.
- **Email draft:** template filled from the case file (customer, start time, impact %, cause, mitigation, next update time). Optional LLM polish pass off the call path, with the template as fallback.

---

## 7. Orchestrator state machine

```
IDLE ──trigger──► MONITORING ──detector fires──► ALERTED ──place call #1──► CALLING
CALLING ──answered──► IN_CALL
CALLING ──no answer/busy/voicemail──► RETRY (wait 15 s, call again) ──no answer──► TELEGRAM_SENT
TELEGRAM_SENT ──button──► CALLING (purpose=on_demand)
IN_CALL ──verdict asked while < threshold──► UNSURE_TOLD
UNSURE_TOLD ──call ends──► WAITING_FOR_EVIDENCE
WAITING_FOR_EVIDENCE ──confidence ≥ threshold──► CALLING (purpose=callback, confident greeting)
IN_CALL (confident) ──agent proposes actions──► CONFIRM_ACTIONS ──"yes"──► EXECUTING ──done──► RESOLVED
RESOLVED ──call ends──► summary appended to ticket; milestones frozen
```

- Transitions are a whitelist in `states.py`; illegal ones raise and are logged (they show up in tests).
- If confidence crosses the threshold **during** a call (e.g. `GATE_MODE=fixed`), no callback: the case file updates and the next answer uses the new verdict. The agent can also announce it via `speak` ("New evidence just came in…").
- The callback fires when confidence ≥ threshold **and** no call is active **and** the last spoken verdict was `unsure`. That's exactly the demo climax.

---

## 8. Dashboard (one page)

Panels, all driven by SSE from `/state/stream`:

1. **Header:** incident ID, customer, state badge, live failure-rate sparkline.
2. **Confidence meter:** big gauge for the leading hypothesis, threshold line at 85%, history ticks (55% → 92%).
3. **Hypotheses + ruled out:** three cards; ruled-out ones struck through with the one-line reason and the evidence that killed them.
4. **Stage timer:** alert → call placed → acknowledged → diagnosed → ticket raised, live-running clocks, with the manual baseline beside it ("Manual: 18 min, Agent: 2 min 40 s").
5. **Live call + transcript:** call state, latency of the last turn, rolling transcript; in browser mode, the incoming-call screen with Answer/Decline.
6. **Actions:** ticket card (with the failed attempt shown, then success + link), email draft preview.

Controls (hidden behind a key, for you only): Trigger, Reset, Advance stage, Pause clock, mock-fallback toggle.

---

## 9. Spikes (Day 1 morning). Exact pass criteria and what each produces

| # | Spike | Steps | Pass means | Timebox | If it fails |
|---|---|---|---|---|---|
| **S0** | Accounts & keys | Agora project (App ID, Certificate, Customer ID/Secret), ConvoAI enabled; Murf API key; Groq key (Developer tier); Plivo work account (approved) + number + Zentrunk; Telegram bot; Linear workspace + API key + Incidents team; tunnel (cloudflared) | `.env` complete, `scripts/check_env.py` passes | 45 min | Blocker. Do this before anything else. |
| **S1** | Agora dials your phone, speaking Murf Falcon | Register Plivo trunk/number with Agora; `/v2/call` with a hard-coded greeting, Agora's native LLM | Your phone rings; you hear the greeting in the Murf voice; 2-way audio for 60 s with no drop | 3 h total (S1+S2) | Try Route A for 1 h max, then Route C (browser) |
| **S2** | Custom LLM on that call | Point `llm.url` at a minimal proxy through the tunnel that injects "The magic word is pineapple" | Proxy logs every turn; asking "what's the magic word" gets "pineapple"; `?attempt=` survives | inside S1 | Browser route for the voice leg (RTC `/join` with the same properties) |
| **S3** | Filler + held stream | Proxy sends `custom_metadata` → filler → `sleep(5)` → answer | Filler plays, ~5 s gap, answer plays fully; no `failure_message`; no timeout | 45 min | Spoken "I'll check, ask me again in a few seconds" and answer on the next turn |
| **S4** | Latency + model behaviour | 10 scripted questions; log the hops | Median end-of-speech → first audio ≤ 2.5 s; no reasoning text spoken; ASR gets "carrier", "SIP", "five oh three" | 45 min | Shorter prompt, `max_tokens` lower, llama-3.3-70b, ASR vendor swap |
| **S5** | Missed call → Telegram | Decline the call | `1201 ended/NO_ANSWER` (or poll) arrives; Telegram message within 5 s; button triggers a new call | 30 min | Poll call status; or skip button (cut list item #1) |

Run S5 and the Claude Code scaffolding in parallel with S1–S4.

---

## 10. Build sequence (hour by hour)

Every block ends with a **done-when** check. Don't move on until it's true.

### Day 1

| Time | Who | Work | Done when |
|---|---|---|---|
| 09:00–09:45 | You | S0 accounts & keys | `check_env.py` green |
| 09:45–10:15 | Cursor | Repo scaffold, `models.py`, `config.py`, `store.py`, `clock.py`, `.env.example`, `.gitignore`; push to GitHub | `uv run uvicorn app.main:app` serves `/state` |
| 10:15–13:00 | You + Cursor | S1 → S2 → S3 → S4 | Each spike pass/fail written into section 12 below |
| 10:15–13:00 | Claude Code (parallel) | Simulator + scenario YAML + detector + investigator + confidence + tests; dashboard skeleton with SSE | `replay_scenario.py` at 10× prints alert → 55% → 92%; tests green |
| 13:00 | You | **Checkpoint 1:** S1+S2 passed? Yes → Route B. No → Route C, stop touching the phone path. | Decision written down |
| 14:00–16:00 | Cursor | Proxy for real: context builder, router, Groq streaming, filler path, transcript, timings | Text harness: full conversation works against the live case file |
| 16:00–17:30 | Cursor | Voice adapters (chosen route) + greetings + orchestrator call #1 | Trigger → phone rings → opening line → one follow-up answered from the case file |
| 17:30–19:00 | Claude Code | RAG index + KB files (you write the runbooks' substance); verdict policy injection; confidence meter + hypotheses on dashboard | "What does the runbook say?" cites the runbook by name |
| 19:00–20:30 | Cursor | Callback loop: unsure → call ends → gate → callback with confident greeting | Full loop runs once end to end |
| 20:30–21:30 | Claude Code | Linear ticket (single `issueCreate`), email template | **Tier C reached:** ring → talk → unsure → callback → confirm → Linear issue |
| 21:30 | You | **Checkpoint 2:** record a rough backup video of whatever works | Video saved |

### Day 2

| Time | Who | Work | Done when |
|---|---|---|---|
| 09:00–10:30 | Cursor | Action gating state machine + executor + fault injection + `speak` narration | "Yes" → spoken failure → spoken retry success → ticket + email on screen |
| 10:30–11:30 | Claude Code | Incident summary threaded onto the ticket at call end; stage timer + manual-vs-agent panel | Summary appears after hangup; timer freezes at RESOLVED |
| 11:30–12:30 | Claude Code + you | Telegram missed-call + "Call me now"; retry logic | Decline twice → Telegram → tap → call |
| 12:00 | You | **Checkpoint 3:** pick the demo tier | Decision written down |
| 12:30–13:30 | You | Time yourself doing the scenario manually (dashboard hidden, raw CDR CSV only) | `MANUAL_BASELINE_SECONDS` set |
| 13:30–14:30 | Cursor | Polish: prompt tuning from rehearsal transcripts, latency fixes, ASR keyword hints | Two clean runs in a row |
| **14:30** | — | **Feature freeze.** Only fixes after this. | — |
| 14:30–18:00 | You | ≥ 5 full rehearsals on the paid Groq tier; reset script; mock-fallback test | 5 runs logged, failures fixed |
| 18:00–19:00 | You | Final backup video; pitch slide (manual vs agent; five-step mapping) | Assets saved in two places |

**Cut order if behind** (from `plan.md`): Telegram button → incident summary → deliberate error → phone path. **Never cut** the callback climax or the ticket action.

---

## 11. Testing and rehearsal

- **Unit:** confidence formula (designed numbers 0.55 and 0.92 hit exactly), detector thresholds, state-machine whitelist, router intents (every rehearsed phrasing has a test).
- **Contract:** proxy emits valid Agora-compatible SSE: `custom_metadata` only as the first chunk, `chat.completion.chunk` deltas, `[DONE]` terminator, no reasoning text.
- **Scenario replay:** full timeline at 100× with every adapter mocked; asserts the exact state sequence `MONITORING → ALERTED → CALLING → IN_CALL → UNSURE_TOLD → WAITING_FOR_EVIDENCE → CALLING → IN_CALL → CONFIRM_ACTIONS → EXECUTING → RESOLVED`.
- **Text harness rehearsal:** the dashboard chat box replays your demo script against the real proxy and Groq; transcript saved for prompt tuning without spending phone minutes.
- **Reset (`scripts/reset.sh`):** stop any active Agora agent, clear the case file and snapshots, reset the clock, delete demo Linear issues (filtered by the `demo` label), clear Telegram state, re-seed the simulator. Target: < 5 s, idempotent.

---

## 12. Spike results log (fill in on Day 1)

| Spike | Result | Notes / numbers | Decision |
|---|---|---|---|
| S0 | | | |
| S1 | | | |
| S2 | | | |
| S3 | | | |
| S4 | | median latency: | |
| S5 | | | |

---

## 13. Risks (updated)

| Risk | Likelihood after research | Mitigation |
|---|---|---|
| Agora can't authenticate/route through Plivo Zentrunk (transport, credentials, codecs) | Medium | Credential auth, try TLS then UDP; check transport mismatch + session timers; Route A for 1 h; then Route C |
| `1201` call-state webhooks don't fire for v2 `/call` | Medium | Poll agent status; ring timeout treated as missed |
| Query string on `llm.url` stripped | Low | Single active call → proxy uses the active attempt |
| gpt-oss reasoning leaks into speech | Medium if unhandled | Hidden reasoning format + strip non-content deltas + contract test |
| Groq 429 during rehearsals | High on free tier | Developer tier before rehearsals; auto-fallback model |
| ASR mishears telecom terms | Medium | ASR keyword hints; router matches loose phrasings; S4 tests |
| Accidental barge-in from stage noise | Medium | Headset mic; `adaptive` interrupt mode; non-interruptible filler |
| Venue Wi-Fi | Medium | Hotspot; Tier D backup video |
| Murf voice sounds poor at PSTN sample rates | Low–medium | Test 8 k/16 k/24 k in S1; pick the clearest |

---

## 14. Decisions needed from you

1. **Ticket system: Linear (decided).** Create a free workspace, an "Incidents" team, and a personal API key.
2. **Plivo: work account (decided).** Get your manager's approval in writing before Day 1. Keep demo resources clearly labelled (one dedicated number, one Zentrunk trunk named e.g. `agora-hackathon-demo`), use only simulated data, and remove the trunk credentials afterwards.
3. **[DECIDE] Hackathon rules:** confirm Plivo, Groq and Telegram are allowed alongside Agora + Murf.
4. **[YOU WRITE] Domain content:** the exact Plivo hangup cause names/codes for each signature, two runbooks (carrier rejection, number formatting), the customer profile, and the demo phrasings you'll actually say on stage (they become router tests).
5. **[DECIDE] Team name:** Second Ring, Pager Whisperer, or Callback.
6. **GitHub:** re-authenticate the `gh` CLI (`gh auth refresh -h github.com`), then pick a repo name and visibility (private recommended).
