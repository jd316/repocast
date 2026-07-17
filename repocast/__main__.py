"""repocast CLI.

    repocast render  walkthrough.yaml [--mock] [--voice Charon] [--no-voice]
    repocast verify  walkthrough.mp4  [--script NARRATION.md]
    repocast voices  [--voice Charon --voice Orus ...]   # sample clips to pick a voice
    repocast validate walkthrough.yaml
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, config, tts
from .engine import render


def _key(args) -> str:
    k = args.key or os.environ.get("GEMINI_API_KEY", "")
    if not k:
        sys.exit("no Gemini key — pass --key or set GEMINI_API_KEY (or use --no-voice / "
                 "tts.provider: none)")
    return k


def cmd_render(args) -> None:
    cfg = config.load(args.config)
    if args.voice:
        cfg["tts"]["voice"] = args.voice
    no_voice = args.no_voice or cfg["tts"]["provider"] == "none"
    workdir = Path(args.workdir).resolve()      # absolute: mux/ffmpeg run from any cwd

    audio: dict = {}
    if not no_voice:
        key = _key(args)
        texts = config.all_narration(cfg)
        print(f"generating voice-over ({cfg['tts'].get('voice', tts.DEFAULT_VOICE)}, "
              f"{len(set(texts))} lines)…")
        audio = tts.synth_all(
            texts, key, workdir / "voice",
            voice=cfg["tts"].get("voice", tts.DEFAULT_VOICE),
            model=cfg["tts"].get("model", tts.DEFAULT_MODEL),
            style=cfg["tts"].get("style", tts.DEFAULT_STYLE),
        )

    out = (cfg["_base"] / cfg["output"]) if not Path(cfg["output"]).is_absolute() else Path(cfg["output"])
    out = Path(args.output) if args.output else out
    script = out.with_name("NARRATION.md")
    print("recording…")
    summary = render(cfg, audio, out, script, workdir)
    print(f"\n✅ {out}  ({summary['size'] / 1e6:.1f} MB, {summary['duration']:.0f}s, "
          f"{summary['w']}x{summary['h']}, audio={'yes' if summary['audio'] else 'no'})")
    print(f"✅ {script}  (timestamped narration)")
    if not args.no_verify:
        _verify(out, script)


def cmd_verify(args) -> None:
    _verify(Path(args.video), Path(args.script) if args.script else
            Path(args.video).with_name("NARRATION.md"))


def _verify(video: Path, script: Path | None) -> None:
    print("verifying…")
    # audio present + is speech, not silence
    codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(video)],
                           capture_output=True, text=True).stdout.strip()
    if codec:
        vol = subprocess.run(["ffmpeg", "-v", "info", "-i", str(video), "-af", "volumedetect",
                              "-f", "null", "-"], capture_output=True, text=True).stderr
        mean = next((ln.split("mean_volume:")[1].strip() for ln in vol.splitlines()
                     if "mean_volume" in ln), "?")
        print(f"  audio: {codec}, mean_volume {mean}  "
              f"({'speech' if 'dB' in mean and float(mean.split()[0]) > -40 else 'CHECK — near silent'})")
    else:
        print("  audio: NONE (silent video)")
    # alignment: speech onsets vs script cues
    if script and script.is_file():
        import re
        onsets = subprocess.run(["ffmpeg", "-v", "info", "-i", str(video),
                                 "-af", "silencedetect=n=-45dB:d=0.8", "-f", "null", "-"],
                                capture_output=True, text=True).stderr
        onset_t = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", onsets)]
        cues = [int(m[0]) * 60 + int(m[1]) for m in
                re.findall(r"`(\d{2}):(\d{2})`", script.read_text())]
        if onset_t and cues:
            drifts = [min(abs(o - c) for o in onset_t) for c in cues]
            bad = sum(1 for d in drifts if d > 1.5)
            print(f"  alignment: {len(cues)} cues, {len(onset_t)} onsets, "
                  f"median drift {sorted(drifts)[len(drifts) // 2]:.1f}s, off-by->1.5s: {bad}")
    print("  (spot-check a frame: ffmpeg -ss <t> -i "
          f"{video.name} -frames:v 1 f.png — LOOK at it; automated freeze checks lie.)")


def cmd_voices(args) -> None:
    key = _key(args)
    outdir = Path("voice_samples")
    outdir.mkdir(exist_ok=True)
    sample = ("Reserve the worst case before generating, settle the truth after, "
              "and refund the difference. A single atomic update keeps two concurrent "
              "requests from both passing the gate.")
    for v in (args.voice or ["Charon", "Orus", "Iapetus", "Algenib"]):
        dur = tts.synth(sample, key, outdir / f"{v}.wav", voice=v,
                        model=args.model or tts.DEFAULT_MODEL)
        subprocess.run(["ffmpeg", "-y", "-i", str(outdir / f"{v}.wav"),
                        str(outdir / f"{v}.mp3")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (outdir / f"{v}.wav").unlink()
        print(f"  {v:10s} {dur:4.1f}s  -> voice_samples/{v}.mp3")
    print("Play them and pick one; pass it as --voice or set tts.voice in the config.")


_TEMPLATE = """\
# repocast walkthrough — a narrated video of your project.
# Paths are relative to THIS file. Render:  repocast render <this-file>
output: walkthrough.mp4
size: [1920, 1080]
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
        - {click: "#submit"}
        - {wait_for: ".result", timeout: 60000}
        - {say: "…and the result comes back."}
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
    print(f"✓ {args.config} valid — {len(cfg['segments'])} segments, {n} narration lines")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="repocast",
                                description="Narrated walkthrough video for any code project.")
    p.add_argument("--version", action="version", version=f"repocast {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="build the narrated video from a config")
    r.add_argument("config")
    r.add_argument("--voice", help="override tts.voice")
    r.add_argument("--no-voice", action="store_true", help="silent video (bring your own voice)")
    r.add_argument("--mock", action="store_true",
                   help="reserved for configs that read $REPOCAST_MOCK to pick a mock backend")
    r.add_argument("--key", help="Gemini API key (or set GEMINI_API_KEY)")
    r.add_argument("--output", help="override output path")
    r.add_argument("--workdir", default="_repocast", help="scratch dir (removed after)")
    r.add_argument("--no-verify", action="store_true")
    r.set_defaults(func=cmd_render)

    v = sub.add_parser("verify", help="check a rendered video (audio + alignment)")
    v.add_argument("video")
    v.add_argument("--script")
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
    va.add_argument("--template", action="store_true",
                    help="skip file-existence checks (for placeholder templates)")
    va.set_defaults(func=cmd_validate)

    args = p.parse_args(argv)
    if getattr(args, "mock", False):
        os.environ["REPOCAST_MOCK"] = "1"   # configs can key env off this
    args.func(args)


if __name__ == "__main__":
    main()
