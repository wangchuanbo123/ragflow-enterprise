"""Local directory, Git, and S3/MinIO connector orchestration."""

from __future__ import annotations

import subprocess
import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.config import (
    CONNECTOR_GIT_TIMEOUT_SECONDS,
    CONNECTOR_MAX_DOWNLOAD_MB,
    INDEX_JOB_MAX_ATTEMPTS,
    LOCAL_SOURCE_ALLOWED_ROOTS,
    MAX_BATCH_UPLOAD_FILES,
    MAX_DOCUMENT_SIZE_MB,
)
from app.models.index_job import IndexJob
from app.repositories.index_job_repository import IndexJobRepository
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.services.credential_service import CredentialResolver
from rag.loaders.document_loader import SUPPORTED_EXTENSIONS


def _utcnow() -> datetime:
    return datetime.utcnow()


class DataSourceService:
    def __init__(self, db: Session, knowledge_base_id: str, task_queue=None):
        self.db = db
        self.knowledge_base_id = knowledge_base_id
        self.repository = KnowledgeBaseRepository(db)
        if task_queue is None:
            from app.bootstrap.container import get_provider_container
            task_queue = get_provider_container().task_queue
        self.task_queue = task_queue

    def check_configuration(self, source_id: str):
        source = self.repository.get_data_source(self.knowledge_base_id, source_id)
        if source is None:
            return None
        status, detail = self._check(source.source_type, source.config_json or {}, source.connection_ref)
        source.health_status = status
        source.health_detail = detail
        source.last_checked_at = _utcnow()
        self.db.commit()
        self.db.refresh(source)
        return source

    def request_sync(self, source_id: str, requested_by: str | None) -> dict | None:
        source = self.repository.get_data_source(self.knowledge_base_id, source_id)
        if source is None:
            return None
        if source.status != "active":
            raise ValueError("数据源已停用")
        if source.source_type == "upload":
            raise ValueError("手工上传数据源无需同步")
        # Preserve the laptop workflow: local scanning is cheap and returns its
        # accepted/skipped counts immediately; actual indexing remains a
        # persisted child job. Network connectors always run in a worker.
        if source.source_type == "local_directory":
            result = self.execute_sync(source_id, requested_by)
            result["job_id"] = result.get("child_job_id", "")
            return result
        job_repository = IndexJobRepository(self.db, self.knowledge_base_id)
        existing = job_repository.find_open(
            "connector_sync", option_key="data_source_id", option_value=source.id
        )
        if existing is not None:
            return {
                "source_id": source.id, "job_id": existing.id,
                "status": "accepted", "accepted": 0, "skipped": 0, "rejected": [],
            }
        job = job_repository.create(IndexJob(
            job_type="connector_sync",
            status="pending",
            requested_by=requested_by,
            total_items=1,
            max_attempts=INDEX_JOB_MAX_ATTEMPTS,
            options_json={"data_source_id": source.id},
        ))
        source.last_sync_status = "queued"
        source.last_sync_job_id = job.id
        if source.sync_interval_minutes:
            source.next_sync_at = _utcnow() + timedelta(minutes=source.sync_interval_minutes)
        self.db.commit()
        self.task_queue.enqueue(job.id)
        return {
            "source_id": source.id,
            "job_id": job.id,
            "status": "accepted",
            "accepted": 0,
            "skipped": 0,
            "rejected": [],
        }

    def execute_sync(self, source_id: str, requested_by: str | None = None) -> dict:
        source = self.repository.get_data_source(self.knowledge_base_id, source_id)
        if source is None:
            raise ValueError("数据源不存在")
        if source.status != "active":
            raise ValueError("数据源已停用")
        config = source.config_json or {}
        status, detail = self._check(source.source_type, config, source.connection_ref)
        source.health_status = status
        source.health_detail = detail
        source.last_checked_at = _utcnow()
        self.db.commit()
        if status == "fail":
            return self._fail(source, detail)

        try:
            if source.source_type == "local_directory":
                root = self._resolve_local_directory(str(config.get("location", "")))
                files, rejected = self._collect_directory(root, bool(config.get("recursive", True)))
                cursor = {"location": str(root)}
                code_root = root
            elif source.source_type == "git":
                with tempfile.TemporaryDirectory(prefix="rag-git-") as temp_dir:
                    root, revision = self._checkout_git(source, Path(temp_dir))
                    files, rejected = self._collect_directory(root, True)
                    result = self._stage(source, files, rejected, requested_by, {"revision": revision})
                    if bool(config.get("index_code", True)):
                        from app.services.code_index_service import CodeIndexService
                        result["code_graph"] = CodeIndexService(
                            self.db, self.knowledge_base_id
                        ).index_directory(root, data_source_id=source.id, revision=revision)
                        self._refresh_code_vectors(result)
                    return self._complete(source, result, {"revision": revision})
            elif source.source_type == "s3":
                files, rejected, cursor = self._collect_s3(source)
                code_root = None
            else:
                raise ValueError("不支持的数据源类型")

            result = self._stage(source, files, rejected, requested_by, cursor)
            if code_root is not None and bool(config.get("index_code", False)):
                from app.services.code_index_service import CodeIndexService
                result["code_graph"] = CodeIndexService(
                    self.db, self.knowledge_base_id
                ).index_directory(code_root, data_source_id=source.id)
                self._refresh_code_vectors(result)
            return self._complete(source, result, cursor)
        except Exception as exc:
            self._fail(source, str(exc))
            raise

    def _stage(self, source, files, rejected, requested_by, cursor) -> dict:
        from app.services.document_index_service import DocumentIndexService

        job, document_ids, skipped, rejected = DocumentIndexService(
            self.db, self.knowledge_base_id
        ).create_data_source_sync_job(
            source.id,
            files,
            requested_by,
            rejected,
            source_type=source.source_type,
            deletion_policy=source.deletion_policy,
            sync_cursor=cursor,
        )
        return {
            "source_id": source.id,
            "child_job_id": job.id,
            "status": "partial" if rejected else ("accepted" if document_ids else "unchanged"),
            "accepted": len(document_ids),
            "skipped": len(skipped),
            "rejected": rejected,
        }

    def _complete(self, source, result: dict, cursor: dict) -> dict:
        source.last_sync_status = result["status"]
        source.last_sync_detail = (
            f"变更 {result['accepted']} 个，跳过 {result['skipped']} 个，"
            f"拒绝 {len(result['rejected'])} 个"
        )
        source.sync_cursor_json = cursor
        if result.get("child_job_id"):
            source.last_sync_job_id = result["child_job_id"]
        source.last_synced_at = _utcnow()
        if source.sync_interval_minutes:
            source.next_sync_at = _utcnow() + timedelta(minutes=source.sync_interval_minutes)
        self.db.commit()
        return result

    def _refresh_code_vectors(self, result: dict) -> None:
        from app.core.config import CODE_VECTOR_ENABLED

        if not CODE_VECTOR_ENABLED:
            return
        from app.services.code_vector_service import CodeVectorService

        result["code_vectors"] = CodeVectorService(
            self.db, self.knowledge_base_id
        ).rebuild()

    def _fail(self, source, detail: str) -> dict:
        source.last_sync_status = "failed"
        source.last_sync_detail = detail[:2000]
        source.last_synced_at = _utcnow()
        if source.sync_interval_minutes:
            source.next_sync_at = _utcnow() + timedelta(minutes=source.sync_interval_minutes)
        self.db.commit()
        return {"source_id": source.id, "status": "failed", "detail": detail}

    @staticmethod
    def _collect_directory(root: Path, recursive: bool) -> tuple[list[tuple[str, bytes]], list[dict]]:
        iterator = root.rglob("*") if recursive else root.glob("*")
        files: list[tuple[str, bytes]] = []
        rejected: list[dict] = []
        for path in sorted(iterator):
            if (
                not path.is_file() or path.is_symlink() or ".versions" in path.parts
                or path.suffix.lower() not in SUPPORTED_EXTENSIONS
            ):
                continue
            relative_path = path.relative_to(root).as_posix()
            if len(files) >= MAX_BATCH_UPLOAD_FILES:
                rejected.append({"path": relative_path, "error": "超过单次同步文件数限制"})
                continue
            try:
                if path.stat().st_size > MAX_DOCUMENT_SIZE_MB * 1024 * 1024:
                    rejected.append({"path": relative_path, "error": "文件过大"})
                    continue
                files.append((relative_path, path.read_bytes()))
            except OSError as exc:
                rejected.append({"path": relative_path, "error": str(exc)})
        return files, rejected

    def _checkout_git(self, source, target: Path) -> tuple[Path, str]:
        config = source.config_json or {}
        location = str(config["location"])
        branch = str(config.get("branch", "")).strip()
        command = ["git"]
        git_environment = os.environ.copy()
        credentials = CredentialResolver.resolve(source.connection_ref)
        token = str(credentials.get("token", "")).strip()
        if token:
            git_environment.update({
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
            })
        command.extend(["clone", "--depth", "1", "--filter=blob:none"])
        if branch:
            command.extend(["--branch", branch])
        command.extend([location, str(target / "repo")])
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=CONNECTOR_GIT_TIMEOUT_SECONDS,
            env=git_environment,
        )
        root = target / "repo"
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        subdirectory = str(config.get("subdirectory", "")).strip("/\\")
        content_root = (root / subdirectory).resolve() if subdirectory else root.resolve()
        if root.resolve() not in {content_root, *content_root.parents} or not content_root.is_dir():
            raise ValueError("Git 子目录不存在或越界")
        return content_root, revision

    def _collect_s3(self, source) -> tuple[list[tuple[str, bytes]], list[dict], dict]:
        import boto3
        from botocore.config import Config

        config = source.config_json or {}
        parsed = urlparse(str(config.get("location", "")))
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError("S3 location 必须使用 s3://bucket/prefix")
        credentials = CredentialResolver.resolve(source.connection_ref)
        client = boto3.client(
            "s3",
            endpoint_url=config.get("endpoint_url"),
            region_name=config.get("region", "us-east-1"),
            aws_access_key_id=credentials.get("access_key") or credentials.get("aws_access_key_id"),
            aws_secret_access_key=credentials.get("secret_key") or credentials.get("aws_secret_access_key"),
            aws_session_token=credentials.get("session_token"),
            config=Config(s3={"addressing_style": config.get("addressing_style", "path")}),
        )
        prefix = parsed.path.strip("/")
        files: list[tuple[str, bytes]] = []
        rejected: list[dict] = []
        token = None
        newest = None
        while len(files) < MAX_BATCH_UPLOAD_FILES:
            kwargs = {"Bucket": parsed.netloc, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []):
                key = item["Key"]
                relative = key[len(prefix):].lstrip("/") if prefix else key
                if not relative or Path(relative).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                size = int(item.get("Size", 0))
                limit_mb = min(MAX_DOCUMENT_SIZE_MB, CONNECTOR_MAX_DOWNLOAD_MB)
                if size > limit_mb * 1024 * 1024:
                    rejected.append({"path": relative, "error": "对象超过下载限制"})
                    continue
                body = client.get_object(Bucket=parsed.netloc, Key=key)["Body"].read(
                    limit_mb * 1024 * 1024 + 1
                )
                if len(body) > limit_mb * 1024 * 1024:
                    rejected.append({"path": relative, "error": "对象实际大小超过下载限制"})
                    continue
                files.append((relative, body))
                newest = max(newest or "", str(item.get("LastModified", "")))
                if len(files) >= MAX_BATCH_UPLOAD_FILES:
                    break
            if not response.get("IsTruncated") or len(files) >= MAX_BATCH_UPLOAD_FILES:
                break
            token = response.get("NextContinuationToken")
        return files, rejected, {"bucket": parsed.netloc, "prefix": prefix, "latest": newest}

    @staticmethod
    def _resolve_local_directory(location: str) -> Path:
        candidate = Path(location)
        if not candidate.is_absolute():
            candidate = LOCAL_SOURCE_ALLOWED_ROOTS[0] / candidate
        return candidate.resolve()

    @staticmethod
    def _check(source_type: str, config: dict, connection_ref: str | None) -> tuple[str, str]:
        if source_type == "upload":
            return "ok", "手工上传数据源无需外部连接"
        location = str(config.get("location", "")).strip()
        if not location:
            return "fail", "未配置数据位置"
        if source_type == "local_directory":
            candidate = DataSourceService._resolve_local_directory(location)
            if not any(candidate == root or root in candidate.parents for root in LOCAL_SOURCE_ALLOWED_ROOTS):
                return "fail", "目录不在 LOCAL_SOURCE_ALLOWED_ROOTS 允许范围内"
            if not candidate.is_dir():
                return "fail", "目录不存在或不可访问"
            return "ok", f"目录可访问：{candidate}"
        if source_type == "git":
            parsed = urlparse(location)
            if not (parsed.scheme in {"http", "https", "ssh"} or location.startswith("git@")):
                return "fail", "Git 地址必须使用 HTTPS 或 SSH"
            return "configured", "Git 地址格式正确"
        if source_type == "s3":
            if not location.startswith("s3://"):
                return "fail", "S3 地址必须使用 s3://bucket/prefix"
            if not connection_ref:
                return "fail", "S3/MinIO 数据源必须配置凭据引用"
            return "configured", "S3/MinIO 配置完整"
        return "fail", "不支持的数据源类型"
