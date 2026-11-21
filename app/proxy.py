"""OpenAI-compatible /v1/chat/completions endpoint that Agora calls on every turn.

Replaces Agora's system context with ours, streams Groq's answer back as SSE, and forwards
only `content` deltas (gpt-oss reasoning must never reach TTS).
"""
import json
import time
from collections import deque

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import config

router = APIRouter()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

turns: deque[dict] = deque(maxlen=50)

SYSTEM_PROMPT = f"""You are an on-call incident agent on a voice call with {config.ENGINEER_NAME}, a telecom engineer.
Speak like a calm senior engineer. Reply in one to three short sentences. No lists, no markdown, no symbols.
Say numbers the way a person would say them out loud.
This is a connectivity test. If asked for the magic word, it is "pineapple"."""


def _chunk(cid: str, model: str, content: str | None = None, finish: str | None = None) -> str:
    delta = {"content": content} if content is not None else {}
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _build_messages(incoming: list[dict]) -> list[dict]:
    # Agora adds fields like `metadata` to history messages; Groq rejects anything beyond role/content.
    history = [{"role": m["role"], "content": m.get("content") or ""}
               for m in incoming if m.get("role") in ("user", "assistant")]
    return [{"role": "system", "content": SYSTEM_PROMPT}, *history]


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request):
    if request.headers.get("authorization", "") != f"Bearer {config.PROXY_SHARED_SECRET}":
        raise HTTPException(status_code=401, detail="bad proxy key")

    body = await request.json()
    t0 = time.perf_counter()
    messages = _build_messages(body.get("messages", []))
    user_text = next((m.get("content", "") for m in reversed(messages) if m["role"] == "user"), "")
    turn = {"turn_id": body.get("turn_id"), "user": user_text, "agent": "", "query": dict(request.query_params),
            "ttft_ms": None, "total_ms": None, "model": config.GROQ_MODEL, "error": None}
    turns.append(turn)
    cid = f"chatcmpl-{body.get('turn_id', int(t0 * 1000))}"

    async def stream():
        groq_body = {"model": config.GROQ_MODEL, "messages": messages, "stream": True,
                     "temperature": 0.3, "max_tokens": 300, "reasoning_effort": "low", "include_reasoning": False}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as c:
                async with c.stream("POST", GROQ_URL, json=groq_body,
                                    headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"}) as r:
                    if r.status_code != 200:
                        turn["error"] = f"groq {r.status_code}: {(await r.aread()).decode()[:200]}"
                        yield _chunk(cid, config.GROQ_MODEL, "Sorry, I hit a snag on my side. Ask me again?")
                    else:
                        async for line in r.aiter_lines():
                            if not line.startswith("data: ") or line == "data: [DONE]":
                                continue
                            delta = json.loads(line[6:])["choices"][0].get("delta", {})
                            text = delta.get("content")
                            if text:
                                if turn["ttft_ms"] is None:
                                    turn["ttft_ms"] = round((time.perf_counter() - t0) * 1000)
                                turn["agent"] += text
                                yield _chunk(cid, config.GROQ_MODEL, text)
        except Exception as e:  # network errors: say something rather than go silent
            turn["error"] = repr(e)[:200]
            yield _chunk(cid, config.GROQ_MODEL, "Sorry, I lost my connection for a second.")
        turn["total_ms"] = round((time.perf_counter() - t0) * 1000)
        print(f"[turn {turn['turn_id']}] ttft={turn['ttft_ms']}ms total={turn['total_ms']}ms "
              f"user={turn['user']!r} agent={turn['agent']!r} err={turn['error']}", flush=True)
        yield _chunk(cid, config.GROQ_MODEL, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
