"""The three seams to the outside world. Each has a mock and a real implementation,
switched in .env, so the whole incident flow can run and be tested without touching
the phone, Linear, or Telegram.
"""
from __future__ import annotations

from typing import Protocol

from app.models import CallAttempt, CaseFile


class VoiceCaller(Protocol):
    async def place_call(self, attempt: CallAttempt, greeting: str) -> str: ...
    async def hangup(self, attempt: CallAttempt) -> None: ...
    async def speak(self, attempt: CallAttempt, text: str, priority: str = "APPEND") -> None: ...


class TicketSink(Protocol):
    async def create(self, payload: dict) -> dict: ...  # returns {"id":..., "url":...}
    async def append(self, ref: dict, text: str) -> None: ...


class Notifier(Protocol):
    async def missed_call(self, case: CaseFile) -> None: ...
