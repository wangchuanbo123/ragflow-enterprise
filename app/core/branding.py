"""Names used by application diagnostics and presentation metadata."""

import re

_RUNTIME_NAME = re.compile("opencode", re.IGNORECASE)
_REFERENCE_KEYS = {
    "id", "model", "url", "base_url", "path", "filename", "name",
    "instructions", "quote", "text", "content", "question", "answer",
    "runtime_profile", "comparison_profiles", "source", "source_path",
}


def public_text(value: str) -> str:
    """Use the product name, including uppercase environment variable names."""
    return _RUNTIME_NAME.sub(
        lambda match: "CODECODE" if match.group().isupper() else "codecode", value
    )


def public_metadata(value):
    """Brand system metadata without changing IDs, model references or source text."""
    if isinstance(value, dict):
        return {
            key: item if (
                str(key).lower() in _REFERENCE_KEYS
                or str(key).lower().endswith(("_id", "_ids", "_url", "_path"))
            ) else public_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [public_metadata(item) for item in value]
    if isinstance(value, str):
        return public_text(value)
    return value
