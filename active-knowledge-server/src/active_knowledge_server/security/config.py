"""Fail-safe startup security configuration validation."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.models import QueryResult, Warning as QueryWarning


@dataclass(frozen=True)
class SecurityBlockedWarning:
    """Structured blocked-level warning for fail-safe startup checks."""

    code: str
    message: str
    suggested_action: str
    details: Mapping[str, Any] | None = None

    def to_warning(self) -> QueryWarning:
        """Return the shared warning model used by QueryResult."""

        return QueryWarning(
            level="blocked",
            code=self.code,
            message=self.message,
            details=dict(self.details or {}),
            actionable=True,
            suggested_action=self.suggested_action,
            affected_sources=(),
            evidence_refs=(),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable warning."""

        return self.to_warning().to_dict()


@dataclass(frozen=True)
class SecurityValidationResult:
    """Result of fail-safe startup config validation."""

    warnings: tuple[SecurityBlockedWarning, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether startup may continue."""

        return not self.warnings

    @property
    def blocked(self) -> bool:
        """Whether startup must be blocked."""

        return bool(self.warnings)

    def to_blocked_response(self) -> dict[str, Any]:
        """Return the shared structured blocked response shape."""

        codes = [warning.code for warning in self.warnings]
        return QueryResult.blocked(
            tool_name="serve",
            summary="Startup was blocked by fail-safe security configuration.",
            warnings=tuple(warning.to_warning() for warning in self.warnings),
            next_queries=(
                "Fix the blocked security configuration and restart active-kb serve.",
            ),
            diagnostics={
                "blocked_reason": "security_config",
                "warning_codes": codes,
            },
        ).to_dict()


class SecurityConfigError(ValueError):
    """Raised when fail-safe startup security validation blocks startup."""

    def __init__(self, result: SecurityValidationResult) -> None:
        self.result = result
        message = "; ".join(warning.message for warning in result.warnings)
        super().__init__(message)


def validate_startup_security(
    config: ActiveKnowledgeConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> SecurityValidationResult:
    """Validate fail-safe security constraints after config merge."""

    environment = env or os.environ
    warnings: list[SecurityBlockedWarning] = []
    mode = config.deployment_mode
    transport = config.server.transport
    http = config.server.http
    is_http = transport == "streamable-http"
    host_is_loopback = is_loopback_host(http.host)

    if mode == "local_single_user":
        if is_http and not host_is_loopback:
            warnings.append(
                SecurityBlockedWarning(
                    code="security.remote_insecure_config",
                    message="local_single_user HTTP transport may only bind loopback hosts.",
                    suggested_action=(
                        "Use 127.0.0.1, ::1, or localhost, or switch to remote_shared "
                        "and enable authentication, Origin checks, and audit."
                    ),
                    details={"host": http.host, "transport": transport},
                )
            )
        return SecurityValidationResult(tuple(warnings))

    if mode == "remote_shared":
        if transport != "streamable-http":
            warnings.append(
                SecurityBlockedWarning(
                    code="security.remote_insecure_config",
                    message="remote_shared requires streamable-http transport.",
                    suggested_action="Set server.transport=streamable-http.",
                    details={"transport": transport},
                )
            )
        if is_http and not host_is_loopback and not http.require_auth:
            warnings.append(
                SecurityBlockedWarning(
                    code="security.auth_required",
                    message="Non-loopback HTTP transport requires authentication.",
                    suggested_action=(
                        "Set server.http.require_auth=true and configure an auth provider."
                    ),
                    details={"host": http.host, "transport": transport},
                )
            )
        if not http.require_auth:
            warnings.append(
                SecurityBlockedWarning(
                    code="security.auth_required",
                    message="remote_shared requires authenticated HTTP transport.",
                    suggested_action=(
                        "Set server.http.require_auth=true and configure auth_provider."
                    ),
                    details={"auth_provider": http.auth_provider},
                )
            )
        if http.require_auth and not token_source_available(config, environment):
            warnings.append(
                SecurityBlockedWarning(
                    code="security.auth_required",
                    message="Token authentication is enabled but no token source is available.",
                    suggested_action=(
                        "Set server.http.token.env and provide that environment variable "
                        "before starting the server."
                    ),
                    details={"auth_provider": http.auth_provider},
                )
            )
        if has_blocked_origin(http.allowed_origins):
            warnings.append(
                SecurityBlockedWarning(
                    code="security.origin_blocked",
                    message="remote_shared requires explicit non-wildcard allowed_origins.",
                    suggested_action=(
                        "Set server.http.allowed_origins to concrete trusted HTTPS origins."
                    ),
                    details={"allowed_origins": list(http.allowed_origins)},
                )
            )
        if not config.security.audit.enabled:
            warnings.append(
                SecurityBlockedWarning(
                    code="security.audit_required",
                    message="remote_shared requires audit logging to be enabled.",
                    suggested_action="Set security.audit.enabled=true.",
                )
            )
        if config.server.expose_ops_tools:
            warnings.append(
                SecurityBlockedWarning(
                    code="security.ops_exposure_blocked",
                    message="remote_shared may not expose ops tools in V1.",
                    suggested_action=(
                        "Set server.expose_ops_tools=false or use local_single_user mode."
                    ),
                )
            )
        return SecurityValidationResult(tuple(warnings))

    return SecurityValidationResult(
        (
            SecurityBlockedWarning(
                code="schema.invalid_request",
                message=f"Unsupported deployment_mode: {mode}",
                suggested_action="Use deployment_mode=local_single_user or remote_shared.",
            ),
        )
    )


def token_source_available(
    config: ActiveKnowledgeConfig,
    env: Mapping[str, str],
) -> bool:
    """Return whether the configured auth provider has a usable token source."""

    http = config.server.http
    if http.auth_provider != "token":
        return http.auth_provider not in {"none", ""}
    if http.token is None or not http.token.env:
        return False
    return bool(env.get(http.token.env))


def is_loopback_host(host: str) -> bool:
    """Return whether an HTTP bind host is loopback-only."""

    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def has_blocked_origin(origins: list[str]) -> bool:
    """Return whether remote origins are absent, empty, or wildcarded."""

    if not origins:
        return True
    for origin in origins:
        normalized = origin.strip().lower()
        if not normalized or "*" in normalized:
            return True
    return False


def raise_if_blocked(result: SecurityValidationResult) -> None:
    """Raise a structured exception when security validation blocks startup."""

    if result.blocked:
        raise SecurityConfigError(result)
