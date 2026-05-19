from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.workdir import layout_from_config
from active_knowledge_server.observability.logging import (
    configure_logging,
    remove_managed_handlers,
)
from active_knowledge_server.security.audit import AuditLogger, query_audit_fields


def resolve_with_workdir(tmp_path: Path, extra: ConfigDict | None = None):
    workdir = tmp_path / ".active-kb"
    overrides: ConfigDict = {"runtime": {"workdir": str(workdir)}}
    if extra:
        overrides = merge_dicts(overrides, extra)
    return resolve_config(cli_overrides=overrides, env={}, cwd=tmp_path)


def merge_dicts(low: ConfigDict, high: ConfigDict) -> ConfigDict:
    merged: ConfigDict = dict(low)
    for key, value in high.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge_dicts(existing, value)
        else:
            merged[key] = value
    return merged


def read_json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_configure_logging_creates_channel_files_and_writes_server_log(tmp_path: Path) -> None:
    resolved = resolve_with_workdir(tmp_path)
    layout = layout_from_config(resolved, cwd=tmp_path)

    setup = configure_logging(resolved.model, layout)
    server_logger = logging.getLogger(setup.logger_names["server"])
    server_logger.info("server plan ready")
    for handler in server_logger.handlers:
        handler.flush()

    assert (layout.local_logs_dir / "server.log").is_file()
    assert (layout.local_logs_dir / "indexer.log").is_file()
    assert (layout.local_logs_dir / "audit.log").is_file()
    assert (layout.local_logs_dir / "eval.log").is_file()
    assert "server plan ready" in (layout.local_logs_dir / "server.log").read_text(encoding="utf-8")

    for logger_name in setup.logger_names.values():
        remove_managed_handlers(logging.getLogger(logger_name))


def test_audit_tool_call_records_safe_contract_fields(tmp_path: Path) -> None:
    resolved = resolve_with_workdir(tmp_path)
    layout = layout_from_config(resolved, cwd=tmp_path)
    audit = AuditLogger.from_config(resolved.model, layout)
    long_source_query = "\n".join(
        [
            "please inspect this source token=super-secret-token",
            "int main(void) {",
            "  return read_sensor_secret();",
            "}",
        ]
        * 20
    )

    audit.record_tool_call(
        tool="kb_search",
        query=long_source_query,
        profile_id="mhs003_watch",
        snapshot_id="current",
        caller="unit-test",
        duration_ms=42,
        result_count=3,
        warning_codes=("compile_db.missing",),
        warning_levels=("degraded",),
        result_status="partial_ready",
        details={
            "api_key": "top-secret",
            "source_text": long_source_query,
            "source_path": "/home/gangan/Active/application/main.c",
        },
    )
    audit.close()

    payload = read_json_lines(layout.local_logs_dir / "audit.log")[0]
    raw_log = (layout.local_logs_dir / "audit.log").read_text(encoding="utf-8")

    assert payload["schema_version"] == "audit.v1"
    assert payload["event_type"] == "tool_call"
    assert payload["tool"] == "kb_search"
    assert isinstance(payload["query_hash"], str)
    assert len(str(payload["query_hash"])) == 64
    assert payload["query_preview"] is None
    assert payload["profile_id"] == "mhs003_watch"
    assert payload["caller"] == "unit-test"
    assert payload["duration_ms"] == 42
    assert payload["result_count"] == 3
    assert payload["warning_codes"] == ["compile_db.missing"]
    assert "super-secret-token" not in raw_log
    assert "top-secret" not in raw_log
    assert "read_sensor_secret" not in raw_log
    assert "/home/gangan/Active/application/main.c" not in raw_log


def test_audit_scope_records_failed_tool_call(tmp_path: Path) -> None:
    resolved = resolve_with_workdir(tmp_path)
    layout = layout_from_config(resolved, cwd=tmp_path)
    audit = AuditLogger.from_config(resolved.model, layout)

    with (
        pytest.raises(RuntimeError, match="boom"),
        audit.tool_call(
            tool="code_resolve",
            query="where is app_manager_start?",
            caller="unit-test",
        ) as scope,
    ):
        scope.add_warning("retrieval.low_confidence", level="caution")
        raise RuntimeError("boom password=do-not-log")

    audit.close()
    payload = read_json_lines(layout.local_logs_dir / "audit.log")[0]
    raw_log = (layout.local_logs_dir / "audit.log").read_text(encoding="utf-8")

    assert payload["success"] is False
    assert payload["result_status"] == "error"
    assert payload["warning_codes"] == ["retrieval.low_confidence"]
    assert payload["query_preview"] == "where is app_manager_start?"
    assert "do-not-log" not in raw_log


def test_short_query_preview_redacts_inline_secret() -> None:
    fields = query_audit_fields("lookup token=super-secret")

    assert fields["query_hash"]
    assert fields["query_preview"] == "lookup token=***REDACTED***"


def test_audit_log_rotation_uses_runtime_config(tmp_path: Path) -> None:
    resolved = resolve_with_workdir(
        tmp_path,
        {
            "runtime": {
                "logging": {
                    "rotation": {
                        "enabled": True,
                        "max_bytes": 256,
                        "backup_count": 2,
                    }
                }
            }
        },
    )
    layout = layout_from_config(resolved, cwd=tmp_path)
    audit = AuditLogger.from_config(resolved.model, layout)

    for index in range(20):
        audit.record_ops_operation(
            operation="index.plan",
            caller="unit-test",
            details={"batch": index, "message": "x" * 80},
        )
    audit.close()

    rotated = list(layout.local_logs_dir.glob("audit.log.*"))
    assert rotated
