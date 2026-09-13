"""End-to-end voiced render — the path that regressed once (render() wiped the
work dir containing the pre-generated voice clips, so the mux found no audio).

Gated on real deps (Gemini key, ffmpeg, Chrome), so it's a local/integration
check, not part of the credential-free unit run.
"""

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("GEMINI_API_KEY") and shutil.which("ffmpeg")),
    reason="needs GEMINI_API_KEY + ffmpeg (+ Chrome via Playwright)",
)


def test_voiced_render_has_audio(tmp_path):
    cfg = tmp_path / "w.yaml"
    cfg.write_text(
        "output: out.mp4\n"
        "size: [1280, 720]\n"
        "tts: {provider: gemini, voice: Charon, model: gemini-3.1-flash-tts-preview}\n"
        "segments:\n"
        "  - terminal:\n"
        f"      cwd: {tmp_path}\n"
        "      steps:\n"
        '        - {run: ["echo", "hello"], say: "A short voiced regression check."}\n'
    )
    out = tmp_path / "out.mp4"
    r = subprocess.run(
        ["repocast", "render", str(cfg), "--output", str(out), "--no-verify"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert out.is_file()
    # the bug: mux failed -> no audio stream. Assert one is present.
    codec = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert codec, "no audio stream — the TTS->render->mux path regressed"
