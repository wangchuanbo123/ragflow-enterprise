"""Repeat the real isolated 50-member/10-client acceptance in a fresh process."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_synthetic_uploads_have_distinct_content_as_well_as_distinct_names():
    """Identical bodies intentionally reuse jobs and cannot test five owners."""
    import hashlib
    from scripts.verify_team_server import synthetic_upload
    uploads=[synthetic_upload(index) for index in range(5)]
    assert len({name for name,_body,_mime in uploads}) == 5
    assert len({hashlib.sha256(body).digest() for _name,body,_mime in uploads}) == 5
    assert all(b'TEAM_ALPHA_ONLY' in body and mime=='text/plain' for _name,body,mime in uploads)


def test_fifty_members_ten_concurrent_requests_and_one_job_owner(tmp_path):
    postgres, redis_url = os.getenv("TEST_POSTGRES_URL"), os.getenv("TEST_REDIS_URL")
    if not postgres or not redis_url:
        pytest.skip("TEST_POSTGRES_URL and TEST_REDIS_URL are required for team acceptance")
    report_file = tmp_path / "team-report.json"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-m", "scripts.verify_team_server", "--report", str(report_file)],
        cwd=Path(__file__).resolve().parents[1], env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["status"] == "completed" and report["logged_in_members"] == 50
    assert report["concurrent_clients"] == 10 and report["peak_inflight_http"] >= 10
    assert report["vector_rows"] == 1000 and report["vector_dimensions"] == 1024
    assert report["unexpected_error_rate"] == 0 and report["cloud_model_calls"] == 0
    assert report["uploaded_jobs"] == 5 and report["competing_workers"] == 10
    assert report["successful_job_executions"] == 1 and report["duplicate_executions"] == 0
    assert report["schema_removed"] and report["redis_namespace_removed"]
    assert report["endpoints"]["cross_kb_denied"]["requests"] == 100
    assert all(item["p95_ms"] > 0 and item["unexpected_errors"] == 0 for item in report["endpoints"].values())
