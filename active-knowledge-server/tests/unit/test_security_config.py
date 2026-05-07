from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.security.config import (
    is_loopback_host,
    validate_startup_security,
)


def resolve_model(tmp_path: Path, overrides: ConfigDict):
    return resolve_config(cli_overrides=overrides, env={}, cwd=tmp_path).model


def remote_shared_overrides() -> ConfigDict:
    return {
        "deployment_mode": "remote_shared",
        "server": {
            "transport": "streamable-http",
            "expose_ops_tools": False,
            "http": {
                "host": "0.0.0.0",
                "require_auth": True,
                "auth_provider": "token",
                "token": {"env": "ACTIVE_KB_AUTH_TOKEN"},
                "allowed_origins": ["https://chatgpt.com"],
            },
        },
        "security": {"audit": {"enabled": True}},
    }


def warning_codes(result) -> set[str]:
    return {warning.code for warning in result.warnings}


def test_local_single_user_stdio_passes_fail_safe(tmp_path: Path) -> None:
    config = resolve_config(env={}, cwd=tmp_path).model

    result = validate_startup_security(config, env={})

    assert result.ok


def test_local_single_user_http_requires_loopback_host(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        {
            "server": {
                "transport": "streamable-http",
                "http": {"host": "0.0.0.0", "require_auth": True},
            }
        },
    )

    result = validate_startup_security(config, env={})

    assert result.blocked
    assert warning_codes(result) == {"security.remote_insecure_config"}


def test_remote_shared_requires_auth(tmp_path: Path) -> None:
    overrides = remote_shared_overrides()
    server = overrides["server"]
    assert isinstance(server, dict)
    http = server["http"]
    assert isinstance(http, dict)
    http["require_auth"] = False

    config = resolve_model(tmp_path, overrides)
    result = validate_startup_security(config, env={})

    assert "security.auth_required" in warning_codes(result)


def test_remote_shared_rejects_wildcard_origins(tmp_path: Path) -> None:
    overrides = remote_shared_overrides()
    server = overrides["server"]
    assert isinstance(server, dict)
    http = server["http"]
    assert isinstance(http, dict)
    http["allowed_origins"] = ["*"]

    config = resolve_model(tmp_path, overrides)
    result = validate_startup_security(config, env={"ACTIVE_KB_AUTH_TOKEN": "token"})

    assert warning_codes(result) == {"security.origin_blocked"}


def test_remote_shared_requires_audit_enabled(tmp_path: Path) -> None:
    overrides = remote_shared_overrides()
    overrides["security"] = {"audit": {"enabled": False}}

    config = resolve_model(tmp_path, overrides)
    result = validate_startup_security(config, env={"ACTIVE_KB_AUTH_TOKEN": "token"})

    assert warning_codes(result) == {"security.audit_required"}


def test_remote_shared_blocks_ops_tool_exposure(tmp_path: Path) -> None:
    overrides = remote_shared_overrides()
    server = overrides["server"]
    assert isinstance(server, dict)
    server["expose_ops_tools"] = True

    config = resolve_model(tmp_path, overrides)
    result = validate_startup_security(config, env={"ACTIVE_KB_AUTH_TOKEN": "token"})

    assert warning_codes(result) == {"security.ops_exposure_blocked"}


def test_remote_shared_requires_token_source(tmp_path: Path) -> None:
    config = resolve_model(tmp_path, remote_shared_overrides())

    result = validate_startup_security(config, env={})

    assert warning_codes(result) == {"security.auth_required"}


def test_remote_shared_secure_config_passes(tmp_path: Path) -> None:
    config = resolve_model(tmp_path, remote_shared_overrides())

    result = validate_startup_security(config, env={"ACTIVE_KB_AUTH_TOKEN": "token"})

    assert result.ok


def test_blocked_result_shape_is_contract_safe(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        {"server": {"transport": "streamable-http", "http": {"host": "0.0.0.0"}}},
    )

    payload = validate_startup_security(config, env={}).to_blocked_response()

    assert payload["result_status"] == "blocked"
    assert payload["items"] == []
    assert payload["evidence_refs"] == []
    assert payload["warnings"][0]["level"] == "blocked"
    assert payload["diagnostics"]["blocked_reason"] == "security_config"


def test_loopback_host_detection() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("[::1]")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("example.com")
