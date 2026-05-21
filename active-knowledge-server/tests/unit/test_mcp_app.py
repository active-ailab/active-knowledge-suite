from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.mcp import create_fastmcp_app
from active_knowledge_server.mcp.schemas import (
	ALL_TOOL_NAMES,
    BOOTSTRAP_RESOURCE_URIS,
    MCPPingResult,
    MCPServerInfoResult,
    QUERY_TOOL_NAMES,
)


def _resolved_config(
    tmp_path: Path,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    mcp_path: str = "/mcp",
) -> object:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    source_docs.mkdir()

    overrides: ConfigDict = {
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
            "transport": transport,
            "http": {
                "host": host,
                "port": port,
                "mcp_path": mcp_path,
            },
        },
    }
    return resolve_config(cli_overrides=overrides, cwd=tmp_path)


def test_create_fastmcp_app_registers_bootstrap_tools_and_resources(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}

    assert runtime.inventory.tool_names == ALL_TOOL_NAMES
    assert runtime.inventory.resource_uris == BOOTSTRAP_RESOURCE_URIS
    assert isinstance(handlers["ping"](), MCPPingResult)
    assert isinstance(handlers["server_info"](), MCPServerInfoResult)
    assert QUERY_TOOL_NAMES == runtime.inventory.tool_names[2:]


def test_run_uses_stdio_transport_by_default(tmp_path: Path, monkeypatch) -> None:
    resolved = _resolved_config(tmp_path, transport="stdio")
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(runtime.mcp, "run", fake_run)

    runtime.run()

    assert captured == {"transport": "stdio"}


def test_run_passes_http_host_port_and_path(tmp_path: Path, monkeypatch) -> None:
    resolved = _resolved_config(
        tmp_path,
        transport="streamable-http",
        host="0.0.0.0",
        port=9100,
        mcp_path="/api/mcp",
    )
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(runtime.mcp, "run", fake_run)

    runtime.run()

    assert captured == {
        "transport": "http",
        "host": "0.0.0.0",
        "port": 9100,
        "path": "/api/mcp",
        "stateless_http": False,
    }


def test_remote_shared_http_prefers_stateless_mode(tmp_path: Path, monkeypatch) -> None:
    resolved = _resolved_config(tmp_path, transport="streamable-http")
    resolved.data["deployment_mode"] = "remote_shared"
    resolved = resolve_config(cli_overrides=resolved.data, cwd=tmp_path)
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    captured: dict[str, object] = {}

    def fake_http_app(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(runtime.mcp, "http_app", fake_http_app)

    runtime.http_app()

    assert captured == {"path": "/mcp", "stateless_http": True}