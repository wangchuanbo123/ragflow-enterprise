"""Resolve connector credential references without persisting raw secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path


class CredentialResolver:
    """Supports environment and mounted-secret-file references."""

    @staticmethod
    def resolve(connection_ref: str | None) -> dict:
        if not connection_ref:
            return {}
        if connection_ref.startswith("env://"):
            name = connection_ref.removeprefix("env://").strip()
            if not name or not name.replace("_", "").isalnum():
                raise ValueError("无效的环境变量凭据引用")
            raw = os.getenv(name)
            if raw is None:
                raise ValueError(f"凭据环境变量不存在：{name}")
        elif connection_ref.startswith("file://"):
            path = Path(connection_ref.removeprefix("file://")).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise ValueError("凭据文件必须是存在的绝对路径")
            if path.stat().st_size > 64 * 1024:
                raise ValueError("凭据文件超过 64KB")
            raw = path.read_text(encoding="utf-8")
        else:
            raise ValueError("connection_ref 仅支持 env:// 或 file:// 引用")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"token": raw.strip()}
        if not isinstance(value, dict):
            raise ValueError("凭据内容必须是 JSON 对象")
        return value
