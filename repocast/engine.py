"""The recording engine: turn a validated config into a narrated MP4.

Design decisions worth knowing (all learned the hard way):
- AUDIO-FIRST pacing. Narration is synthesised first; every on-screen hold is at
  least as long as the line spoken over it, using the clip's REAL measured
  duration. Never the other way round (that drifts).
- The terminal segment renders captured stdout into a styled HTML "terminal" and
  films that — so it works on boxes with no terminal emulator (servers, Wayland).
- Verify by looking at frames + audio levels, not md5/freezedetect (h264 noise
  makes visually-static frames differ bit-for-bit; those tools lie).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RESULT = re.compile(r"passed|OK\b|✓|SUCCESS|Success:|All checks|TOTAL|FAIL|error|Error", re.I)
WPS = 2.3          # words/sec fallback when a line has no audio (e.g. --no-voice)
PAD = 0.6          # breathing room after each spoken line


# ----------------------------------------------------------------- helpers ---
def clean_env(extra: dict | None = None) -> dict:
    """Env without this process's own venv, so child `uv`/`make` calls don't emit
    'VIRTUAL_ENV does not match' warnings into the filmed terminal output."""
    e = {k: v for k, v in os.environ.items()
         if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}
    return {**e, **(extra or {})}


def _dur(mp4: Path) -> float:
    return float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)],
        capture_output=True, text=True).stdout.strip() or 0)


class Marks:
    """Collects (offset_seconds, spoken_text) per segment for NARRATION.md, and
    holds the page long enough to actually say each line."""

    def __init__(self, audio: dict[str, tuple[Path, float]]):
        self.audio = audio
        self.t0 = time.monotonic()
        self.items: list[tuple[float, str]] = []

    def speak_time(self, text: str) -> float:
        if text in self.audio:
            return self.audio[text][1] + PAD
        return max(2.0, len(text.split()) / WPS + PAD)

    def say(self, page, text: str, extra: float = 0.0) -> None:
        self.items.append((time.monotonic() - self.t0, text))
        page.wait_for_timeout(int((self.speak_time(text) + extra) * 1000))


# --------------------------------------------------------------- 1. doc ------
_DOC_CSS = """
 body{margin:0;background:#fff;color:#1f2328;font:17px/1.75 -apple-system,'Segoe UI',Inter,Roboto,sans-serif}
 #doc{max-width:1180px;margin:0 auto;padding:60px 56px 70vh}
 h1{font-size:40px;border-bottom:2px solid #d0d7de;padding-bottom:14px;margin-top:0}
 h2{font-size:30px;border-bottom:1px solid #d8dee4;padding-bottom:9px;margin-top:46px;scroll-margin-top:24px}
 h3{font-size:23px;margin-top:34px;scroll-margin-top:24px}
 code{background:#f0f1f3;padding:2px 6px;border-radius:5px;font-size:15px;font-family:'DejaVu Sans Mono',monospace}
 pre{background:#0d1117;color:#e6edf3;padding:18px 20px;border-radius:9px;overflow-x:auto;font-size:14.5px;line-height:1.55}
 pre code{background:none;color:inherit;padding:0}
 table{border-collapse:collapse;margin:18px 0;font-size:15.5px}
 th,td{border:1px solid #d0d7de;padding:8px 13px;text-align:left} th{background:#f6f8fa}
 blockquote{border-left:4px solid #d0d7de;margin:0;padding-left:16px;color:#59636e}
 strong{color:#0a0c10}
"""


def _render_doc_html(md_path: Path, out_html: Path) -> set[str]:
    import markdown
    html = markdown.markdown(md_path.read_text(),
                             extensions=["fenced_code", "tables", "toc"])
    out_html.write_text(f"<!doctype html><meta charset=utf-8><style>{_DOC_CSS}</style>"
                        f"<div id=doc>{html}</div>")
    return set(re.findall(r'<h[1-4] id="([^"]+)"', html))


def _drive_doc(page, body: dict, marks: Marks, base: Path, work: Path) -> None:
    html = work / "doc.html"
    ids = _render_doc_html(base / body["file"], html)
    for s in body.get("steps", []):          # fail loud on a bad anchor (silent scroll = missing section)
        if s["anchor"] not in ids:
            raise SystemExit(f"doc anchor {s['anchor']!r} not in {body['file']} "
                             f"(headings: {sorted(ids)})")
    page.goto(html.as_uri())
    page.wait_for_timeout(1200)
    if body.get("steps") and body["steps"][0].get("intro"):
        marks.say(page, body["steps"][0]["intro"])
    for s in body["steps"]:
        page.evaluate("(id)=>document.getElementById(id)"
                      ".scrollIntoView({behavior:'smooth',block:'start'})", s["anchor"])
        page.wait_for_timeout(900)
        if s.get("say"):
            marks.say(page, s["say"])


# --------------------------------------------------------- 2. terminal -------
_TERM_TPL = """<!doctype html><meta charset=utf-8><style>
 html,body{margin:0;background:#0d1117;height:100%;overflow:hidden}
 #wrap{height:100vh;display:flex;flex-direction:column;font-family:'DejaVu Sans Mono','Liberation Mono',monospace}
 #bar{background:#161b22;color:#8b949e;padding:11px 20px;font-size:15px;border-bottom:1px solid #30363d;flex:0 0 auto}
 #bar b{color:#c9d1d9}
 #term{flex:1;overflow:hidden;padding:20px 26px;color:#c9d1d9;font-size:18px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
 .cmd{color:#58a6ff;font-weight:700;margin-top:12px}.ok{color:#3fb950}
</style><div id=wrap><div id=bar>__TITLE__</div><div id=term></div></div><script>
const L=__LINES__,t=document.getElementById('term');let i=0;
function step(){if(i>=L.length){window.__done=true;return;}const[x,d,k]=L[i++];
const e=document.createElement('div');if(k)e.className=k;e.textContent=x===''?' ':x;t.appendChild(e);
while(t.scrollHeight>t.clientHeight)t.removeChild(t.firstChild);setTimeout(step,d);}step();</script>"""


def _run(argv: list[str], cwd: Path, env: dict | None) -> list[str]:
    print(f"  $ {' '.join(argv)}")
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                       env=clean_env(env), timeout=900)
    return ANSI.sub("", (p.stdout or "") + (p.stderr or "")).rstrip("\n").split("\n")


def _build_terminal(body: dict, marks: Marks, base: Path, work: Path, pace: float):
    """Returns (html_path, total_seconds). Each command's output is typed out,
    then held long enough to speak its `say` line before the next command."""
    cwd = base / body.get("cwd", ".")
    lines: list[list] = []
    t = 0.0

    def add(text, ms, kind=""):
        nonlocal t
        ms = int(ms * pace)
        lines.append([text, ms, kind])
        t += ms / 1000.0

    for step in body["steps"]:
        say = step.get("say")
        if say:
            marks.items.append((t, say))          # cue fires as the command starts
        add("$ " + " ".join(step["run"]), 900, "cmd")
        out = _run(step["run"], cwd, step.get("env"))
        for ln in out:
            if ln.strip() == "":
                add("", 60)
            elif _RESULT.search(ln):
                add(ln, 1500, "ok")
            else:
                add(ln, 110)
        # hold the finished output long enough to narrate `say`
        need = marks.speak_time(say) if say else 3.0
        held = t - (marks.items[-1][0] if say and marks.items else t)
        if say and held < need and lines:
            lines[-1][1] += int((need - held) * 1000)
            t += need - held
        elif not say and lines:                    # generic beat between commands
            lines[-1][1] += 2500
            t += 2.5

    html = work / "terminal.html"
    title = body.get("title", "terminal")
    html.write_text(_TERM_TPL.replace("__TITLE__", title)
                    .replace("__LINES__", json.dumps(lines)))
    return html, t


def _drive_terminal(page, html: Path, secs: float) -> None:
    page.goto(html.as_uri())
    page.wait_for_function("window.__done === true", timeout=int(secs * 1000) + 60_000)


# --------------------------------------------------------------- 3. app ------
def _wait_ready(url: str, path: str, timeout: float = 90) -> bool:
    import urllib.request
    for _ in range(int(timeout * 4)):
        try:
            with urllib.request.urlopen(url.rstrip("/") + path, timeout=1) as r:
                if r.status < 500:
                    return True
        except Exception:                          # noqa: BLE001
            time.sleep(0.25)
    return False


def _drive_app(page, body: dict, marks: Marks, base: Path):
    proc = None
    if body.get("start"):
        proc = subprocess.Popen(body["start"], cwd=base / body.get("cwd", "."),
                                env=clean_env(body.get("env")),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_ready(body["url"], body.get("ready_path", "/")):
            proc.terminate()
            raise SystemExit(f"app did not become ready at {body['url']}")
    try:
        page.goto(body["url"])
        if body.get("zoom"):                       # fill a 1080p frame with a centred web app
            page.evaluate(f"document.body.style.zoom='{body['zoom']}'")
        page.wait_for_timeout(700)
        for a in body["actions"]:
            _do_action(page, a, marks)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:                      # noqa: BLE001
                proc.kill()


def _do_action(page, a: dict, marks: Marks) -> None:
    if "say" in a:
        marks.say(page, a["say"], extra=a.get("extra", 0.0))
    elif "click" in a:
        page.click(a["click"])
        page.wait_for_timeout(int(a.get("after", 0.4) * 1000))
    elif "fill" in a:
        page.fill(a["fill"], str(a["value"]))
        page.wait_for_timeout(int(a.get("after", 0.3) * 1000))
    elif "press" in a:
        page.keyboard.press(a["press"])
    elif "goto" in a:
        page.goto(a["goto"])
    elif "wait_for" in a:
        page.wait_for_selector(a["wait_for"], timeout=int(a.get("timeout", 60000)))
    elif "eval" in a:
        page.evaluate(a["eval"])
    elif "sleep" in a:
        page.wait_for_timeout(int(a["sleep"] * 1000))


# ------------------------------------------------------- record / encode -----
def _encode(webm: Path, mp4: Path, w: int, h: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(webm), "-c:v", "libx264", "-preset", "slow",
         "-crf", "21", "-pix_fmt", "yuv420p", "-r", "30",
         "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1",
         "-movflags", "+faststart", str(mp4)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _record(pw, url, driver, sub: Path, w: int, h: int):
    shutil.rmtree(sub, ignore_errors=True)   # clean THIS segment only (stale webms)
    sub.mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(channel="chrome",
                                 args=[f"--window-size={w},{h}", "--hide-scrollbars"])
    ctx = browser.new_context(viewport={"width": w, "height": h},
                              record_video_dir=str(sub),
                              record_video_size={"width": w, "height": h})
    page = ctx.new_page()
    if url:
        page.goto(url)
    marks = driver(page)
    ctx.close()
    browser.close()
    mp4 = sub.with_suffix(".mp4")
    _encode(next(sub.glob("*.webm")), mp4, w, h)
    return mp4, marks


def _mux(video: Path, parts: list, audio: dict, out: Path) -> bool:
    clips = sorted((off + t, audio[text][0])
                   for _n, off, m in parts for t, text in m.items if text in audio)
    if not clips:
        shutil.copy(video, out)
        return False
    ins, fc, labels = ["-i", str(video)], [], []
    for i, (t, p) in enumerate(clips, start=1):
        ins += ["-i", str(p)]
        ms = max(0, int(t * 1000))
        fc.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    fc.append("".join(labels) + f"amix=inputs={len(clips)}:normalize=0[aout]")
    subprocess.run(["ffmpeg", "-y", *ins, "-filter_complex", ";".join(fc),
                    "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def _write_script(parts, total: float, path: Path, video_name: str) -> None:
    def ts(s):
        return f"{int(s) // 60:02d}:{int(s) % 60:02d}"
    out = [f"# Narration script — {video_name}", "",
           f"Total run time **{ts(total)}**. Timestamps are the video's own clock — "
           "read each line as its timestamp comes up. Numbers are what's on screen.", ""]
    for name, off, m in parts:
        out += [f"## {name}  ({ts(off)} – {ts(off + m.seg_len)})", ""]
        for t, say in m.items:
            out += [f"**`{ts(off + t)}`**  {say}", ""]
    out += ["---", "",
            "Regenerate the video and this script together with `repocast render` — "
            "they stay in sync. To use your own voice, read this over the silent "
            "video (render with `tts.provider: none`)."]
    path.write_text("\n".join(out))


def render(cfg: dict, audio: dict, out: Path, script: Path, work: Path) -> dict:
    """Record every segment, concat, mux the voice-over, write the script.
    Returns a summary dict."""
    from playwright.sync_api import sync_playwright
    w, h = cfg["size"]
    base = cfg["_base"].resolve()
    cfg["_base"] = base
    work = Path(work).resolve()              # file:// URIs need absolute paths
    parts = []
    # Do NOT wipe `work` here — the voice clips (audio-first) were already written
    # into work/voice by the caller. Each segment cleans its own subdir instead.
    work.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        for idx, seg in enumerate(cfg["segments"]):
            kind, body = next(iter(seg.items()))
            name = body.get("name") or {"doc": "Design document", "terminal": "Tests & demo",
                                        "app": "Live app"}[kind]
            sub = work / f"seg{idx}_{kind}"
            print(f"recording segment {idx} ({kind})…")
            if kind == "doc":
                mp4, m = _record(pw, None,
                                 lambda p, b=body: _drv_doc(p, b, audio, base, work),
                                 sub, w, h)
            elif kind == "terminal":
                marks = Marks(audio)
                html, secs = _build_terminal(body, marks, base, work, body.get("pace", 1.0))
                mp4, _ = _record(pw, None,
                                 lambda p, h_=html, s_=secs: (_drive_terminal(p, h_, s_), marks)[1],
                                 sub, w, h)
                m = marks
            else:  # app
                mp4, m = _record(pw, None,
                                 lambda p, b=body: _drv_app(p, b, audio, base), sub, w, h)
            parts.append((name, mp4, m))

    # concat
    silent = work / "silent.mp4"
    if len(parts) == 1:
        shutil.copy(parts[0][1], silent)
    else:
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{p}'\n" for _n, p, _m in parts))
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", "-movflags", "+faststart", str(silent)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    named, off = [], 0.0
    for name, mp4, m in parts:
        m.seg_len = _dur(mp4)
        named.append((name, off, m))
        off += m.seg_len

    _write_script(named, off, script, out.name)
    has_audio = _mux(silent, named, audio, out)
    shutil.rmtree(work, ignore_errors=True)
    return {"duration": _dur(out), "size": out.stat().st_size, "audio": has_audio,
            "w": w, "h": h}


# marks-returning driver wrappers (Playwright driver must return the Marks obj)
def _drv_doc(page, body, audio, base, work):
    m = Marks(audio)
    _drive_doc(page, body, m, base, work)
    return m


def _drv_app(page, body, audio, base):
    m = Marks(audio)
    _drive_app(page, body, m, base)
    return m
