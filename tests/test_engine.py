"""Pure-unit tests for engine helpers that don't need a browser."""

import os
from pathlib import Path

import pytest

from repocast.engine import (
    _RESULT,
    CommandError,
    Marks,
    _atempo,
    _concat_entry,
    _run,
    _subtitle_time,
    _write_captions,
    clean_env,
)


def test_clean_env_strips_own_venv():
    os.environ["VIRTUAL_ENV"] = "/tmp/whatever"
    os.environ["UV_PROJECT_ENVIRONMENT"] = "/tmp/uv"
    e = clean_env({"EXTRA": "1"})
    assert "VIRTUAL_ENV" not in e
    assert "UV_PROJECT_ENVIRONMENT" not in e
    assert e["EXTRA"] == "1"


def test_speak_time_uses_measured_audio_when_present():
    m = Marks({"hello there": (Path("x.wav"), 4.0)})
    assert m.speak_time("hello there") == 4.0 + 0.6  # measured + PAD
    # no audio -> word-rate fallback, floored at 2s
    assert m.speak_time("hi") == 2.0
    assert m.speak_time("one two three four five six seven eight") > 2.0


def test_result_regex_highlights_outcomes():
    assert _RESULT.search("56 passed in 1.3s")
    assert _RESULT.search("All checks passed!")
    assert _RESULT.search("NO OVERSELL ✓")
    assert _RESULT.search("TOTAL   95%")
    assert not _RESULT.search("routers/generate.py")


def test_terminal_failure_is_loud_unless_allowed(tmp_path):
    command = ["sh", "-c", "printf broken; exit 7"]
    with pytest.raises(CommandError, match="exit 7.*broken"):
        _run(command, tmp_path, None)
    assert _run(command, tmp_path, None, allow_failure=True) == ["broken"]


def test_subtitles_and_concat_escaping(tmp_path):
    marks = Marks({})
    marks.items = [(0.0, "Hello"), (2.0, "World")]
    marks.seg_len = 4.0
    out = tmp_path / "demo.srt"
    _write_captions([("Demo", 1.0, marks)], out)
    assert "00:00:01,000 --> 00:00:03,000" in out.read_text()
    assert _subtitle_time(3661.234) == "01:01:01,234"
    assert _concat_entry(Path("it's.mp4")) == "file 'it'\\''s.mp4'\n"
    assert _atempo(4) == "atempo=2,atempo=2"
    assert _atempo(0.25) == "atempo=0.5,atempo=0.5"
