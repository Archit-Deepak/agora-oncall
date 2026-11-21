# V2 spec — spend-aware on-call agent with an LLM investigator

Goal: a tool an FDE with high-spend accounts would actually want. It watches several accounts
weighted by spend, catches failures dashboards miss, works out *who owns* the problem, phones the
engineer for high-value accounts, debugs **live in the background with an LLM investigator** while
on the call, proposes the fix (engineer approves by voice), applies it and **verifies recovery**.

All data is simulated, with real Plivo field names, hangup codes and product facts. Fictional
customer names only (never real Plivo customers).

## Architecture

```
FleetTraffic (app/fleet.py)  ── CDRs, edge logs, account state, faults, fixes
      │
      ├── AccountWatch (app/fleet_detect.py) ── per-account signals → Alert(signal=…)
      │
      ├── tools (app/tools.py) ── deterministic query functions + OpenAI tool schemas
      │        ▲
      │        │ tool calls
      │   InvestigatorAgent (app/investigator_agent.py, Groq tool-calling loop, background)
      │        │ writes findings / answers
      ▼        ▼
   CaseFile (store) ◄── voice LLM proxy (brain_proxy) reads it every turn, never waits
      │                    engineer questions → case.pending_questions → investigator
      ▼                    answers/“found it” → Agora /speak break-in
   Orchestrator: MONITORING → SUSPECTED → (confirm) → call → CONFIRM_FIX → apply → verify → RESOLVED
```

Rule: **the LLM proposes, code verifies.** Numbers come from tools. A per-signature code check
(app/verify.py) must agree before the agent may say "found it".

## Accounts (app/accounts.py) — fictional, India

| id | name | use case | traffic | spend ₹/day | notes |
|---|---|---|---|---|---|
| acct_northwind | Northwind Health | appointment-reminder calls, outbound Zentrunk | ~3 calls/s | 48,000 | HA **disabled**, CPS limit 10 |
| acct_zylo | Zylo Lending | collections predictive dialer, outbound SIP trunk | ~2 calls/s, bursty | 22,000 | CPS limit 4 (100 concurrency) |
| acct_medley | Medley Voice AI | AI voice agents (audio streaming / Zentrunk), Krisp NC on | ~2 calls/s | 35,000 | media region ap-south-1 (Mumbai) |
| acct_brightline | Brightline BPO | inbound support lines → customer's PBX over inbound trunk | ~1 call/s inbound | 9,000 | low spend → Telegram tier |

`AccountProfile` (dataclass/pydantic): id, name, use_case, spend_per_day_inr, plan, cps_limit,
concurrency, ha_enabled, caller_ids (list, E.164 +91…), media_region, ip_acl (list of CIDRs),
trunk_id, direction ("outbound"|"inbound"), contacts (list of {name, role, email}),
change_log (list of {at: datetime, change: str}), past_incidents (list of {date, summary, fix}),
outbound carriers (e.g. ["TATA", "Airtel", "Jio"]) with weights, destination networks
(e.g. ["Jio", "Airtel", "Vi-Delhi", "BSNL"]) with weights.

Spend at risk: `spend_per_day_inr / 86400 * failure_or_silent_share * 3600` → ₹/hour.

## Fault modes (operator picks account + fault on the dashboard)

| fault | signature in the data | owner | fix(es) the agent can propose → effect |
|---|---|---|---|
| `carrier_reject` | one outbound carrier (TATA) → 5350 `service_unavailable_by_carrier` SIP 503, source Carrier; short retry-overflow blips on the other carriers decaying (2% + 45%·e^(−t/15s)) | carrier | `enable_ha` (only if `ha_enabled` is False; costs a 2nd number rental): failed-carrier calls now retry via backup number from another carrier and succeed (CDR `from_number` = backup number) → failure rate drops |
| `cps_burst` | dialer bursts at ~3× `cps_limit` in waves; calls over the limit → 5180 `cps_limit_reached`, source Zentrunk; per-second peaks visible | customer dialer | `pace_dialer` (customer action, free) or `raise_cps` (buy concurrency: +1 CPS per +25, ~₹1,200/mo per CPS) → 5180s stop |
| `silent_route` | calls via TATA to destination `Vi-Delhi` answer normally (3000/3010) but `audio_energy` ≤ 5 and short durations (8–20 s, callee hangs up); everything else normal energy 40–90. **Failure rate stays healthy.** | carrier route | `reroute_destination` (to_carrier=Vi-Delhi via Jio) → energy normal again |
| `pbx_timeout` | inbound calls to the customer's PBX → 4170 `request_timeout_customer`, source Zentrunk | **customer** | no Plivo-side fix: `notify_customer` (email asking them to check PBX reachability / SIP OPTIONS); recovery only when operator clicks "all pass" |
| `anchoring_4590` | change_log entry "media/agent infra moved to us-east-1" at fault time; +91 caller ID → +91 destinations rejected 4590 `domestic_anchored_terms_not_met`; non-Indian destinations fine | integration config | `switch_caller_id` (use a non-Indian caller ID) or `move_media_to_india` → recovers |
| `acl_drop` | account's CDR volume drops ~95% (INVITEs never reach a call); `edge_log` shows many "INVITE from 34.93.x.x rejected: source IP not in trunk IP ACL" for the account's trunk; change_log "customer migrated SBC/egress to new IP" | customer config | `add_ip_to_acl` (ip) → volume returns |
| (none) / all pass | healthy: ~85% answered normal, busy 4410, no-answer 6030, cancel 4550, ~1–2% real failures (4340/4160), audio_energy 40–90 on answered | | |

Multiple faults can be active on different accounts; one incident at a time in the orchestrator
(the highest ₹-at-risk account wins).

## FleetTraffic API (app/fleet.py) — new module; app/traffic.py stays untouched for old tests

```python
class FleetTraffic:
    def __init__(self, anchor: datetime, seed: int = 7, accounts: dict[str, AccountProfile] | None = None)
    accounts: dict[str, AccountProfile]          # live, mutable (fixes change them)
    running: bool
    def start(self, now=None); def stop(self, now=None)
    def set_fault(self, account_id: str, fault: str | None, now=None)   # None = healthy
    def clear_all(self, now=None)                                        # "all calls pass"
    def faults(self) -> dict[str, dict]          # {account_id: {"fault": str, "since": datetime}}
    def apply_fix(self, account_id, fix: str, params: dict | None = None, now=None) -> dict
        # {"ok": bool, "message": str, "cost": str|None}; validates fix vs fault/profile
    def calls_through(self, t_seconds: float) -> list[CallRecord]      # all accounts (orchestrator compat)
    def calls(self, account_id: str | None, since: datetime, until: datetime) -> list[CallRecord]
    def edge_log(self, account_id: str, since: datetime, until: datetime) -> list[dict]
    def expected_rate(self, account_id: str) -> float                   # calls/s when healthy
    def recent(self, limit=60, account_id=None) -> list[CallRecord]
    def stats(self, now=None, window_s=60) -> dict
        # {"running", "accounts": {id: {name, spend_per_day_inr, calls, failed, fail_rate,
        #   answered, silent, silent_rate, volume_ratio, at_risk_inr_per_hour, fault, top_causes{}, spark[]}}}
```
Lazy generation up to "now" like `app/traffic.py` (mode changes flush first). Deterministic per
seed. Prune > 15 min. Each CallRecord sets `account_id`, `trunk_id`, `to_carrier`, `audio_energy`
(answered calls only), `carrier_route` (= outbound carrier name, e.g. "TATA"), `call_direction`.

## AccountWatch (app/fleet_detect.py)
`AccountWatch(window_s=60, fail_rate=0.20, silent_rate=0.15, volume_ratio=0.5, min_calls=60)`
`.check(fleet, now) -> Alert | None` — per account, the first breached signal; latches per account;
`.rearm(account_id)`. Alert.account_id/customer_name/signal/signal_value/failure_rate filled.
`is_failure` from app.detector: also count 5180, 4170, 4590 as failures (they are non-200 SIP
already — make sure their sip codes are not in NON_FAILURE_SIP: use 503/486/487 conventions:
5180 → SIP 503, 4170 → 408, 4590 → 403, 5350 → 503).

## Tools (app/tools.py) — deterministic, compact JSON (LLM-friendly, ≤ ~40 lines each)
All take `ctx` (`ToolContext(fleet, now)`) + args; scoped to one account.
- `overview(account_id, window_s=60)` — volume vs expected, fail rate, silent rate, top 5 hangup causes, ₹ at risk/h
- `breakdown(account_id, by, window_s=60, only_failures=False)` — by ∈ carrier|to_carrier|hangup_cause|direction|from_number|hangup_source
- `cps_profile(account_id, window_s=60)` — per-second peak, p95, cps_limit, count_5180, burst pattern
- `audio_by_route(account_id, window_s=120)` — per carrier×to_carrier: answered, silent, silent_rate, median energy
- `volume_trend(account_id, minutes=5)` — calls/min per minute vs expected
- `sample_calls(account_id, hangup_code=None, carrier=None, to_carrier=None, silent=None, n=3)`
- `edge_log(account_id, window_s=120)` — ingress rejections grouped by source IP + reason
- `account_profile(account_id)` — plan, CPS limit, HA, caller IDs, media region, IP ACL, contacts, change_log (last 24h)
- `past_incidents(account_id)`
- `search_kb(query, k=3)` — KnowledgeBase over app/kb/*.md
- `TOOL_SPECS: list[dict]` OpenAI function-calling schemas; `run_tool(name, args, ctx) -> dict` (errors → {"error": ...})

KB files to add (app/kb/, short, factual, heading-chunked): `ha_numbers.md` (Plivo India HA
numbers: why no auto carrier failover in India — numbers tied to carriers by regulation; HA rents a
backup number from a different carrier, separate rental at standard India local rate; retries once
via fallback on carrier fault; Voice API + Zentrunk domestic India; outside India failover is
automatic), `cps_limits.md` (5180 on SIP trunks vs queueing on Voice API; India 25 concurrency =
1 CPS; options pace / buy concurrency / move to Voice API), `silent_calls.md` (Krisp max noise
energy ≤ 5 on answered calls = silent callee leg; compare carriers within an account; reroute
destination), `pbx_timeout.md` (4170 = customer PBX didn't answer the INVITE; check reachability,
SIP OPTIONS, firewall; transport field on 4170 rows is not evidence), `anchoring_4590.md`
(domestic anchoring: India→India calls must have media anchored in India; fix: Indian media or
non-Indian caller ID), `ip_acl.md` (INVITEs from IPs not in the trunk ACL are dropped before a CDR
exists → volume drop with no failures; check edge logs; add the IP). Plus one customer profile md per account.

## Tests (tests/test_fleet.py, tests/test_tools.py)
Each fault produces its signature (tools show it; AccountWatch fires the right signal within ~30 s
of the fault at the right account and not others); each valid fix recovers within ~30 s; invalid
fixes are rejected with a reason; silent_route keeps fail_rate < 5% while silent_rate > 15%;
acl_drop gives volume_ratio < 0.3 and edge_log entries; tools return small JSON; run_tool handles
bad args. All existing tests keep passing.
