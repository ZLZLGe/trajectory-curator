from __future__ import annotations

import json
import subprocess
import sys

import pytest

from trajectory_classifier.cli import main


def test_taxonomy_command_reports_all_three_catalogs(capsys):
    main(["taxonomy"])
    value = json.loads(capsys.readouterr().out)
    assert {k: len(v) for k, v in value.items()} == {
        "scenario_labels": 90,
        "capability_labels": 10,
        "subject_labels": 25,
    }


def test_cli_requires_endpoint_and_key_without_displaying_key(monkeypatch, capsys):
    monkeypatch.delenv("CLASSIFIER_API_URL", raising=False)
    monkeypatch.setenv("CLASSIFIER_API_KEY", "synthetic-cli-key")
    with pytest.raises(SystemExit) as error:
        main(["local", "--input", "/unused/input", "--output", "/unused/output"])
    assert error.value.code == 2
    result = capsys.readouterr()
    assert "--api-url" in result.err
    assert "synthetic-cli-key" not in result.err + result.out


def test_classifier_import_and_help_do_not_load_optional_dependencies():
    script = """
import sys
from importlib.abc import MetaPathFinder
class Block(MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'orjson', 'pydantic', 'boto3', 'trajfoundry'}:
            raise ImportError('optional dependency blocked')
sys.meta_path.insert(0, Block())
import trajectory_classifier
from trajectory_classifier.cli import main
main(['--help'])
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "local" in result.stdout and "s3" in result.stdout
