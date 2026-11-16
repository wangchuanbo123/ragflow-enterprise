import os
import subprocess
import sys


def test_fresh_database_migrates_idempotently_and_matches_models(tmp_path):
    env = dict(os.environ, DATABASE_URL="sqlite:///" + (tmp_path / "migration.db").as_posix())
    for arguments in (("upgrade", "head"), ("upgrade", "head"), ("check",)):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
