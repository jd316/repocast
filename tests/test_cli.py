"""Agent-facing CLI contracts: stable JSON and executable dry runs."""

import json

from repocast.__main__ import _scratch_is_owned, main


def _config(tmp_path, command="true"):
    path = tmp_path / "demo.yaml"
    path.write_text(
        f'tts: {{provider: none}}\nsegments:\n  - terminal: {{steps: [{{run: ["{command}"]}}]}}\n'
    )
    return path


def test_validate_and_dry_run_emit_json(tmp_path, capsys):
    path = _config(tmp_path)
    assert main(["validate", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert main(["dry-run", str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True, "segments": [{"segment": 0, "type": "terminal", "status": "ok"}]}


def test_json_error_has_nonzero_status(tmp_path, capsys):
    path = _config(tmp_path, "false")
    assert main(["dry-run", str(path), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert "command failed" in result["error"]


def test_schema_is_json(capsys):
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["$schema"].endswith("2020-12/schema")
    assert "segments" in schema["properties"]


def test_failed_render_preserves_owned_scratch(tmp_path, capsys):
    path = _config(tmp_path, "false")
    work = tmp_path / "scratch"
    assert main(["render", str(path), "--no-voice", "--json", "--workdir", str(work)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert _scratch_is_owned(work)


def test_scratch_marker_does_not_authorize_deleting_user_files(tmp_path):
    work = tmp_path / "scratch"
    work.mkdir()
    (work / ".repocast-work").write_text("repocast scratch v1\n")
    (work / "user.txt").write_text("keep")
    assert _scratch_is_owned(work) is False
