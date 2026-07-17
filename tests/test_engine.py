"""Pure-unit tests for engine helpers that don't need a browser."""
import os

from repocast.engine import _RESULT, Marks, clean_env


def test_clean_env_strips_own_venv():
    os.environ["VIRTUAL_ENV"] = "/tmp/whatever"
    os.environ["UV_PROJECT_ENVIRONMENT"] = "/tmp/uv"
    e = clean_env({"EXTRA": "1"})
    assert "VIRTUAL_ENV" not in e
    assert "UV_PROJECT_ENVIRONMENT" not in e
    assert e["EXTRA"] == "1"


def test_speak_time_uses_measured_audio_when_present():
    m = Marks({"hello there": ("x.wav", 4.0)})
    assert m.speak_time("hello there") == 4.0 + 0.6      # measured + PAD
    # no audio -> word-rate fallback, floored at 2s
    assert m.speak_time("hi") == 2.0
    assert m.speak_time("one two three four five six seven eight") > 2.0


def test_result_regex_highlights_outcomes():
    assert _RESULT.search("56 passed in 1.3s")
    assert _RESULT.search("All checks passed!")
    assert _RESULT.search("NO OVERSELL ✓")
    assert _RESULT.search("TOTAL   95%")
    assert not _RESULT.search("routers/generate.py")
