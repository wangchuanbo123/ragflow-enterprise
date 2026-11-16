"""Start the same PostgreSQL/Redis team stack on a workstation or server."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request
import webbrowser
import uuid
from urllib.parse import unquote

from scripts.configure_server import ROOT, configure_server, read_env


def compose_command(root: Path = ROOT) -> list[str]:
    return [shutil.which("docker") or "docker", "compose", "--env-file", str(root / ".env.server"),
            "-f", str(root / "deploy/compose.yml")]


def checked(command: list[str], *, quiet: bool = False):
    result = subprocess.run(command, cwd=ROOT, capture_output=quiet, text=quiet)
    if result.returncode:
        phase = command[command.index("-m") + 1] if "-m" in command else "Docker Compose"
        detail = "请检查上方命令输出"
        if quiet:
            # Raw driver/CLI errors can contain secrets. Keep a private log,
            # expose only the phase, exit code and a structured error type.
            path = ROOT / "data/server-start-failure.log"
            try:
                from scripts.server_backup import _private_write
                path.parent.mkdir(parents=True, exist_ok=True)
                _private_write(path, ((result.stdout or "") + "\n" + (result.stderr or "")).encode("utf-8"), replace=True)
                detail = "请查看私有诊断日志 data/server-start-failure.log"
            except (OSError, ValueError, RuntimeError):
                detail = "无法建立私有诊断日志，请检查目录权限"
        raise RuntimeError(f"{phase} 启动步骤失败，exit code {result.returncode}；{detail}。已有数据保留。") from None
    return result.stdout if quiet else None


def legacy_paths(root: Path = ROOT) -> dict[str, Path]:
    old = read_env(root / ".env")
    def absolute(value):
        path = Path(value)
        return (path if path.is_absolute() else root / path).resolve()
    data = absolute(old.get("DATA_DIR") or root / "data")
    url = old.get("DATABASE_URL", "").strip()
    if url:
        if not url.startswith(("sqlite:///", "sqlite+pysqlite:///")):
            raise RuntimeError("旧 DATABASE_URL 不是可导入的文件 SQLite；请显式停用旧数据导入或提供旧 SQLite 文件配置")
        location = unquote(url.split(":///", 1)[1].split("?", 1)[0])
        if not location or location == ":memory:":
            raise RuntimeError("旧 SQLite 必须是持久文件")
        sqlite = absolute(location)
        if not sqlite.is_file() and sqlite != (root / "data/app.db").resolve():
            raise RuntimeError("旧 DATABASE_URL 指定的 SQLite 文件不存在，拒绝静默启动空库")
    else:
        sqlite = data / "app.db"
        if old.get("DATA_DIR") and data != (root / "data").resolve() and not sqlite.is_file():
            raise RuntimeError("旧 DATA_DIR 指定的 SQLite 文件不存在，拒绝静默启动空库")
    return {"data": data, "sqlite": sqlite,
            "objects": absolute(old.get("LOCAL_OBJECT_STORE_DIR") or data / "docs"),
            "vectors": data / "vector_db"}


def target_identity(base: list[str], *, ensure=False) -> str:
    value = checked(base + ["run", "--rm", "--no-deps", "api", "python", "-m", "scripts.server_identity",
                            "--ensure" if ensure else "--read"], quiet=True).strip()
    if value == "missing":
        return value
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise RuntimeError("服务器数据库身份响应无效，拒绝跳过迁移") from None


def legacy_image_required(root: Path) -> bool:
    """Only the one-shot Chroma importer needs the optional legacy packages."""
    try:
        paths = legacy_paths(root)
    except RuntimeError:
        # A completed migration may legitimately have archived its old source.
        # The importer checks the deployment identity before validating paths.
        return False
    return (paths["sqlite"].is_file() and paths["vectors"].is_dir()
            and any(paths["vectors"].iterdir()))


def write_state(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def import_legacy(base: list[str], root: Path = ROOT) -> None:
    reports = root / "data/server-migration"
    reports.mkdir(parents=True, exist_ok=True)
    done = reports / "complete.json"
    current = target_identity(base)
    if done.exists():
        previous = json.loads(done.read_text(encoding="utf-8"))
        if current != "missing" and previous.get("deployment_id") == current:
            return
        if current != "missing":
            raise RuntimeError("迁移标记与当前数据库身份不同；拒绝将旧资料覆盖到另一实例")
    paths = legacy_paths(root)
    source = paths["sqlite"]
    if not source.is_file():
        if done.exists():
            raise RuntimeError("目标数据库已丢失且旧源文件不可用，请从完整备份恢复")
        return
    # Every source is read-only inside the one-shot migration container. API
    # and worker services are started only after all three imports succeed.
    owner = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "10001:10001"
    common = base + ["run", "--rm", "--no-deps", "--user", owner, "-v", f"{source.parent}:/legacy-sqlite:ro",
                     "-v", f"{reports}:/reports", "api", "python", "-m"]
    metadata_report = reports / "metadata.json"
    pending = reports / "pending.json"
    resume = pending.exists() and json.loads(pending.read_text(encoding="utf-8")).get("deployment_id") == current and current != "missing"
    command = common + ["scripts.migrate_to_postgres", "--source-sqlite", f"/legacy-sqlite/{source.name}", "--report", "/reports/metadata.json"]
    if resume:
        command += ["--verify-only"]
    checked(command, quiet=True)
    current = target_identity(base, ensure=True)
    write_state(pending, {"deployment_id": current, "phase": "metadata"})
    docs = paths["objects"]
    if docs.is_dir():
        checked(base + ["run", "--rm", "--no-deps", "--user", "0:0", "-v", f"{docs.resolve()}:/legacy-objects:ro",
                        "api", "python", "-m", "scripts.import_legacy_objects", "--source", "/legacy-objects", "--owner", "10001:10001"])
    vector_dir = paths["vectors"]
    if vector_dir.is_dir() and any(vector_dir.iterdir()):
        vector_command = base + ["run", "--rm", "--no-deps", "--user", owner,
                                 "-v", f"{vector_dir}:/legacy-vectors:ro", "-v", f"{reports}:/reports", "api", "python", "-m"]
        checked(vector_command + ["scripts.migrate_vectors_to_pgvector", "--source", "/legacy-vectors",
                          "--active-only", "--report", "/reports/vectors.json"], quiet=True)
    checked(base + ["run", "--rm", "--no-deps", "api", "python", "-m", "scripts.verify_server_storage"])
    write_state(done, {"status": "complete", "deployment_id": current, "source_preserved": True})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-legacy-import", action="store_true", help="Start a new empty deployment without importing local data")
    args = parser.parse_args(argv)
    if not shutil.which("docker"):
        print("请安装并启动 Docker 后重新运行。")
        return 1
    configure_server()
    base = compose_command()
    try:
        checked(base + ["config", "--quiet"])
        migration_image = False
        if not args.skip_build:
            migration_image = not args.no_legacy_import and legacy_image_required(ROOT)
            arguments = ["--build-arg", "INSTALL_LEGACY=true"] if migration_image else []
            checked(base + ["build", *arguments, "api"])
        checked(base + ["up", "-d", "--wait", "postgres", "redis"])
        if not args.no_legacy_import:
            print("正在检查原本地资料迁移，源文件保留。", flush=True)
            import_legacy(base)
        if migration_image:
            # The migration image must not leave unused legacy adapters in
            # the long-running production API and worker image.
            checked(base + ["build", "--build-arg", "INSTALL_LEGACY=false", "api"])
        checked(base + ["up", "-d", "--wait"])
        settings = read_env(ROOT / ".env.server")
        url = f"http://127.0.0.1:{settings.get('APP_PORT', '8080')}"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url + "/api/v1/ready", timeout=5) as response:
                    if json.load(response).get("status") == "ready":
                        print(f"团队服务已就绪：{url}", flush=True)
                        if not args.no_browser:
                            webbrowser.open(url)
                        return 0
            except (OSError, ValueError):
                pass
            time.sleep(2)
        print("服务已启动，但就绪检查尚未通过。请查看部署文档中的诊断步骤。")
        return 1
    except (OSError, RuntimeError) as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
