"""Consistent PostgreSQL/documents/settings backup and guarded server restore.

Use an output directory on a separately mounted storage location for off-host
copies. Infrastructure credentials and deployment paths remain local on restore;
business settings are restored. Original private settings stay in env.original.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import gzip
import shutil
from functools import lru_cache
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from scripts.configure_server import ROOT, read_env
from scripts.start_server import compose_command


APP_SERVICES = ("api", "worker", "scheduler")
COMPONENTS = ("database.dump", "documents.tar.gz", "env.original")
MAX_DOCUMENT_BYTES = 100 * 1024 ** 3


@lru_cache(maxsize=1)
def application_tables():
    """Only application-owned tables in the dedicated public schema are reset."""
    from app.models import Base
    from rag.providers.pgvector_store import PGVECTOR_METADATA
    return frozenset({"alembic_version", *Base.metadata.tables, *PGVECTOR_METADATA.tables})


def validate_database_toc(value: bytes) -> None:
    """Reject archives outside the supported application/public schema boundary."""
    tables = application_tables()
    table_count = 0
    descriptions = ("SEQUENCE OWNED BY", "SEQUENCE SET", "TABLE DATA", "FK CONSTRAINT", "CHECK CONSTRAINT",
                    "CONSTRAINT", "SEQUENCE", "EXTENSION", "SCHEMA", "TABLE", "INDEX", "DEFAULT", "COMMENT", "FUNCTION", "TRIGGER")
    for line in value.decode("utf-8").splitlines():
        if not line or line.startswith(";"):
            continue
        match = re.fullmatch(r"\d+;\s+\d+\s+\d+\s+(.+)", line)
        if not match:
            raise BackupError("Unsupported database archive directory")
        entry = match.group(1)
        description = next((kind for kind in descriptions if entry.startswith(kind + " ")), None)
        if description is None:
            raise BackupError("Database archive contains unsupported objects")
        parts = entry[len(description) + 1:].split()
        if len(parts) < 2:
            raise BackupError("Unsupported database archive object")
        schema, name = parts[:2]
        if description == "EXTENSION":
            allowed = schema == "-" and name == "vector"
        elif description == "SCHEMA":
            allowed = schema == "-" and name == "public"
        elif description == "COMMENT" and schema == "-":
            allowed = parts[1:3] in (["EXTENSION", "vector"], ["SCHEMA", "public"])
        elif description == "FUNCTION":
            allowed = schema == "public" and name == "bump_knowledge_graph_revision()"
        elif description == "TRIGGER":
            from app.services.graph_revision import GRAPH_TABLES
            allowed = (schema == "public" and name in GRAPH_TABLES and len(parts) >= 3
                       and parts[2] in {"graph_revision_insert", "graph_revision_update", "graph_revision_delete", "graph_revision_truncated"})
        else:
            allowed = schema == "public"
            if description in {"TABLE", "TABLE DATA", "CONSTRAINT", "CHECK CONSTRAINT", "FK CONSTRAINT", "DEFAULT"}:
                allowed = allowed and name in tables
            elif description in {"SEQUENCE", "SEQUENCE SET", "SEQUENCE OWNED BY"}:
                allowed = allowed and any(name.startswith(table + "_") and name.endswith("_seq") for table in tables)
            elif description not in {"INDEX", "COMMENT"}:
                allowed = False
        if not allowed:
            raise BackupError("Database archive is outside the dedicated application public schema")
        table_count += description == "TABLE"
    if not table_count:
        raise BackupError("Database archive contains no application tables")


DATABASE_SCOPE_QUERY = """
SELECT json_build_object(
 'database', current_database(),
 'extensions', (SELECT coalesce(json_agg(extname), '[]'::json) FROM pg_extension WHERE extname NOT IN ('plpgsql','vector')),
 'routines', (SELECT coalesce(json_agg(p.proname), '[]'::json) FROM pg_proc p
  JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')
  AND NOT (p.proname='bump_knowledge_graph_revision' AND p.pronargs=0 AND p.prorettype='trigger'::regtype AND p.prokind='f')),
 'schemas', (SELECT coalesce(json_agg(nspname), '[]'::json) FROM pg_namespace
             WHERE nspname <> 'public' AND nspname <> 'information_schema' AND nspname NOT LIKE 'pg_%'),
 'relations', (SELECT coalesce(json_agg(json_build_object('name', c.relname, 'kind', c.relkind)), '[]'::json)
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f','S')
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'))
);
"""

# This is deliberately a stdlib-only one-shot helper: it runs with api's existing
# image/volume bindings and does not import app settings, start services or models.
DOCUMENT_HELPER = r"""
import json, os, pathlib, re, shutil, stat, sys, tarfile
root=pathlib.Path(sys.argv[2]).resolve()
mode=sys.argv[1]
root.mkdir(parents=True,exist_ok=True)
def owned(name):
 if not re.fullmatch(r'\.server-restore-(stage|old)-[a-f0-9]{32}',name): raise ValueError('Invalid staging name')
 p=root/name
 if p.is_symlink() or not p.resolve().is_relative_to(root): raise ValueError('Unsafe staging path')
 return p
def member_path(item):
 p=pathlib.PurePosixPath(item.name)
 if not item.name or p.is_absolute() or '..' in p.parts or '\\' in item.name or ':' in item.name or '\x00' in item.name or p.as_posix()=='.': raise ValueError('Unsafe archive path')
 if not (item.isdir() or item.isfile()) or item.size<0: raise ValueError('Archive links and special files are unsupported')
 if p.parts[0].startswith('.server-restore-'): raise ValueError('Reserved restore path')
 return p
if mode=='archive':
 total=0;limit=int(sys.argv[4])
 with tarfile.open(fileobj=sys.stdout.buffer,mode='w|' if sys.argv[3]=='uncompressed' else 'w|gz') as archive:
  for p in sorted(root.rglob('*')):
   if p.parts[len(root.parts)].startswith('.server-restore-'): raise ValueError('Unfinished restore requires attention before backup')
   info=archive.gettarinfo(str(p),arcname=p.relative_to(root).as_posix())
   member_path(info)
   total+=info.size
   if total>limit: raise ValueError('Source documents exceed backup resource budget')
   if info.isfile():
    fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as content:
     metadata=os.fstat(content.fileno())
     if not stat.S_ISREG(metadata.st_mode) or metadata.st_size!=info.size: raise ValueError('Source changed during backup')
     archive.addfile(info,content)
   else: archive.addfile(info)
elif mode=='stage':
 stage=owned(sys.argv[3]);stage.mkdir(mode=0o700)
 limit=int(sys.argv[4]);seen=set();total=0
 with tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz') as archive:
  for item in archive:
   relative=member_path(item);name=relative.as_posix()
   if name in seen: raise ValueError('Duplicate archive path')
   seen.add(name);total+=item.size
   if total>limit or len(seen)>1000000: raise ValueError('Archive exceeds resource budget')
   path=stage.joinpath(*relative.parts)
   if not path.resolve().is_relative_to(stage): raise ValueError('Archive escapes staging directory')
   if item.isdir(): path.mkdir(parents=True,exist_ok=True,mode=0o700)
   else:
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with archive.extractfile(item) as source, path.open('xb') as target: shutil.copyfileobj(source,target,1024*1024)
    path.chmod(0o600)
 print(json.dumps({'files':len(seen),'bytes':total}))
elif mode=='activate':
 stage=owned(sys.argv[3]);old=owned(sys.argv[3].replace('-stage-','-old-'))
 if not stage.is_dir(): raise ValueError('Missing staged documents')
 old.mkdir(mode=0o700)
 moved=[];installed=[]
 try:
  for p in list(root.iterdir()):
   if p in (stage,old): continue
   p.rename(old/p.name);moved.append(p.name)
  for p in list(stage.iterdir()):
   p.rename(root/p.name);installed.append(p.name)
  stage.rmdir()
 except BaseException:
  stage.mkdir(exist_ok=True,mode=0o700)
  for name in reversed(installed): (root/name).rename(stage/name)
  for name in reversed(moved): (old/name).rename(root/name)
  raise
 print(json.dumps({'old':old.name}))
elif mode=='cleanup':
 old=owned(sys.argv[3])
 if old.exists(): shutil.rmtree(old)
else: raise ValueError('Unsupported document operation')
"""


class BackupError(RuntimeError):
    pass


class QuiescedServices(list):
    def __init__(self, services):
        super().__init__(services)
        self.resume_allowed = True


@lru_cache(maxsize=1)
def _windows_owner_sid():
    command = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/whoami.exe")
    result = subprocess.run([command, "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    match = re.search(r"S-\d+(?:-\d+){2,}", result.stdout)
    if result.returncode or not match:
        raise BackupError("Cannot establish private backup permissions")
    return match[0]


def _restrict_private(path, *, directory=False):
    path.chmod(0o700 if directory else 0o600)
    if os.name == "nt":
        # Windows chmod does not remove inherited read access. Apply the
        # owner-only ACL equivalent of 0700/0600 before writing any secrets.
        command = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/icacls.exe")
        grant = f"*{_windows_owner_sid()}:" + ("(OI)(CI)F" if directory else "F")
        result = subprocess.run([command, str(path), "/inheritance:r", "/grant:r", grant], capture_output=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise BackupError("Cannot establish private backup permissions")


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _private_write(path: Path, content: bytes, *, replace=False):
    if path.is_symlink():
        raise BackupError("Private settings path must not be a symbolic link")
    if replace:
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                _restrict_private(temporary)
                stream.write(content)
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            _restrict_private(path)
            stream.write(content)
    path.chmod(0o600)


def _member_path(member):
    path = PurePosixPath(member.name)
    if (not member.name or path.is_absolute() or ".." in path.parts or "\\" in member.name
        or ":" in member.name or "\x00" in member.name or path.as_posix() == "."):
        raise BackupError("Documents archive contains an unsafe path")
    if not (member.isdir() or member.isfile()) or member.size < 0 or path.parts[0].startswith(".server-restore-"):
        raise BackupError("Documents archive contains a link, special file or reserved path")
    return path.as_posix()


def validate_backup(path: Path, *, max_document_bytes=MAX_DOCUMENT_BYTES):
    original = Path(path).expanduser()
    if original.is_symlink():
        raise BackupError("Backup directory must not be a symbolic link")
    path = original.resolve(strict=True)
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 1024 * 1024:
        raise BackupError("Backup manifest is missing or invalid")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise BackupError("Backup manifest is invalid") from None
    if (not isinstance(manifest, dict) or manifest.get("format_version") != 1
        or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != set(COMPONENTS)):
        raise BackupError("Backup format or components are unsupported")
    for name in COMPONENTS:
        item = path / name
        metadata = manifest["files"][name]
        if item.is_symlink() or not item.is_file() or item.stat().st_nlink != 1:
            raise BackupError("Backup components must be regular, unlinked files")
        if (not isinstance(metadata, dict) or metadata.get("bytes") != item.stat().st_size
            or metadata.get("sha256") != _sha256(item)):
            raise BackupError("Backup checksum verification failed")
    if (path / "env.original").stat().st_size > 2 * 1024 * 1024:
        raise BackupError("Private settings file exceeds its size budget")
    with (path / "database.dump").open("rb") as database:
        if database.read(5) != b"PGDMP":
            raise BackupError("Backup database is not a PostgreSQL custom archive")
    total, seen = 0, set()
    try:
        with tarfile.open(path / "documents.tar.gz", "r:gz") as archive:
            for member in archive:
                name = _member_path(member)
                if name in seen:
                    raise BackupError("Documents archive contains duplicate paths")
                seen.add(name)
                total += member.size
                if total > max_document_bytes or len(seen) > 1000000:
                    raise BackupError("Documents archive exceeds its resource budget")
                if member.isfile():
                    # Consume content now too, detecting truncated gzip/tar data
                    # before stopping services or modifying the target.
                    with archive.extractfile(member) as stream:
                        while stream.read(1024 * 1024):
                            pass
    except (tarfile.TarError, EOFError, OSError):
        raise BackupError("Documents archive is damaged") from None
    return path, manifest


def _infrastructure_key(key):
    return (key.startswith(("POSTGRES_", "REDIS_", "DATABASE_", "PGVECTOR_"))
            or key.endswith(("_DIR", "_PATH", "_ROOT", "_ROOTS", "_EXECUTABLE"))
            or key in {"APP_BIND_ADDRESS", "APP_PORT", "APP_UID", "APP_GID", "UID", "GID", "PUID", "PGID",
                       "COMPOSE_PROJECT_NAME", "DATA_DIR", "APP_PROFILE", "APP_ROLE", "REDIS_URL",
                       "METADATA_STORE_PROVIDER", "VECTOR_STORE_PROVIDER", "GRAPH_STORE_PROVIDER",
                       "TASK_QUEUE_PROVIDER", "CACHE_PROVIDER", "OBJECT_STORE_PROVIDER",
                       "AGENT_TOOL_URL", "AGENT_TRUSTED_HTTP_HOSTS"})


def restored_environment(current: dict, archived: dict) -> bytes:
    restored = {key: value for key, value in archived.items() if not _infrastructure_key(key)}
    restored.update({key: value for key, value in current.items() if _infrastructure_key(key)})
    return ("# Restored business settings; local infrastructure settings preserved.\n"
            + "\n".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in sorted(restored.items())) + "\n").encode("utf-8")


@contextmanager
def operation_lock(root):
    directory = root / "data"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "server-backup.lock"
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0"); stream.flush()
        deadline = time.monotonic() + 180
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BackupError("Another backup or restore is still active") from None
                time.sleep(0.05)
        yield


class ServerBackup:
    def __init__(self, root=ROOT, *, timeout=1800, max_document_bytes=MAX_DOCUMENT_BYTES):
        self.root = Path(root).resolve()
        self.base = compose_command(self.root)
        self.timeout = timeout
        self.max_document_bytes = max_document_bytes
        self.environment = self.root / ".env.server"
        self._capture_only = False
        self._timings = {}

    def _maintenance_status(self, status, **values):
        path = self.root / "data/operations/maintenance.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _private_write(path, (json.dumps({"status": status, "updated_at": datetime.now(timezone.utc).isoformat(),
                                        **values}) + "\n").encode(), replace=True)
        path.chmod(0o644)  # Aggregate status only; readable by the application UID.

    def _run(self, arguments, *, stdin=None, stdout=None, capture=False):
        try:
            result = subprocess.run(self.base + list(arguments), cwd=self.root, stdin=stdin,
                                    stdout=subprocess.PIPE if capture else stdout, stderr=subprocess.PIPE,
                                    timeout=self.timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as exc:
            raise BackupError(f"Server backup operation failed ({type(exc).__name__})") from None
        if result.returncode:
            # Docker and pg_restore stderr can contain environment values or row
            # contents. Keep the raw output off the user/logging path.
            raise BackupError("Server backup container operation failed")
        return result.stdout if capture else None

    def _document(self, mode, *, stage="", stdin=None, stdout=None, capture=False):
        return self._run(["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "api", "-c", DOCUMENT_HELPER,
                          mode, "/app/data/docs", stage, str(self.max_document_bytes)],
                         stdin=stdin, stdout=stdout, capture=capture)

    def _database_scope(self):
        raw = self._run(["exec", "-T", "postgres", "sh", "-c",
                         'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -X -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"',
                         "server-backup", DATABASE_SCOPE_QUERY], capture=True)
        try:
            scope = json.loads(raw)
        except (ValueError, TypeError):
            raise BackupError("Unable to verify the dedicated application database") from None
        if (not isinstance(scope, dict) or not isinstance(scope.get("database"), str)
                or not isinstance(scope.get("schemas"), list) or not isinstance(scope.get("relations"), list)):
            raise BackupError("Unable to verify the dedicated application database")
        tables = application_tables()
        if scope.get("database") in {"postgres", "template0", "template1"} or scope.get("schemas") or scope.get("routines") or scope.get("extensions"):
            raise BackupError("Restore supports only a dedicated application database with the public schema")
        for relation in scope.get("relations", []):
            name, kind = relation.get("name", ""), relation.get("kind")
            allowed = name in tables if kind in {"r", "p"} else (
                kind == "S" and any(name.startswith(table + "_") and name.endswith("_seq") for table in tables))
            if not allowed:
                raise BackupError("Database contains objects outside the application table boundary")

    def _database_restore(self, dump):
        """Reset every known app table and replay a legacy dump atomically.

        New target tables absent in an old archive must disappear too. Keeping
        the extension/schema/database/roles avoids collateral infrastructure DDL.
        Plain SQL is streamed to an owner-only temporary file, never memory/logs.
        """
        self._database_scope()
        with Path(dump).open("rb") as stream:
            toc = self._run(["exec", "-T", "postgres", "pg_restore", "--list"], stdin=stream, capture=True)
        validate_database_toc(toc)
        with tempfile.TemporaryDirectory(prefix="rag-server-restore-") as temporary:
            directory = Path(temporary)
            _restrict_private(directory, directory=True)
            sql = directory / "restore.sql"
            names = ", ".join('public."' + name + '"' for name in sorted(application_tables()))
            _private_write(sql, ("BEGIN;\nSET LOCAL lock_timeout='30s';\nDROP TABLE IF EXISTS " + names + " CASCADE;\n"
                                "DROP FUNCTION IF EXISTS public.bump_knowledge_graph_revision() CASCADE;\n").encode())
            with sql.open("ab") as destination, Path(dump).open("rb") as source:
                self._run(["exec", "-T", "postgres", "pg_restore", "--no-owner", "--no-acl", "--file=-"], stdin=source, stdout=destination)
                destination.write(b"\nCOMMIT;\n")
            with sql.open("rb") as source:
                self._run(["exec", "-T", "postgres", "sh", "-c",
                           'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -X -v ON_ERROR_STOP=1 --quiet -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                          stdin=source, stdout=subprocess.DEVNULL)

    @contextmanager
    def _quiesced(self, *, recreate=False):
        running = set(self._run(["ps", "--services", "--status", "running"], capture=True).decode().split())
        previous = QuiescedServices(name for name in APP_SERVICES if name in running)
        paused_at = time.monotonic()
        self._maintenance_status("restoring" if recreate else "snapshotting", services=list(previous))
        try:
            # Stop restarting containers too, not only those currently reported
            # running. Otherwise an automatic restart could write during restore.
            self._run(["stop", "--timeout", "90", *reversed(APP_SERVICES)])
            yield previous
        finally:
            if previous and previous.resume_allowed:
                # A routine backup must resume the exact stopped containers.
                # `up` reconciles newly edited Compose mounts, healthchecks and
                # environment against an image that may not be rebuilt yet.
                # A restore deliberately recreates containers for restored
                # business settings; ordinary backup is not a deployment.
                arguments = (["up", "-d", "--no-deps", "--force-recreate"]
                             if recreate else ["start"])
                if "api" in previous:
                    # Compose waits for api's configured HTTP healthcheck. Do
                    # not let workers claim MCP jobs while API/OpenCode boots.
                    self._run([*arguments, "--wait", "--wait-timeout", "180", "api"])
                consumers = [name for name in previous if name != "api"]
                if consumers:
                    self._run([*arguments, "--wait", "--wait-timeout", "180", *consumers])
            self._timings["maintenance_seconds"] = round(time.monotonic() - paused_at, 3)
            self._maintenance_status("idle" if previous.resume_allowed else "recovery_required", **self._timings)

    def _snapshot(self, output, previous):
        self._database_scope()
        output = Path(output).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        folder = output / (datetime.now(timezone.utc).strftime("server-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
        folder.mkdir(mode=0o700)
        _restrict_private(folder, directory=True)
        if self.environment.is_symlink() or not self.environment.is_file():
            raise BackupError("Private server settings are missing or linked")
        _private_write(folder / "env.original", self.environment.read_bytes())
        for name, arguments in [
            ("database.dump", ["exec", "-T", "postgres", "sh", "-c",
                               'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump --format=custom --compress=0 --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"']),
        ]:
            descriptor = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                self._run(arguments, stdout=stream)
        name = "documents.tar" if self._capture_only else "documents.tar.gz"
        descriptor = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            self._document("archive", stage="uncompressed" if self._capture_only else "", stdout=stream)
        if self._capture_only:
            return folder
        return self._finalize_snapshot(folder, previous)

    def _finalize_snapshot(self, folder, previous):
        """Compression, checksums and full archive verification run after resume."""
        raw = folder / "documents.tar"
        if raw.exists():
            descriptor = os.open(folder / "documents.tar.gz", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as destination, raw.open("rb") as source:
                with gzip.GzipFile(fileobj=destination, mode="wb", compresslevel=1, mtime=0) as compressed:
                    shutil.copyfileobj(source, compressed, 1024 * 1024)
            raw.unlink()
        manifest = {"format_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                    "database_format": "postgresql-custom", "services_previously_running": list(previous),
                    "consistency": "application_writes_quiesced_during_database_and_document_capture",
                    "timings": dict(self._timings),
                    "files": {name: {"bytes": (folder / name).stat().st_size, "sha256": _sha256(folder / name)} for name in COMPONENTS}}
        _private_write(folder / "manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
                       replace=(folder / "manifest.json").exists())
        validate_backup(folder, max_document_bytes=self.max_document_bytes)
        return folder

    def backup(self, output):
        started = time.monotonic()
        with operation_lock(self.root):
            try:
                self._capture_only = True
                with self._quiesced() as previous:
                    folder = self._snapshot(output, previous)
            finally:
                self._capture_only = False
            self._finalize_snapshot(folder, previous)
        self._timings["backup_seconds"] = round(time.monotonic() - started, 3)
        self._maintenance_status("idle", **self._timings)
        return {"status": "backed_up", "backup": str(folder), "services_restored": previous, **self._timings}

    def _apply(self, folder, *, merged_environment):
        stage = ".server-restore-stage-" + uuid.uuid4().hex
        with (folder / "documents.tar.gz").open("rb") as stream:
            self._document("stage", stage=stage, stdin=stream, capture=True)
        self._database_restore(folder / "database.dump")
        self._document("activate", stage=stage, capture=True)
        _private_write(self.environment, merged_environment, replace=True)
        self._document("cleanup", stage=stage.replace("-stage-", "-old-"))

    def restore(self, backup, *, protection_output=None):
        started = time.monotonic()
        # Validate the entire artifact before changing service state.
        folder, manifest = validate_backup(Path(backup), max_document_bytes=self.max_document_bytes)
        with (folder / "database.dump").open("rb") as stream:
            toc = self._run(["exec", "-T", "postgres", "pg_restore", "--list"], stdin=stream, capture=True)
        validate_database_toc(toc)
        self._database_scope()
        merged = restored_environment(read_env(self.environment), read_env(folder / "env.original"))
        with operation_lock(self.root), self._quiesced(recreate=True) as previous:
            protection = self._snapshot(protection_output or folder.parent / "pre-restore", previous)
            try:
                self._apply(folder, merged_environment=merged)
            except Exception:
                try:
                    self._apply(protection, merged_environment=(protection / "env.original").read_bytes())
                except Exception:
                    previous.resume_allowed = False
                    raise BackupError(f"Restore failed and automatic rollback was incomplete; application services remain stopped. Protection backup is preserved at {protection}") from None
                raise BackupError(f"Restore failed; the previous database, documents and settings were restored from the protection backup at {protection}") from None
        self._timings["restore_seconds"] = round(time.monotonic() - started, 3)
        self._maintenance_status("idle", **self._timings)
        return {"status": "restored", "backup": str(folder), "protection_backup": str(protection),
                "services_restored": previous, "infra_credentials": "preserved", "business_settings": "restored", **self._timings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-document-bytes", type=int, default=MAX_DOCUMENT_BYTES)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", required=True, type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--protection-output", type=Path)
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.max_document_bytes < 1:
        parser.error("Timeout and document byte budget must be positive")
    try:
        service = ServerBackup(args.project_root, timeout=args.timeout, max_document_bytes=args.max_document_bytes)
        report = service.backup(args.output) if args.command == "backup" else service.restore(args.backup, protection_output=args.protection_output)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (BackupError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, BackupError) else f"Server backup operation failed ({type(exc).__name__})"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

