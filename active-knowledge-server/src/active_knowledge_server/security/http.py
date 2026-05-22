"""HTTP transport security middleware for MCP streamable-http deployments."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.security.audit import AuditLogger
from active_knowledge_server.security.config import is_loopback_host

_AUTH_SCHEME_PREFIX: Final = "bearer "


@dataclass(frozen=True)
class HTTPRequestSecurityContext:
    """Resolved security dependencies shared by the HTTP middleware."""

    config: ActiveKnowledgeConfig
    audit_logger: AuditLogger
    env: Mapping[str, str]


class HTTPSecurityMiddleware(BaseHTTPMiddleware):
    """Enforce Origin and bearer-token checks for the HTTP MCP transport."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        context: HTTPRequestSecurityContext,
    ) -> None:
        super().__init__(app)
        self._context = context

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """Validate one HTTP request before the FastMCP app handles it."""

        started_at = time.perf_counter()
        request_id = str(uuid.uuid4())
        details = _request_audit_details(request)

        try:
            blocked = _origin_blocking_response(request, self._context)
            if blocked is not None:
                _record_http_audit(
                    self._context.audit_logger,
                    request_id=request_id,
                    started_at=started_at,
                    success=False,
                    warning_codes=("security.origin_blocked",),
                    details={**details, "blocked_reason": "security.origin_blocked"},
                )
                return blocked

            blocked = _auth_blocking_response(request, self._context)
            if blocked is not None:
                _record_http_audit(
                    self._context.audit_logger,
                    request_id=request_id,
                    started_at=started_at,
                    success=False,
                    warning_codes=("security.auth_required",),
                    details={**details, "blocked_reason": "security.auth_required"},
                )
                return blocked

            response = await call_next(request)
            _record_http_audit(
                self._context.audit_logger,
                request_id=request_id,
                started_at=started_at,
                success=response.status_code < 400,
                details={
                    **details,
                    "response_status": response.status_code,
                },
            )
            return response
        except Exception as exc:  # noqa: BLE001 - preserve error behavior after auditing.
            _record_http_audit(
                self._context.audit_logger,
                request_id=request_id,
                started_at=started_at,
                success=False,
                details={
                    **details,
                    "error_kind": exc.__class__.__name__,
                    "error_summary": str(exc),
                },
            )
            raise


def build_http_security_middleware(
    *,
    config: ActiveKnowledgeConfig,
    audit_logger: AuditLogger,
    env: Mapping[str, str] | None = None,
) -> list[Any]:
    """Return HTTP middleware entries for FastMCP's streamable-http app."""

    if config.server.transport != "streamable-http":
        return []

    context = HTTPRequestSecurityContext(
        config=config,
        audit_logger=audit_logger,
        env=env or os.environ,
    )
    return [{"middleware_class": HTTPSecurityMiddleware, "context": context}]


def fastmcp_http_middleware_entries(
    *,
    config: ActiveKnowledgeConfig,
    audit_logger: AuditLogger,
    env: Mapping[str, str] | None = None,
) -> list[Any]:
    """Return Starlette Middleware entries in the shape expected by FastMCP."""

    from starlette.middleware import Middleware

    return [
        Middleware(item["middleware_class"], context=item["context"])
        for item in build_http_security_middleware(
            config=config,
            audit_logger=audit_logger,
            env=env,
        )
    ]


def _origin_blocking_response(
    request: Request,
    context: HTTPRequestSecurityContext,
) -> JSONResponse | None:
    """Return a blocked response when the HTTP Origin is not allowed."""

    origin = request.headers.get("origin")
    if not origin:
        return _blocked_response(
            status_code=403,
            code="security.origin_blocked",
            message="HTTP Origin header is required for MCP HTTP transport.",
            suggested_action="Send an Origin header that matches server.http.allowed_origins.",
        )

    normalized_origin = origin.strip().lower()
    allowed_origins = {value.strip().lower() for value in context.config.server.http.allowed_origins}
    if normalized_origin not in allowed_origins:
        return _blocked_response(
            status_code=403,
            code="security.origin_blocked",
            message="HTTP Origin is not allowed by the configured MCP server policy.",
            suggested_action="Use a trusted Origin from server.http.allowed_origins or update the server config.",
            details={
                "origin": origin,
                "allowed_origins": sorted(allowed_origins),
            },
        )
    return None


def _auth_blocking_response(
    request: Request,
    context: HTTPRequestSecurityContext,
) -> JSONResponse | None:
    """Return a blocked response when the configured auth policy is not satisfied."""

    config = context.config
    http = config.server.http
    if not http.require_auth:
        return None
    if http.auth_provider != "token":
        return _blocked_response(
            status_code=401,
            code="security.auth_required",
            message="Only token auth is implemented for the HTTP transport in V1.",
            suggested_action="Configure server.http.auth_provider=token for V1 HTTP deployments.",
            details={"auth_provider": http.auth_provider},
        )
    expected_token = _configured_token(http.token.env if http.token is not None else None, context.env)
    if expected_token is None:
        return _blocked_response(
            status_code=401,
            code="security.auth_required",
            message="Bearer token authentication is enabled but no token value is available.",
            suggested_action="Provide the configured server.http.token.env value before starting the server.",
        )

    header_name = http.token.header if http.token is not None else "Authorization"
    scheme = http.token.scheme if http.token is not None else "Bearer"
    header_value = request.headers.get(header_name)
    if not header_value:
        return _blocked_response(
            status_code=401,
            code="security.auth_required",
            message="Bearer token authentication is required for this MCP HTTP request.",
            suggested_action=f"Send {header_name}: {scheme} <token>.",
            details={"header": header_name, "scheme": scheme},
        )

    token = _extract_bearer_token(header_value=header_value, scheme=scheme)
    if token is None or token != expected_token:
        return _blocked_response(
            status_code=401,
            code="security.auth_required",
            message="Bearer token authentication failed for this MCP HTTP request.",
            suggested_action=f"Refresh the configured {scheme} token and retry the request.",
            details={"header": header_name, "scheme": scheme},
        )
    return None


def _configured_token(env_name: str | None, env: Mapping[str, str]) -> str | None:
    """Return the configured bearer token value from the environment."""

    if env_name is None or not env_name.strip():
        return None
    value = env.get(env_name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _extract_bearer_token(*, header_value: str, scheme: str) -> str | None:
    """Return the token when the Authorization-like header matches the expected scheme."""

    expected_prefix = f"{scheme.strip().lower()} "
    normalized = header_value.strip()
    if not normalized.lower().startswith(expected_prefix):
        return None
    token = normalized[len(expected_prefix) :].strip()
    return token or None


def _request_audit_details(request: Request) -> dict[str, Any]:
    """Build safe HTTP request audit details for one MCP transport request."""

    host = request.headers.get("host")
    origin = request.headers.get("origin")
    forwarded_for = request.headers.get("x-forwarded-for")
    client_host = request.client.host if request.client is not None else None
    return {
        "transport": "mcp_http",
        "http_method": request.method,
        "path": request.url.path,
        "query_string": request.url.query,
        "origin": origin,
        "host": host,
        "client_host": client_host,
        "client_scope": "loopback" if client_host and is_loopback_host(client_host) else "remote",
        "origin_allowed": origin is not None,
        "auth_header_present": "authorization" in request.headers,
        "auth_header_digest": _header_hash(request.headers.get("authorization")),
        "forwarded_for": forwarded_for,
    }


def _header_hash(value: str | None) -> str | None:
    """Return a stable digest for sensitive header presence without logging the raw value."""

    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record_http_audit(
    audit_logger: AuditLogger,
    *,
    request_id: str,
    started_at: float,
    success: bool,
    warning_codes: tuple[str, ...] = (),
    details: dict[str, Any],
) -> None:
    """Record one HTTP request audit row through the existing ops audit channel."""

    audit_logger.record_ops_operation(
        operation="http.request",
        caller="mcp.http",
        duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
        success=success,
        warning_codes=warning_codes,
        request_id=request_id,
        details=details,
    )


def _blocked_response(
    *,
    status_code: int,
    code: str,
    message: str,
    suggested_action: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Return the shared blocked JSON envelope for HTTP security failures."""

    payload = {
        "result_status": "blocked",
        "summary": "HTTP request was blocked by MCP transport security policy.",
        "items": [],
        "evidence_refs": [],
        "warnings": [
            {
                "level": "blocked",
                "code": code,
                "message": message,
                "details": details or {},
                "actionable": True,
                "suggested_action": suggested_action,
                "affected_sources": [],
                "evidence_refs": [],
            }
        ],
        "diagnostics": {
            "blocked_reason": code,
        },
        "next_queries": [],
    }
    return JSONResponse(payload, status_code=status_code)