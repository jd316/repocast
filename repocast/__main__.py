"""repocast CLI.

repocast render  walkthrough.yaml [--mock] [--voice Charon] [--no-voice]
repocast verify  walkthrough.mp4  [--script NARRATION.md]
repocast voices  [--voice Charon --voice Orus ...]   # sample clips to pick a voice
repocast validate walkthrough.yaml
repocast dry-run walkthrough.yaml
repocast schema
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import __version__, config, tts
from .engine import dry_run, render


def _key(args) -> str:
    k = args.key or os.environ.get("GEMINI_API_KEY", "")
    if not k:
        raise RuntimeError(
            "no Gemini key — pass --key or set GEMINI_API_KEY "
            "(or use --no-voice / tts.provider: none)"
        )
    return k


def _scratch_is_owned(path: Path) -> bool:
    marker = path / ".repocast-work"
    if not marker.is_file() or marker.read_text(errors="replace") != "repocast scratch v1\n":
        return False
    fixed = {
        "voice",
        "doc.html",
        "terminal.html",
        "silent.mp4",
        "concat.txt",
        "finished.mp4",
        "webcam.mp4",
        "captioned.mp4",
        "watermarked.mp4",
        "watermark.txt",
        "NARRATION.md",
        "captions.srt",
        "captions.vtt",
    }
    return all(
        item.name == marker.name
        or item.name in fixed
        or re.fullmatch(r"seg\d+_[a-z]+(?:_edited)?(?:\.mp4|\.audio\.wav)?", item.name)
        or item.name.startswith("delivery.")
        for item in path.iterdir()
    )


def cmd_render(args) -> int:
    cfg = config.load(args.config)
    configured = Path(cfg["output"])
    out = cfg["_base"] / configured if not configured.is_absolute() else configured
    out = Path(args.output) if args.output else out
    if out.suffix.lower() not in (".mp4", ".gif"):
        raise ValueError("output must end in .mp4 or .gif")
    workdir = Path(args.workdir).resolve()
    protected = {
        Path("/").resolve(),
        Path.cwd().resolve(),
        Path.home().resolve(),
        cfg["_base"].resolve(),
    }
    if workdir in protected:
        raise ValueError(f"unsafe scratch directory: {workdir}")
    if out.resolve().is_relative_to(workdir):
        raise ValueError("output cannot be inside the scratch directory")
    marker = workdir / ".repocast-work"
    if workdir.exists() and any(workdir.iterdir()) and not _scratch_is_owned(workdir):
        raise RuntimeError(f"scratch directory is not owned by Repocast: {workdir}")
    workdir.mkdir(parents=True, exist_ok=True)
    marker.write_text("repocast scratch v1\n")
    stream = sys.stderr if args.json else sys.stdout
    try:
        with contextlib.redirect_stdout(stream):
            if args.voice:
                cfg["tts"]["voice"] = args.voice
            no_voice = args.no_voice or cfg["tts"]["provider"] == "none"
            audio: dict = {}
            if not no_voice:
                key = _key(args)
                texts = config.all_narration(cfg)
                print(
                    f"generating voice-over ({cfg['tts'].get('voice', tts.DEFAULT_VOICE)}, "
                    f"{len(set(texts))} lines)…"
                )
                audio = tts.synth_all(
                    texts,
                    key,
                    workdir / "voice",
                    voice=cfg["tts"].get("voice", tts.DEFAULT_VOICE),
                    model=cfg["tts"].get("model", tts.DEFAULT_MODEL),
                    style=cfg["tts"].get("style", tts.DEFAULT_STYLE),
                )
            script = out.with_name("NARRATION.md")
            print("recording…")
            summary = render(cfg, audio, out, script, workdir)
            summary["script"] = str(script)
            summary["sha256"] = _sha256(out)
            if out.suffix.lower() == ".mp4":
                sheet = out.with_suffix(".contact-sheet.jpg")
                _contact_sheet(out, sheet)
                summary["contact_sheet"] = str(sheet)
            if not args.no_verify:
                expected = {
                    **cfg.get("quality", {}),
                    "width": cfg["size"][0],
                    "height": cfg["size"][1],
                    "fps": min(cfg["video"]["fps"], 20)
                    if out.suffix.lower() == ".gif"
                    else cfg["video"]["fps"],
                }
                summary["verification"] = _verify(
                    out,
                    script,
                    require_audio=not no_voice and out.suffix.lower() == ".mp4",
                    expected=expected,
                )
                report = out.with_suffix(".verify.json")
                _atomic_write_text(report, json.dumps(summary["verification"], indent=2) + "\n")
                summary["verification_report"] = str(report)
                summary["ok"] = summary["verification"]["ok"]
            else:
                summary["ok"] = True
    except Exception:
        if workdir.exists():
            print(f"render failed; diagnostic files preserved in {workdir}", file=sys.stderr)
        else:
            print("post-render verification failed; rendered output was retained", file=sys.stderr)
        raise
    if args.json:
        print(json.dumps(summary))
    else:
        print(
            f"\n✅ {out}  ({summary['size'] / 1e6:.1f} MB, {summary['duration']:.0f}s, "
            f"{summary['w']}x{summary['h']}, audio={'yes' if summary['audio'] else 'no'})"
        )
        print(f"✅ {script}  (timestamped narration)")
        if summary.get("captions"):
            print(f"✅ {summary['captions']}  (captions)")
        if "verification" in summary:
            _print_verification(summary["verification"], out)
            print(f"✅ {summary['verification_report']}  (verification report)")
    return 0 if summary["ok"] else 1


def cmd_verify(args) -> int:
    video = Path(args.video)
    result = _verify(
        video,
        Path(args.script) if args.script else video.with_name("NARRATION.md"),
        require_audio=not args.allow_silent,
        expected={
            key: value
            for key, value in {
                "min_duration": args.min_duration,
                "max_duration": args.max_duration,
                "max_silence": args.max_silence,
                "max_black": args.max_black,
                "loudness_target": args.loudness_target,
                "loudness_tolerance": args.loudness_tolerance,
                "width": args.width,
                "height": args.height,
                "fps": args.fps,
            }.items()
            if value is not None
        },
    )
    if args.json:
        print(json.dumps(result))
    else:
        _print_verification(result, video)
    return 0 if result["ok"] else 1


def _verify(
    video: Path, script: Path | None, require_audio: bool = True, expected: dict | None = None
) -> dict:
    if not video.is_file():
        raise FileNotFoundError(f"video not found: {video}")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode:
        raise RuntimeError(f"ffprobe could not read {video}: {probe.stderr.strip()}")
    metadata = json.loads(probe.stdout)
    if not metadata.get("streams"):
        raise RuntimeError(f"no video stream: {video}")
    issues = []
    expected = expected or {}
    # audio present + is speech, not silence
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
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if require_audio and not codec:
        issues.append("video has no audio track")
    stream = metadata["streams"][0]
    duration = float(metadata.get("format", {}).get("duration", 0))
    if duration <= 0:
        issues.append("video duration is zero")
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if decode.returncode:
        issues.append(f"decode failed: {decode.stderr.strip()[-1000:]}")
    if expected.get("width") is not None and stream.get("width") != expected["width"]:
        issues.append(f"width is {stream.get('width')}, expected {expected['width']}")
    if expected.get("height") is not None and stream.get("height") != expected["height"]:
        issues.append(f"height is {stream.get('height')}, expected {expected['height']}")
    rate = stream.get("avg_frame_rate", "0/1")
    numerator, denominator = (float(part) for part in rate.split("/"))
    fps = numerator / denominator if denominator else 0
    if expected.get("fps") is not None and abs(fps - expected["fps"]) > 0.01:
        issues.append(f"frame rate is {fps:g}, expected {expected['fps']}")
    if duration < expected.get("min_duration", 0):
        issues.append(f"duration {duration:.2f}s is below {expected['min_duration']}s")
    if expected.get("max_duration") is not None and duration > expected["max_duration"]:
        issues.append(f"duration {duration:.2f}s exceeds {expected['max_duration']}s")
    result = {
        "ok": True,
        "video": str(video),
        "sha256": _sha256(video),
        "video_codec": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "frame_rate": fps,
        "duration": duration,
        "audio_codec": codec or None,
        "mean_volume_db": None,
        "audible": False,
        "alignment": None,
        "integrated_loudness_lufs": None,
        "max_silence_seconds": None,
        "max_black_seconds": None,
        "issues": issues,
    }
    if codec:
        vol = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(video), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
            check=False,
        ).stderr
        mean = next(
            (ln.split("mean_volume:")[1].strip() for ln in vol.splitlines() if "mean_volume" in ln),
            "?",
        )
        if "dB" in mean:
            result["mean_volume_db"] = float(mean.split()[0])
            result["audible"] = result["mean_volume_db"] > -40
            if not result["audible"]:
                issues.append("audio track is near silent")
        levels = subprocess.run(
            [
                "ffmpeg",
                "-nostats",
                "-i",
                str(video),
                "-filter_complex",
                "ebur128",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stderr
        loudness = re.findall(r"I:\s*(-?[0-9.]+) LUFS", levels)
        if loudness:
            result["integrated_loudness_lufs"] = float(loudness[-1])
            if "loudness_target" in expected:
                tolerance = expected.get("loudness_tolerance", 2)
                if (
                    abs(result["integrated_loudness_lufs"] - expected["loudness_target"])
                    > tolerance
                ):
                    issues.append(
                        f"loudness is {result['integrated_loudness_lufs']} LUFS, expected "
                        f"{expected['loudness_target']}±{tolerance} LUFS"
                    )
        if "max_silence" in expected:
            silence = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "info",
                    "-i",
                    str(video),
                    "-af",
                    "silencedetect=n=-45dB:d=0.1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                check=False,
            ).stderr
            durations = [
                float(value) for value in re.findall(r"silence_duration: ([0-9.]+)", silence)
            ]
            result["max_silence_seconds"] = max(durations, default=0)
            if result["max_silence_seconds"] > expected["max_silence"]:
                issues.append(
                    f"silence {result['max_silence_seconds']:.2f}s exceeds "
                    f"{expected['max_silence']}s"
                )
    if "max_black" in expected:
        black = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "info",
                "-i",
                str(video),
                "-vf",
                "blackdetect=d=0.1:pix_th=0.02",
                "-an",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stderr
        durations = [float(value) for value in re.findall(r"black_duration:([0-9.]+)", black)]
        result["max_black_seconds"] = max(durations, default=0)
        if result["max_black_seconds"] > expected["max_black"]:
            issues.append(
                f"black frame run {result['max_black_seconds']:.2f}s exceeds "
                f"{expected['max_black']}s"
            )
    # alignment: speech onsets vs script cues
    if script and script.is_file():
        onsets = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "info",
                "-i",
                str(video),
                "-af",
                "silencedetect=n=-45dB:d=0.8",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stderr
        onset_t = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", onsets)]
        cues = [
            int(m[0]) * 60 + int(m[1]) for m in re.findall(r"`(\d{2}):(\d{2})`", script.read_text())
        ]
        if cues and not codec and require_audio:
            issues.append("narration cues exist but the video has no audio")
        if onset_t and cues:
            drifts = [min(abs(o - c) for o in onset_t) for c in cues]
            bad = sum(1 for d in drifts if d > 1.5)
            result["alignment"] = {
                "cues": len(cues),
                "onsets": len(onset_t),
                "median_drift_seconds": round(sorted(drifts)[len(drifts) // 2], 2),
                "off_by_over_1_5_seconds": bad,
            }
    result["ok"] = not issues
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contact_sheet(video: Path, out: Path, count: int = 12) -> None:
    duration = max(_probe_duration(video), 0.1)
    columns = 4
    rows = (count + columns - 1) // columns
    temporary = out.with_name(f".{out.stem}.{os.getpid()}.tmp{out.suffix}")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video),
                "-vf",
                f"fps={count / duration},scale=320:-1,tile={columns}x{rows}",
                "-frames:v",
                "1",
                str(temporary),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.replace(temporary, out)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _probe_duration(video: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def _print_verification(result: dict, video: Path) -> None:
    print("verifying…")
    if result["audio_codec"]:
        state = "audible" if result["audible"] else "CHECK — near silent"
        print(
            f"  audio: {result['audio_codec']}, mean_volume {result['mean_volume_db']} dB ({state})"
        )
    else:
        print("  audio: NONE (silent video)")
    if result["alignment"]:
        a = result["alignment"]
        print(
            f"  alignment: {a['cues']} cues, {a['onsets']} onsets, "
            f"median drift {a['median_drift_seconds']:.1f}s, "
            f"off-by->1.5s: {a['off_by_over_1_5_seconds']}"
        )
    for issue in result.get("issues", []):
        print(f"  CHECK: {issue}")
    print(
        "  (spot-check a frame: ffmpeg -ss <t> -i "
        f"{video.name} -frames:v 1 f.png — LOOK at it; automated freeze checks lie.)"
    )


def cmd_voices(args) -> None:
    key = _key(args)
    outdir = Path("voice_samples")
    outdir.mkdir(exist_ok=True)
    sample = (
        "Reserve the worst case before generating, settle the truth after, "
        "and refund the difference. A single atomic update keeps two concurrent "
        "requests from both passing the gate."
    )
    for v in args.voice or ["Charon", "Orus", "Iapetus", "Algenib"]:
        dur = tts.synth(
            sample, key, outdir / f"{v}.wav", voice=v, model=args.model or tts.DEFAULT_MODEL
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(outdir / f"{v}.wav"), str(outdir / f"{v}.mp3")],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (outdir / f"{v}.wav").unlink()
        print(f"  {v:10s} {dur:4.1f}s  -> voice_samples/{v}.mp3")
    print("Play them and pick one; pass it as --voice or set tts.voice in the config.")


_TEMPLATE = """\
# repocast walkthrough — a narrated video of your project.
# Paths are relative to THIS file. Render:  repocast render <this-file>
output: walkthrough.mp4
size: youtube
captions: true
burn_captions: true
theme: {background: "#111827", padding: 36, radius: 16, shadow: true}
video: {crf: 21, fps: 30, preset: slow}
quality: {loudness_target: -16, loudness_tolerance: 2}
tts:
  provider: gemini            # gemini | none (none = silent, use your own voice)
  voice: Charon               # run `repocast voices` to compare
  model: gemini-3.1-flash-tts-preview
segments:
  - doc:                      # scroll a rendered markdown doc, section by section
      file: DESIGN.md
      steps:
        - {anchor: architecture, say: "The system is layered — HTTP, service, storage."}
  - terminal:                 # run real commands, film their real output
      cwd: .
      steps:
        - {run: ["make", "test"], say: "The test suite — green."}
  - app:                      # drive the live app in a browser
      cwd: .
      start: ["make", "run"]
      url: "http://127.0.0.1:8000"
      ready_path: /health
      zoom: 1.4
      actions:
        - {say: "And here it is running for real."}
        - {click: "#submit", focus: true, effect: ripple}
        - {wait_for: ".result", timeout: 60000}
        - annotate: {target: ".result", text: "Live result"}
        - {say: "…and the result comes back."}
# Add screenshots, diagrams, animated GIFs, or existing video with:
# - media: {file: architecture.png, duration: 5, say: "The architecture at a glance."}
# Add a persistent disclosure with:
# watermark: {text: "DEMO · SYNTHETIC DATA", position: top-left, size: 24}
"""


def cmd_init(args) -> None:
    dest = Path(args.path)
    if dest.exists() and not args.force:
        sys.exit(f"{dest} exists (use --force to overwrite)")
    dest.write_text(_TEMPLATE)
    print(f"✓ wrote {dest} — edit it, then: repocast render {dest}")


def cmd_validate(args) -> None:
    cfg = config.load(args.config, check_paths=not args.template)
    n = len(config.all_narration(cfg))
    result = {
        "ok": True,
        "config": str(args.config),
        "segments": len(cfg["segments"]),
        "narration_lines": n,
        "size": cfg["size"],
    }
    print(
        json.dumps(result)
        if args.json
        else f"✓ {args.config} valid — {len(cfg['segments'])} segments, {n} narration lines"
    )


def cmd_schema(args) -> None:
    print(json.dumps(config.SCHEMA, indent=2))


def cmd_dry_run(args) -> None:
    cfg = config.load(args.config)
    stream = sys.stderr if args.json else sys.stdout
    with contextlib.redirect_stdout(stream):
        result = dry_run(cfg)
    print(
        json.dumps(result)
        if args.json
        else f"✓ dry run passed — {len(result['segments'])} segments"
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="repocast",
        description="Make reproducible product and technical demos into polished video.",
    )
    p.add_argument("--version", action="version", version=f"repocast {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="build the narrated video from a config")
    r.add_argument("config")
    r.add_argument("--voice", help="override tts.voice")
    r.add_argument("--no-voice", action="store_true", help="silent video (bring your own voice)")
    r.add_argument(
        "--mock",
        action="store_true",
        help="reserved for configs that read $REPOCAST_MOCK to pick a mock backend",
    )
    r.add_argument("--key", help="Gemini API key (or set GEMINI_API_KEY)")
    r.add_argument("--output", help="override output path")
    r.add_argument("--workdir", default="_repocast", help="scratch dir (removed after)")
    r.add_argument("--no-verify", action="store_true")
    r.add_argument("--json", action="store_true", help="write one JSON result to stdout")
    r.set_defaults(func=cmd_render)

    v = sub.add_parser("verify", help="check a rendered video (audio + alignment)")
    v.add_argument("video")
    v.add_argument("--script")
    v.add_argument("--allow-silent", action="store_true", help="accept a video without audio")
    v.add_argument("--min-duration", type=float)
    v.add_argument("--max-duration", type=float)
    v.add_argument("--max-silence", type=float)
    v.add_argument("--max-black", type=float)
    v.add_argument("--loudness-target", type=float)
    v.add_argument("--loudness-tolerance", type=float)
    v.add_argument("--width", type=int)
    v.add_argument("--height", type=int)
    v.add_argument("--fps", type=float)
    v.add_argument("--json", action="store_true", help="write one JSON result to stdout")
    v.set_defaults(func=cmd_verify)

    vo = sub.add_parser("voices", help="synthesise sample clips to choose a voice")
    vo.add_argument("--voice", action="append", help="voice to sample (repeatable)")
    vo.add_argument("--model")
    vo.add_argument("--key")
    vo.set_defaults(func=cmd_voices)

    ini = sub.add_parser("init", help="scaffold a walkthrough.yaml template")
    ini.add_argument("path", nargs="?", default="walkthrough.yaml")
    ini.add_argument("--force", action="store_true")
    ini.set_defaults(func=cmd_init)

    va = sub.add_parser("validate", help="validate a config without rendering")
    va.add_argument("config")
    va.add_argument(
        "--template",
        action="store_true",
        help="skip file-existence checks (for placeholder templates)",
    )
    va.add_argument("--json", action="store_true", help="write one JSON result to stdout")
    va.set_defaults(func=cmd_validate)

    schema = sub.add_parser("schema", help="print the configuration schema as JSON")
    schema.set_defaults(func=cmd_schema)

    dry = sub.add_parser("dry-run", help="execute commands and browser actions without recording")
    dry.add_argument("config")
    dry.add_argument("--json", action="store_true", help="write one JSON result to stdout")
    dry.set_defaults(func=cmd_dry_run)

    args = p.parse_args(argv)
    if getattr(args, "mock", False):
        os.environ["REPOCAST_MOCK"] = "1"  # configs can key env off this
    try:
        return args.func(args) or 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts failures to stable output
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
