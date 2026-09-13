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

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RESULT = re.compile(
    r"passed|OK\b|✓|SUCCESS|Success:|All checks|TOTAL|FAIL|error|Error", re.IGNORECASE
)
WPS = 2.3  # words/sec fallback when a line has no audio (e.g. --no-voice)
PAD = 0.6  # breathing room after each spoken line


class CommandError(RuntimeError):
    pass


# ----------------------------------------------------------------- helpers ---
def clean_env(extra: dict | None = None) -> dict:
    """Env without this process's own venv, so child `uv`/`make` calls don't emit
    'VIRTUAL_ENV does not match' warnings into the filmed terminal output."""
    e = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")}
    return {**e, **(extra or {})}


def _dur(mp4: Path) -> float:
    return float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(mp4),
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        or 0
    )


def _has_audio(path: Path) -> bool:
    return bool(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )


def _atempo(speed: float) -> str:
    factors = []
    while speed > 2:
        factors.append(2.0)
        speed /= 2
    while speed < 0.5:
        factors.append(0.5)
        speed /= 0.5
    factors.append(speed)
    return ",".join(f"atempo={factor:g}" for factor in factors)


class Marks:
    """Collects (offset_seconds, spoken_text) per segment for NARRATION.md, and
    holds the page long enough to actually say each line."""

    def __init__(self, audio: dict[str, tuple[Path, float]]):
        self.audio = audio
        self.t0 = time.monotonic()
        self.items: list[tuple[float, str]] = []
        self.seg_len = 0.0
        self.source_audio: dict | None = None

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

    html = markdown.markdown(md_path.read_text(), extensions=["fenced_code", "tables", "toc"])
    out_html.write_text(
        f"<!doctype html><meta charset=utf-8><style>{_DOC_CSS}</style><div id=doc>{html}</div>"
    )
    return set(re.findall(r'<h[1-4] id="([^"]+)"', html))


def _drive_doc(
    page, body: dict, marks: Marks, base: Path, work: Path, theme: dict | None = None
) -> None:
    html = work / "doc.html"
    ids = _render_doc_html(base / body["file"], html)
    for s in body.get("steps", []):  # fail loud on a bad anchor (silent scroll = missing section)
        if s["anchor"] not in ids:
            raise RuntimeError(
                f"doc anchor {s['anchor']!r} not in {body['file']} (headings: {sorted(ids)})"
            )
    page.goto(html.as_uri())
    _apply_theme(page, theme or {})
    page.wait_for_timeout(1200)
    if body.get("steps") and body["steps"][0].get("intro"):
        marks.say(page, body["steps"][0]["intro"])
    for s in body["steps"]:
        page.evaluate(
            "(id)=>document.getElementById(id).scrollIntoView({behavior:'smooth',block:'start'})",
            s["anchor"],
        )
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


def _run(argv: list[str], cwd: Path, env: dict | None, allow_failure: bool = False) -> list[str]:
    print(f"  $ {' '.join(argv)}")
    p = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=clean_env(env),
        timeout=900,
        check=False,
    )
    output = ANSI.sub("", (p.stdout or "") + (p.stderr or "")).rstrip("\n")
    if p.returncode and not allow_failure:
        raise CommandError(
            f"command failed with exit {p.returncode}: {' '.join(argv)}\n{output}".rstrip()
        )
    return output.split("\n")


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
            marks.items.append((t, say))  # cue fires as the command starts
        add("$ " + " ".join(step["run"]), 900, "cmd")
        out = _run(step["run"], cwd, step.get("env"), step.get("allow_failure", False))
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
        elif not say and lines:  # generic beat between commands
            lines[-1][1] += 2500
            t += 2.5

    html = work / "terminal.html"
    title = body.get("title", "terminal")
    html.write_text(_TERM_TPL.replace("__TITLE__", title).replace("__LINES__", json.dumps(lines)))
    return html, t


def _drive_terminal(page, html: Path, secs: float, theme: dict | None = None) -> None:
    page.goto(html.as_uri())
    _apply_theme(page, theme or {})
    page.wait_for_function("window.__done === true", timeout=int(secs * 1000) + 60_000)


# --------------------------------------------------------------- 3. app ------
def _wait_ready(url: str, path: str, timeout: float = 90, proc=None) -> bool:
    import urllib.request

    for _ in range(int(timeout * 4)):
        if proc and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url.rstrip("/") + path, timeout=1) as r:
                if r.status < 500:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


_DEMO_LAYER = """
() => {
  if (document.getElementById('__repocast_layer')) return;
  const style = document.createElement('style');
  style.textContent = `
    #__repocast_layer{position:fixed;inset:0;z-index:2147483647;pointer-events:none}
    .__repocast_cursor{position:absolute;width:18px;height:18px;background:#fff;
      clip-path:polygon(0 0,0 82%,24% 62%,42% 100%,55% 93%,37% 58%,68% 58%);
      filter:drop-shadow(0 1px 1px #000) drop-shadow(0 2px 4px #0008);
      transition:left .28s ease,top .28s ease}
    .__repocast_focus{position:absolute;border:4px solid #60a5fa;border-radius:10px;
      box-shadow:0 0 0 9999px #0004,0 0 24px #60a5fa;transition:all .25s ease}
    .__repocast_note{position:absolute;max-width:360px;padding:12px 16px;border-radius:10px;
      color:white;background:#111827eF;font:600 18px/1.35 system-ui;box-shadow:0 6px 24px #0006}
    .__repocast_ripple{position:absolute;width:14px;height:14px;border:4px solid #60a5fa;border-radius:50%;
      transform:translate(-50%,-50%);animation:__repocast_ripple .55s ease-out forwards}
    @keyframes __repocast_ripple{to{width:90px;height:90px;opacity:0}}
  `;
  document.head.appendChild(style);
  const layer = document.createElement('div'); layer.id = '__repocast_layer';
  const cursor = document.createElement('div'); cursor.className = '__repocast_cursor';
  layer.appendChild(cursor); document.documentElement.appendChild(layer);
}
"""


def _apply_theme(page, theme: dict) -> None:
    if not theme:
        return
    page.evaluate(
        """theme => {
      const padding = theme.padding || 0;
      Object.assign(document.documentElement.style, {
        background: theme.background || '#111827', padding: padding + 'px',
        boxSizing: 'border-box', overflow: 'hidden'
      });
      Object.assign(document.body.style, {
        minHeight: `calc(100vh - ${padding * 2}px)`, margin: '0',
        borderRadius: (theme.radius || 0) + 'px', overflow: 'hidden',
        boxShadow: theme.shadow ? '0 18px 55px rgba(0,0,0,.5)' : 'none'
      });
    }""",
        theme,
    )


def _configure_cursor(page, cursor: dict) -> None:
    page.evaluate(
        """c => {
      const el=document.querySelector('.__repocast_cursor');
      const size=c.size||18;
      el.style.width=size+'px';el.style.height=size+'px';
      el.style.backgroundColor=c.color||'#fff';
      el.style.transitionDuration=(c.smoothing??.28)+'s';
      el.style.filter=c.blur?`blur(${c.blur}px)`:'';
    }""",
        cursor,
    )


def _point_at(
    page, selector: str, *, focus: bool = False, ripple: bool = False, zoom: float | None = None
) -> None:
    locator = page.locator(selector).first
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    if not box:
        raise RuntimeError(f"selector has no visible box: {selector}")
    page.evaluate(_DEMO_LAYER)
    page.evaluate(
        """p => {
      const layer=document.getElementById('__repocast_layer'), c=layer.querySelector('.__repocast_cursor');
      const x=p.x+p.width/2,y=p.y+p.height/2;c.style.left=x+'px';c.style.top=y+'px';
      layer.querySelector('.__repocast_focus')?.remove();
      if(p.focus){const f=document.createElement('div');f.className='__repocast_focus';
        Object.assign(f.style,{left:(p.x-6)+'px',top:(p.y-6)+'px',width:(p.width+12)+'px',height:(p.height+12)+'px'});layer.appendChild(f)}
      if(p.zoom){document.body.style.transition='transform .35s ease';
        document.body.style.transformOrigin=x+'px '+y+'px';document.body.style.transform=`scale(${p.zoom})`}
      if(p.ripple){const r=document.createElement('div');r.className='__repocast_ripple';
        r.style.left=x+'px';r.style.top=y+'px';layer.appendChild(r);setTimeout(()=>r.remove(),600)}
    }""",
        {
            **box,
            "focus": focus,
            "ripple": ripple,
            "zoom": zoom if zoom is not None else (1.12 if focus else None),
        },
    )
    page.wait_for_timeout(300)


def _annotate(page, spec: dict, base: Path) -> None:
    page.evaluate(_DEMO_LAYER)
    spec = spec.copy()
    if spec.get("image"):
        path = (base / spec["image"]).resolve()
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        spec["src"] = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"
    box = None
    if spec.get("target"):
        box = page.locator(spec["target"]).first.bounding_box()
        if not box:
            raise RuntimeError(f"annotation target has no visible box: {spec['target']}")
    page.evaluate(
        """p => {
      const n=p.src?document.createElement('img'):document.createElement('div');
      if(p.src){n.src=p.src;n.style.width=(p.width||240)+'px';n.style.borderRadius='10px'}
      else if(p.shape){n.style.width=(p.width||160)+'px';n.style.height=(p.height||100)+'px';
        n.style.border=`4px solid ${p.color||'#60a5fa'}`;n.style.borderRadius=p.shape==='circle'?'50%':'10px'}
      else{n.className='__repocast_note';n.textContent=p.text}
      n.style.position='absolute';n.style.filter=p.src?'drop-shadow(0 6px 16px #0008)':'';
      n.style.left=(p.x ?? 32)+'px';n.style.top=(p.y ?? 32)+'px';
      document.getElementById('__repocast_layer').appendChild(n);
      if(p.duration)setTimeout(()=>n.remove(),p.duration*1000);
    }""",
        {
            "text": spec.get("text"),
            "src": spec.get("src"),
            "shape": spec.get("shape"),
            "color": spec.get("color"),
            "width": spec.get("width"),
            "height": spec.get("height"),
            "x": spec.get("x", box["x"] if box else None),
            "y": spec.get("y", box["y"] + box["height"] + 12 if box else None),
            "duration": spec.get("duration"),
        },
    )


def _install_app_style(page, body: dict, theme: dict) -> None:
    _apply_theme(page, theme)
    page.evaluate(_DEMO_LAYER)
    _configure_cursor(page, body.get("cursor", {}))
    if body.get("zoom"):
        page.evaluate("zoom => { document.body.style.zoom = String(zoom) }", body["zoom"])


def _drive_app(
    page,
    body: dict,
    marks: Marks,
    base: Path,
    theme: dict | None = None,
    log_path: Path | None = None,
):
    proc = None
    log = None
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        if body.get("start"):
            if log_path:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log = log_path.open("w+")
            proc = subprocess.Popen(
                body["start"],
                cwd=base / body.get("cwd", "."),
                env=clean_env(body.get("env")),
                stdout=log or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
            )
            if not _wait_ready(body["url"], body.get("ready_path", "/"), proc=proc):
                detail = ""
                if log:
                    log.flush()
                    assert log_path is not None
                    detail = log_path.read_text(errors="replace").strip()
                state = f"; process exited {proc.returncode}" if proc.poll() is not None else ""
                tail = f"\n{detail[-4000:]}" if detail else ""
                raise RuntimeError(f"app did not become ready at {body['url']}{state}{tail}")
        page.goto(body["url"])
        _install_app_style(page, body, theme or {})
        page.wait_for_timeout(700)
        for i, a in enumerate(body["actions"]):
            try:
                _do_action(page, a, marks, base, body, theme or {})
                if page_errors:
                    raise RuntimeError(f"page error: {page_errors[-1]}")
            except Exception as exc:
                raise RuntimeError(f"app action {i}: {exc}") from exc
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
                proc.wait()
        if log:
            log.close()


def _do_action(
    page,
    a: dict,
    marks: Marks,
    base: Path | None = None,
    app: dict | None = None,
    theme: dict | None = None,
) -> None:
    if "say" in a:
        marks.say(page, a["say"], extra=a.get("extra", 0.0))
    elif "click" in a:
        _point_at(
            page,
            a["click"],
            focus=a.get("focus", False),
            ripple=a.get("effect") == "ripple",
            zoom=a.get("zoom"),
        )
        page.click(a["click"])
        page.wait_for_timeout(int(a.get("after", 0.4) * 1000))
    elif "fill" in a:
        _point_at(page, a["fill"], focus=a.get("focus", False), zoom=a.get("zoom"))
        page.fill(a["fill"], str(a["value"]))
        page.wait_for_timeout(int(a.get("after", 0.3) * 1000))
    elif "press" in a:
        page.keyboard.press(a["press"])
    elif "goto" in a:
        page.goto(a["goto"])
        _install_app_style(page, app or {}, theme or {})
    elif "wait_for" in a:
        page.wait_for_selector(a["wait_for"], timeout=int(a.get("timeout", 60000)))
    elif "eval" in a:
        page.evaluate(a["eval"])
    elif "sleep" in a:
        page.wait_for_timeout(int(a["sleep"] * 1000))
    elif "annotate" in a:
        _annotate(page, a["annotate"], base or Path.cwd())
        page.wait_for_timeout(int(a.get("after", 0.4) * 1000))
    elif "check" in a:
        _check_page(page, a["check"])


def _check_page(page, spec: dict) -> None:
    result = page.evaluate(
        """spec => {
          const text=document.documentElement.innerText;
          const required=(spec.required_text||[]).filter(value=>!text.includes(value));
          const forbidden=(spec.forbidden_text||[]).filter(value=>text.includes(value));
          const scrolling=window.scrollX!==0||window.scrollY!==0||
            document.documentElement.scrollWidth>window.innerWidth||
            document.documentElement.scrollHeight>window.innerHeight;
          let smallest=null;
          if(spec.min_font_size){
            for(const el of document.documentElement.querySelectorAll('*')){
              if(![...el.childNodes].some(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim()))continue;
              const box=el.getBoundingClientRect(),style=getComputedStyle(el);
              if(box.width&&box.height&&style.visibility!=='hidden'&&style.display!=='none'){
                const size=parseFloat(style.fontSize);if(!smallest||size<smallest.size)smallest={size,text:el.textContent.trim().slice(0,80)};
              }
            }
          }
          return {required,forbidden,scrolling,smallest};
        }""",
        spec,
    )
    problems = []
    if result["required"]:
        problems.append(f"required text missing: {result['required']}")
    if result["forbidden"]:
        problems.append(f"forbidden text present: {result['forbidden']}")
    if spec.get("no_scroll") and result["scrolling"]:
        problems.append("page scroll or overflow detected")
    smallest = result["smallest"]
    if smallest and smallest["size"] < spec.get("min_font_size", 0):
        problems.append(
            f"text is {smallest['size']:g}px, below {spec['min_font_size']}px: {smallest['text']!r}"
        )
    if problems:
        raise RuntimeError("; ".join(problems))


# ------------------------------------------------------- record / encode -----
def _encode(webm: Path, mp4: Path, w: int, h: int, video: dict | None = None) -> None:
    video = video or {}
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(webm),
            "-c:v",
            "libx264",
            "-preset",
            video.get("preset", "slow"),
            "-crf",
            str(video.get("crf", 21)),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(video.get("fps", 30)),
            "-vf",
            (
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
            ),
            "-movflags",
            "+faststart",
            str(mp4),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _record(pw, url, driver, sub: Path, w: int, h: int, video: dict | None = None):
    shutil.rmtree(sub, ignore_errors=True)  # clean THIS segment only (stale webms)
    sub.mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(
        channel="chrome", args=[f"--window-size={w},{h}", "--hide-scrollbars"]
    )
    ctx = None
    try:
        ctx = browser.new_context(
            viewport={"width": w, "height": h},
            record_video_dir=str(sub),
            record_video_size={"width": w, "height": h},
        )
        page = ctx.new_page()
        if url:
            page.goto(url)
        marks = driver(page)
    finally:
        if ctx:
            ctx.close()
        browser.close()
    mp4 = sub.with_suffix(".mp4")
    _encode(next(sub.glob("*.webm")), mp4, w, h, video)
    return mp4, marks


def _edit_segment(mp4: Path, body: dict, marks: Marks, video: dict) -> Path:
    trim, fade = body.get("trim"), float(body.get("fade", 0))
    if not trim and not fade:
        return mp4
    original = _dur(mp4)
    start, end = (float(trim[0]), min(float(trim[1]), original)) if trim else (0.0, original)
    if end <= start:
        raise RuntimeError(f"trim starts after media ends: {mp4}")
    duration = end - start
    filters = [f"trim=start={start}:end={end}", "setpts=PTS-STARTPTS"]
    if fade:
        fade = min(fade, duration / 2)
        filters += [f"fade=t=in:st=0:d={fade}", f"fade=t=out:st={max(0, duration - fade)}:d={fade}"]
    edited = mp4.with_name(mp4.stem + "_edited.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(mp4),
            "-an",
            "-vf",
            ",".join(filters),
            "-c:v",
            "libx264",
            "-preset",
            video["preset"],
            "-crf",
            str(video["crf"]),
            "-r",
            str(video["fps"]),
            "-pix_fmt",
            "yuv420p",
            str(edited),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marks.items = [(at - start, text) for at, text in marks.items if start <= at < end]
    return edited


def _concat_entry(path: Path) -> str:
    return "file '" + str(path).replace("'", "'\\''") + "'\n"


def _mux(
    video: Path,
    parts: list,
    audio: dict,
    out: Path,
    extra_audio: list | None = None,
    base: Path | None = None,
) -> bool:
    clips = sorted(
        (off + t, audio[text][0]) for _n, off, m in parts for t, text in m.items if text in audio
    )
    extras = extra_audio or []
    if not clips and not extras:
        shutil.copy(video, out)
        return False
    ins, fc, labels = ["-i", str(video)], [], []
    for i, (t, p) in enumerate(clips, start=1):
        ins += ["-i", str(p)]
        ms = max(0, int(t * 1000))
        fc.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    for item in extras:
        i = len(labels) + 1
        path = Path(item["file"])
        if not path.is_absolute():
            path = (base or Path.cwd()) / path
        ins += ["-i", str(path)]
        ms = int(item.get("at", 0) * 1000)
        fc.append(f"[{i}:a]volume={item.get('volume', 1)},adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    fc.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0,loudnorm=I=-16:LRA=11:TP=-1.5,apad[aout]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            *ins,
            "-filter_complex",
            ";".join(fc),
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def _subtitle_time(seconds: float, vtt: bool = False) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    minute, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{minute:02d}:{sec:02d}{'.' if vtt else ','}{ms:03d}"


def _write_captions(parts: list, path: Path) -> None:
    vtt = path.suffix.lower() == ".vtt"
    cues = []
    for _name, off, marks in parts:
        for i, (at, text) in enumerate(marks.items):
            next_at = marks.items[i + 1][0] if i + 1 < len(marks.items) else marks.seg_len
            end = min(next_at, at + marks.speak_time(text))
            cues.append((off + at, off + max(at + 0.2, end), text))
    lines = ["WEBVTT", ""] if vtt else []
    for i, (start, end, text) in enumerate(cues, 1):
        if not vtt:
            lines.append(str(i))
        lines += [f"{_subtitle_time(start, vtt)} --> {_subtitle_time(end, vtt)}", text, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def _render_media(
    body: dict,
    base: Path,
    out: Path,
    w: int,
    h: int,
    video: dict,
    audio: dict,
    theme: dict | None = None,
) -> tuple[Path, Marks]:
    source = (base / body["file"]).resolve()
    marks = Marks(audio)
    say = body.get("say")
    if say:
        marks.items.append((0.0, say))
    mime = mimetypes.guess_type(source)[0] or ""
    image = mime.startswith("image/") and source.suffix.lower() not in {".gif"}
    speed = float(body.get("speed", 1))
    trim = body.get("trim") if not image else None
    if trim:
        source_duration = (min(float(trim[1]), _dur(source)) - float(trim[0])) / speed
    else:
        source_duration = 0 if image else _dur(source) / speed
    duration = float(body.get("duration", 0))
    duration = max(duration, marks.speak_time(say) if say else 0, source_duration)
    if duration <= 0:
        raise RuntimeError(f"cannot determine media duration: {source}")
    theme = theme or {}
    padding = min(int(theme.get("padding", 0)), (min(w, h) - 2) // 2)
    inner_w, inner_h = w - 2 * padding, h - 2 * padding
    background = theme.get("background", "black")
    filters = [f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease", "setsar=1"]
    args = ["ffmpeg", "-y"]
    if image:
        args += ["-loop", "1"]
    elif trim:
        args += ["-ss", str(trim[0]), "-t", str(trim[1] - trim[0])]
    args += ["-i", str(source)]
    if not image and speed != 1:
        filters.append(f"setpts=PTS/{speed}")
    if not image:
        filters.append(f"tpad=stop_mode=clone:stop_duration={duration}")
    radius = min(float(theme.get("radius", 0)), inner_w / 2, inner_h / 2)
    if radius:
        r = f"{radius:g}"
        alpha = f"if(lte(hypot(max(max({r}-X,0),X-W+{r}),max(max({r}-Y,0),Y-H+{r})),{r}),255,0)"
        filters += ["format=rgba", f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha}'"]
    chain = f"[0:v]{','.join(filters)}[fg];color=c={background}:s={w}x{h}:d={duration}[bg];"
    if theme.get("shadow"):
        chain += (
            "[fg]split[main][shadow];[shadow]colorchannelmixer=rr=0:gg=0:bb=0:aa=.45,"
            "boxblur=12:6[blur];[bg][blur]overlay=(W-w)/2+8:(H-h)/2+12[tmp];"
            "[tmp][main]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
        )
    else:
        chain += "[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[v]"
    args += [
        "-t",
        str(duration),
        "-an",
        "-filter_complex",
        chain,
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-preset",
        video["preset"],
        "-crf",
        str(video["crf"]),
        "-r",
        str(video["fps"]),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out),
    ]
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not image and _has_audio(source):
        source_audio = out.with_suffix(".audio.wav")
        audio_args = ["ffmpeg", "-y"]
        if trim:
            audio_args += ["-ss", str(trim[0]), "-t", str(trim[1] - trim[0])]
        audio_args += ["-i", str(source), "-vn"]
        if speed != 1:
            audio_args += ["-af", _atempo(speed)]
        audio_args += ["-t", str(duration), str(source_audio)]
        subprocess.run(audio_args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marks.source_audio = {"file": str(source_audio), "volume": body.get("volume", 1)}
    return out, marks


def _apply_webcam(video: Path, spec: dict, base: Path, out: Path, quality: dict) -> None:
    source = Path(spec["file"])
    if not source.is_absolute():
        source = base / source
    width, margin = int(spec.get("width", 320)), int(spec.get("margin", 32))
    positions = {
        "top-left": (str(margin), str(margin)),
        "top-right": (f"W-w-{margin}", str(margin)),
        "bottom-left": (str(margin), f"H-h-{margin}"),
        "bottom-right": (f"W-w-{margin}", f"H-h-{margin}"),
    }
    x, y = positions.get(spec.get("position", "bottom-right"), positions["bottom-right"])
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-stream_loop",
            "-1",
            "-i",
            str(source),
            "-filter_complex",
            f"[1:v]scale={width}:-2[cam];[0:v][cam]overlay={x}:{y}:shortest=1[v]",
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            quality["preset"],
            "-crf",
            str(quality["crf"]),
            "-r",
            str(quality["fps"]),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-shortest",
            "-movflags",
            "+faststart",
            str(out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _to_gif(video: Path, out: Path, fps: int) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-filter_complex",
            f"fps={min(fps, 20)},split[a][b];[a]palettegen[p];[b][p]paletteuse",
            str(out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _burn_captions(video: Path, captions: Path, out: Path, quality: dict, work: Path) -> None:
    local = work / f"captions{captions.suffix.lower()}"
    if captions.resolve() != local.resolve():
        shutil.copy(captions, local)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"subtitles={local.name}",
            "-c:v",
            "libx264",
            "-preset",
            quality["preset"],
            "-crf",
            str(quality["crf"]),
            "-r",
            str(quality["fps"]),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ],
        cwd=work,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _apply_watermark(video: Path, spec: dict, out: Path, quality: dict, work: Path) -> None:
    text = work / "watermark.txt"
    text.write_text(spec["text"])
    margin = int(spec.get("margin", 24))
    positions = {
        "top-left": (str(margin), str(margin)),
        "top-right": (f"w-tw-{margin}", str(margin)),
        "bottom-left": (str(margin), f"h-th-{margin}"),
        "bottom-right": (f"w-tw-{margin}", f"h-th-{margin}"),
    }
    x, y = positions[spec.get("position", "top-left")]
    draw = (
        f"drawtext=textfile={text.name}:x={x}:y={y}:fontsize={int(spec.get('size', 24))}:"
        f"fontcolor={spec.get('color', 'white')}:box=1:"
        f"boxcolor={spec.get('background', 'black@0.7')}:boxborderw=10"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            draw,
            "-c:v",
            "libx264",
            "-preset",
            quality["preset"],
            "-crf",
            str(quality["crf"]),
            "-r",
            str(quality["fps"]),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ],
        cwd=work,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_script(parts, total: float, path: Path, video_name: str) -> None:
    def ts(s):
        return f"{int(s) // 60:02d}:{int(s) % 60:02d}"

    out = [
        f"# Narration script — {video_name}",
        "",
        (
            f"Total run time **{ts(total)}**. Timestamps are the video's own clock — "
            "read each line as its timestamp comes up. Numbers are what's on screen."
        ),
        "",
    ]
    for name, off, m in parts:
        out += [f"## {name}  ({ts(off)} – {ts(off + m.seg_len)})", ""]
        for t, say in m.items:
            out += [f"**`{ts(off + t)}`**  {say}", ""]
    out += [
        "---",
        "",
        (
            "Regenerate the video and this script together with `repocast render` — "
            "they stay in sync. To use your own voice, read this over the silent "
            "video (render with `tts.provider: none`)."
        ),
    ]
    path.write_text("\n".join(out))


def render(cfg: dict, audio: dict, out: Path, script: Path, work: Path) -> dict:
    """Record every segment, concat, mux the voice-over, write the script.
    Returns a summary dict."""
    from playwright.sync_api import sync_playwright

    w, h = cfg["size"]
    video = cfg["video"]
    theme = cfg.get("theme", {})
    base = cfg["_base"].resolve()
    cfg["_base"] = base
    work = Path(work).resolve()  # file:// URIs need absolute paths
    protected = {Path("/").resolve(), Path.cwd().resolve(), Path.home().resolve(), base}
    if work in protected:
        raise ValueError(f"unsafe scratch directory: {work}")
    if out.resolve().is_relative_to(work):
        raise ValueError("output cannot be inside the scratch directory")
    parts = []
    # Do NOT wipe `work` here — the voice clips (audio-first) were already written
    # into work/voice by the caller. Each segment cleans its own subdir instead.
    work.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    script.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        for idx, seg in enumerate(cfg["segments"]):
            kind, body = next(iter(seg.items()))
            name = (
                body.get("name")
                or {
                    "doc": "Design document",
                    "terminal": "Tests & demo",
                    "app": "Live app",
                    "media": "Media",
                }[kind]
            )
            sub = work / f"seg{idx}_{kind}"
            print(f"recording segment {idx} ({kind})…")
            try:
                if kind == "doc":
                    mp4, m = _record(
                        pw,
                        None,
                        lambda p, b=body: _drv_doc(p, b, audio, base, work, theme),
                        sub,
                        w,
                        h,
                        video,
                    )
                elif kind == "terminal":
                    marks = Marks(audio)
                    html, secs = _build_terminal(body, marks, base, work, body.get("pace", 1.0))
                    mp4, _ = _record(
                        pw,
                        None,
                        lambda p, h_=html, s_=secs, m_=marks: (
                            _drive_terminal(p, h_, s_, theme),
                            m_,
                        )[1],
                        sub,
                        w,
                        h,
                        video,
                    )
                    m = marks
                elif kind == "app":
                    mp4, m = _record(
                        pw,
                        None,
                        lambda p, b=body, log=sub / "app.log": _drv_app(
                            p, b, audio, base, theme, log
                        ),
                        sub,
                        w,
                        h,
                        video,
                    )
                else:
                    mp4, m = _render_media(
                        body, base, sub.with_suffix(".mp4"), w, h, video, audio, theme
                    )
                edit_body = {**body, "trim": None} if kind == "media" else body
                mp4 = _edit_segment(mp4, edit_body, m, video)
            except Exception as exc:
                raise RuntimeError(f"segment {idx} ({kind}): {exc}") from exc
            parts.append((name, mp4, m))

    # concat
    silent = work / "silent.mp4"
    if len(parts) == 1:
        shutil.copy(parts[0][1], silent)
    else:
        lst = work / "concat.txt"
        lst.write_text("".join(_concat_entry(p) for _n, p, _m in parts))
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(silent),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    named, off = [], 0.0
    media_audio = []
    for name, mp4, m in parts:
        m.seg_len = _dur(mp4)
        named.append((name, off, m))
        if m.source_audio is not None:
            media_audio.append({**m.source_audio, "at": off})
        off += m.seg_len

    staged_script = work / "NARRATION.md"
    _write_script(named, off, staged_script, out.name)
    captions = cfg.get("captions")
    caption_path = None
    staged_captions = None
    if captions:
        caption_path = out.with_suffix(".srt") if captions is True else Path(captions)
        if not caption_path.is_absolute():
            caption_path = base / caption_path
        staged_captions = work / f"captions{caption_path.suffix.lower()}"
        _write_captions(named, staged_captions)

    requested_out = out
    staged_out = work / f"delivery{out.suffix.lower()}"
    mp4_out = (
        work / "finished.mp4" if out.suffix.lower() == ".gif" or cfg.get("webcam") else staged_out
    )
    has_audio = _mux(silent, named, audio, mp4_out, [*media_audio, *cfg.get("audio", [])], base)
    if cfg.get("webcam"):
        webcam_out = work / "webcam.mp4" if out.suffix.lower() == ".gif" else staged_out
        _apply_webcam(mp4_out, cfg["webcam"], base, webcam_out, video)
        mp4_out = webcam_out
    if cfg.get("burn_captions"):
        assert staged_captions is not None
        captioned = work / "captioned.mp4"
        _burn_captions(mp4_out, staged_captions, captioned, video, work)
        mp4_out = captioned
    if cfg.get("watermark"):
        watermarked = work / "watermarked.mp4"
        _apply_watermark(mp4_out, cfg["watermark"], watermarked, video, work)
        mp4_out = watermarked
    if out.suffix.lower() == ".gif":
        _to_gif(mp4_out, staged_out, video["fps"])
    elif mp4_out != staged_out:
        shutil.copy(mp4_out, staged_out)
    os.replace(staged_out, out)
    os.replace(staged_script, script)
    if staged_captions is not None:
        assert caption_path is not None
        caption_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_captions, caption_path)
    shutil.rmtree(work, ignore_errors=True)
    return {
        "output": str(requested_out),
        "duration": _dur(out),
        "size": out.stat().st_size,
        "audio": has_audio and out.suffix.lower() != ".gif",
        "captions": str(caption_path) if caption_path else None,
        "w": w,
        "h": h,
    }


# marks-returning driver wrappers (Playwright driver must return the Marks obj)
def _drv_doc(page, body, audio, base, work, theme=None):
    m = Marks(audio)
    _drive_doc(page, body, m, base, work, theme)
    return m


def _drv_app(page, body, audio, base, theme=None, log_path=None):
    m = Marks(audio)
    _drive_app(page, body, m, base, theme, log_path)
    return m


class _DryMarks(Marks):
    def say(self, page, text: str, extra: float = 0.0) -> None:
        self.items.append((0.0, text))


def dry_run(cfg: dict) -> dict:
    """Execute commands and browser actions without recording or synthesising speech."""
    from playwright.sync_api import sync_playwright

    base = cfg["_base"].resolve()
    checked = []
    browser_segments = any(next(iter(seg)) == "app" for seg in cfg["segments"])
    pw = sync_playwright().start() if browser_segments else None
    try:
        for i, segment in enumerate(cfg["segments"]):
            kind, body = next(iter(segment.items()))
            try:
                if kind == "terminal":
                    cwd = base / body.get("cwd", ".")
                    for step in body["steps"]:
                        _run(step["run"], cwd, step.get("env"), step.get("allow_failure", False))
                elif kind == "doc":
                    html = base / f".repocast-dry-{i}.html"
                    try:
                        ids = _render_doc_html(base / body["file"], html)
                        missing = [
                            s["anchor"] for s in body.get("steps", []) if s["anchor"] not in ids
                        ]
                        if missing:
                            raise RuntimeError(f"missing anchors: {missing}")
                    finally:
                        html.unlink(missing_ok=True)
                elif kind == "app":
                    assert pw is not None
                    browser = pw.chromium.launch(channel="chrome")
                    page = browser.new_page()
                    try:
                        with tempfile.TemporaryDirectory(prefix="repocast-dry-") as temporary:
                            _drive_app(
                                page,
                                body,
                                _DryMarks({}),
                                base,
                                cfg.get("theme"),
                                Path(temporary) / "app.log",
                            )
                    finally:
                        browser.close()
                elif kind == "media":
                    source = (base / body["file"]).resolve()
                    probe = subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-i",
                            str(source),
                            "-frames:v",
                            "1",
                            "-f",
                            "null",
                            "-",
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if probe.returncode:
                        raise RuntimeError(f"unreadable media: {source}: {probe.stderr.strip()}")
            except Exception as exc:
                raise RuntimeError(f"segment {i} ({kind}): {exc}") from exc
            checked.append({"segment": i, "type": kind, "status": "ok"})
    finally:
        if pw:
            pw.stop()
    return {"ok": True, "segments": checked}
