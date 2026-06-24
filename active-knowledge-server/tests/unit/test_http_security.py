from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.mcp import create_fastmcp_app


def _resolved_config(
    tmp_path: Path,
    *,
    deployment_mode: str = "local_single_user",
    require_auth: bool = False,
    allowed_origins: list[str] | None = None,
    auth_provider: str = "none",
) -> object:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    source_docs.mkdir()

    overrides: ConfigDict = {
        "deployment_mode": deployment_mode,
        "runtime": {
            "workdir": ".active-kb",
            "source_docs_root": "knowledge-sources",
        },
        "project": {
            "workspace_root": "workspace",
            "id": "active-test",
            "display_name": "Active Test",
        },
        "server": {
            "transport": "streamable-http",
            "http": {
                "host": "127.0.0.1" if deployment_mode == "local_single_user" else "0.0.0.0",
                "port": 8765,
                "mcp_path": "/mcp",
                "require_auth": require_auth,
                "auth_provider": auth_provider,
                "allowed_origins": allowed_origins
                or (["http://127.0.0.1", "http://localhost"] if deployment_mode == "local_single_user" else ["https://chatgpt.com"]),
            },
        },
    }
    if require_auth:
        overrides["server"]["http"]["token"] = {
            "env": "ACTIVE_KB_AUTH_TOKEN",
            "header": "Authorization",
            "scheme": "Bearer",
        }
    if deployment_mode == "remote_shared":
        overrides["security"] = {"audit": {"enabled": True}}
    return resolve_config(cli_overrides=overrides, cwd=tmp_path)


def _read_audit_events(runtime: object) -> list[dict[str, object]]:
    runtime.context.audit_logger.close()
    audit_path = runtime.context.layout.local_logs_dir / "audit.log"
    return [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line]


def test_local_loopback_http_allows_unauthenticated_requests_and_audits(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)

    with TestClient(runtime.http_app()) as client:
        response = client.get("/health", headers={"Origin": "http://127.0.0.1"})

    events = _read_audit_events(runtime)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["health_summary"]["query"]["health_state"] == "missing"
    assert "storage_size_bytes" in response.json()["health_summary"]["gauges"]
    assert events[-1]["event_type"] == "ops"
    assert events[-1]["ops_operation"] == "http.request"
    assert events[-1]["success"] is True
    assert events[-1]["details"]["path"] == "/health"


def test_http_origin_validation_blocks_untrusted_origin(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)

    with TestClient(runtime.http_app()) as client:
        response = client.get("/health", headers={"Origin": "https://evil.example.com"})

    events = _read_audit_events(runtime)

    assert response.status_code == 403
    assert response.json()["warnings"][0]["code"] == "security.origin_blocked"
    assert events[-1]["success"] is False
    assert events[-1]["warning_codes"] == ["security.origin_blocked"]


def test_remote_shared_rejects_unauthenticated_http_requests(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ACTIVE_KB_AUTH_TOKEN", "super-secret-token")
    resolved = _resolved_config(
        tmp_path,
        deployment_mode="remote_shared",
        require_auth=True,
        auth_provider="token",
    )
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)

    with TestClient(runtime.http_app()) as client:
        response = client.get("/health", headers={"Origin": "https://chatgpt.com"})

    events = _read_audit_events(runtime)
    raw_log = (runtime.context.layout.local_logs_dir / "audit.log").read_text(encoding="utf-8")

    assert response.status_code == 401
    assert response.json()["warnings"][0]["code"] == "security.auth_required"
    assert events[-1]["success"] is False
    assert events[-1]["warning_codes"] == ["security.auth_required"]
    assert "super-secret-token" not in raw_log


def test_remote_shared_accepts_allowed_origin_with_valid_bearer_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ACTIVE_KB_AUTH_TOKEN", "super-secret-token")
    resolved = _resolved_config(
        tmp_path,
        deployment_mode="remote_shared",
        require_auth=True,
        auth_provider="token",
    )
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)

    with TestClient(runtime.http_app()) as client:
        response = client.get(
            "/health",
            headers={
                "Origin": "https://chatgpt.com",
                "Authorization": "Bearer super-secret-token",
            },
        )

    events = _read_audit_events(runtime)
    raw_log = (runtime.context.layout.local_logs_dir / "audit.log").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert events[-1]["success"] is True
    assert events[-1]["details"]["auth_header_present"] is True
    assert events[-1]["details"]["auth_header_digest"]
    assert "super-secret-token" not in raw_log
