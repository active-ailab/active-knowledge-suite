"""MCP bootstrap schemas and registration metadata."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, Field

from active_knowledge_server import __version__
from active_knowledge_server.config.schema import ActiveKnowledgeConfig, Transport
from active_knowledge_server.config.workdir import WorkdirLayout
from active_knowledge_server.security.audit import AuditLogger

BOOTSTRAP_TOOL_NAMES: Final[tuple[str, ...]] = ("ping", "server_info")
QUERY_TOOL_NAMES: Final[tuple[str, ...]] = (
	"kb_search",
	"docs_search",
	"code_resolve",
	"code_context",
	"code_trace",
	"config_impact",
	"workspace_view",
	"evidence_bundle",
)
ALL_TOOL_NAMES: Final[tuple[str, ...]] = BOOTSTRAP_TOOL_NAMES + QUERY_TOOL_NAMES
BOOTSTRAP_RESOURCE_URIS: Final[tuple[str, ...]] = (
	"active://config/current",
	"active://server/runtime",
)


def normalize_mcp_path(path: str) -> str:
	"""Return a normalized MCP path with one leading slash."""

	normalized = path.strip() or "/mcp"
	if not normalized.startswith("/"):
		normalized = f"/{normalized}"
	return normalized


@dataclass(frozen=True)
class MCPAppContext:
	"""Runtime context shared by bootstrap tools and resources."""

	config: ActiveKnowledgeConfig
	layout: WorkdirLayout
	workspace_root: Path
	source_docs_root: Path
	config_summary: dict[str, Any]
	audit_logger: AuditLogger
	cwd: Path

	def http_endpoint(self) -> str | None:
		"""Return the configured MCP endpoint URL when HTTP transport is enabled."""

		if self.config.server.transport != "streamable-http":
			return None
		http = self.config.server.http
		return f"http://{http.host}:{http.port}{normalize_mcp_path(http.mcp_path)}"

	def health_endpoint(self) -> str | None:
		"""Return the configured health-check URL when HTTP transport is enabled."""

		if self.config.server.transport != "streamable-http":
			return None
		http = self.config.server.http
		return f"http://{http.host}:{http.port}/health"


class MCPPingResult(BaseModel):
	"""Structured response for the lightweight bootstrap ping tool."""

	status: str = "ok"
	server: str
	version: str = __version__
	deployment_mode: str
	transport: Transport
	workspace_root: str
	workdir: str
	source_docs_root: str


class MCPServerInfoResult(BaseModel):
	"""Structured response describing the current MCP bootstrap runtime."""

	server: str
	version: str = __version__
	deployment_mode: str
	transport: Transport
	http_endpoint: str | None = None
	mcp_path: str | None = None
	expose_ops_tools: bool
	audit_enabled: bool
	workspace_root: str
	workdir: str
	source_docs_root: str
	bootstrap_tools: tuple[str, ...] = Field(default=BOOTSTRAP_TOOL_NAMES)
	query_tools: tuple[str, ...] = Field(default=QUERY_TOOL_NAMES)
	bootstrap_resources: tuple[str, ...] = Field(default=BOOTSTRAP_RESOURCE_URIS)


class MCPConfigSummaryResource(BaseModel):
	"""JSON resource payload for the current non-sensitive config summary."""

	server: str
	version: str = __version__
	deployment_mode: str
	transport: Transport
	summary: dict[str, Any]


class MCPServerRuntimeResource(BaseModel):
	"""JSON resource payload for current MCP runtime metadata."""

	server: str
	version: str = __version__
	deployment_mode: str
	transport: Transport
	http_endpoint: str | None = None
	health_endpoint: str | None = None
	expose_ops_tools: bool
	audit_enabled: bool
	bootstrap_tools: tuple[str, ...] = Field(default=BOOTSTRAP_TOOL_NAMES)
	query_tools: tuple[str, ...] = Field(default=QUERY_TOOL_NAMES)
	bootstrap_resources: tuple[str, ...] = Field(default=BOOTSTRAP_RESOURCE_URIS)
	workspace_root: str
	workdir: str
	source_docs_root: str


@dataclass(frozen=True)
class RegisteredTool:
	"""One registered bootstrap tool plus its direct handler for testing."""

	name: str
	description: str
	handler: Callable[..., BaseModel]
	tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegisteredResource:
	"""One registered bootstrap resource plus its direct handler for testing."""

	uri: str
	name: str
	description: str
	handler: Callable[..., str]
	tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class MCPComponentInventory:
	"""Stable summary of bootstrap tools/resources exposed by the app."""

	tools: tuple[RegisteredTool, ...]
	resources: tuple[RegisteredResource, ...]

	@property
	def tool_names(self) -> tuple[str, ...]:
		"""Return tool names in registration order."""

		return tuple(item.name for item in self.tools)

	@property
	def resource_uris(self) -> tuple[str, ...]:
		"""Return resource URIs in registration order."""

		return tuple(item.uri for item in self.resources)

	def to_dict(self) -> dict[str, list[str]]:
		"""Return a JSON-serializable inventory summary."""

		return {
			"tools": list(self.tool_names),
			"resources": list(self.resource_uris),
		}


def serialize_json_resource(payload: BaseModel) -> str:
	"""Serialize one resource payload as stable JSON text."""

	return json.dumps(payload.model_dump(mode="json"), indent=2, sort_keys=True)
