"""ScenarioClock — scenario time that can run fast, pause, and jump.

Scenario time is measured in seconds since the incident "trigger" (t=0). Real
wall-clock seconds are multiplied by `speed` (10x in dev so a 5-minute scenario
runs in 30 seconds). Pause freezes it; advance() jumps it forward as a stage
safety valve.

Time source is injectable so tests don't have to actually sleep.
"""
from __future__ import annotations

import time
from collections.abc import Callable


class ScenarioClock:
    def __init__(self, speed: float = 1.0, time_fn: Callable[[], float] = time.monotonic):
        self._speed = speed
        self._time_fn = time_fn
        self._accum = 0.0  # scenario seconds banked up to the last pause/advance
        self._anchor = 0.0  # real time at the last resume
        self._running = False

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Begin at t=0 and run."""
        self._accum = 0.0
        self._anchor = self._time_fn()
        self._running = True

    def now(self) -> float:
        """Current scenario time in seconds since trigger."""
        if self._running:
            return self._accum + (self._time_fn() - self._anchor) * self._speed
        return self._accum

    def pause(self) -> None:
        if self._running:
            self._accum = self.now()
            self._running = False

    def resume(self) -> None:
        if not self._running:
            self._anchor = self._time_fn()
            self._running = True

    def advance(self, seconds: float) -> None:
        """Jump scenario time forward by `seconds`, running or paused."""
        self._accum = self.now() + seconds
        self._anchor = self._time_fn()

    def reset(self) -> None:
        self._accum = 0.0
        self._anchor = self._time_fn()
        self._running = False
