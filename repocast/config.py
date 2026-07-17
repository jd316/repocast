"""Load and validate a walkthrough.yaml config.

Kept intentionally small: YAML in, a validated dict out, with clear errors so a
typo names the offending key instead of blowing up deep inside Playwright.
"""
from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError as e:                              # pragma: no cover
    raise SystemExit("PyYAML is required: `pip install pyyaml` "
                     "or run via `uv run --with pyyaml ...`") from e

_APP_ACTIONS = {"say", "click", "fill", "wait_for", "eval", "sleep", "goto", "press"}


class ConfigError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def load(path: str | Path, check_paths: bool = True) -> dict:
    """Parse + validate a config. `check_paths=False` skips filesystem existence
    checks, so a template with placeholder paths still validates structurally."""
    path = Path(path)
    _require(path.is_file(), f"config not found: {path}")
    cfg = yaml.safe_load(path.read_text()) or {}
    _require(isinstance(cfg, dict), "top level of the config must be a mapping")

    cfg.setdefault("output", "walkthrough.mp4")
    cfg.setdefault("size", [1920, 1080])
    cfg.setdefault("tts", {})
    _require(isinstance(cfg["size"], list) and len(cfg["size"]) == 2,
             "`size` must be [width, height]")
    cfg["tts"].setdefault("provider", "gemini")
    _require(cfg["tts"]["provider"] in ("gemini", "none"),
             "tts.provider must be 'gemini' or 'none'")

    segs = cfg.get("segments")
    _require(isinstance(segs, list) and segs, "`segments` must be a non-empty list")
    base = path.parent
    for i, seg in enumerate(segs):
        _require(isinstance(seg, dict) and len(seg) == 1,
                 f"segment {i} must be a single-key mapping (doc/terminal/app)")
        kind, body = next(iter(seg.items()))
        _require(kind in ("doc", "terminal", "app"),
                 f"segment {i}: unknown type {kind!r} (doc|terminal|app)")
        _validate_segment(kind, body, i, base, check_paths)
    # resolve project-relative paths once
    cfg["_base"] = base
    return cfg


def _validate_segment(kind: str, body: dict, i: int, base: Path, check_paths: bool) -> None:
    if kind == "doc":
        _require("file" in body, f"segment {i} (doc): needs `file`")
        _require(not check_paths or (base / body["file"]).is_file(),
                 f"segment {i} (doc): file not found: {body['file']}")
        for s in body.get("steps", []):
            _require("anchor" in s, f"segment {i} (doc): each step needs an `anchor`")
    elif kind == "terminal":
        _require(isinstance(body.get("steps"), list) and body["steps"],
                 f"segment {i} (terminal): needs a non-empty `steps` list")
        for s in body["steps"]:
            _require("run" in s and isinstance(s["run"], list),
                     f"segment {i} (terminal): each step needs `run` as a list of argv")
    elif kind == "app":
        _require("url" in body, f"segment {i} (app): needs `url`")
        _require(isinstance(body.get("actions"), list) and body["actions"],
                 f"segment {i} (app): needs a non-empty `actions` list")
        for a in body["actions"]:
            _require(isinstance(a, dict) and len(a) >= 1,
                     f"segment {i} (app): each action must be a mapping")
            verb = next(k for k in a if k in _APP_ACTIONS) if any(
                k in _APP_ACTIONS for k in a) else None
            _require(verb is not None,
                     f"segment {i} (app): action {a} has no known verb "
                     f"({', '.join(sorted(_APP_ACTIONS))})")


def all_narration(cfg: dict) -> list[str]:
    """Every spoken line, in order — for TTS pre-flight."""
    out: list[str] = []
    for seg in cfg["segments"]:
        kind, body = next(iter(seg.items()))
        if kind == "doc":
            out += [s["say"] for s in body.get("steps", []) if s.get("say")]
        elif kind == "terminal":
            out += [s["say"] for s in body["steps"] if s.get("say")]
        elif kind == "app":
            out += [a["say"] for a in body["actions"] if a.get("say")]
    return out
