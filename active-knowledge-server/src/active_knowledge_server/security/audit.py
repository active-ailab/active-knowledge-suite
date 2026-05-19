"""Structured audit logging boundary."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import Handler, Logger
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal, Self, TypeAlias

from active_knowledge_server.config.schema import ActiveKnowledgeConfig, LogRotationConfig
from active_knowledge_server.config.workdir import WorkdirLayout
from active_knowledge_server.observability.logging import (
    build_file_handler,
    mark_managed_handler,
    remove_managed_handlers,
)

AuditValue: TypeAlias = (
    str | int | float | bool | None | list["AuditValue"] | dict[str, "AuditValue"]
)
AuditEventType = Literal["tool_call", "ops"]

AUDIT_SCHEMA_VERSION: Final = "audit.v1"
MAX_QUERY_PREVIEW_CHARS: Final = 160
MAX_DETAIL_STRING_CHARS: Final = 256
MAX_LARGE_TEXT_CHARS: Final = 512
MAX_COLLECTION_ITEMS: Final = 50
_AUDIT_LOGGER_PREFIX: Final = "active_knowledge_server.audit"
_SENSITIVE_KEY_PARTS: Final = (
    "api_key",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-=]+"),
    re.compile(r"(?i)\b(api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)
_ABSOLUTE_PATH_RE: Final = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[^/\s]+/){2,}[^,\s:]+")


@dataclass
class AuditToolCallScope:
    """Context manager that guarantees one audit row per tool invocation."""

    audit_logger: AuditLogger
    tool: str
    query: str | None = None
    profile_id: str | None = None
    snapshot_id: str | None = None
    caller: str | None = None
    request_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    result_status: str = "ok"
    result_count: int = 0
    warning_codes: list[str] = field(default_factory=list)
    warning_levels: list[str] = field(default_factory=list)
    _started_at: float = field(default=0.0, init=False, repr=False)

    def __enter__(self) -> Self:
        """Start timing the audited tool call."""

        self._started_at = time.perf_counter()
        return self

    def set_result(
        self,
        *,
        result_count: int | None = None,
        result_status: str | None = None,
        warning_codes: tuple[str, ...] | list[str] = (),
        warning_levels: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Attach result metadata gathered by the tool implementation."""

        if result_count is not None:
            self.result_count = result_count
        if result_status is not None:
            self.result_status = result_status
        self.warning_codes.extend(warning_codes)
        self.warning_levels.extend(warning_levels)

    def add_warning(self, code: str, *, level: str | None = None) -> None:
        """Attach one warning to the audited tool call."""

        self.warning_codes.append(code)
        if level is not None:
            self.warning_levels.append(level)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Write the audit event and preserve the original exception behavior."""

        success = exc_type is None
        details = dict(self.details)
        result_status = self.result_status
        if exc_type is not None:
            result_status = "error"
            details["error_kind"] = exc_type.__name__
            details["error_summary"] = (
                str(exc_value) if exc_value is not None else exc_type.__name__
            )

        self.audit_logger.record_tool_call(
            tool=self.tool,
            query=self.query,
            profile_id=self.profile_id,
            snapshot_id=self.snapshot_id,
            caller=self.caller,
            duration_ms=elapsed_ms(self._started_at),
            result_count=self.result_count,
            warning_codes=tuple(self.warning_codes),
            warning_levels=tuple(self.warning_levels),
            result_status=result_status,
            request_id=self.request_id,
            success=success,
            details=details,
        )
        return False


class AuditLogger:
    """JSONL audit logger with redaction and optional rotation."""

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool,
        rotation: LogRotationConfig,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self._logger: Logger | None = None
        if enabled:
            self._logger = configure_audit_logger(path, rotation)

    @classmethod
    def from_config(cls, config: ActiveKnowledgeConfig, layout: WorkdirLayout) -> AuditLogger:
        """Create an audit logger from validated config and workdir layout."""

        return cls(
            layout.local_logs_dir / "audit.log",
            enabled=config.security.audit.enabled,
            rotation=config.runtime.logging.rotation,
        )

    def tool_call(
        self,
        *,
        tool: str,
        query: str | None = None,
        profile_id: str | None = None,
        snapshot_id: str | None = None,
        caller: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditToolCallScope:
        """Return a context manager for auditing one tool call."""

        return AuditToolCallScope(
            audit_logger=self,
            tool=tool,
            query=query,
            profile_id=profile_id,
            snapshot_id=snapshot_id,
            caller=caller,
            request_id=request_id,
            details=details or {},
        )

    def record_tool_call(
        self,
        *,
        tool: str,
        query: str | None = None,
        profile_id: str | None = None,
        snapshot_id: str | None = None,
        caller: str | None = None,
        duration_ms: int | None = None,
        result_count: int | None = None,
        warning_codes: tuple[str, ...] | list[str] = (),
        warning_levels: tuple[str, ...] | list[str] = (),
        result_status: str | None = None,
        request_id: str | None = None,
        success: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record a tool-call audit event."""

        query_fields = query_audit_fields(query)
        event = base_event("tool_call", request_id=request_id)
        event.update(
            {
                "tool": tool,
                "query_hash": query_fields["query_hash"],
                "query_preview": query_fields["query_preview"],
                "profile_id": profile_id,
                "snapshot_id": snapshot_id,
                "caller": caller,
                "duration_ms": duration_ms,
                "result_count": result_count,
                "warning_codes": list(warning_codes),
                "warning_levels": list(warning_levels),
                "result_status": result_status,
                "success": success,
                "details": sanitize_for_audit(details or {}),
            }
        )
        self.record(event)

    def record_ops_operation(
        self,
        *,
        operation: str,
        caller: str | None = None,
        duration_ms: int | None = None,
        success: bool = True,
        warning_codes: tuple[str, ...] | list[str] = (),
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record an operational command or sensitive action."""

        event = base_event("ops", request_id=request_id)
        event.update(
            {
                "ops_operation": operation,
                "caller": caller,
                "duration_ms": duration_ms,
                "success": success,
                "warning_codes": list(warning_codes),
                "details": sanitize_for_audit(details or {}),
            }
        )
        self.record(event)

    def record(self, event: dict[str, AuditValue]) -> None:
        """Write a sanitized audit event as one JSON line."""

        if not self.enabled or self._logger is None:
            return
        payload = sanitize_for_audit(event)
        self._logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def close(self) -> None:
        """Flush and close handlers held by this audit logger."""

        if self._logger is None:
            return
        for handler in tuple(self._logger.handlers):
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)


def configure_audit_logger(path: Path, rotation: LogRotationConfig) -> Logger:
    """Configure a dedicated JSONL audit logger for one file path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    logger_name = f"{_AUDIT_LOGGER_PREFIX}.{hashlib.sha256(str(path).encode()).hexdigest()[:12]}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    remove_managed_handlers(logger)

    handler = build_file_handler(path, rotation)
    handler.setFormatter(logging.Formatter("%(message)s"))
    mark_managed_handler(handler)
    logger.addHandler(handler)
    return logger


def base_event(event_type: AuditEventType, *, request_id: str | None) -> dict[str, AuditValue]:
    """Build common audit event fields."""

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "request_id": request_id,
    }


def query_audit_fields(query: str | None) -> dict[str, str | None]:
    """Return query hash plus a safe short preview when available."""

    if query is None:
        return {"query_hash": None, "query_preview": None}

    normalized = normalize_query(query)
    preview: str | None = None
    if is_safe_short_query(query, normalized):
        preview = redact_text(normalized)[:MAX_QUERY_PREVIEW_CHARS]
    return {
        "query_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "query_preview": preview,
    }


def normalize_query(query: str) -> str:
    """Normalize whitespace before hashing or previewing a query."""

    return " ".join(query.strip().split())


def is_safe_short_query(original: str, normalized: str) -> bool:
    """Return whether a query can be stored as a short audit preview."""

    if not normalized:
        return False
    if len(normalized) > MAX_QUERY_PREVIEW_CHARS:
        return False
    return original.count("\n") <= 1


def sanitize_for_audit(value: Any, *, key: str | None = None) -> AuditValue:
    """Return a JSON-compatible value safe for audit logs."""

    if is_sensitive_key(key):
        return "***REDACTED***"
    if isinstance(value, dict):
        sanitized: dict[str, AuditValue] = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                sanitized["__truncated__"] = True
                break
            sanitized[str(item_key)] = sanitize_for_audit(item_value, key=str(item_key))
        return sanitized
    if isinstance(value, list | tuple | set):
        items = list(value)
        sanitized_items = [
            sanitize_for_audit(item, key=key) for item in items[:MAX_COLLECTION_ITEMS]
        ]
        if len(items) > MAX_COLLECTION_ITEMS:
            sanitized_items.append({"__truncated__": True})
        return sanitized_items
    if isinstance(value, bool | int | float) or value is None:
        return value
    return sanitize_text_scalar(str(value), key=key)


def sanitize_text_scalar(value: str, *, key: str | None) -> str:
    """Redact sensitive scalar text and suppress large source-like blocks."""

    redacted = redact_text(value)
    if key is not None and "path" in key.lower():
        redacted = redact_absolute_paths(redacted)
    if len(redacted) > MAX_LARGE_TEXT_CHARS or redacted.count("\n") >= 3:
        return "[redacted:large_text]"
    if len(redacted) > MAX_DETAIL_STRING_CHARS:
        return f"{redacted[:MAX_DETAIL_STRING_CHARS]}...<truncated>"
    return redacted


def redact_text(value: str) -> str:
    """Apply secret redaction patterns to text."""

    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(redact_match, redacted)
    return redact_absolute_paths(redacted)


def redact_match(match: re.Match[str]) -> str:
    """Preserve key names where useful while redacting their values."""

    text = match.group(0)
    if "=" in text:
        return f"{text.split('=', 1)[0]}=***REDACTED***"
    if ":" in text:
        return f"{text.split(':', 1)[0]}:***REDACTED***"
    if text.lower().startswith("bearer "):
        return "Bearer ***REDACTED***"
    return "***REDACTED***"


def redact_absolute_paths(value: str) -> str:
    """Avoid writing raw deep absolute paths into audit details."""

    return _ABSOLUTE_PATH_RE.sub("[redacted:absolute_path]", value)


def is_sensitive_key(key: str | None) -> bool:
    """Return whether a mapping key is secret-bearing."""

    if key is None:
        return False
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def elapsed_ms(started_at: float) -> int:
    """Return elapsed wall time in milliseconds."""

    return max(0, int((time.perf_counter() - started_at) * 1000))


def close_handlers(logger: Logger) -> None:
    """Close all handlers for tests and short-lived CLIs."""

    for handler in tuple(logger.handlers):
        close_handler(logger, handler)


def close_handler(logger: Logger, handler: Handler) -> None:
    """Flush, close, and detach one handler."""

    handler.flush()
    logger.removeHandler(handler)
    handler.close()
