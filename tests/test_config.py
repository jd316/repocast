"""Config validation — the user-facing contract. No browser/network needed."""

import textwrap
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from repocast import config


def _write(tmp_path, body):
    p = tmp_path / "w.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_minimal_terminal_config_loads(tmp_path):
    cfg = config.load(
        _write(
            tmp_path,
            """
        output: out.mp4
        tts: {provider: none}
        segments:
          - terminal:
              steps:
                - {run: ["echo", "hi"], say: "hello"}
    """,
        )
    )
    assert cfg["size"] == [1920, 1080]  # default filled in
    assert cfg["tts"]["provider"] == "none"
    assert config.all_narration(cfg) == ["hello"]


def test_published_schema_is_valid_and_accepts_examples():
    Draft202012Validator.check_schema(config.SCHEMA)
    validator = Draft202012Validator(config.SCHEMA)
    root = Path(__file__).parents[1]
    for name in ("selfdemo.yaml", "walkthrough.yaml"):
        validator.validate(yaml.safe_load((root / "examples" / name).read_text()))


def test_rejects_empty_segments(tmp_path):
    with pytest.raises(config.ConfigError, match="non-empty list"):
        config.load(_write(tmp_path, "segments: []\n"))


def test_rejects_unknown_segment_type(tmp_path):
    with pytest.raises(config.ConfigError, match="unknown type"):
        config.load(
            _write(
                tmp_path,
                """
            segments:
              - bogus: {}
        """,
            )
        )


def test_rejects_unknown_app_verb(tmp_path):
    with pytest.raises(config.ConfigError, match="exactly one verb"):
        config.load(
            _write(
                tmp_path,
                """
            tts: {provider: none}
            segments:
              - app:
                  url: http://x
                  actions:
                    - {frobnicate: "#x"}
        """,
            )
        )


def test_terminal_run_must_be_list(tmp_path):
    with pytest.raises(config.ConfigError, match="list of argv"):
        config.load(
            _write(
                tmp_path,
                """
            tts: {provider: none}
            segments:
              - terminal:
                  steps:
                    - {run: "echo hi"}
        """,
            )
        )


def test_bad_tts_provider(tmp_path):
    with pytest.raises(config.ConfigError, match="tts.provider"):
        config.load(
            _write(
                tmp_path,
                """
            tts: {provider: elevenlabs}
            segments:
              - terminal: {steps: [{run: ["x"]}]}
        """,
            )
        )


def test_caption_and_quality_contract(tmp_path):
    with pytest.raises(config.ConfigError, match="burn_captions requires"):
        config.load(
            _write(
                tmp_path, "burn_captions: true\nsegments:\n  - terminal: {steps: [{run: [x]}]}\n"
            )
        )

    with pytest.raises(config.ConfigError, match="max_duration"):
        config.load(
            _write(
                tmp_path,
                "quality: {min_duration: 10, max_duration: 5}\n"
                "segments:\n  - terminal: {steps: [{run: [x]}]}\n",
            )
        )


def test_missing_doc_file_is_caught_only_with_check_paths(tmp_path):
    src = """
        segments:
          - doc:
              file: does_not_exist.md
              steps: [{anchor: intro, say: "x"}]
    """
    with pytest.raises(config.ConfigError, match="file not found"):
        config.load(_write(tmp_path, src), check_paths=True)
    # template mode: structure is fine, missing file tolerated
    cfg = config.load(_write(tmp_path, src), check_paths=False)
    assert config.all_narration(cfg) == ["x"]


def test_all_narration_across_segment_types(tmp_path):
    cfg = config.load(
        _write(
            tmp_path,
            """
        tts: {provider: none}
        segments:
          - terminal: {steps: [{run: ["a"], say: "one"}, {run: ["b"]}]}
          - app:
              url: http://x
              actions: [{say: "two"}, {click: "#c"}, {say: "three"}]
    """,
        )
    )
    assert config.all_narration(cfg) == ["one", "two", "three"]


def test_presets_media_and_output_options(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"png")
    (tmp_path / "music.wav").write_bytes(b"wav")
    cfg = config.load(
        _write(
            tmp_path,
            """
        size: portrait
        captions: demo.vtt
        video: {crf: 18, fps: 24, preset: medium}
        audio: [{file: music.wav, at: 1.5, volume: 0.4}]
        segments:
          - media: {file: shot.png, duration: 3, say: "A result"}
    """,
        )
    )
    assert cfg["size"] == [1080, 1920]
    assert cfg["video"] == {"crf": 18, "fps": 24, "preset": "medium"}
    assert config.all_narration(cfg) == ["A result"]


def test_app_action_must_have_one_verb(tmp_path):
    with pytest.raises(config.ConfigError, match=r"segment 0 .* action 0"):
        config.load(
            _write(
                tmp_path,
                """
            segments:
              - app:
                  url: http://x
                  actions: [{click: "#x", say: "ambiguous"}]
        """,
            )
        )


@pytest.mark.parametrize("value", ["unknown", [0, 0], [1920, -1]])
def test_rejects_bad_size(tmp_path, value):
    import yaml

    path = _write(
        tmp_path,
        yaml.safe_dump(
            {
                "size": value,
                "segments": [{"terminal": {"steps": [{"run": ["echo"]}]}}],
            }
        ),
    )
    with pytest.raises(config.ConfigError, match="size"):
        config.load(path)
