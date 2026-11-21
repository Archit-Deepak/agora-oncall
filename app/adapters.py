"""Mock adapters — record what would have happened, so the flow is fully testable.

The real Agora/Linear/Telegram adapters come in Phase C and implement the same
interfaces; nothing in the orchestrator changes when we swap them in.
"""
from __future__ import annotations

from app.models import CallAttempt, CaseFile


class MockVoiceCaller:
    def __init__(self) -> None:
        self.placed: list[tuple[str, str, str]] = []  # (attempt_id, purpose, greeting)
        self.spoken: list[str] = []
        self._n = 0

    async def place_call(self, attempt: CallAttempt, greeting: str) -> str:
        self._n += 1
        self.placed.append((attempt.attempt_id, attempt.purpose, greeting))
        return f"mock-agent-{self._n}"

    async def hangup(self, attempt: CallAttempt) -> None:
        pass

    async def speak(self, attempt: CallAttempt, text: str, priority: str = "APPEND") -> None:
        self.spoken.append(text)


class MockTicketSink:
    """Deliberately fails the first create() (FAIL_FIRST_TICKET) to show the retry."""

    def __init__(self, fail_first: bool = True) -> None:
        self.fail_first = fail_first
        self.create_calls = 0
        self.appended: list[str] = []

    async def create(self, payload: dict) -> dict:
        self.create_calls += 1
        if self.fail_first and self.create_calls == 1:
            raise RuntimeError("ticket system returned HTTP 503")
        return {"id": "VPS-42", "url": "https://linear.app/voice-platform-support/issue/VPS-42"}

    async def append(self, ref: dict, text: str) -> None:
        self.appended.append(text)

    async def loop_in(self, ref: dict, people: list[dict], note: str) -> dict:
        self.looped = getattr(self, "looped", []) + [p["name"] for p in people]
        self.appended.append(note)
        return {"subscribed": []}


class MockNotifier:
    def __init__(self) -> None:
        self.missed: list[str] = []
        self.notices: list[str] = []

    async def missed_call(self, case: CaseFile) -> None:
        self.missed.append(case.incident_id)

    async def mild_outage(self, case: CaseFile, text: str) -> None:
        self.notices.append(text)

    async def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        self.notices.append(text)
        self.buttons = buttons or []
