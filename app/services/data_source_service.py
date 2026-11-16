"""Local directory, Git, and S3 connector orchestration."""

from __future__ import annotations

import subprocess
import tempfile
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.config import (
    CONNECTOR_GIT_TIMEOUT_SECONDS,
    CONNECTOR_MAX_DOWNLOAD_MB,
    INDEX_JOB_MAX_ATTEMPTS,
    LOCAL_SOURCE_ALLOWED_ROOTS,
    LOCAL_OBJECT_STORE_DIR,
    DOC_DIR,
    MAX_BATCH_UPLOAD_FILES,
    MAX_DOCUMENT_SIZE_MB,
)
from app.models.index_job import IndexJob
from app.repositories.index_job_repository import IndexJobRepository, signal_persisted_job
from app.repositories.knowledge_base_repository import KnowledgeBaseRepository
from app.services.credential_service import CredentialResolver
from app.services.knowledge_mutation import serialized_knowledge_mutation
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

    def validate_configuration(self, source_type: str, config: dict, connection_ref: str | None) -> None:
        if connection_ref and source_type not in {"git", "s3"}:
            raise ValueError("该数据源类型无需连接凭据")
        status, detail = self._check(source_type, config, connection_ref)
        if status == "fail":
            raise ValueError(detail)

    def resolve_local_source(self, source) -> Path:
        """Revalidate saved sources at execution time, including code-index jobs."""
        if self.repository.get(self.knowledge_base_id) is None or source.status != "active":
            raise ValueError("知识库或数据源已停用")
        self.validate_configuration(source.source_type, source.config_json or {}, source.connection_ref)
        return self._resolve_local_directory(str((source.config_json or {}).get("location", "")))

    @serialized_knowledge_mutation
    def request_sync(self, source_id: str, requested_by: str | None) -> dict | None:
        if self.repository.get(self.knowledge_base_id) is None:
            raise ValueError("知识库不存在或已停用")
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
        from app.core import config as app_config
        if source.source_type == "local_directory" and app_config.APP_PROFILE in {"local", "test"}:
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
        signal_persisted_job(self.task_queue, job.id)
        return {
            "source_id": source.id,
            "job_id": job.id,
            "status": "accepted",
            "accepted": 0,
            "skipped": 0,
            "rejected": [],
        }

    @serialized_knowledge_mutation
    def execute_sync(self, source_id: str, requested_by: str | None = None) -> dict:
        if self.repository.get(self.knowledge_base_id) is None:
            raise ValueError("知识库不存在或已停用")
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
                root = self.resolve_local_source(source)
                files, rejected, cursor = self._collect_directory(
                    root, bool(config.get("recursive", True)),
                    offset=int((source.sync_cursor_json or {}).get("next_offset", 0)),
                )
                cursor["location"] = str(root)
                code_root = root
            elif source.source_type == "git":
                with tempfile.TemporaryDirectory(prefix="rag-git-") as temp_dir:
                    root, revision = self._checkout_git(source, Path(temp_dir))
                    previous = source.sync_cursor_json or {}
                    offset = int(previous.get("next_offset", 0)) if previous.get("revision") == revision else 0
                    files, rejected, cursor = self._collect_directory(root, True, offset=offset)
                    cursor["revision"] = revision
                    result = self._stage(source, files, rejected, requested_by, cursor)
                    if bool(config.get("index_code", True)):
                        from app.services.code_index_service import CodeIndexService
                        result["code_graph"] = CodeIndexService(
                            self.db, self.knowledge_base_id
                        ).index_directory(root, data_source_id=source.id, revision=revision)
                        self._refresh_code_vectors(result)
                    return self._complete(source, result, cursor)
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
            sync_cursor={key: value for key, value in cursor.items() if key != "observed_paths"},
            snapshot_complete=not rejected and cursor.get("snapshot_complete", True),
            observed_paths=cursor.get("observed_paths"),
        )
        return {
            "source_id": source.id,
            "child_job_id": job.id,
            "status": "partial" if rejected or cursor.get("has_more") else ("accepted" if document_ids else "unchanged"),
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
        source.sync_cursor_json = {key: value for key, value in cursor.items() if key != "observed_paths"}
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
    def _collect_directory(root: Path, recursive: bool, *, offset: int = 0
                           ) -> tuple[list[tuple[str, bytes]], list[dict], dict]:
        files: list[tuple[str, bytes]] = []
        rejected: list[dict] = []
        def iterate():
            if not recursive:
                yield from root.iterdir()
                return
            def on_error(exc):
                rejected.append({"path": str(exc.filename or ""), "error": "目录无法完整读取"})
            for directory, subdirectories, names in os.walk(root, followlinks=False, onerror=on_error):
                permitted = []
                for name in subdirectories:
                    child = Path(directory) / name
                    if child.is_symlink() or not child.resolve().is_relative_to(root.resolve()):
                        rejected.append({"path": child.relative_to(root).as_posix(), "error": "跳过未授权的目录链接"})
                    elif name != ".versions":
                        permitted.append(name)
                subdirectories[:] = permitted
                for name in names:
                    yield Path(directory) / name
        candidates = []
        for path in sorted(iterate()):
            if (
                not path.is_file() or path.is_symlink() or ".versions" in path.parts
                or path.suffix.lower() not in SUPPORTED_EXTENSIONS
            ):
                continue
            relative_path = path.relative_to(root).as_posix()
            # Do not follow directory junctions or symlinks out of the granted root.
            if not path.resolve().is_relative_to(root.resolve()):
                rejected.append({"path": relative_path, "error": "文件路径超出已授权目录"})
                continue
            candidates.append((relative_path, path))
        start = max(0, offset) if offset < len(candidates) else 0
        end = min(start + MAX_BATCH_UPLOAD_FILES, len(candidates))
        for relative_path, path in candidates[start:end]:
            try:
                if path.stat().st_size > MAX_DOCUMENT_SIZE_MB * 1024 * 1024:
                    rejected.append({"path": relative_path, "error": "文件过大"})
                    continue
                files.append((relative_path, path.read_bytes()))
            except OSError as exc:
                rejected.append({"path": relative_path, "error": str(exc)})
        return files, rejected, {
            "observed_paths": [name for name, _ in candidates],
            "snapshot_complete": not rejected,
            "next_offset": end if end < len(candidates) else 0,
            "has_more": end < len(candidates),
        }

    def _checkout_git(self, source, target: Path) -> tuple[Path, str]:
        config = source.config_json or {}
        self.validate_configuration("git", config, source.connection_ref)
        location = str(config["location"])
        branch = str(config.get("branch", "")).strip()
        # libcurl may read .netrc independently of Git's credential helpers.
        # Use an empty private home and disable all interactive/ambient identity
        # sources, including injected Git config and SSH authentication helpers.
        isolated_home = target / ".connector-home"
        isolated_home.mkdir(parents=True, exist_ok=True)
        empty_netrc = isolated_home / ".netrc"
        empty_netrc.write_text("", encoding="utf-8")
        (isolated_home / "_netrc").write_text("", encoding="utf-8")
        disabled_askpass = str(isolated_home / "disabled-askpass")
        command = [
            "git", "-c", "http.followRedirects=false", "-c", "http.emptyAuth=false",
            "-c", "http.proactiveAuth=none", "-c", "credential.helper=",
            "-c", "credential.interactive=false", "-c", "core.askPass=",
            "-c", f"core.hooksPath={isolated_home}", "-c", "init.templateDir=",
            "-c", "protocol.allow=never", "-c", "protocol.http.allow=always",
            "-c", "protocol.https.allow=always",
        ]
        git_environment = {key: value for key, value in os.environ.items()
                           if not key.upper().startswith(("GIT_", "SSH_"))}
        # Explicit CA configuration is deployment configuration, not an identity.
        for key in ("GIT_SSL_CAINFO", "GIT_SSL_CAPATH"):
            if key in os.environ:
                git_environment[key] = os.environ[key]
        git_environment.update({
            "HOME": str(isolated_home), "USERPROFILE": str(isolated_home),
            "CURL_HOME": str(isolated_home), "XDG_CONFIG_HOME": str(isolated_home),
            "NETRC": str(empty_netrc), "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ASKPASS": disabled_askpass,
            "SSH_ASKPASS": disabled_askpass,
        })
        credentials = CredentialResolver.resolve(source.connection_ref, knowledge_base_id=self.knowledge_base_id,
                                                 source_type="git", config=config)
        token = str(credentials.get("token", "")).strip()
        if token:
            git_environment.update({
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{location.rstrip('/')}.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token}",
            })
        command.extend(["clone", "--depth", "1", "--filter=blob:none", "--no-recurse-submodules"])
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
        credentials = CredentialResolver.resolve(source.connection_ref, knowledge_base_id=self.knowledge_base_id,
                                                 source_type="s3", config=config)
        access_key = credentials.get("access_key") or credentials.get("aws_access_key_id")
        secret_key = credentials.get("secret_key") or credentials.get("aws_secret_access_key")
        if not access_key or not secret_key:
            raise ValueError("登记的 S3 凭据缺少 access_key 或 secret_key")
        client = boto3.client(
            "s3",
            endpoint_url=config.get("endpoint_url"),
            region_name=config.get("region", "us-east-1"),
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=credentials.get("session_token"),
            config=Config(s3={"addressing_style": config.get("addressing_style", "path")}),
        )
        prefix = parsed.path.strip("/")
        if prefix:
            prefix += "/"
        files: list[tuple[str, bytes]] = []
        rejected: list[dict] = []
        token = None
        newest = None
        candidates = []
        while True:
            kwargs = {"Bucket": parsed.netloc, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []):
                key = item["Key"]
                relative = key[len(prefix):].lstrip("/") if prefix else key
                if not relative or Path(relative).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                candidates.append((relative, item))
                newest = max(newest or "", str(item.get("LastModified", "")))
            if not response.get("IsTruncated"):
                break
            next_token = response.get("NextContinuationToken")
            if not next_token or next_token == token:
                raise ValueError("S3 未返回有效分页游标，已停止同步")
            token = next_token
        candidates.sort(key=lambda item: item[0])
        offset = max(0, int((source.sync_cursor_json or {}).get("next_offset", 0)))
        start = offset if offset < len(candidates) else 0
        end = min(start + MAX_BATCH_UPLOAD_FILES, len(candidates))
        downloaded = 0
        limit_mb = min(MAX_DOCUMENT_SIZE_MB, CONNECTOR_MAX_DOWNLOAD_MB)
        for index in range(start, end):
            relative, item = candidates[index]
            size = int(item.get("Size", 0))
            if size > limit_mb * 1024 * 1024:
                rejected.append({"path": relative, "error": "对象超过下载限制"})
                continue
            if downloaded + size > CONNECTOR_MAX_DOWNLOAD_MB * 1024 * 1024:
                end = index
                break
            stream = client.get_object(Bucket=parsed.netloc, Key=item["Key"])["Body"]
            try:
                body = stream.read(limit_mb * 1024 * 1024 + 1)
            finally:
                stream.close()
            if len(body) > limit_mb * 1024 * 1024:
                rejected.append({"path": relative, "error": "对象实际大小超过下载限制"})
                continue
            downloaded += len(body)
            files.append((relative, body))
        return files, rejected, {"bucket": parsed.netloc, "prefix": prefix, "latest": newest,
                                 "snapshot_complete": not rejected,
                                 "observed_paths": [name for name, _ in candidates],
                                 "next_offset": end if end < len(candidates) else 0,
                                 "has_more": end < len(candidates)}

    @staticmethod
    def _resolve_local_directory(location: str) -> Path:
        candidate = Path(location)
        if not candidate.is_absolute():
            if not LOCAL_SOURCE_ALLOWED_ROOTS:
                raise ValueError("未配置允许访问的本地目录")
            candidate = LOCAL_SOURCE_ALLOWED_ROOTS[0] / candidate
        return candidate.resolve()

    def _local_root_allowed(self, candidate: Path) -> bool:
        upload_root = (DOC_DIR / "code_uploads" / self.knowledge_base_id).resolve()
        if candidate == upload_root or candidate.is_relative_to(upload_root):
            return True
        # Internal object stores are never connector sources, even when a broad
        # global allowed-root or a KB root would otherwise include them.
        for protected in {DOC_DIR.resolve(), LOCAL_OBJECT_STORE_DIR.resolve()}:
            if candidate == protected or candidate.is_relative_to(protected) or protected.is_relative_to(candidate):
                return False
        try:
            registry = json.loads(os.getenv("LOCAL_SOURCE_ROOTS_JSON", "{}"))
            entries = registry.get(self.knowledge_base_id, []) if isinstance(registry, dict) else []
            if not isinstance(entries, list):
                return False
            registered = [Path(item).resolve() for item in entries if isinstance(item, str) and Path(item).is_absolute()]
        except (ValueError, TypeError, OSError):
            return False
        return (any(candidate == root or candidate.is_relative_to(root) for root in registered)
                and any(candidate == root or candidate.is_relative_to(root) for root in LOCAL_SOURCE_ALLOWED_ROOTS))

    def _check(self, source_type: str, config: dict, connection_ref: str | None) -> tuple[str, str]:
        if source_type == "upload":
            return "ok", "手工上传数据源无需外部连接"
        location = str(config.get("location", "")).strip()
        if not location:
            return "fail", "未配置数据位置"
        if source_type == "local_directory":
            candidate = self._resolve_local_directory(location)
            if not self._local_root_allowed(candidate):
                return "fail", "目录未在 LOCAL_SOURCE_ROOTS_JSON 中授权给当前知识库，或属于内部对象存储"
            if not candidate.is_dir():
                return "fail", "目录不存在或不可访问"
            return "ok", f"目录可访问：{candidate}"
        if source_type == "git":
            parsed = urlparse(location)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return "fail", "Git 连接器仅支持 HTTP(S)；私有仓库请登记 HTTPS 凭据"
            if parsed.password or parsed.query or parsed.fragment or parsed.username:
                return "fail", "Git 地址不能内嵌凭据或查询参数"
            try:
                CredentialResolver.validate(connection_ref, knowledge_base_id=self.knowledge_base_id,
                                            source_type=source_type, config=config)
            except ValueError as exc:
                return "fail", str(exc)
            return "configured", "Git 地址格式正确"
        if source_type == "s3":
            if not location.startswith("s3://"):
                return "fail", "S3 地址必须使用 s3://bucket/prefix"
            try:
                CredentialResolver.validate(connection_ref, knowledge_base_id=self.knowledge_base_id,
                                            source_type=source_type, config=config)
            except ValueError as exc:
                return "fail", str(exc)
            return "configured", "S3 配置完整"
        return "fail", "不支持的数据源类型"
