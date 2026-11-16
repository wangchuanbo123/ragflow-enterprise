"""Server-registered connector credentials, scoped to a KB and destination."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _destination(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("连接地址不能包含用户名、密码、查询参数或片段")
    if parsed.scheme not in {"https", "http", "s3", "ssh"}:
        raise ValueError("不支持的连接协议")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


class CredentialResolver:
    """Only the operator's registry may contain env:// or file:// secret references."""

    @staticmethod
    def _registry() -> dict:
        try:
            registry = json.loads(os.getenv("CONNECTOR_CREDENTIALS_JSON", "{}"))
        except (ValueError, TypeError) as exc:
            raise ValueError("CONNECTOR_CREDENTIALS_JSON 必须是 JSON 对象") from exc
        if not isinstance(registry, dict):
            raise ValueError("CONNECTOR_CREDENTIALS_JSON 必须是 JSON 对象")
        return registry

    @classmethod
    def list_for_knowledge_base(cls, knowledge_base_id: str) -> list[dict]:
        result = []
        for credential_id, entry in cls._registry().items():
            if not isinstance(entry, dict) or knowledge_base_id not in entry.get("knowledge_base_ids", []):
                continue
            if entry.get("source_type") not in {"git", "s3"}:
                continue
            result.append({
                "id": credential_id,
                "connection_ref": f"credential://{credential_id}",
                "source_type": entry["source_type"],
                "allowed_locations": entry.get("allowed_locations", []),
            })
        return result

    @classmethod
    def validate(cls, connection_ref: str | None, *, knowledge_base_id: str,
                 source_type: str, config: dict) -> dict | None:
        if not connection_ref:
            if source_type == "s3":
                raise ValueError("S3 数据源必须选择已登记凭据")
            return None
        if not connection_ref.startswith("credential://"):
            raise ValueError("请使用服务器登记的 credential:// 凭据，不能直接引用环境变量或文件")
        credential_id = connection_ref.removeprefix("credential://")
        entry = cls._registry().get(credential_id)
        if not isinstance(entry, dict) or knowledge_base_id not in entry.get("knowledge_base_ids", []):
            raise ValueError("凭据不存在或未授权给当前知识库")
        if entry.get("source_type") != source_type:
            raise ValueError("凭据与数据源类型不匹配")
        location = _destination(str(config.get("location", "")))
        allowed = entry.get("allowed_locations")
        if not isinstance(allowed, list) or location not in {_destination(str(item)) for item in allowed}:
            raise ValueError("凭据未授权用于该连接地址")
        if source_type == "git" and urlsplit(location).scheme != "https":
            raise ValueError("Git 凭据仅支持 HTTPS 地址")
        if source_type == "s3":
            configured_endpoint = config.get("endpoint_url")
            registered_endpoint = entry.get("endpoint_url")
            if bool(configured_endpoint) != bool(registered_endpoint) or (
                configured_endpoint and _destination(str(configured_endpoint)) != _destination(str(registered_endpoint))
            ):
                raise ValueError("S3 服务地址与凭据登记的目标不匹配")
            if config.get("region", "us-east-1") != entry.get("region", "us-east-1"):
                raise ValueError("S3 区域与凭据登记的区域不匹配")
        return entry

    @classmethod
    def resolve(cls, connection_ref: str | None, *, knowledge_base_id: str,
                source_type: str, config: dict) -> dict:
        entry = cls.validate(connection_ref, knowledge_base_id=knowledge_base_id,
                             source_type=source_type, config=config)
        if entry is None:
            return {}
        secret_ref = entry.get("secret_ref", "")
        if secret_ref.startswith("env://"):
            name = secret_ref.removeprefix("env://")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("登记的凭据环境变量无效")
            raw = os.getenv(name)
            if raw is None:
                raise ValueError("登记的凭据尚未配置")
        elif secret_ref.startswith("file://"):
            path = Path(secret_ref.removeprefix("file://")).expanduser()
            if not path.is_absolute() or not path.is_file() or path.stat().st_size > 64 * 1024:
                raise ValueError("登记的凭据文件不可用或超过 64KB")
            raw = path.read_text(encoding="utf-8")
        else:
            raise ValueError("登记的 secret_ref 仅支持 env:// 或 file://")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = {"token": raw.strip()}
        if not isinstance(value, dict):
            raise ValueError("凭据内容必须是 JSON 对象")
        return value
