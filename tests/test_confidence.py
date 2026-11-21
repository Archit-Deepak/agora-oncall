"""Part 6 test: the confidence formula, exact.

These are the designed numbers from build-plan.md §4.4: 55% early, 92% at the end.
"""
import pytest

from app.confidence import confidence, verdict_for


def test_designed_early_number():
    # match_share 0.55, not yet stable, not yet corroborated -> 55%.
    assert confidence(0.55, stable=False, corroborated=False) == pytest.approx(0.55)


def test_designed_final_number():
    # match_share 0.82, stable, corroborated -> 0.82 + 0.05 + 0.05 = 92%.
    assert confidence(0.82, stable=True, corroborated=True) == pytest.approx(0.92)


def test_ruled_out_is_zero():
    assert confidence(0.95, stable=True, corroborated=True, ruled_out=True) == 0.0


def test_ceiling():
    assert confidence(0.99, stable=True, corroborated=True) == pytest.approx(0.99)


def test_verdict_bands():
    assert verdict_for(0.54) == "unsure"
    assert verdict_for(0.60) == "leaning"
    assert verdict_for(0.84) == "leaning"
    assert verdict_for(0.85) == "confident"
    assert verdict_for(0.92) == "confident"
