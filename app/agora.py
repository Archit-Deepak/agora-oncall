"""Agora RTC tokens and the Conversational AI v2 REST API (start/stop an agent in an RTC channel)."""
import base64
import time
import uuid

import httpx
from agora_token_builder import RtcTokenBuilder

from app import config

API = f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{config.AGORA_APP_ID}"
_AUTH = "Basic " + base64.b64encode(f"{config.AGORA_CUSTOMER_ID}:{config.AGORA_CUSTOMER_SECRET}".encode()).decode()
ROLE_PUBLISHER = 1


def rtc_token(channel: str, uid: int, ttl_s: int = 3600) -> str:
    return RtcTokenBuilder.buildTokenWithUid(
        config.AGORA_APP_ID, config.AGORA_APP_CERTIFICATE, channel, uid, ROLE_PUBLISHER, int(time.time()) + ttl_s
    )


# Probed 21 Nov: idle_timeout also counts BEFORE anyone joins (agent exits "Idle for too long"
# ~idle_timeout s after start). On a phone call the callee only joins on answer, so it must cover
# the ring time; it also sets how long the agent lingers after a hang-up.
PHONE_IDLE_TIMEOUT_S = int(config.env("AGORA_PHONE_IDLE_TIMEOUT", "50"))


def agent_properties(channel: str, greeting: str, idle_timeout: int = 30) -> dict:
    return {
        "channel": channel,
        "token": rtc_token(channel, config.AGENT_UID),
        "agent_rtc_uid": str(config.AGENT_UID),
        "remote_rtc_uids": [str(config.USER_UID)],
        "idle_timeout": idle_timeout,
        "asr": {"vendor": "ares", "language": "en-US", "params": {}},
        # Turn-taking (real call 21 Nov: default 640 ms VAD end-of-speech replied to "um, yeah"
        # and cut off "wait, I was just—"). Semantic end-of-speech judges whether the sentence
        # is finished; pause_state handles "hold on"; a slightly longer barge-in threshold stops
        # coughs/line noise from interrupting the agent.
        "turn_detection": {
            "mode": "default",
            "config": {
                "speech_threshold": 0.5,
                "start_of_speech": {"mode": "vad", "vad_config": {
                    "interrupt_duration_ms": 240, "speaking_interrupt_duration_ms": 240, "prefix_padding_ms": 800}},
                "end_of_speech": {"mode": "semantic", "semantic_config": {
                    "silence_duration_ms": 480, "max_wait_ms": 3000, "pause_state_enabled": True}},
            },
        },
        "interruption": {"enable": True, "mode": "start_of_speech"},
        # Native "thinking" filler if our first token is slow (one per turn, doesn't cancel the LLM).
        "filler_words": {
            "enable": True,
            "trigger": {"fixed_time_config": {"response_wait_ms": 1300}},
            "content": {"mode": "static", "static_config": {
                "phrases": ["Mm, one sec.", "Let me look.", "Okay, checking.", "Right, pulling that up."],
                "selection_rule": "shuffle"}},
        },
        "llm": {
            "vendor": "custom",
            "style": "openai",
            "url": f"{config.PUBLIC_BASE_URL}/v1/chat/completions",
            "api_key": config.PROXY_SHARED_SECRET,
            "system_messages": [],
            "max_history": 20,
            "greeting_message": greeting,
            "failure_message": "Sorry, give me a second, I lost that. Could you say it again?",
            "params": {"model": "incident-proxy"},
        },
        "tts": {
            "vendor": "murf",
            "params": {
                "api_key": config.MURF_API_KEY,
                "base_url": "wss://global.api.murf.ai/v1/speech/stream-input",
                "voiceId": config.MURF_VOICE_ID,
                "locale": "en-US",
                "rate": 0,
                "pitch": 0,
                "model": config.MURF_MODEL,
                "sample_rate": 24000,
            },
        },
    }


async def start_agent(channel: str, greeting: str, idle_timeout: int = 30) -> dict:
    """Join an RTC channel. The browser publishes as USER_UID; nobody is dialled."""
    body = {"name": f"agent-{uuid.uuid4().hex[:12]}",
            "properties": agent_properties(channel, greeting, idle_timeout=idle_timeout)}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{API}/join", json=body, headers={"Authorization": _AUTH})
    return {"status_code": r.status_code, **_json(r)}


async def stop_agent(agent_id: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{API}/agents/{agent_id}/leave", headers={"Authorization": _AUTH})
    return {"status_code": r.status_code, **_json(r)}


async def speak(agent_id: str, text: str, priority: str = "APPEND") -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{API}/agents/{agent_id}/speak", json={"text": text, "priority": priority},
                         headers={"Authorization": _AUTH})
    return {"status_code": r.status_code, **_json(r)}


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:300]}


async def place_phone_call(channel: str, greeting: str, to_number: str, from_number: str) -> dict:
    """Agora v2 telephony: the agent dials `to_number` via the trunk registered for `from_number`.
    Same body shape as scripts/spike_s1_v2.py, which is proven end to end."""
    sip_uid = config.USER_UID
    body = {
        "name": f"call-{uuid.uuid4().hex[:10]}",
        "sip": {"to_number": to_number, "from_number": from_number,
                "rtc_uid": str(sip_uid), "rtc_token": rtc_token(channel, sip_uid)},
        "properties": agent_properties(channel, greeting, idle_timeout=PHONE_IDLE_TIMEOUT_S),
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{API}/call", json=body, headers={"Authorization": _AUTH})
    return {"status_code": r.status_code, **_json(r)}


async def agent_status(agent_id: str) -> dict:
    """GET the agent. `status` is e.g. RUNNING / STOPPED / FAILED; 404 once it is gone."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{API}/agents/{agent_id}", headers={"Authorization": _AUTH})
    return {"status_code": r.status_code, **_json(r)}


async def agent_history(agent_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{API}/agents/{agent_id}/history", headers={"Authorization": _AUTH})
    return {"status_code": r.status_code, **_json(r)}
