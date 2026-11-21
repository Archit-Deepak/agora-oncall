"""Spike S1 (v2): Agora's v2 telephony /call API dials ENGINEER_PHONE from PLIVO_NUMBER,
with the full agent config (custom LLM -> our proxy, Murf TTS).

  .venv/bin/python scripts/spike_s1_v2.py call
  .venv/bin/python scripts/spike_s1_v2.py stop <agent_id>
"""
import asyncio
import json
import sys
import uuid

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from app import agora, config  # noqa: E402

SIP_UID = config.USER_UID  # the phone leg joins the RTC channel as this uid


async def call():
    channel = f"tel-{uuid.uuid4().hex[:8]}"
    props = agora.agent_properties(channel, (
        f"Hi {config.ENGINEER_NAME}, this is your incident agent calling from the v2 telephony API. "
        "If you can hear me, the phone path works. Ask me for the magic word."))
    body = {
        "name": f"call-{uuid.uuid4().hex[:10]}",
        "sip": {
            "to_number": config.env("ENGINEER_PHONE"),
            "from_number": config.env("PLIVO_NUMBER"),
            "rtc_uid": str(SIP_UID),
            "rtc_token": agora.rtc_token(channel, SIP_UID),
        },
        "properties": props,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{agora.API}/call", json=body, headers={"Authorization": agora._AUTH})
    print("HTTP", r.status_code, r.text[:800])
    try:
        print("agent_id:", r.json().get("agent_id"))
    except Exception:
        pass


async def stop(agent_id):
    print(await agora.stop_agent(agent_id))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "call":
        asyncio.run(call())
    elif cmd == "stop":
        asyncio.run(stop(sys.argv[2]))
    else:
        print(__doc__)
