"""Load and validate a walkthrough.yaml config.

Kept intentionally small: YAML in, a validated dict out, with clear errors so a
typo names the offending key instead of blowing up deep inside Playwright.
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required: `pip install pyyaml` or run via `uv run --with pyyaml ...`"
    ) from e

APP_ACTIONS = {
    "say",
    "click",
    "fill",
    "wait_for",
    "eval",
    "sleep",
    "goto",
    "press",
    "annotate",
    "check",
}
SIZE_PRESETS = {
    "youtube": [1920, 1080],
    "landscape": [1920, 1080],
    "portrait": [1080, 1920],
    "square": [1080, 1080],
}

_NUMBER = {"type": "number", "minimum": 0}
_STRING = {"type": "string", "minLength": 1}
_EDITS = {
    "trim": {"type": "array", "items": _NUMBER, "minItems": 2, "maxItems": 2},
    "fade": _NUMBER,
}
_ACTION_SCHEMAS = []
for _verb, _properties in {
    "say": {"say": _STRING, "extra": _NUMBER},
    "click": {
        "click": _STRING,
        "focus": {"type": "boolean"},
        "effect": {"const": "ripple"},
        "zoom": {"type": "number", "exclusiveMinimum": 0},
        "after": _NUMBER,
    },
    "fill": {
        "fill": _STRING,
        "value": {},
        "focus": {"type": "boolean"},
        "zoom": {"type": "number", "exclusiveMinimum": 0},
        "after": _NUMBER,
    },
    "wait_for": {"wait_for": _STRING, "timeout": {"type": "number", "exclusiveMinimum": 0}},
    "press": {"press": _STRING},
    "goto": {"goto": _STRING},
    "eval": {"eval": _STRING},
    "sleep": {"sleep": _NUMBER},
    "annotate": {
        "annotate": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": _STRING,
                "image": _STRING,
                "shape": {"enum": ["rectangle", "circle"]},
                "target": _STRING,
                "x": _NUMBER,
                "y": _NUMBER,
                "width": _NUMBER,
                "height": _NUMBER,
                "color": _STRING,
                "duration": _NUMBER,
            },
            "anyOf": [{"required": ["text"]}, {"required": ["image"]}, {"required": ["shape"]}],
        },
        "after": _NUMBER,
    },
    "check": {
        "check": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "no_scroll": {"type": "boolean"},
                "min_font_size": _NUMBER,
                "required_text": {"type": "array", "items": _STRING},
                "forbidden_text": {"type": "array", "items": _STRING},
            },
            "minProperties": 1,
        }
    },
}.items():
    _required = [_verb, "value"] if _verb == "fill" else [_verb]
    _ACTION_SCHEMAS.append(
        {
            "type": "object",
            "required": _required,
            "additionalProperties": False,
            "properties": _properties,
        }
    )
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://github.com/jd316/repocast/walkthrough.schema.json",
    "title": "Repocast walkthrough",
    "type": "object",
    "required": ["segments"],
    "additionalProperties": False,
    "patternProperties": {"^_": {}},
    "properties": {
        "output": {"type": "string", "pattern": "\\.(mp4|gif)$", "default": "walkthrough.mp4"},
        "size": {
            "oneOf": [
                {"enum": sorted(SIZE_PRESETS)},
                {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 2,
                    "maxItems": 2,
                },
            ]
        },
        "theme": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "background": {"type": "string"},
                "padding": _NUMBER,
                "radius": _NUMBER,
                "shadow": {"type": "boolean"},
            },
        },
        "video": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "crf": {"type": "integer", "minimum": 0, "maximum": 51},
                "fps": {"type": "integer", "minimum": 1, "maximum": 60},
                "preset": {"type": "string"},
            },
        },
        "tts": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "provider": {"enum": ["gemini", "none"]},
                "voice": _STRING,
                "model": _STRING,
                "style": {"type": "string"},
            },
        },
        "captions": {
            "oneOf": [{"type": "boolean"}, {"type": "string", "pattern": "\\.(srt|vtt)$"}]
        },
        "burn_captions": {"type": "boolean"},
        "quality": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "min_duration": _NUMBER,
                "max_duration": _NUMBER,
                "max_silence": _NUMBER,
                "max_black": _NUMBER,
                "loudness_target": {"type": "number", "minimum": -70, "maximum": 0},
                "loudness_tolerance": _NUMBER,
            },
        },
        "audio": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file"],
                "additionalProperties": False,
                "properties": {
                    "file": _STRING,
                    "at": _NUMBER,
                    "volume": _NUMBER,
                },
            },
        },
        "webcam": {
            "type": "object",
            "required": ["file"],
            "additionalProperties": False,
            "properties": {
                "file": _STRING,
                "position": {"enum": ["top-left", "top-right", "bottom-left", "bottom-right"]},
                "width": {"type": "integer", "minimum": 1},
                "margin": {"type": "integer", "minimum": 0},
            },
        },
        "watermark": {
            "type": "object",
            "required": ["text"],
            "additionalProperties": False,
            "properties": {
                "text": _STRING,
                "position": {"enum": ["top-left", "top-right", "bottom-left", "bottom-right"]},
                "size": {"type": "integer", "minimum": 1},
                "color": _STRING,
                "background": _STRING,
                "margin": {"type": "integer", "minimum": 0},
            },
        },
        "segments": {
            "type": "array",
            "minItems": 1,
            "items": {
                "oneOf": [
                    {"$ref": "#/$defs/docSegment"},
                    {"$ref": "#/$defs/terminalSegment"},
                    {"$ref": "#/$defs/appSegment"},
                    {"$ref": "#/$defs/mediaSegment"},
                ]
            },
        },
    },
    "$defs": {
        "edits": {"type": "object", "properties": _EDITS},
        "docSegment": {
            "type": "object",
            "required": ["doc"],
            "additionalProperties": False,
            "properties": {
                "doc": {
                    "type": "object",
                    "required": ["file", "steps"],
                    "additionalProperties": False,
                    "properties": {
                        "name": _STRING,
                        "file": _STRING,
                        **_EDITS,
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["anchor"],
                                "additionalProperties": False,
                                "properties": {"anchor": _STRING, "intro": _STRING, "say": _STRING},
                            },
                        },
                    },
                }
            },
        },
        "terminalSegment": {
            "type": "object",
            "required": ["terminal"],
            "additionalProperties": False,
            "properties": {
                "terminal": {
                    "type": "object",
                    "required": ["steps"],
                    "additionalProperties": False,
                    "properties": {
                        "name": _STRING,
                        "cwd": _STRING,
                        "title": _STRING,
                        "pace": {"type": "number", "exclusiveMinimum": 0},
                        **_EDITS,
                        "steps": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["run"],
                                "additionalProperties": False,
                                "properties": {
                                    "run": {"type": "array", "items": _STRING, "minItems": 1},
                                    "say": _STRING,
                                    "env": {"type": "object"},
                                    "allow_failure": {"type": "boolean"},
                                },
                            },
                        },
                    },
                }
            },
        },
        "appSegment": {
            "type": "object",
            "required": ["app"],
            "additionalProperties": False,
            "properties": {
                "app": {
                    "type": "object",
                    "required": ["url", "actions"],
                    "additionalProperties": False,
                    "properties": {
                        "name": _STRING,
                        "cwd": _STRING,
                        "start": {"type": "array", "items": _STRING, "minItems": 1},
                        "env": {"type": "object"},
                        "url": _STRING,
                        "ready_path": _STRING,
                        "zoom": {"type": "number", "exclusiveMinimum": 0},
                        **_EDITS,
                        "cursor": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "size": {"type": "integer", "minimum": 1},
                                "color": _STRING,
                                "smoothing": _NUMBER,
                                "blur": _NUMBER,
                            },
                        },
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"oneOf": _ACTION_SCHEMAS},
                        },
                    },
                }
            },
        },
        "mediaSegment": {
            "type": "object",
            "required": ["media"],
            "additionalProperties": False,
            "properties": {
                "media": {
                    "type": "object",
                    "required": ["file"],
                    "additionalProperties": False,
                    "properties": {
                        "name": _STRING,
                        "file": _STRING,
                        "duration": {"type": "number", "exclusiveMinimum": 0},
                        "speed": {"type": "number", "exclusiveMinimum": 0},
                        "volume": _NUMBER,
                        "say": _STRING,
                        **_EDITS,
                    },
                }
            },
        },
    },
}


class ConfigError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


def _only_keys(value: dict, allowed: set[str], where: str, allow_private: bool = False) -> None:
    unknown = {
        key for key in set(value) - allowed if not allow_private or not str(key).startswith("_")
    }
    _require(not unknown, f"{where}: unknown key(s): {', '.join(sorted(map(str, unknown)))}")


def load(path: str | Path, check_paths: bool = True) -> dict:
    """Parse + validate a config. `check_paths=False` skips filesystem existence
    checks, so a template with placeholder paths still validates structurally."""
    path = Path(path)
    _require(path.is_file(), f"config not found: {path}")
    cfg = yaml.safe_load(path.read_text()) or {}
    _require(isinstance(cfg, dict), "top level of the config must be a mapping")
    _only_keys(
        cfg,
        {
            "output",
            "size",
            "tts",
            "theme",
            "video",
            "captions",
            "burn_captions",
            "quality",
            "audio",
            "webcam",
            "watermark",
            "segments",
        },
        "config",
        allow_private=True,
    )

    cfg.setdefault("output", "walkthrough.mp4")
    _require(
        isinstance(cfg["output"], str) and Path(cfg["output"]).suffix.lower() in (".mp4", ".gif"),
        "output must be an .mp4 or .gif path",
    )
    cfg.setdefault("size", [1920, 1080])
    if isinstance(cfg["size"], str):
        _require(
            cfg["size"] in SIZE_PRESETS,
            f"unknown size preset {cfg['size']!r} ({'|'.join(SIZE_PRESETS)})",
        )
        cfg["size"] = SIZE_PRESETS[cfg["size"]].copy()
    cfg.setdefault("tts", {})
    _require(isinstance(cfg["tts"], dict), "`tts` must be a mapping")
    _require(
        isinstance(cfg["size"], list)
        and len(cfg["size"]) == 2
        and all(isinstance(n, int) and n > 0 for n in cfg["size"]),
        "`size` must be a preset or [positive width, positive height]",
    )
    cfg.setdefault("theme", {})
    _require(isinstance(cfg["theme"], dict), "`theme` must be a mapping")
    _only_keys(cfg["theme"], {"background", "padding", "radius", "shadow"}, "theme")
    _require(
        "background" not in cfg["theme"] or isinstance(cfg["theme"]["background"], str),
        "theme.background must be a CSS color string",
    )
    for key in ("padding", "radius"):
        _require(
            key not in cfg["theme"]
            or isinstance(cfg["theme"][key], int)
            and cfg["theme"][key] >= 0,
            f"theme.{key} must be a non-negative integer",
        )
    _require(
        "shadow" not in cfg["theme"] or isinstance(cfg["theme"]["shadow"], bool),
        "theme.shadow must be true or false",
    )
    cfg.setdefault("video", {})
    _require(isinstance(cfg["video"], dict), "`video` must be a mapping")
    _only_keys(cfg["video"], {"crf", "fps", "preset"}, "video")
    cfg["video"].setdefault("crf", 21)
    cfg["video"].setdefault("fps", 30)
    cfg["video"].setdefault("preset", "slow")
    _require(
        isinstance(cfg["video"]["crf"], int) and 0 <= cfg["video"]["crf"] <= 51,
        "video.crf must be an integer from 0 to 51",
    )
    _require(
        isinstance(cfg["video"]["fps"], int) and 1 <= cfg["video"]["fps"] <= 60,
        "video.fps must be an integer from 1 to 60",
    )
    _require(
        isinstance(cfg["video"]["preset"], str) and cfg["video"]["preset"],
        "video.preset must be a string",
    )
    cfg["tts"].setdefault("provider", "gemini")
    _only_keys(cfg["tts"], {"provider", "voice", "model", "style"}, "tts")
    _require(
        cfg["tts"]["provider"] in ("gemini", "none"), "tts.provider must be 'gemini' or 'none'"
    )
    _validate_outputs(cfg, path.parent, check_paths)

    segs = cfg.get("segments")
    _require(bool(isinstance(segs, list) and segs), "`segments` must be a non-empty list")
    assert isinstance(segs, list)
    base = path.parent
    for i, seg in enumerate(segs):
        _require(
            isinstance(seg, dict) and len(seg) == 1,
            f"segment {i} must be a single-key mapping (doc/terminal/app/media)",
        )
        kind, body = next(iter(seg.items()))
        _require(
            kind in ("doc", "terminal", "app", "media"),
            f"segment {i}: unknown type {kind!r} (doc|terminal|app|media)",
        )
        _require(isinstance(body, dict), f"segment {i} ({kind}): body must be a mapping")
        _validate_segment(kind, body, i, base, check_paths)
    # resolve project-relative paths once
    cfg["_base"] = base
    return cfg


def _validate_segment(kind: str, body: dict, i: int, base: Path, check_paths: bool) -> None:
    allowed = {
        "doc": {"name", "file", "steps", "trim", "fade"},
        "terminal": {"name", "cwd", "title", "steps", "pace", "trim", "fade"},
        "app": {
            "name",
            "cwd",
            "start",
            "env",
            "url",
            "ready_path",
            "zoom",
            "cursor",
            "actions",
            "trim",
            "fade",
        },
        "media": {"name", "file", "duration", "speed", "volume", "say", "trim", "fade"},
    }
    _only_keys(body, allowed[kind], f"segment {i} ({kind})")
    _validate_edits(body, i, kind)
    if kind == "doc":
        _require(isinstance(body.get("file"), str), f"segment {i} (doc): needs string `file`")
        _require(
            not check_paths or (base / body["file"]).is_file(),
            f"segment {i} (doc): file not found: {body['file']}",
        )
        _require(
            isinstance(body.get("steps"), list) and body["steps"],
            f"segment {i} (doc): needs a non-empty `steps` list",
        )
        for j, s in enumerate(body["steps"]):
            _require(isinstance(s, dict), f"segment {i} (doc) action {j}: must be a mapping")
            _only_keys(s, {"anchor", "intro", "say"}, f"segment {i} (doc) action {j}")
            _require("anchor" in s, f"segment {i} (doc) action {j}: needs an `anchor`")
    elif kind == "terminal":
        _require(
            isinstance(body.get("steps"), list) and body["steps"],
            f"segment {i} (terminal): needs a non-empty `steps` list",
        )
        for j, s in enumerate(body["steps"]):
            _require(isinstance(s, dict), f"segment {i} (terminal) action {j}: must be a mapping")
            _only_keys(
                s, {"run", "say", "env", "allow_failure"}, f"segment {i} (terminal) action {j}"
            )
            _require(
                "run" in s and isinstance(s["run"], list),
                f"segment {i} (terminal) action {j}: needs `run` as a list of argv",
            )
            _require(
                s["run"] and all(isinstance(x, str) for x in s["run"]),
                f"segment {i} (terminal) action {j}: `run` must contain string argv",
            )
            _require(
                "allow_failure" not in s or isinstance(s["allow_failure"], bool),
                f"segment {i} (terminal) action {j}: allow_failure must be boolean",
            )
            _require(
                "env" not in s or isinstance(s["env"], dict),
                f"segment {i} (terminal) action {j}: env must be a mapping",
            )
    elif kind == "app":
        _require(isinstance(body.get("url"), str), f"segment {i} (app): needs string `url`")
        _require(
            isinstance(body.get("actions"), list) and body["actions"],
            f"segment {i} (app): needs a non-empty `actions` list",
        )
        cursor = body.get("cursor", {})
        _require(isinstance(cursor, dict), f"segment {i} (app): cursor must be a mapping")
        _only_keys(cursor, {"size", "color", "smoothing", "blur"}, f"segment {i} (app) cursor")
        _require(
            isinstance(cursor.get("size", 18), int) and cursor.get("size", 18) > 0,
            f"segment {i} (app): cursor.size must be positive",
        )
        _require(
            isinstance(cursor.get("smoothing", 0.28), (int, float))
            and cursor.get("smoothing", 0.28) >= 0,
            f"segment {i} (app): cursor.smoothing must be non-negative",
        )
        _require(
            isinstance(cursor.get("blur", 0), (int, float)) and cursor.get("blur", 0) >= 0,
            f"segment {i} (app): cursor.blur must be non-negative",
        )
        _require(
            bool(
                "start" not in body
                or isinstance(body["start"], list)
                and body["start"]
                and all(isinstance(v, str) for v in body["start"])
            ),
            f"segment {i} (app): start must be a non-empty argv list",
        )
        _require(
            "env" not in body or isinstance(body["env"], dict),
            f"segment {i} (app): env must be a mapping",
        )
        _require(
            "zoom" not in body or isinstance(body["zoom"], (int, float)) and body["zoom"] > 0,
            f"segment {i} (app): zoom must be positive",
        )
        for j, a in enumerate(body["actions"]):
            _require(
                isinstance(a, dict) and len(a) >= 1,
                f"segment {i} (app) action {j}: must be a mapping",
            )
            verbs = APP_ACTIONS.intersection(a)
            _require(
                len(verbs) == 1,
                f"segment {i} (app) action {j}: needs exactly one verb "
                f"({', '.join(sorted(APP_ACTIONS))})",
            )
            verb = next(iter(verbs))
            modifiers = {
                "say": {"say", "extra"},
                "click": {"click", "focus", "effect", "zoom", "after"},
                "fill": {"fill", "value", "focus", "zoom", "after"},
                "wait_for": {"wait_for", "timeout"},
                "press": {"press"},
                "goto": {"goto"},
                "eval": {"eval"},
                "sleep": {"sleep"},
                "annotate": {"annotate", "after"},
                "check": {"check"},
            }
            _only_keys(a, modifiers[verb], f"segment {i} (app) action {j}")
            _require(
                "focus" not in a or isinstance(a["focus"], bool),
                f"segment {i} (app) action {j}: focus must be boolean",
            )
            _require(
                "zoom" not in a or isinstance(a["zoom"], (int, float)) and a["zoom"] > 0,
                f"segment {i} (app) action {j}: zoom must be positive",
            )
            _require(
                "after" not in a or isinstance(a["after"], (int, float)) and a["after"] >= 0,
                f"segment {i} (app) action {j}: after must be non-negative",
            )
            _require(
                verb != "click" or a.get("effect") in (None, "ripple"),
                f"segment {i} (app) action {j}: effect must be ripple",
            )
            if verb in {"say", "click", "fill", "wait_for", "press", "goto", "eval"}:
                _require(
                    isinstance(a[verb], str) and a[verb],
                    f"segment {i} (app) action {j}: {verb} must be a non-empty string",
                )
            if verb == "sleep":
                _require(
                    isinstance(a[verb], (int, float)) and a[verb] >= 0,
                    f"segment {i} (app) action {j}: sleep must be non-negative",
                )
            _require(
                "timeout" not in a or isinstance(a["timeout"], (int, float)) and a["timeout"] > 0,
                f"segment {i} (app) action {j}: timeout must be positive",
            )
            _require(
                verb != "fill" or "value" in a, f"segment {i} (app) action {j}: fill needs `value`"
            )
            if verb == "annotate":
                _require(
                    isinstance(a[verb], dict)
                    and any(a[verb].get(key) for key in ("text", "image", "shape")),
                    f"segment {i} (app) action {j}: annotate needs text, image, or shape",
                )
                image = a[verb].get("image")
                _only_keys(
                    a[verb],
                    {
                        "text",
                        "image",
                        "shape",
                        "target",
                        "x",
                        "y",
                        "width",
                        "height",
                        "color",
                        "duration",
                    },
                    f"segment {i} (app) action {j} annotation",
                )
                _require(
                    not image or not check_paths or (base / image).is_file(),
                    f"segment {i} (app) action {j}: annotation image not found: {image}",
                )
                _require(
                    a[verb].get("shape") in (None, "rectangle", "circle"),
                    f"segment {i} (app) action {j}: shape must be rectangle or circle",
                )
                _require(
                    "duration" not in a[verb]
                    or isinstance(a[verb]["duration"], (int, float))
                    and a[verb]["duration"] >= 0,
                    f"segment {i} (app) action {j}: duration must be non-negative",
                )
            if verb == "check":
                check = a[verb]
                _require(
                    bool(isinstance(check, dict) and check),
                    f"segment {i} (app) action {j}: check must be a non-empty mapping",
                )
                _only_keys(
                    check,
                    {"no_scroll", "min_font_size", "required_text", "forbidden_text"},
                    f"segment {i} (app) action {j} check",
                )
                _require(
                    "no_scroll" not in check or isinstance(check["no_scroll"], bool),
                    f"segment {i} (app) action {j}: no_scroll must be boolean",
                )
                _require(
                    "min_font_size" not in check
                    or isinstance(check["min_font_size"], (int, float))
                    and check["min_font_size"] > 0,
                    f"segment {i} (app) action {j}: min_font_size must be positive",
                )
                for key in ("required_text", "forbidden_text"):
                    _require(
                        key not in check
                        or isinstance(check[key], list)
                        and all(isinstance(text, str) and text for text in check[key]),
                        f"segment {i} (app) action {j}: {key} must be a string list",
                    )
    elif kind == "media":
        _require(isinstance(body.get("file"), str), f"segment {i} (media): needs string `file`")
        _require(
            not check_paths or (base / body["file"]).is_file(),
            f"segment {i} (media): file not found: {body['file']}",
        )
        _require(
            "duration" not in body
            or isinstance(body["duration"], (int, float))
            and body["duration"] > 0,
            f"segment {i} (media): duration must be positive",
        )
        _require(
            "speed" not in body or isinstance(body["speed"], (int, float)) and body["speed"] > 0,
            f"segment {i} (media): speed must be positive",
        )
        _require(
            "volume" not in body
            or isinstance(body["volume"], (int, float))
            and body["volume"] >= 0,
            f"segment {i} (media): volume must be non-negative",
        )
        image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ppm", ".svg"}
        _require(
            bool(
                Path(body["file"]).suffix.lower() not in image_suffixes
                or body.get("duration")
                or body.get("say")
            ),
            f"segment {i} (media): a still image needs duration or narration",
        )


def _validate_edits(body: dict, i: int, kind: str) -> None:
    trim = body.get("trim")
    if trim is not None:
        _require(
            isinstance(trim, list)
            and len(trim) == 2
            and all(isinstance(v, (int, float)) and v >= 0 for v in trim)
            and trim[1] > trim[0],
            f"segment {i} ({kind}): trim must be [start, end] with end > start",
        )
    fade = body.get("fade")
    _require(
        fade is None or isinstance(fade, (int, float)) and fade >= 0,
        f"segment {i} ({kind}): fade must be non-negative seconds",
    )


def _validate_outputs(cfg: dict, base: Path, check_paths: bool) -> None:
    captions = cfg.get("captions", False)
    _require(isinstance(captions, (bool, str)), "captions must be true, false, or a path")
    _require(
        not isinstance(captions, str) or Path(captions).suffix.lower() in (".srt", ".vtt"),
        "captions path must end in .srt or .vtt",
    )
    _require(isinstance(cfg.get("burn_captions", False), bool), "burn_captions must be boolean")
    _require(
        bool(not cfg.get("burn_captions") or captions),
        "burn_captions requires captions to be enabled",
    )
    quality = cfg.get("quality", {})
    _require(isinstance(quality, dict), "quality must be a mapping")
    _only_keys(
        quality,
        {
            "min_duration",
            "max_duration",
            "max_silence",
            "max_black",
            "loudness_target",
            "loudness_tolerance",
        },
        "quality",
    )
    for key, value in quality.items():
        if key == "loudness_target":
            _require(
                isinstance(value, (int, float)) and -70 <= value <= 0,
                "quality.loudness_target must be between -70 and 0 LUFS",
            )
            continue
        _require(
            isinstance(value, (int, float)) and value >= 0, f"quality.{key} must be non-negative"
        )
    _require(
        not {"min_duration", "max_duration"}.issubset(quality)
        or quality["max_duration"] >= quality["min_duration"],
        "quality.max_duration must be at least min_duration",
    )
    audio = cfg.get("audio", [])
    _require(isinstance(audio, list), "audio must be a list")
    for i, item in enumerate(audio):
        _require(isinstance(item, dict) and "file" in item, f"audio {i}: needs `file`")
        _only_keys(item, {"file", "at", "volume"}, f"audio {i}")
        _require(isinstance(item["file"], str), f"audio {i}: file must be a string")
        _require(
            not check_paths or (base / item["file"]).is_file(),
            f"audio {i}: file not found: {item['file']}",
        )
        _require(
            isinstance(item.get("at", 0), (int, float)) and item.get("at", 0) >= 0,
            f"audio {i}: at must be non-negative",
        )
        _require(
            isinstance(item.get("volume", 1), (int, float)) and item.get("volume", 1) >= 0,
            f"audio {i}: volume must be non-negative",
        )
    webcam = cfg.get("webcam")
    if webcam is not None:
        _require(isinstance(webcam, dict) and "file" in webcam, "webcam needs `file`")
        _only_keys(webcam, {"file", "position", "width", "margin"}, "webcam")
        _require(isinstance(webcam["file"], str), "webcam.file must be a string")
        _require(
            not check_paths or (base / webcam["file"]).is_file(),
            f"webcam file not found: {webcam['file']}",
        )
        _require(
            webcam.get("position", "bottom-right")
            in ("top-left", "top-right", "bottom-left", "bottom-right"),
            "webcam.position is invalid",
        )
        _require(
            isinstance(webcam.get("width", 320), int) and webcam.get("width", 320) > 0,
            "webcam.width must be a positive integer",
        )
        _require(
            isinstance(webcam.get("margin", 32), int) and webcam.get("margin", 32) >= 0,
            "webcam.margin must be a non-negative integer",
        )
    watermark = cfg.get("watermark")
    if watermark is not None:
        _require(
            isinstance(watermark, dict)
            and isinstance(watermark.get("text"), str)
            and watermark["text"],
            "watermark needs non-empty `text`",
        )
        _only_keys(
            watermark,
            {"text", "position", "size", "color", "background", "margin"},
            "watermark",
        )
        _require(
            watermark.get("position", "top-left")
            in ("top-left", "top-right", "bottom-left", "bottom-right"),
            "watermark.position is invalid",
        )
        _require(
            isinstance(watermark.get("size", 24), int) and watermark.get("size", 24) > 0,
            "watermark.size must be a positive integer",
        )
        _require(
            isinstance(watermark.get("margin", 24), int) and watermark.get("margin", 24) >= 0,
            "watermark.margin must be a non-negative integer",
        )


def all_narration(cfg: dict) -> list[str]:
    """Every spoken line, in order — for TTS pre-flight."""
    out: list[str] = []
    for seg in cfg["segments"]:
        kind, body = next(iter(seg.items()))
        if kind == "doc":
            if body["steps"][0].get("intro"):
                out.append(body["steps"][0]["intro"])
            out += [s["say"] for s in body["steps"] if s.get("say")]
        elif kind == "terminal":
            out += [s["say"] for s in body["steps"] if s.get("say")]
        elif kind == "app":
            out += [a["say"] for a in body["actions"] if a.get("say")]
        elif kind == "media" and body.get("say"):
            out.append(body["say"])
    return out
