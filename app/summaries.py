"""Mild-outage summaries for Telegram. Off the call path, so there's time for an LLM to
write it properly; the template is the fallback if the LLM is slow or down."""
from __future__ import annotations

import httpx

from app import config
from app.models import CaseFile


def _facts(cf: CaseFile, peak_rate: float, duration_s: int, during_call: bool) -> str:
    a = cf.alert
    lead = next((h for h in cf.hypotheses if h.id == "carrier_rejection"), None)
    return (f"customer={a.customer_name if a else 'Northwind Health'}; incident={cf.incident_id}; "
            f"peak failure rate={int(round(peak_rate * 100))}% (baseline {int(round((a.baseline_rate if a else 0.02) * 100))}%); "
            f"lasted about {duration_s}s; now failing {int(round((cf.recent_fail_rate or 0) * 100))}%; "
            f"pattern: {lead.reason if lead else 'n/a'}; cause never confirmed; "
            f"engineer was {'on a call' if during_call else 'not called (recovered before the call)'}.")


def template_summary(cf: CaseFile, peak_rate: float, duration_s: int, during_call: bool) -> str:
    a = cf.alert
    return (f"⚠️ Mild outage — {a.customer_name if a else 'Northwind Health'} outbound calls\n"
            f"Up to {int(round(peak_rate * 100))}% of calls failed for about {duration_s} s "
            f"(normal is {int(round((a.baseline_rate if a else 0.02) * 100))}%), mostly carrier X 503 rejects.\n"
            f"✅ Recovered on its own — now {int(round((cf.recent_fail_rate or 0) * 100))}% failing. "
            f"No action needed; I'm still watching.")


async def groq_summary(cf: CaseFile, peak_rate: float, duration_s: int, during_call: bool) -> str:
    fallback = template_summary(cf, peak_rate, duration_s, during_call)
    if not config.GROQ_API_KEY:
        return fallback
    prompt = ("Write a short Telegram alert (max 4 lines, 1-2 emoji) for an on-call telecom engineer about a "
              "brief outage that recovered by itself. Plain text, no markdown. Say it's fixed now and whether "
              "action is needed (none). Facts: " + _facts(cf, peak_rate, duration_s, during_call))
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                             headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                             json={"model": config.GROQ_MODEL, "temperature": 0.3, "max_tokens": 300,
                                   "reasoning_effort": "low", "include_reasoning": False,
                                   "messages": [{"role": "user", "content": prompt}]})
        text = r.json()["choices"][0]["message"]["content"].strip()
        return text or fallback
    except Exception:  # noqa: BLE001
        return fallback
