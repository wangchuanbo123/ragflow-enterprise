"""Index configuration changes give users a concrete recovery action."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("detail", ["Legacy vector identity is unverified", "Vector collection embedding_profile_id does not match"])
def test_runtime_reports_shadow_rebuild_action(db_engine, monkeypatch, detail):
    from sqlalchemy.orm import sessionmaker
    from rag.runtime import runtime
    from rag.errors import IndexCompatibilityError
    def incompatible(**kwargs):
        raise ValueError(detail + "; private-driver-details")
    monkeypatch.setattr(runtime, "SessionLocal", sessionmaker(bind=db_engine))
    monkeypatch.setattr(runtime, "get_provider_container", lambda: SimpleNamespace(
        embedding=SimpleNamespace(get_model=lambda: object()),
        vector_store=SimpleNamespace(open=incompatible),
    ))
    runtime.get_runtime.cache_clear()
    try:
        with pytest.raises(IndexCompatibilityError) as error:
            runtime.get_runtime()
        assert error.value.code == "INDEX_REBUILD_REQUIRED"
        assert error.value.status_code == 409
        assert "重建全库索引" in str(error.value)
        assert "private-driver-details" not in str(error.value)
    finally:
        runtime.get_runtime.cache_clear()
