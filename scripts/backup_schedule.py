"""Daily consistent server backup, verified mirror copy and RPO status.

Run --once from the host scheduler. No Docker socket is exposed to application
containers. A mirror path can point to separately mounted NAS/remote storage;
without such a configured destination, status explicitly says unconfigured.
"""
from datetime import datetime, timedelta, timezone
import argparse
import json
from pathlib import Path
import shutil
import uuid

from scripts.configure_server import ROOT, read_env
from scripts.server_backup import ServerBackup, validate_backup, _private_write, _restrict_private, COMPONENTS, operation_lock


def copy_verified_backup(source, destination):
    source, _ = validate_backup(Path(source))
    destination = Path(destination).expanduser().resolve()
    if destination == source or source in destination.parents:
        raise ValueError("Mirror destination must be outside the source backup")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / source.name
    if target.exists():
        validate_backup(target)
        if (target / "manifest.json").read_bytes() != (source / "manifest.json").read_bytes():
            raise ValueError("Existing mirror has a different manifest")
        return target
    temporary = destination / (".upload-" + uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    _restrict_private(temporary, directory=True)
    # Complete the private destination before publishing its final name. A
    # failed copy stays an identifiable incomplete directory for investigation.
    for name in (*COMPONENTS, "manifest.json"):
        target_file = temporary / name
        target_file.touch(mode=0o600)
        _restrict_private(target_file)
        with (source / name).open("rb") as src, target_file.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    validate_backup(temporary)
    temporary.rename(target)
    return target


def publish_status(root, state):
    directory = Path(root) / "data" / "operations"
    directory.mkdir(parents=True, exist_ok=True)
    allowed = {key: value for key, value in state.items() if key in {
        "status", "last_success_at", "last_attempt_at", "last_error", "mirror_status",
        "rpo_target_hours", "rto_target_minutes", "rto_verified", "mirror_error",
        "maintenance_seconds", "backup_seconds"}}
    temporary = directory / (".backup-status-" + uuid.uuid4().hex + ".json")
    temporary.write_text(json.dumps(allowed), encoding="utf-8")
    temporary.chmod(0o644)  # Aggregate status contains no credentials or paths.
    temporary.replace(directory / "backup-status.json")


def _run_once(root=ROOT, *, force=False, service_factory=ServerBackup, now=None):
    root = Path(root).resolve()
    settings = read_env(root / ".env.server")
    output = Path(settings.get("SERVER_BACKUP_DIRECTORY") or root / "data/server-backups").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "schedule-status.json"
    now = now or datetime.now(timezone.utc)
    interval = max(1, int(settings.get("SERVER_BACKUP_INTERVAL_HOURS", "24")))
    rto = max(1, int(settings.get("SERVER_BACKUP_RTO_MINUTES", "60")))
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    last = datetime.fromisoformat(previous["last_success_at"]) if previous.get("last_success_at") else None
    mirror = settings.get("SERVER_BACKUP_MIRROR_DIRECTORY", "")
    if not force and last and now - last < timedelta(hours=interval):
        # Retry an unavailable mirror without pausing application services again.
        if mirror and previous.get("mirror_status") != "verified" and previous.get("backup"):
            mirrored = copy_verified_backup(previous["backup"], mirror)
            previous.update(status="backed_up", mirror_status="verified", mirror=str(mirrored))
            previous.pop("last_error", None)
            previous.pop("mirror_error", None)
            _private_write(state_path, json.dumps(previous).encode(), replace=True)
        publish_status(root, previous)
        return {**previous, "status": "not_due"}
    try:
        result = service_factory(root).backup(output)
        folder = Path(result["backup"])
        validate_backup(folder)
        state = {"status": "backed_up", "last_success_at": now.isoformat(), "backup": str(folder),
                 "rpo_target_hours": interval, "rto_target_minutes": rto,
                 "rto_verified": False, "mirror_status": "unconfigured"}
        state.update({key: result[key] for key in ("maintenance_seconds", "backup_seconds") if key in result})
        # Record the local success even if the remote copy fails; monitoring can
        # then distinguish backup failure from mirror failure accurately.
        _private_write(state_path, json.dumps(state).encode(), replace=True)
        if mirror:
            try:
                mirrored = copy_verified_backup(folder, mirror)
                state.update(mirror_status="verified", mirror=str(mirrored))
            except Exception as exc:
                state.update(mirror_status="failed", mirror_error=type(exc).__name__)
                _private_write(state_path, json.dumps(state).encode(), replace=True)
                raise
        _private_write(state_path, json.dumps(state).encode(), replace=True)
        publish_status(root, state)
        return state
    except Exception as exc:
        current = json.loads(state_path.read_text()) if state_path.exists() else previous
        current.update(status="failed", last_attempt_at=now.isoformat(), last_error=type(exc).__name__)
        _private_write(state_path, json.dumps(current).encode(), replace=True)
        publish_status(root, current)
        raise


def run_once(root=ROOT, *, force=False, service_factory=ServerBackup, now=None):
    root = Path(root).resolve()
    # A separate host lock keeps due checks and mirror retries single-owner.
    # ServerBackup retains its own backup/restore lock inside this scope.
    with operation_lock(root / "data" / "backup-schedule"):
        return _run_once(root, force=force, service_factory=service_factory, now=now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_once(args.project_root, force=args.force)
        print(json.dumps(report, ensure_ascii=True))
        return 0 if report.get("mirror_status") != "failed" else 2
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
