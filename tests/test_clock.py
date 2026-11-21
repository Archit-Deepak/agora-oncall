"""Part 3 test: scenario clock runs at speed, pauses, and jumps — deterministically.

We inject a fake time source (a mutable list cursor) so no real sleeping happens
and the assertions are exact.
"""
from app.clock import ScenarioClock


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_runs_at_speed_factor():
    ft = FakeTime()
    clk = ScenarioClock(speed=10.0, time_fn=ft)
    clk.start()
    assert clk.now() == 0.0
    ft.t += 2.0  # 2 real seconds
    assert clk.now() == 20.0  # x10 speed


def test_pause_freezes_and_resume_continues():
    ft = FakeTime()
    clk = ScenarioClock(speed=1.0, time_fn=ft)
    clk.start()
    ft.t += 5.0
    clk.pause()
    assert clk.now() == 5.0
    ft.t += 100.0  # time passes while paused
    assert clk.now() == 5.0  # frozen
    clk.resume()
    ft.t += 3.0
    assert clk.now() == 8.0


def test_advance_jumps_forward():
    ft = FakeTime()
    clk = ScenarioClock(speed=1.0, time_fn=ft)
    clk.start()
    ft.t += 10.0
    clk.advance(120.0)  # stage safety valve: skip ahead 2 minutes
    assert clk.now() == 130.0
    ft.t += 1.0
    assert clk.now() == 131.0  # keeps running from the jumped point
