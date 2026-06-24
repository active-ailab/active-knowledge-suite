"""FastMCP application assembly for Active Knowledge Server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from active_knowledge_server import __version__
from active_knowledge_server.config.loader import ResolvedConfig, resolve_runtime_path
from active_knowledge_server.config.schema import summarize_config
from active_knowledge_server.config.workdir import layout_from_config
from active_knowledge_server.mcp.resources import (
	register_bootstrap_resources,
	register_query_resources,
)
from active_knowledge_server.mcp.schemas import (
	MCPAppContext,
	MCPComponentInventory,
	normalize_mcp_path,
)
from active_knowledge_server.mcp.tools import (
	LazyQueryToolRuntime,
	register_bootstrap_tools,
	register_ops_tools,
	register_query_tools,
)
from active_knowledge_server.observability.logging import configure_logging
from active_knowledge_server.observability.metrics import ObservabilityStore
from active_knowledge_server.security.audit import AuditLogger
from active_knowledge_server.security.http import fastmcp_http_middleware_entries

try:
	from fastmcp import FastMCP
	from starlette.requests import Request
	from starlette.responses import JSONResponse
except ModuleNotFoundError as exc:  # pragma: no cover - exercised through dependency install
	FastMCP = None  # type: ignore[assignment]
	Request = Any  # type: ignore[misc,assignment]
	JSONResponse = Any  # type: ignore[misc,assignment]
	_IMPORT_ERROR = exc
else:
	_IMPORT_ERROR = None


@dataclass(frozen=True)
class ActiveKnowledgeFastMCPApp:
	"""Structured FastMCP runtime wrapper used by CLI and tests."""

	mcp: Any
	context: MCPAppContext
	inventory: MCPComponentInventory
	query_runtime: LazyQueryToolRuntime | None = None
	http_middleware: tuple[Any, ...] = ()

	def describe(self) -> dict[str, object]:
		"""Return a machine-readable summary of the current runtime wiring."""

		return {
			"name": self.context.config.server.name,
			"version": __version__,
			"deployment_mode": self.context.config.deployment_mode,
			"transport": self.context.config.server.transport,
			"http_endpoint": self.context.http_endpoint(),
			"health_endpoint": self.context.health_endpoint(),
			"mcp_path": normalize_mcp_path(self.context.config.server.http.mcp_path),
			"stateless_http": self._stateless_http(),
			"components": self.inventory.to_dict(),
		}

	def run(self) -> None:
		"""Run the FastMCP server using the configured transport."""

		transport = self.context.config.server.transport
		if transport == "stdio":
			self.mcp.run(transport="stdio")
			return

		http = self.context.config.server.http
		self.mcp.run(
			transport="http",
			host=http.host,
			port=http.port,
			path=normalize_mcp_path(http.mcp_path),
			stateless_http=self._stateless_http(),
			middleware=list(self.http_middleware),
		)

	def http_app(self) -> Any:
		"""Return an ASGI app for streamable-http deployments."""

		return self.mcp.http_app(
			path=normalize_mcp_path(self.context.config.server.http.mcp_path),
			stateless_http=self._stateless_http(),
			middleware=list(self.http_middleware),
		)

	def _stateless_http(self) -> bool:
		"""Prefer stateless HTTP for shared remote deployments."""

		return self.context.config.deployment_mode == "remote_shared"


def create_fastmcp_app(
	resolved: ResolvedConfig,
	*,
	cwd: Path | None = None,
) -> ActiveKnowledgeFastMCPApp:
	"""Create the configured FastMCP application and register bootstrap/query surfaces."""

	if FastMCP is None:
		raise RuntimeError(
			"FastMCP is not installed. Install project dependencies before serving the MCP app."
		) from _IMPORT_ERROR

	root = cwd or Path.cwd()
	layout = layout_from_config(resolved, cwd=root)
	workspace_root = resolve_runtime_path(resolved.model.project.workspace_root, root)
	source_docs_root = resolve_runtime_path(resolved.model.runtime.source_docs_root, root)
	config_summary = dict(
		summarize_config(
			resolved.model,
			cwd=root,
			loaded_files=resolved.loaded_files,
			local_config_path=resolved.local_config_path,
		)
	)

	configure_logging(resolved.model, layout)
	audit_logger = AuditLogger.from_config(resolved.model, layout)
	observability_store = ObservabilityStore.from_layout(layout)
	context = MCPAppContext(
		config=resolved.model,
		layout=layout,
		workspace_root=workspace_root,
		source_docs_root=source_docs_root,
		config_summary=config_summary,
		audit_logger=audit_logger,
		observability_store=observability_store,
		cwd=root,
	)
	http_middleware = tuple(
		fastmcp_http_middleware_entries(
			config=resolved.model,
			audit_logger=audit_logger,
		)
	)

	mcp = FastMCP(
		resolved.model.server.name,
		instructions=_server_instructions(),
		version=__version__,
		on_duplicate="error",
		mask_error_details=True,
	)
	_register_health_route(mcp, context)
	query_runtime = LazyQueryToolRuntime(context)
	tools = register_bootstrap_tools(mcp, context) + register_query_tools(
		mcp,
		context,
		runtime=query_runtime,
	)
	tools += register_ops_tools(
		mcp,
		context,
		runtime=query_runtime,
	)
	resources = register_bootstrap_resources(mcp, context) + register_query_resources(
		mcp,
		context,
		runtime=query_runtime,
	)
	return ActiveKnowledgeFastMCPApp(
		mcp=mcp,
		context=context,
		inventory=MCPComponentInventory(tools=tools, resources=resources),
		query_runtime=query_runtime,
		http_middleware=http_middleware,
	)


def _register_health_route(mcp: Any, context: MCPAppContext) -> None:
	"""Expose a basic health route for HTTP transport deployments."""

	@mcp.custom_route("/health", methods=["GET"])
	async def health_check(
		request: Request,
	) -> JSONResponse:  # pragma: no cover - HTTP runtime hook
		del request
		observability = context.observability_store.collect_status(
			config=context.config,
			layout=context.layout,
			cwd=context.cwd,
		)
		return JSONResponse(
			{
				"status": "ok",
				"server": context.config.server.name,
				"version": __version__,
				"transport": context.config.server.transport,
				"deployment_mode": context.config.deployment_mode,
				"health_summary": observability["health_summary"],
			}
		)


def _server_instructions() -> str:
	"""Return concise server instructions surfaced to MCP clients."""

	return (
		"Local-first Active Knowledge server. "
		"Use ping or server_info for readiness and runtime metadata, then invoke the V1 "
		"query tools for hybrid retrieval, workspace projections, and evidence packaging."
	)
