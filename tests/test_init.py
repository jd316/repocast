"""The `init` template must itself be a valid config (it broke once: str.format
choked on the literal `{anchor: ...}` YAML braces)."""

from repocast import config
from repocast.__main__ import _TEMPLATE


def test_init_template_is_valid_yaml_config(tmp_path):
    p = tmp_path / "walkthrough.yaml"
    p.write_text(_TEMPLATE)
    cfg = config.load(p, check_paths=False)  # placeholder paths -> skip existence
    kinds = [next(iter(s)) for s in cfg["segments"]]
    assert kinds == ["doc", "terminal", "app"]  # scaffolds all three segment types
    assert config.all_narration(cfg)  # has narration
