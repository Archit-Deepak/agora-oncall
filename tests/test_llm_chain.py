"""Voice hedging + investigator priority chain, with fake providers (no network)."""
import asyncio
import time

from app import llm


def fake_streams(monkeypatch, plans):
    """plans: model -> (delay_before_first_s, [texts]) or ("fail", reason)."""
    started = []

    async def _stream_one(model, messages, max_tokens, out):
        started.append(model)
        plan = plans[model]
        if plan[0] == "fail":
            await out.put(("fail", plan[1]))
            return
        delay, texts = plan
        await asyncio.sleep(delay)
        for t in texts:
            await out.put(("text", t))
        await out.put(("done", model))
    monkeypatch.setattr(llm, "_stream_one", _stream_one)
    return started


def collect(models, hedge_s=0.05):
    turn = {}

    async def go():
        out = []
        async for t in llm.stream_text([{"role": "user", "content": "hi"}], turn, time.perf_counter(),
                                       models=models, hedge_s=hedge_s):
            out.append(t)
        return out
    return "".join(asyncio.run(go())), turn


def test_fast_primary_owns_the_turn(monkeypatch):
    started = fake_streams(monkeypatch, {"groq": (0.0, ["Hello", " there"]), "azure:x": (0.0, ["NO"])})
    text, turn = collect(["groq", "azure:x"])
    assert text == "Hello there" and turn["model"] == "groq" and started == ["groq"]


def test_slow_primary_is_hedged_and_backup_wins(monkeypatch):
    started = fake_streams(monkeypatch, {"groq": (0.5, ["late"]), "azure:x": (0.0, ["Backup", " answer"])})
    text, turn = collect(["groq", "azure:x"], hedge_s=0.05)
    assert text == "Backup answer" and turn["model"] == "azure:x" and started == ["groq", "azure:x"]


def test_primary_429_falls_through_immediately(monkeypatch):
    fake_streams(monkeypatch, {"groq": ("fail", "groq 429"), "azure:x": (0.0, ["ok"])})
    text, turn = collect(["groq", "azure:x"], hedge_s=5.0)
    assert text == "ok" and "429" in turn["error"]


def test_everything_down_says_something(monkeypatch):
    fake_streams(monkeypatch, {"a": ("fail", "a down"), "b": ("fail", "b down")})
    text, turn = collect(["a", "b"])
    assert text.startswith("Sorry") and "a down" in turn["error"]


def test_tts_cleanup():
    assert llm.clean_for_tts("34 percent now‑ish​") == "34 percent now-ish"


def test_reasoning_body_and_azure_url(monkeypatch):
    monkeypatch.setattr(llm.config, "AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setattr(llm.config, "AZURE_OPENAI_KEY", "k")
    b = llm._body("azure:gpt-5-mini", [], 700, 0.1)
    assert b["max_completion_tokens"] >= 2100 and "temperature" not in b and "model" not in b
    b = llm._body("azure:gpt-4.1-mini", [], 200, 0.3)
    assert b["max_tokens"] == 200 and b["temperature"] == 0.3
    url, h = llm._url_headers("azure:gpt-4.1-mini")
    assert url.startswith("https://x.openai.azure.com/openai/deployments/gpt-4.1-mini/") and h == {"api-key": "k"}
    assert llm._body("qwen/qwen3.8-27b", [], 10, 0)["reasoning_effort"] == "none"


def test_voice_deadline_says_a_holding_line(monkeypatch):
    # sim 21 Nov: 13.7 s of silence when every provider stalled
    monkeypatch.setattr(llm, "VOICE_DEADLINE_S", 0.3)
    fake_streams(monkeypatch, {"a": (5.0, ["late"]), "b": (5.0, ["late"])})
    text, turn = collect(["a", "b"], hedge_s=0.05)
    assert text.startswith("Sorry, I'm a bit slow") and "within" in turn["error"]


def test_tts_cleanup_removes_codes_ids_and_underscores():
    t = llm.clean_for_tts("apply pace_dialer for acct_zylo, see 089b1012-1fc0-434e-821e-1592525c78a3 at 01:43 UTC")
    assert "_" not in t and "acct" not in t and "UTC" not in t and "that call" in t
