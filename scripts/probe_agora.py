"""Probe the Agora ConvoAI agent lifecycle WITHOUT a phone call (RTC /join into an empty channel).

    .venv/bin/python scripts/probe_agora.py

Answers: does status/history/speak work, and when does an agent with nobody in the channel
exit (idle_timeout semantics: matters while a phone is still ringing)?
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import agora  # noqa: E402


async def main() -> None:
    ch = f"probe-{int(time.time())}"
    r = await agora.start_agent(ch, "Hello, this is a probe.")
    aid = r.get("agent_id")
    print("join:", r.get("status_code"), {k: r.get(k) for k in ("agent_id", "status", "create_ts")})
    if not aid:
        print(r)
        return
    t0 = time.time()
    s = await agora.agent_status(aid)
    print(f"status +0s: {s.get('status_code')} {s.get('status')} keys={sorted(s)}")
    h = await agora.agent_history(aid)
    print(f"history: {h.get('status_code')} {str(h)[:300]}")
    sp = await agora.speak(aid, "Testing the speak endpoint.", "APPEND")
    print(f"speak: {sp}")
    last = None
    while time.time() - t0 < 60:
        await asyncio.sleep(4)
        s = await agora.agent_status(aid)
        cur = (s.get("status_code"), s.get("status"))
        if cur != last:
            print(f"status +{time.time() - t0:4.0f}s: {cur} {str({k: s[k] for k in s if k not in ('status_code',)})[:200]}")
            last = cur
        if s.get("status_code") == 404 or s.get("status") in ("STOPPED", "FAILED"):
            break
    st = await agora.stop_agent(aid)
    print("leave:", st.get("status_code"), str(st)[:150])


asyncio.run(main())
