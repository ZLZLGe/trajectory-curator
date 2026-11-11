from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from trajectory_classifier.jobs import run_local_label_job
from trajectory_classifier.storage import DirectoryClient, DirectoryLocation, S3Location

from .test_s3_jobs import _install_v4_input, _MemoryS3


@pytest.fixture
def normalized(tmp_path):
    memory = _MemoryS3()
    location = S3Location("test-bucket", "normalized/day/")
    _install_v4_input(memory, location, without_toolcalls=("later.json",))
    root = tmp_path / "normalized"
    root.mkdir()
    for (bucket, key), payload in memory.objects.items():
        assert bucket == location.bucket
        path = root / key[len(location.prefix) :]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


@contextmanager
def model_service():
    calls = []
    status = {"value": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer synthetic-test-key"
            calls.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "scenario_labels": ["购物"],
                                        "capability_labels": ["工具操作与自动化"],
                                        "subject_labels": ["通用（非学科）"],
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {"total_tokens": 100},
                }
            ).encode()
            self.send_response(status["value"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            calls,
            status,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def snapshot(root):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def run_cli(source, output, endpoint, state):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "trajectory_classifier",
            "local",
            "--input",
            str(source),
            "--output",
            str(output),
            "--api-url",
            endpoint,
            "--state",
            str(state),
            "--workers",
            "2",
            "--max-retries",
            "0",
            "--requests-per-minute",
            "6000",
            "--tokens-per-minute",
            "4500000",
            "--workspace",
            str(source.parent),
        ],
        cwd=source.parent,
        env={
            **os.environ,
            "CLASSIFIER_API_KEY": "synthetic-test-key",
            "CLASSIFIER_RATE_LIMIT_STATE": "",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_local_cli_publishes_valid_dataset_and_reuses_successful_cache(
    normalized, tmp_path
):
    output = tmp_path / "classified"
    state = tmp_path / "labels.sqlite"
    original = snapshot(normalized)
    with model_service() as (url, calls, _):
        first = run_cli(normalized, output, url, state)
        assert first.returncode == 0, first.stderr
        result = json.loads(first.stdout)
        assert result == {
            "input_trajectories": 3,
            "classified": 2,
            "failed": 0,
            "cache_hits": 0,
            "skipped_no_toolcall": 1,
            "validation_valid": True,
        }
        assert len(calls) == 2
        second = run_cli(normalized, output, url, state)
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout)["cache_hits"] == 2
        assert len(calls) == 2
    assert snapshot(normalized) == original
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["input_root"] == normalized.as_uri() + "/"
    assert manifest["counts"]["input_trajectories"] == 3
    assert manifest["counts"]["cache_hits"] == 2
    for entry in manifest["files"]:
        raw = (output / entry["path"]).read_bytes()
        assert len(raw) == entry["bytes"]
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        if entry["kind"] == "trajectory":
            row = json.loads(raw)
            original_row = json.loads(original[entry["path"]])
            row.pop("classification")
            assert row == original_row


def test_local_configuration_failure_preserves_published_output(normalized, tmp_path):
    output = tmp_path / "classified"
    with model_service() as (url, _, status):
        first = run_cli(normalized, output, url, tmp_path / "first.sqlite")
        assert first.returncode == 0, first.stderr
        before = snapshot(output)
        status["value"] = 404
        second = run_cli(normalized, output, url, tmp_path / "second.sqlite")
        assert second.returncode == 1
        assert "HTTP 404" in second.stderr
        assert "synthetic-test-key" not in second.stdout + second.stderr
    assert snapshot(output) == before


def test_local_bad_input_is_rejected_before_model_calls(normalized, tmp_path):
    (normalized / "lineage.jsonl").write_bytes(b"corrupted\n")
    with model_service() as (url, calls, _):
        result = run_cli(
            normalized, tmp_path / "classified", url, tmp_path / "labels.sqlite"
        )
        assert result.returncode == 1
        assert "normalized input failed validation" in result.stderr
        assert calls == []
    assert not (tmp_path / "classified/manifest.json").exists()


@pytest.mark.parametrize("relative", [".", "child", ".."])
def test_local_rejects_overlapping_directories_before_requests(normalized, relative):
    with pytest.raises(ValueError, match="must not overlap"):
        run_local_label_job(normalized, normalized / relative)


def test_directory_storage_rejects_input_writes_and_symlink_escape(
    normalized, tmp_path
):
    output = tmp_path / "classified"
    output.mkdir()
    client = DirectoryClient(
        DirectoryLocation(normalized, "in"), DirectoryLocation(output, "out")
    )
    with pytest.raises(ValueError, match="read-only"):
        client.put_object(Bucket="in", Key="dataset/manifest.json", Body=b"bad")
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"keep")
    (output / "escape.jsonl").symlink_to(outside)
    with pytest.raises(ValueError, match="outside its directory"):
        client.put_object(Bucket="out", Key="dataset/escape.jsonl", Body=b"bad")
    assert outside.read_bytes() == b"keep"
