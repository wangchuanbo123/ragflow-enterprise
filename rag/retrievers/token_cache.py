"""Private bounded JSON token cache; safe across processes and LRU eviction.

Only tokenization is persisted. Current document content, metadata and activity
always come from the database. Each entry must match the actual content hash.
"""

import gzip
import hashlib
import json
import os
from pathlib import Path
import uuid


MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 32


class TokenCache:
    def __init__(self, directory, namespace, knowledge_base_id):
        self.directory = Path(directory).resolve()
        digest = hashlib.sha256(f"{namespace}|{knowledge_base_id}".encode()).hexdigest()
        self.path = self.directory / f"bm25-{digest}.json.gz"

    def read(self):
        try:
            if self.path.stat().st_size > MAX_BYTES:
                return {}
            with gzip.open(self.path, "rb") as stream:
                payload = stream.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                return {}
            data = json.loads(payload)
            if data.get("format") != "jieba-search-1" or not isinstance(data.get("chunks"), dict):
                return {}
            entries = data["chunks"]
            if len(entries) > 100000:
                return {}
            return {key: item for key, item in entries.items() if isinstance(key, str) and isinstance(item, dict)
                    and isinstance(item.get("sha256"), str) and isinstance(item.get("tokens"), list)
                    and all(isinstance(token, str) and len(token) <= 8192 for token in item["tokens"])}
        except (OSError, ValueError, TypeError, AttributeError, EOFError):
            return {}

    def write(self, chunks):
        payload = json.dumps({"format": "jieba-search-1", "chunks": chunks}, ensure_ascii=False, separators=(",", ":")).encode()
        if len(payload) > MAX_BYTES:
            return
        temporary = self.directory / (self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(gzip.compress(payload, compresslevel=3, mtime=0))
            os.replace(temporary, self.path)
            paths = sorted(self.directory.glob("bm25-*.json.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
            for old in paths[MAX_FILES:]:
                old.unlink(missing_ok=True)
        except OSError:
            # Cache failure never changes correctness or disables retrieval.
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def content_signature(content):
    return hashlib.sha256(content.encode()).hexdigest()
