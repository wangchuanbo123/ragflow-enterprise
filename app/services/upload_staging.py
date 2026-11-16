"""Bounded upload admission and disk staging, independent of indexing."""
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
import tempfile
import threading

from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from app.api.errors import APIError, bad_request

_guard = threading.Lock()
_slots = {}


async def upload_db_operation(request_db: Session, operation, *args):
    """Run blocking upload work with a session owned by the worker thread.

    Only the engine is shared. Never move the request's ORM session or loaded
    ORM entities between the event loop and worker threads.
    """
    bind = request_db.get_bind()
    def execute():
        with Session(bind=bind, autoflush=False, expire_on_commit=False) as db:
            return operation(db, *args)
    return await run_in_threadpool(execute)


@asynccontextmanager
async def _threaded_context(factory):
    def enter():
        manager = factory()
        return manager, manager.__enter__()
    manager, value = await run_in_threadpool(enter)
    try:
        yield value
    finally:
        # Admission release, close, and recursive temp cleanup all do I/O.
        import anyio
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(manager.__exit__, None, None, None)


@contextmanager
def upload_slot():
    from app.core import config
    maximum = getattr(config, "UPLOAD_MAX_CONCURRENCY", 4)
    if config.CACHE_PROVIDER == "redis":
        from app.adapters.redis import RedisSemaphoreLease
        slot = RedisSemaphoreLease("upload-global", maximum, 60)
        acquired = slot.acquire(0)
        release = slot.close
    else:
        with _guard:
            slot = _slots.setdefault(maximum, threading.BoundedSemaphore(maximum))
        acquired = slot.acquire(blocking=False)
        release = slot.release
    if not acquired:
        raise APIError(status_code=429, code="UPLOAD_BUSY", message="当前上传较多，请稍后重试")
    try:
        yield
    finally:
        release()


@asynccontextmanager
async def stage_uploads(files: list[UploadFile], *, partial: bool = False, max_file_mb: int | None = None):
    """No complete file or batch is held in application memory.

    Oversize individual files may be rejected in a batch; the aggregate limit
    counts every received byte, including rejected files. Files always close.
    """
    from app.core import config
    file_limit_mb = config.MAX_DOCUMENT_SIZE_MB if max_file_mb is None else max_file_mb
    maximum = file_limit_mb * 1024 * 1024
    total_maximum = config.MAX_BATCH_UPLOAD_TOTAL_MB * 1024 * 1024
    total = 0
    try:
        async with _threaded_context(upload_slot), _threaded_context(lambda: tempfile.TemporaryDirectory(prefix="rag-upload-")) as directory:
            accepted, rejected = [], []
            for index, file in enumerate(files):
                path = Path(directory) / str(index)
                size = 0
                async with _threaded_context(lambda: path.open("xb")) as target:
                    while block := await file.read(1024 * 1024):
                        size += len(block)
                        total += len(block)
                        if total > total_maximum:
                            raise bad_request(f"本批文件超过 {config.MAX_BATCH_UPLOAD_TOTAL_MB}MB，请分批上传")
                        if size <= maximum:
                            await run_in_threadpool(target.write, block)
                if size > maximum:
                    message = f"文件超过 {file_limit_mb}MB，请拆分后上传"
                    if not partial:
                        raise bad_request(message)
                    rejected.append({"index": index, "filename": file.filename or "upload.txt", "error": message})
                    await run_in_threadpool(path.unlink)
                else:
                    accepted.append((index, file.filename or "upload.txt", path))
            yield accepted, rejected
    finally:
        for file in files:
            await file.close()
