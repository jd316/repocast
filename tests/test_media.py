"""Small FFmpeg integration check for the generalized media pipeline."""

import shutil
import subprocess

import pytest

import repocast.engine
from repocast import config
from repocast.__main__ import _verify
from repocast.engine import _to_gif, render

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)


def test_media_render_writes_video_script_and_captions(tmp_path):
    # Portable pixmap is a valid image and needs no image dependency to create.
    (tmp_path / "card.ppm").write_bytes(b"P6\n2 2\n255\n" + b"\x25\x63\xeb" * 4)
    source = tmp_path / "demo.yaml"
    source.write_text(
        "output: demo.mp4\n"
        "size: [320, 180]\n"
        "tts: {provider: none}\n"
        "captions: true\n"
        "burn_captions: true\n"
        "watermark: {text: DEMO, size: 18}\n"
        "video: {crf: 30, fps: 10, preset: ultrafast}\n"
        "segments:\n"
        "  - media: {file: card.ppm, duration: 1, say: 'A media scene.'}\n"
    )
    cfg = config.load(source)
    result = render(cfg, {}, tmp_path / "demo.mp4", tmp_path / "NARRATION.md", tmp_path / "work")

    assert result["duration"] == pytest.approx(2, abs=0.15)  # narration minimum wins
    assert (tmp_path / "demo.mp4").is_file()
    assert "A media scene." in (tmp_path / "demo.srt").read_text()
    assert "A media scene." in (tmp_path / "NARRATION.md").read_text()
    dimensions = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(tmp_path / "demo.mp4"),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert dimensions == "320,180"
    gif = tmp_path / "demo.gif"
    _to_gif(tmp_path / "demo.mp4", gif, 10)
    assert gif.read_bytes().startswith(b"GIF89a")


def test_video_media_keeps_audio_through_trim_and_speed(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yaml = tmp_path / "video.yaml"
    yaml.write_text(
        "output: result.mp4\n"
        "size: [320, 180]\n"
        "tts: {provider: none}\n"
        "video: {crf: 30, fps: 10, preset: ultrafast}\n"
        "segments:\n"
        "  - media: {file: source.mp4, trim: [0.5, 1.5], speed: 2}\n"
    )
    cfg = config.load(yaml)
    result = render(cfg, {}, tmp_path / "result.mp4", tmp_path / "NARRATION.md", tmp_path / "work")

    assert result["duration"] == pytest.approx(0.5, abs=0.15)
    streams = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(tmp_path / "result.mp4"),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert streams == ["video", "audio"]


def test_verify_rejects_silent_audio_and_accepts_intentional_video_only(tmp_path):
    silent_audio = tmp_path / "silent-audio.mp4"
    video_only = tmp_path / "video-only.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x64:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(silent_audio),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x64:d=1",
            "-c:v",
            "libx264",
            str(video_only),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert _verify(silent_audio, None)["ok"] is False
    assert _verify(video_only, None)["ok"] is False
    assert _verify(video_only, None, require_audio=False)["ok"] is True
    assert (
        _verify(video_only, None, require_audio=False, expected={"max_black": 0.5})["ok"] is False
    )


def test_failed_finalization_preserves_existing_output(tmp_path, monkeypatch):
    (tmp_path / "card.ppm").write_bytes(b"P6\n2 2\n255\n" + b"\x25\x63\xeb" * 4)
    source = tmp_path / "demo.yaml"
    source.write_text(
        "output: demo.mp4\nsize: [64, 64]\ntts: {provider: none}\n"
        "video: {crf: 35, fps: 5, preset: ultrafast}\nsegments:\n"
        "  - media: {file: card.ppm, duration: 0.2}\n"
    )
    output = tmp_path / "demo.mp4"
    output.write_bytes(b"known-good")

    def fail_mux(*_args, **_kwargs):
        raise RuntimeError("mux failed")

    monkeypatch.setattr(repocast.engine, "_mux", fail_mux)
    with pytest.raises(RuntimeError, match="mux failed"):
        render(config.load(source), {}, output, tmp_path / "NARRATION.md", tmp_path / "work")
    assert output.read_bytes() == b"known-good"
