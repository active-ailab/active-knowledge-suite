"""Bootstrap MCP tools for the FastMCP facade."""

from __future__ import annotations

from typing import Any

from active_knowledge_server.mcp.annotations import readonly_annotations
from active_knowledge_server.mcp.schemas import MCPAppContext, MCPPingResult, MCPServerInfoResult, RegisteredTool


def register_bootstrap_tools(mcp: Any, context: MCPAppContext) -> tuple[RegisteredTool, ...]:
	"""Register the minimal bootstrap tools required by M6-01."""

	@mcp.tool(
		annotations=readonly_annotations(title="Active Knowledge Ping"),
		tags={"bootstrap", "status"},
	)
	def ping() -> MCPPingResult:
		"""Return a lightweight readiness probe for the MCP facade."""

		with context.audit_logger.tool_call(tool="ping", caller="mcp.bootstrap") as scope:
			result = MCPPingResult(
				server=context.config.server.name,
				deployment_mode=context.config.deployment_mode,
				transport=context.config.server.transport,
				workspace_root=str(context.workspace_root),
				workdir=str(context.layout.workdir),
				source_docs_root=str(context.source_docs_root),
			)
			scope.set_result(result_count=1, result_status="ok")
			return result

	@mcp.tool(
		name="server_info",
		annotations=readonly_annotations(title="Active Knowledge Server Info"),
		tags={"bootstrap", "status", "config"},
	)
	def server_info() -> MCPServerInfoResult:
		"""Return the current bootstrap server identity and transport metadata."""

		with context.audit_logger.tool_call(tool="server_info", caller="mcp.bootstrap") as scope:
			http_endpoint = context.http_endpoint()
			result = MCPServerInfoResult(
				server=context.config.server.name,
				deployment_mode=context.config.deployment_mode,
				transport=context.config.server.transport,
				http_endpoint=http_endpoint,
				mcp_path=context.config.server.http.mcp_path if http_endpoint else None,
				expose_ops_tools=context.config.server.expose_ops_tools,
				audit_enabled=context.config.security.audit.enabled,
				workspace_root=str(context.workspace_root),
				workdir=str(context.layout.workdir),
				source_docs_root=str(context.source_docs_root),
			)
			scope.set_result(result_count=1, result_status="ok")
			return result

	return (
		RegisteredTool(
			name="ping",
			description="Return a lightweight readiness probe for the MCP facade.",
			handler=ping,
			tags=("bootstrap", "status"),
		),
		RegisteredTool(
			name="server_info",
			description="Return the current bootstrap server identity and transport metadata.",
			handler=server_info,
			tags=("bootstrap", "status", "config"),
		),
	)
