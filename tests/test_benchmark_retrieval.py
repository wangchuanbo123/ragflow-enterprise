"""The benchmark CLI must run without the application's files or providers."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_benchmark_uses_only_temporary_database_and_skips_dotenv(tmp_path):
    output = tmp_path / "benchmark.json"
    guard = r'''
import builtins, runpy, sys, tempfile
from pathlib import Path
from unittest.mock import patch
import sqlalchemy
from sqlalchemy.engine import make_url
project = Path.cwd().resolve()
data = project / 'data'
original_open = builtins.open
original_mkdir = Path.mkdir
original_engine = sqlalchemy.create_engine
engines = []
def safe_open(file, *args, **kwargs):
    if isinstance(file, (str, Path)):
        assert Path(file).resolve() != project / '.env', 'read or wrote real .env'
    return original_open(file, *args, **kwargs)
def safe_mkdir(path, *args, **kwargs):
    assert not path.resolve().is_relative_to(data), 'touched real data directory'
    return original_mkdir(path, *args, **kwargs)
def safe_engine(url, *args, **kwargs):
    parsed = make_url(url)
    assert parsed.drivername == 'sqlite'
    assert Path(parsed.database).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    engines.append(parsed.database)
    return original_engine(url, *args, **kwargs)
sys.argv = ['benchmark_retrieval', '--chunks', '24', '--queries', '4', '--output', sys.argv[1]]
with patch('builtins.open', safe_open), patch.object(Path, 'mkdir', safe_mkdir), patch.object(sqlalchemy, 'create_engine', safe_engine):
    try:
        runpy.run_module('scripts.benchmark_retrieval', run_name='__main__')
    except SystemExit as exit:
        assert exit.code == 0
assert engines and all(not Path(name).exists() for name in engines), 'temporary database was not cleaned up'
'''
    result = subprocess.run(
        [sys.executable, "-c", guard, str(output)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["benchmark"] == "local_bm25_only"
    assert report["dataset"]["chunks"] == 24
    assert report["queries"] == len(report["per_query"]) == 4
    assert report["query_latency_ms"]["p95"] >= report["query_latency_ms"]["p50"] >= 0
    assert report["memory_approximation"]["peak_sampled_rss_bytes"] > 0
    assert all(row["returned_chunks"] > 0 for row in report["per_query"])


@pytest.mark.parametrize("arguments", [
    ["--chunks", "0"],
    ["--queries", "-1"],
    ["--output", "data/benchmark.json"],
    ["--output", ".env"],
])
def test_benchmark_rejects_invalid_counts_and_application_output(arguments):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.benchmark_retrieval", *arguments],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
