"""Public runtime branding, historical records and credential redaction."""

import json
import logging
import sys

from sqlalchemy import select

from app.core.branding import public_metadata, public_text


def test_system_metadata_uses_public_name_without_rewriting_references():
    original = {
        "backend": "opencode", "message": "OpenCode unavailable; OPENCODE_MODEL",
        "session_id": "opencode-session", "model": "vendor/opencode-model",
        "diagnostics": [{"detail": "OPENCODE_BASE_URL is invalid"}],
        "evidence": [{"document_id": "opencode-doc", "quote": "OpenCode source text"}],
    }
    result = public_metadata(original)
    assert result["backend"] == "codecode"
    assert result["message"] == "codecode unavailable; CODECODE_MODEL"
    assert result["diagnostics"][0]["detail"] == "CODECODE_BASE_URL is invalid"
    assert result["session_id"] == original["session_id"]
    assert result["model"] == original["model"]
    assert result["evidence"] == original["evidence"]
    assert original["backend"] == "opencode"
    assert public_text("openCODE") == "codecode"


def test_structured_logs_brand_message_logger_and_traceback_and_redact_secrets():
    from app.core.logger import StructuredFormatter

    try:
        raise RuntimeError("OpenCode failed password=private-password")
    except RuntimeError:
        record = logging.LogRecord(
            "app.agents.opencode", logging.ERROR, "", 1,
            "OPENCODE_MODEL failed: %s", ("sk-abcdefghijklmnop",), sys.exc_info(),
        )
    output = StructuredFormatter().format(record)
    parsed = json.loads(output)
    assert parsed["logger"] == "app.agents.codecode"
    assert "CODECODE_MODEL" in parsed["message"]
    assert "codecode failed" in parsed["exception"]
    assert "opencode" not in output.lower()
    assert "private-password" not in output and "sk-abcdefghijklmnop" not in output


def test_error_responses_use_public_runtime_name():
    from app.api.errors import error_response

    result = json.loads(error_response("OPENCODE_UNAVAILABLE", "OpenCode请求失败").body)
    assert result == {"error": {"code": "CODECODE_UNAVAILABLE", "message": "codecode请求失败"}}


def test_existing_server_log_format_is_branded_including_exceptions():
    from app.core.logger import PublicFormatter

    try:
        raise ValueError("OpenCode startup failed")
    except ValueError:
        record = logging.LogRecord(
            "app.agents.opencode", logging.ERROR, "", 1,
            "OPENCODE_BASE_URL is invalid", (), sys.exc_info(),
        )
    output = PublicFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s")).format(record)
    assert output.startswith("ERROR app.agents.codecode CODECODE_BASE_URL is invalid")
    assert "codecode startup failed" in output and "opencode" not in output.lower()


def test_historical_run_and_artifact_serialization_brand_runtime_metadata(db_engine):
    from app.core.database import SessionLocal
    from app.core.knowledge_bases import DEFAULT_KNOWLEDGE_BASE_ID as KB
    from app.models.agent_run import AgentRun, AgentRunEvent
    from app.services.agent_workflow_service import AgentWorkflowService
    from tests.services.test_agent_workflows import seed

    with SessionLocal() as db:
        seed(db)
        run = AgentRun(
            knowledge_base_id=KB, requested_by="workflow-user", task="business_analysis",
            status="failed", backend="opencode", error_message="OpenCode请求失败",
            session_id="opencode-session", model="vendor/opencode-model",
            output_json={"diagnostics": [{"message": "OpenCode执行失败"}]},
        )
        db.add(run)
        db.flush()
        event = AgentRunEvent(
            knowledge_base_id=KB, run_id=run.id, sequence=1, type="session",
            message="OpenCode会话已创建", data_json={"backend": "opencode"},
        )
        db.add(event)
        db.commit()
        service = AgentWorkflowService(db, KB, task_queue=object())
        result = service.serialize(run)
        assert result["backend"] == "codecode"
        assert result["error_message"] == "codecode请求失败"
        assert result["output"]["diagnostics"][0]["message"] == "codecode执行失败"
        assert result["events"][0]["message"] == "codecode会话已创建"
        assert result["events"][0]["data"]["backend"] == "codecode"
        assert result["session_id"] == "opencode-session"
        assert result["model"] == "vendor/opencode-model"
        assert run.backend == "opencode" and event.message == "OpenCode会话已创建"
        service.event(run, "info", "OpenCode已就绪", {"backend": "opencode"})
        new_event = db.scalar(select(AgentRunEvent).where(
            AgentRunEvent.run_id == run.id, AgentRunEvent.sequence == 2,
        ))
        assert new_event.message == "codecode已就绪"
        assert new_event.data_json["backend"] == "codecode"
