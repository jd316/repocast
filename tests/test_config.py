"""Config validation — the user-facing contract. No browser/network needed."""
import textwrap

import pytest

from repocast import config


def _write(tmp_path, body):
    p = tmp_path / "w.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_minimal_terminal_config_loads(tmp_path):
    cfg = config.load(_write(tmp_path, """
        output: out.mp4
        tts: {provider: none}
        segments:
          - terminal:
              steps:
                - {run: ["echo", "hi"], say: "hello"}
    """))
    assert cfg["size"] == [1920, 1080]              # default filled in
    assert cfg["tts"]["provider"] == "none"
    assert config.all_narration(cfg) == ["hello"]


def test_rejects_empty_segments(tmp_path):
    with pytest.raises(config.ConfigError, match="non-empty list"):
        config.load(_write(tmp_path, "segments: []\n"))


def test_rejects_unknown_segment_type(tmp_path):
    with pytest.raises(config.ConfigError, match="unknown type"):
        config.load(_write(tmp_path, """
            segments:
              - bogus: {}
        """))


def test_rejects_unknown_app_verb(tmp_path):
    with pytest.raises(config.ConfigError, match="no known verb"):
        config.load(_write(tmp_path, """
            tts: {provider: none}
            segments:
              - app:
                  url: http://x
                  actions:
                    - {frobnicate: "#x"}
        """))


def test_terminal_run_must_be_list(tmp_path):
    with pytest.raises(config.ConfigError, match="list of argv"):
        config.load(_write(tmp_path, """
            tts: {provider: none}
            segments:
              - terminal:
                  steps:
                    - {run: "echo hi"}
        """))


def test_bad_tts_provider(tmp_path):
    with pytest.raises(config.ConfigError, match="tts.provider"):
        config.load(_write(tmp_path, """
            tts: {provider: elevenlabs}
            segments:
              - terminal: {steps: [{run: ["x"]}]}
        """))


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
    cfg = config.load(_write(tmp_path, """
        tts: {provider: none}
        segments:
          - terminal: {steps: [{run: ["a"], say: "one"}, {run: ["b"]}]}
          - app:
              url: http://x
              actions: [{say: "two"}, {click: "#c"}, {say: "three"}]
    """))
    assert config.all_narration(cfg) == ["one", "two", "three"]
