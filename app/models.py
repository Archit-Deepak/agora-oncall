"""Shared data models — the contract every other module imports from.

Written first (build-plan.md §3). Nothing here talks to the network or holds
state; these are just the shapes that flow between the simulator, detector,
investigator, orchestrator, voice layer, actions and dashboard.

Field names on CallRecord mirror Plivo Zentrunk CDR fields so the simulated
data looks exactly like what an FDE would pull from the Plivo console.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ── Incident lifecycle (orchestrator state machine, build-plan.md §7) ────────
class IncidentState(str, Enum):
    IDLE = "IDLE"
    MONITORING = "MONITORING"
    SUSPECTED = "SUSPECTED"          # failure rate crossed the threshold; confirming it's sustained
    AUTO_RECOVERED = "AUTO_RECOVERED"  # it was a blip; Telegram sent, still watching
    ALERTED = "ALERTED"
    CALLING = "CALLING"
    IN_CALL = "IN_CALL"
    RETRY = "RETRY"
    TELEGRAM_SENT = "TELEGRAM_SENT"
    UNSURE_TOLD = "UNSURE_TOLD"
    WAITING_FOR_EVIDENCE = "WAITING_FOR_EVIDENCE"
    CONFIRM_ACTIONS = "CONFIRM_ACTIONS"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"            # fix applied; watching the account recover
    RESOLVED = "RESOLVED"


# v2: the LLM investigator works over an open set of failure signatures, so ids are free-form
# (e.g. "carrier_rejection", "cps_burst", "silent_route", "pbx_timeout", "anchoring_4590", "acl_drop").
HypothesisId = str


# ── Telephony data (mirrors Plivo CDR) ──────────────────────────────────────
class CallRecord(BaseModel):
    """One outbound call, shaped like a Plivo Zentrunk CDR row."""

    call_uuid: str
    from_number: str  # E.164
    to_number: str  # E.164
    call_direction: Literal["outbound", "inbound"] = "outbound"
    initiation_time: datetime
    answer_time: datetime | None = None
    end_time: datetime
    call_duration: int = 0  # seconds
    bill_duration: int = 0
    hangup_cause_name: str  # real Plivo names, e.g. "Normal Hangup"
    hangup_cause_code: int  # real Plivo codes, e.g. 3000, 4590
    hangup_source: str  # "Callee", "Carrier", "Plivo", "zentrunk"

    # Simulated enrichment — not in the public CDR, but what an FDE looks up:
    sip_response_code: int | None = None  # 503, 403, 484, 401, 407, 200
    carrier_route: str = "carrier_x"  # carrier_x | carrier_y | carrier_z
    account_id: str  # the big customer's account
    api_auth_ok: bool = True  # did the Make Call API request authenticate
    # v2 enrichment (fleet simulator)
    to_carrier: str = ""  # destination network, e.g. "Vi-Delhi", "Jio", "Airtel"
    audio_energy: float | None = None  # Krisp max noise energy of the callee leg; <= 5 = silent call
    trunk_id: str = ""


class Alert(BaseModel):
    incident_id: str  # "INC-<yyyymmdd>-<n>"
    customer_name: str
    account_id: str
    failure_rate: float  # 0.34
    baseline_rate: float  # 0.02
    since: datetime  # first bad bucket
    direction: str = "outbound"
    window_calls: int
    fired_at: datetime
    signal: str = "failure_rate"  # failure_rate | silent_rate | volume_drop
    signal_value: float | None = None


# ── Investigation ───────────────────────────────────────────────────────────
class Evidence(BaseModel):
    id: str
    at: datetime
    check: str  # "auth_check", "numbering_check", "carrier_breakdown", ...
    summary: str  # one human, spoken-friendly sentence
    data: dict = Field(default_factory=dict)  # numbers backing the summary
    supports: list[str] = Field(default_factory=list)  # hypothesis ids
    refutes: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    id: HypothesisId
    title: str
    status: Literal["open", "ruled_out", "leading", "confirmed"] = "open"
    confidence: float = 0.0  # computed, 0..1
    match_share: float = 0.0
    stable: bool = False
    reason: str = ""  # why ruled out / why leading


# ── Voice ───────────────────────────────────────────────────────────────────
class CallAttempt(BaseModel):
    attempt_id: str
    purpose: Literal["initial", "retry", "callback", "on_demand"]
    route: str  # agora_phone | agora_rtc | plivo_bridge | mock
    agora_agent_id: str | None = None
    placed_at: datetime
    state: Literal["placing", "ringing", "answered", "ended", "failed"] = "placing"
    hangup_reason: str | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    verdict_told: Literal["none", "unsure", "confident"] = "none"


# ── Actions ─────────────────────────────────────────────────────────────────
class ActionRecord(BaseModel):
    kind: Literal["ticket", "email_draft", "incident_summary", "telegram_notice", "loop_in"]
    status: Literal["proposed", "confirmed", "running", "failed", "retrying", "done"] = "proposed"
    attempts: int = 0
    external_id: str | None = None  # Linear issue identifier, e.g. "VPS-42"
    external_url: str | None = None
    error: str | None = None
    payload: dict = Field(default_factory=dict)


class Milestones(BaseModel):
    """Drives the stage timer on the dashboard."""

    alert_at: datetime | None = None
    call_placed_at: datetime | None = None
    acknowledged_at: datetime | None = None  # first answered call
    diagnosed_at: datetime | None = None  # confident verdict spoken
    ticket_at: datetime | None = None
    fix_applied_at: datetime | None = None
    resolved_at: datetime | None = None
    manual_baseline_seconds: int = 0  # from config; you time this yourself


# ── The whole incident ──────────────────────────────────────────────────────
class CaseFile(BaseModel):
    incident_id: str
    state: IncidentState = IncidentState.IDLE
    alert: Alert | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)  # append-only
    leading: str | None = None  # hypothesis id
    verdict: Literal["unsure", "leaning", "confident", "recovered"] = "unsure"
    recent_fail_rate: float | None = None  # last ~20 s, live-traffic mode only
    top_confidence: float = 0.0
    calls: list[CallAttempt] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    email_draft: str | None = None
    milestones: Milestones = Field(default_factory=Milestones)
    transcript: list[dict] = Field(default_factory=list)  # turns seen by the proxy
    # v2: which account, and a live trace of the investigator agent's work
    account_id: str | None = None
    investigator_log: list[dict] = Field(default_factory=list)  # {at, kind: tool|thought|finding|question, ...}
    pending_questions: list[dict] = Field(default_factory=list)  # engineer questions queued for the investigator
    fixes: list[dict] = Field(default_factory=list)  # remediations proposed/applied + verification
    version: int = 0  # bumped on every write; dashboard + proxy watch this
