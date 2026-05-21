"""Bootstrap MCP resources for the FastMCP facade."""

from __future__ import annotations

from typing import Any

from active_knowledge_server.mcp.annotations import readonly_annotations
from active_knowledge_server.mcp.schemas import (
	MCPAppContext,
	MCPConfigSummaryResource,
	MCPServerRuntimeResource,
	RegisteredResource,
	serialize_json_resource,
)


def register_bootstrap_resources(
	mcp: Any,
	context: MCPAppContext,
) -> tuple[RegisteredResource, ...]:
	"""Register the minimal bootstrap resources required by M6-01."""

	@mcp.resource(
		"active://config/current",
		name="ActiveKnowledgeCurrentConfig",
		description="Current non-sensitive Active Knowledge config summary.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Current Config"),
		tags={"bootstrap", "config"},
	)
	def current_config() -> str:
		"""Return the current config summary as JSON."""

		return serialize_json_resource(
			MCPConfigSummaryResource(
				server=context.config.server.name,
				deployment_mode=context.config.deployment_mode,
				transport=context.config.server.transport,
				summary=dict(context.config_summary),
			)
		)

	@mcp.resource(
		"active://server/runtime",
		name="ActiveKnowledgeServerRuntime",
		description="Current Active Knowledge FastMCP runtime metadata.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Server Runtime"),
		tags={"bootstrap", "status"},
	)
	def server_runtime() -> str:
		"""Return the current runtime summary as JSON."""

		return serialize_json_resource(
			MCPServerRuntimeResource(
				server=context.config.server.name,
				deployment_mode=context.config.deployment_mode,
				transport=context.config.server.transport,
				http_endpoint=context.http_endpoint(),
				health_endpoint=context.health_endpoint(),
				expose_ops_tools=context.config.server.expose_ops_tools,
				audit_enabled=context.config.security.audit.enabled,
				workspace_root=str(context.workspace_root),
				workdir=str(context.layout.workdir),
				source_docs_root=str(context.source_docs_root),
			)
		)

	return (
		RegisteredResource(
			uri="active://config/current",
			name="ActiveKnowledgeCurrentConfig",
			description="Current non-sensitive Active Knowledge config summary.",
			handler=current_config,
			tags=("bootstrap", "config"),
		),
		RegisteredResource(
			uri="active://server/runtime",
			name="ActiveKnowledgeServerRuntime",
			description="Current Active Knowledge FastMCP runtime metadata.",
			handler=server_runtime,
			tags=("bootstrap", "status"),
		),
	)
