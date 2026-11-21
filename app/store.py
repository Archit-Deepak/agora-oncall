"""IncidentStore — the single source of truth for the live incident.

One CaseFile held in memory, guarded by an asyncio.Lock, snapshotted to
state/incident.json after every change, with a tiny pub/sub so the dashboard
(SSE) and anything else can react to updates. No network, no second process.

Rules (build-plan.md §3):
  - get()           -> a deep copy, so callers can never mutate the live state
  - update(fn)      -> lock, mutate in place, bump version, snapshot, publish
  - subscribe()     -> async iterator; yields current state, then every change
  - reset()         -> back to a fresh case file
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from app.models import CaseFile, IncidentState

DEFAULT_SNAPSHOT = Path("state/incident.json")


def new_case(incident_id: str = "INC-UNSET") -> CaseFile:
    return CaseFile(incident_id=incident_id, state=IncidentState.IDLE)


class IncidentStore:
    def __init__(self, snapshot_path: str | Path = DEFAULT_SNAPSHOT, case: CaseFile | None = None):
        self._path = Path(snapshot_path)
        self._case = case or new_case()
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[CaseFile]] = set()

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self) -> CaseFile:
        """A deep copy. Mutating it never touches the live state."""
        return self._case.model_copy(deep=True)

    @property
    def version(self) -> int:
        return self._case.version

    # ── writes ───────────────────────────────────────────────────────────────
    async def update(self, fn: Callable[[CaseFile], None]) -> CaseFile:
        """Apply fn to the live case file under the lock, then persist + publish."""
        async with self._lock:
            fn(self._case)
            self._case.version += 1
            self._write_snapshot()
            snapshot = self._case.model_copy(deep=True)
        self._publish(snapshot)
        return snapshot

    async def reset(self, case: CaseFile | None = None) -> CaseFile:
        async with self._lock:
            self._case = case or new_case()
            self._write_snapshot()
            snapshot = self._case.model_copy(deep=True)
        self._publish(snapshot)
        return snapshot

    # ── persistence ──────────────────────────────────────────────────────────
    def _write_snapshot(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = self._case.model_dump_json(indent=2)
        # Atomic: write to a temp file in the same dir, then rename over the target,
        # so a crash mid-write can never leave a half-written snapshot.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @classmethod
    def load(cls, snapshot_path: str | Path = DEFAULT_SNAPSHOT) -> "IncidentStore":
        """Restore from the snapshot if it exists, else start fresh."""
        p = Path(snapshot_path)
        if p.exists():
            case = CaseFile.model_validate_json(p.read_text())
            return cls(snapshot_path=p, case=case)
        return cls(snapshot_path=p)

    # ── pub/sub ──────────────────────────────────────────────────────────────
    def _publish(self, snapshot: CaseFile) -> None:
        for q in list(self._subscribers):
            q.put_nowait(snapshot)

    async def subscribe(self) -> AsyncIterator[CaseFile]:
        """Yield the current state immediately, then one CaseFile per update."""
        q: asyncio.Queue[CaseFile] = asyncio.Queue()
        q.put_nowait(self.get())
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)
