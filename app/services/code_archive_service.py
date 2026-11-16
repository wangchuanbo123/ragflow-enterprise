"""Persist uploaded archives quickly; validate and unpack them in the worker."""
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import struct
import uuid
import zipfile

from app.core import config
from app.models.index_job import IndexJob
from app.models.knowledge_base import KnowledgeDataSource
from app.repositories.index_job_repository import IndexJobRepository, signal_persisted_job

MAX_ARCHIVE_ENTRIES = 10_000
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024


def _archive_root(knowledge_base_id: str, source_id: str) -> Path:
    base = (config.DOC_DIR / "code_uploads").resolve()
    for identifier in (knowledge_base_id, source_id):
        if not identifier or Path(identifier).name != identifier or "/" in identifier or "\\" in identifier or identifier in {".", ".."}:
            raise ValueError("代码上传标识无效")
    root = (base / knowledge_base_id / source_id).resolve()
    if not root.is_relative_to(base) or root == base:
        raise ValueError("代码上传路径无效")
    return root


def _remove_tree(path: Path, root: Path) -> None:
    """Only delete a verified extraction directory inside this upload root."""
    if path.is_symlink() or path.resolve() == root or not path.resolve().is_relative_to(root):
        raise ValueError("代码解压清理路径无效")
    if path.exists():
        shutil.rmtree(path)


def _validated_members(package: zipfile.ZipFile, maximum: int):
    members = package.infolist()
    if len(members) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("压缩包文件数量超过 10000 个")
    if not any(not member.is_dir() for member in members):
        raise ValueError("代码压缩包中没有文件")
    if sum(member.file_size for member in members) > maximum:
        raise ValueError("压缩后的代码体积异常，已停止解压")
    seen = set()
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        if (not name or "\x00" in member.orig_filename or "\\" in name or ":" in name
                or path.is_absolute() or PureWindowsPath(name).drive or ".." in path.parts):
            raise ValueError("压缩包包含不安全的文件路径")
        # Reject Windows devices and aliases on every deployment platform.
        if any(PureWindowsPath(part).is_reserved() or part.endswith((".", " ")) for part in path.parts):
            raise ValueError("压缩包包含不安全的文件路径")
        normalized = path.as_posix().casefold()
        if normalized in seen:
            raise ValueError("压缩包包含重复文件路径")
        seen.add(normalized)
        kind = stat.S_IFMT(member.external_attr >> 16)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError("代码压缩包不能包含符号链接或特殊文件")
        if member.flag_bits & 1:
            raise ValueError("代码压缩包不能加密")
    return members


def _preflight_archive(path: Path) -> None:
    """Bound ZIP metadata before ZipFile materializes its complete directory."""
    with path.open("rb") as incoming:
        incoming.seek(0, os.SEEK_END)
        size = incoming.tell()
        incoming.seek(max(0, size - (65535 + 22)))
        tail = incoming.read(65535 + 22)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or len(tail) - position < 22:
        raise zipfile.BadZipFile("ZIP end record is missing")
    _, disk, directory_disk, disk_count, total_count, directory_bytes, _, comment_bytes = struct.unpack_from("<4s4H2LH", tail, position)
    if position + 22 + comment_bytes != len(tail) or disk or directory_disk or disk_count != total_count:
        raise zipfile.BadZipFile("ZIP end record is invalid")
    # ZIP64's sentinel count is already above our supported 10,000 entries;
    # its >4 GiB offsets/sizes are unnecessary under the upload/expanded caps.
    if total_count > MAX_ARCHIVE_ENTRIES:
        raise ValueError("压缩包文件数量超过 10000 个")
    if directory_bytes > MAX_CENTRAL_DIRECTORY_BYTES:
        raise ValueError("压缩包目录信息过大，请减少文件数量或路径长度")


class CodeArchiveService:
    def __init__(self, db, knowledge_base_id: str, task_queue=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        if task_queue is None:
            from app.bootstrap.container import get_provider_container
            task_queue = get_provider_container().task_queue
        self.task_queue = task_queue

    def stage(self, filename: str, path: Path, requested_by: str):
        """Copy bounded staging to shared storage and atomically publish work."""
        source_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        root = _archive_root(self.knowledge_base_id, source_id)
        root.mkdir(parents=True, exist_ok=False)
        committed = False
        try:
            from app.adapters.local import LocalObjectStore
            LocalObjectStore(root).put_file("source.zip", path)
            source = KnowledgeDataSource(
                id=source_id, knowledge_base_id=self.knowledge_base_id,
                source_type="local_directory", name=f"{Path(filename).stem[:100] or '上传代码'}-{source_id[:6]}",
                config_json={"location": str(root / "content"), "index_code": True,
                             "uploaded_archive": True, "archive_pending": True},
                status="preparing", health_status="pending", health_detail="等待后台校验和解压代码",
                last_sync_status="queued", last_sync_job_id=job_id,
            )
            self.db.add(source)
            IndexJobRepository(self.db, self.knowledge_base_id).create(IndexJob(
                id=job_id, job_type="code_index", status="pending", requested_by=requested_by,
                total_items=1, max_attempts=config.INDEX_JOB_MAX_ATTEMPTS,
                options_json={"data_source_id": source_id, "prepare_archive": True},
            ))
            committed = True
        finally:
            if not committed:
                self.db.rollback()
                # The root is generated here and verified against DOC_DIR.
                if root.is_symlink() or not root.is_relative_to((config.DOC_DIR / "code_uploads").resolve()):
                    raise ValueError("代码上传清理路径无效")
                shutil.rmtree(root)
        signal_persisted_job(self.task_queue, job_id)
        return source

    def prepare(self, source, *, cancelled=None) -> Path:
        """Retryable worker preparation with bounded, streaming extraction."""
        root = _archive_root(self.knowledge_base_id, source.id)
        archive = root / "source.zip"
        destination = root / "content"
        if source.status == "disabled":
            raise ValueError("代码数据源已停用")
        if not (source.config_json or {}).get("archive_pending"):
            candidate = Path((source.config_json or {})["location"]).resolve()
            if not candidate.is_relative_to(destination) or not candidate.is_dir():
                raise ValueError("代码解压目录不可用")
            archive.unlink(missing_ok=True)
            return candidate
        stage = root / ("extract-" + uuid.uuid4().hex)
        stage.mkdir()
        maximum = min(config.MAX_BATCH_UPLOAD_TOTAL_MB * 1024 * 1024 * 4, MAX_EXPANDED_BYTES)
        try:
            written = 0
            _preflight_archive(archive)
            with zipfile.ZipFile(archive) as package:
                members = _validated_members(package, maximum)
                for member in members:
                    if cancelled and cancelled():
                        from app.agents.contracts import AgentCancelledError
                        raise AgentCancelledError("代码解压已取消")
                    target = (stage / member.filename).resolve()
                    if target == stage or not target.is_relative_to(stage):
                        raise ValueError("压缩包包含不安全的文件路径")
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as incoming, target.open("xb") as outgoing:
                        while block := incoming.read(1024 * 1024):
                            if cancelled and cancelled():
                                from app.agents.contracts import AgentCancelledError
                                raise AgentCancelledError("代码解压已取消")
                            written += len(block)
                            if written > maximum:
                                raise ValueError("压缩后的代码体积异常，已停止解压")
                            outgoing.write(block)
            _remove_tree(destination, root)
            os.replace(stage, destination)
            children = list(destination.iterdir())
            code_root = children[0] if len(children) == 1 and children[0].is_dir() else destination
            source.config_json = {**(source.config_json or {}), "location": str(code_root), "archive_pending": False}
            source.status, source.health_status = "active", "ok"
            source.health_detail = "代码压缩包已通过校验并解压"
            source.last_sync_status = "running"
            source.last_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self.db.commit()
            archive.unlink(missing_ok=True)
            return code_root
        except (zipfile.BadZipFile, ValueError, OSError, NotImplementedError) as exc:
            source.status, source.health_status, source.last_sync_status = "error", "fail", "failed"
            source.health_detail = source.last_sync_detail = str(exc) if isinstance(exc, ValueError) else "代码压缩包损坏或无法解压，请重新上传"
            self.db.commit()
            raise ValueError(source.health_detail) from None
        finally:
            _remove_tree(stage, root)
