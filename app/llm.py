"""LLM client: Azure OpenAI (student credits) + Groq (free tier), as one priority chain.

Model ids: plain ids are Groq ("openai/gpt-oss-120b"); "azure:<deployment>" is Azure OpenAI.

- Investigator (non-streaming, tool calling): Azure gpt-5-mini → Azure gpt-4.1-mini →
  Groq gpt-oss-20b → Groq qwen. Tried in priority order; a model is skipped while its rolling
  60 s token ledger has no headroom, and after a 429 until its window rolls.
- Voice (streaming, content only): Groq gpt-oss-120b first (fastest first word, ~0.35–0.6 s
  measured from the VM), HEDGED with Azure gpt-4.1-mini: if Groq hasn't produced a word
  within VOICE_HEDGE_S (or 429s / errors), Azure starts too and whichever speaks first wins.
- Groq free tier = 8,000 tokens/min PER MODEL, so voice and investigator never share a Groq
  model. qwen needs reasoning_effort "none" (with reasoning on it hallucinates tool names);
  gpt-oss uses "low" and hides reasoning so it never reaches TTS.
- Azure: gpt-5* are reasoning models (max_completion_tokens incl. reasoning, no temperature);
  gpt-4.1* take max_tokens + temperature. api-versions verified on 21 Nov 2026.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from typing import AsyncIterator

import httpx

from app import config

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TPM = 8000          # free tier, per model
AZURE_TPM = 100_000      # well under the deployment quota; only used to pace bursts
RETRYABLE = {408, 409, 413, 429, 500, 502, 503, 504}
VOICE_HEDGE_S = 1.2      # start the backup voice model if the primary hasn't spoken by then
VOICE_DEADLINE_S = 6.0   # never leave more than this of dead air: say a holding line instead

AZURE = "azure:"


def _azure_models(*deps: str) -> list[str]:
    if not (config.AZURE_OPENAI_ENDPOINT and config.AZURE_OPENAI_KEY):
        return []
    return [AZURE + d for d in deps if d]


VOICE_PRIMARY = config.GROQ_MODEL or "openai/gpt-oss-120b"
# Groq's second model first (separate free-tier quota, ~0.4 s), then Azure (~1-2 s): sims 21 Nov showed
# 429s on gpt-oss-120b costing 2.4-3.1 s when the fallback went straight to Azure.
VOICE_BACKUPS = [config.GROQ_FALLBACK_MODEL or "openai/gpt-oss-20b"] + _azure_models(config.AZURE_OPENAI_DEPLOYMENT_MINI)
VOICE_MODELS = [VOICE_PRIMARY, *VOICE_BACKUPS]
INVESTIGATOR_MODELS = (_azure_models(config.AZURE_OPENAI_DEPLOYMENT, config.AZURE_OPENAI_DEPLOYMENT_MINI)
                       + ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"])

_ledger: dict[str, deque] = {}  # model → deque[(t, tokens)]

# Characters some models emit that TTS reads oddly (narrow/no-break spaces, soft hyphens…).
_SPACES = re.compile("[      　]")
_DROP = re.compile("[­​‌‍⁠﻿]")


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def clean_for_tts(text: str) -> str:
    """Last line of defence before Murf reads it (sim 21 Nov: "pace_dialer", "cps_limit_reached")."""
    text = _SPACES.sub(" ", text)
    text = _DROP.sub("", text)
    text = _UUID.sub("that call", text)
    text = re.sub(r"\bacct_", "", text)
    text = re.sub(r"\s*\bUTC\b", "", text)
    text = text.replace("_", " ")
    return text.replace("‑", "-").replace("‐", "-")


# ── per-model wire details ─────────────────────────────────────────────────
def is_azure(model: str) -> bool:
    return model.startswith(AZURE)


def _is_reasoning(model: str) -> bool:
    dep = model[len(AZURE):] if is_azure(model) else model
    return dep.startswith(("gpt-5", "o1", "o3", "o4"))


def _url_headers(model: str) -> tuple[str, dict]:
    if is_azure(model):
        dep = model[len(AZURE):]
        ver = "2025-04-01-preview" if _is_reasoning(model) else "2024-10-21"
        return (f"{config.AZURE_OPENAI_ENDPOINT}/openai/deployments/{dep}/chat/completions?api-version={ver}",
                {"api-key": config.AZURE_OPENAI_KEY})
    return GROQ_URL, {"Authorization": f"Bearer {config.GROQ_API_KEY}"}


def _body(model: str, messages: list[dict], max_tokens: int, temperature: float, **extra) -> dict:
    body: dict = {"messages": messages, **extra}
    if is_azure(model):
        if _is_reasoning(model):
            # reasoning tokens count against the cap; leave room for the actual answer
            body["max_completion_tokens"] = max(max_tokens * 3, 2500)
            body["reasoning_effort"] = "low"
        else:
            body["max_tokens"], body["temperature"] = max_tokens, temperature
        return body
    body.update(model=model, max_tokens=max_tokens, temperature=temperature)
    body.update({"reasoning_effort": "none"} if model.startswith("qwen/")
                else {"reasoning_effort": "low", "include_reasoning": False})
    return body


def _tpm(model: str) -> int:
    return AZURE_TPM if is_azure(model) else GROQ_TPM


# ── token ledger ───────────────────────────────────────────────────────────
def used_last_minute(model: str) -> int:
    q = _ledger.setdefault(model, deque())
    cutoff = time.monotonic() - 60
    while q and q[0][0] < cutoff:
        q.popleft()
    return sum(n for _, n in q)


def _record(model: str, tokens: int) -> None:
    _ledger.setdefault(model, deque()).append((time.monotonic(), tokens))


def estimate_tokens(messages: list[dict], tools: list[dict] | None, max_tokens: int) -> int:
    chars = len(json.dumps(messages, default=str)) + (len(json.dumps(tools)) if tools else 0)
    return chars // 4 + max_tokens // 2


def headroom(model: str) -> int:
    return _tpm(model) - used_last_minute(model)


class LLMError(RuntimeError):
    pass


# ── investigator: non-streaming, tool calling ──────────────────────────────
async def chat(messages: list[dict], tools: list[dict] | None = None, *, models: list[str] | None = None,
               max_tokens: int = 700, temperature: float = 0.1, timeout_s: float = 30.0,
               max_wait_s: float = 12.0) -> dict:
    """Returns the assistant message + "_model", "_ms", "_tokens". Tries models in priority order."""
    models = models or INVESTIGATOR_MODELS
    need = estimate_tokens(messages, tools, max_tokens)
    waited, last = 0.0, None
    while True:
        ready = [m for m in models if headroom(m) >= need]
        if not ready and waited < max_wait_s:
            await asyncio.sleep(1.0)  # let a 60 s window roll instead of eating a 429
            waited += 1.0
            continue
        for model in ready or models:
            url, headers = _url_headers(model)
            body = _body(model, messages, max_tokens, temperature)
            if tools:
                body["tools"], body["tool_choice"] = tools, "auto"
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5)) as c:
                    r = await c.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = f"{model}: {e!r}"
                continue
            if r.status_code == 200:
                j = r.json()
                total = (j.get("usage") or {}).get("total_tokens") or need
                _record(model, total)
                choice = (j.get("choices") or [{}])[0]
                msg = dict(choice.get("message") or {})
                if not msg.get("content") and not msg.get("tool_calls"):
                    # e.g. a reasoning model that spent its whole budget thinking
                    last = f"{model}: empty reply (finish_reason={choice.get('finish_reason')})"
                    continue
                msg = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")}
                msg.setdefault("role", "assistant")
                msg.update({"_model": model, "_ms": round((time.perf_counter() - t0) * 1000), "_tokens": total})
                return msg
            last = f"{model}: HTTP {r.status_code} {r.text[:160]}"
            if r.status_code == 429:
                _record(model, _tpm(model))  # treat the bucket as full until it rolls
            elif r.status_code not in RETRYABLE and r.status_code != 400:
                continue  # auth/config problem with this provider: try the next one
        if waited >= max_wait_s:
            raise LLMError(last or "no model available")
        await asyncio.sleep(2.0)
        waited += 2.0


# ── voice: streaming, content only, hedged ─────────────────────────────────
async def _stream_one(model: str, messages: list[dict], max_tokens: int, out: asyncio.Queue) -> None:
    """Push ("text", s) items then ("done", model) — or ("fail", reason) if no text was produced."""
    url, headers = _url_headers(model)
    body = _body(model, messages, max_tokens, 0.3, stream=True)
    got = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as c:
            async with c.stream("POST", url, json=body, headers=headers) as r:
                if r.status_code != 200:
                    if r.status_code == 429:
                        _record(model, _tpm(model))
                    await out.put(("fail", f"{model} {r.status_code}: {(await r.aread()).decode()[:160]}"))
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    j = json.loads(line[6:])
                    usage = (j.get("x_groq") or {}).get("usage") or j.get("usage")
                    if usage:
                        _record(model, usage.get("total_tokens", 0))
                    choices = j.get("choices") or []
                    text = (choices[0].get("delta") or {}).get("content") if choices else None
                    if text:
                        got = True
                        await out.put(("text", text))
        await out.put(("done", model) if got else ("fail", f"{model}: no text"))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        await out.put(("done", model) if got else ("fail", f"{model}: {e!r}"[:200]))


async def stream_text(messages: list[dict], turn: dict, t0: float, *, max_tokens: int = 200,
                      models: list[str] | None = None, hedge_s: float | None = None) -> AsyncIterator[str]:
    """Voice answers. Starts the first model; if it fails, or hasn't produced a word within
    hedge_s, starts the next one alongside. The first model to produce text owns the turn;
    the others are cancelled. Text is cleaned for TTS."""
    order = list(models or VOICE_MODELS)
    hedge_s = VOICE_HEDGE_S if hedge_s is None else hedge_s
    # Skip a model whose free-tier minute is already spent (saves a 429 round trip).
    need = estimate_tokens(messages, None, max_tokens)
    order = [m for m in order if headroom(m) >= need] or order
    queues: dict[str, asyncio.Queue] = {}
    tasks: dict[str, asyncio.Task] = {}
    owner: str | None = None
    failed: list[str] = []

    def launch() -> bool:
        if not order:
            return False
        m = order.pop(0)
        queues[m] = asyncio.Queue()
        tasks[m] = asyncio.ensure_future(_stream_one(m, messages, max_tokens, queues[m]))
        return True

    launch()
    next_hedge = time.perf_counter() + hedge_s
    hard_stop = time.perf_counter() + VOICE_DEADLINE_S
    timed_out = False
    try:
        while owner is None:
            if not tasks:
                break
            if time.perf_counter() >= hard_stop:
                # sim 21 Nov: Groq connect timeouts + slow Azure gave 13.7 s of silence on a call
                timed_out = True
                failed.append(f"no first word within {VOICE_DEADLINE_S}s")
                break
            # wait for any queue item (or the hedge deadline)
            getters = {asyncio.ensure_future(q.get()): m for m, q in queues.items()}
            timeout = max(0.0, (next_hedge if order else hard_stop) - time.perf_counter())
            done, pending = await asyncio.wait(getters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for g in pending:
                g.cancel()
            if not done:  # hedge deadline: primary is slow, start the next model too
                if order:
                    launch()
                    next_hedge = time.perf_counter() + hedge_s
                continue
            for g in done:
                m = getters[g]
                kind, val = g.result()
                if kind == "text" and owner is None:
                    owner = m
                    if turn.get("ttft_ms") is None:
                        turn["ttft_ms"] = round((time.perf_counter() - t0) * 1000)
                    turn["model"] = m
                    yield clean_for_tts(val)
                elif kind == "text":
                    pass  # a slower hedge also started talking; its text is dropped
                elif kind == "fail":
                    failed.append(val)
                    tasks.pop(m, None)
                    queues.pop(m, None)
                    if not tasks and not launch():
                        break
                    next_hedge = time.perf_counter() + hedge_s
                else:  # "done" without ever owning: impossible, but be safe
                    tasks.pop(m, None)
                    queues.pop(m, None)
        if owner is None:
            turn["error"] = "; ".join(failed)[:200] or "no voice model available"
            yield ("Sorry, I'm a bit slow on my side right now. Give me a second and ask me that again?"
                   if timed_out else "Sorry, I hit a snag on my side. Ask me again?")
            return
        if failed:
            turn["error"] = "; ".join(failed)[:200]
        for m, t in list(tasks.items()):  # cancel the losers
            if m != owner:
                t.cancel()
        q = queues[owner]
        while True:
            kind, val = await q.get()
            if kind == "text":
                yield clean_for_tts(val)
            else:
                break
    finally:
        for t in tasks.values():
            if not t.done():
                t.cancel()
