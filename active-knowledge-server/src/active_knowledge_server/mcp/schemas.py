"""MCP bootstrap schemas and registration metadata."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from active_knowledge_server import __version__
from active_knowledge_server.config.schema import ActiveKnowledgeConfig, Transport
from active_knowledge_server.models.evidence import EvidenceRef, SourceIndex
from active_knowledge_server.models.evidence import EvidenceRef, SourceIndex
from active_knowledge_server.models.responses import Warning
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
OPS_TOOL_NAMES: Final[tuple[str, ...]] = (
	"ops_get_config",
	"ops_validate_setup",
	"ops_index_status",
	"ops_start_index",
	"ops_cancel_index",
	"ops_list_profiles",
	"ops_list_sources",
)
ALL_TOOL_NAMES: Final[tuple[str, ...]] = BOOTSTRAP_TOOL_NAMES + QUERY_TOOL_NAMES
BOOTSTRAP_RESOURCE_URIS: Final[tuple[str, ...]] = (
	"active://config/current",
	"active://server/runtime",
)
QUERY_RESOURCE_URIS: Final[tuple[str, ...]] = (
	"active://snapshot/current",
	"active://profile/{profile_id}",
	"active://workspace/current/summary",
	"active://workspace/current/tree",
	"active://entity/{entity_id}",
	"active://evidence/{evidence_id}",
	"active://index/status",
)
ALL_RESOURCE_URIS: Final[tuple[str, ...]] = BOOTSTRAP_RESOURCE_URIS + QUERY_RESOURCE_URIS
ResourceReadStatus = Literal["ok", "missing", "degraded", "blocked", "error"]
OpsToolStatus = Literal["ok", "accepted", "blocked", "error", "not_found", "conflict"]


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

	def ops_tools_enabled(self) -> bool:
		"""Return whether operational tools should be exposed to MCP clients."""

		return (
			self.config.deployment_mode == "local_single_user"
			and self.config.server.expose_ops_tools
		)

	def exposed_ops_tools(self) -> tuple[str, ...]:
		"""Return the effective operational tool inventory."""

		return OPS_TOOL_NAMES if self.ops_tools_enabled() else ()

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
	ops_tools: tuple[str, ...] = ()
	bootstrap_resources: tuple[str, ...] = Field(default=BOOTSTRAP_RESOURCE_URIS)
	query_resources: tuple[str, ...] = Field(default=QUERY_RESOURCE_URIS)


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
	ops_tools: tuple[str, ...] = ()
	bootstrap_resources: tuple[str, ...] = Field(default=BOOTSTRAP_RESOURCE_URIS)
	query_resources: tuple[str, ...] = Field(default=QUERY_RESOURCE_URIS)
	workspace_root: str
	workdir: str
	source_docs_root: str


class MCPResourcePayload(BaseModel):
	"""Base payload shared by read-only MCP resources."""

	status: ResourceReadStatus = "ok"
	requested_uri: str
	message: str | None = None


class MCPSnapshotResource(MCPResourcePayload):
	"""Current snapshot metadata surfaced through a read-only MCP resource."""

	requested_snapshot_id: str
	snapshot_id: str | None = None
	resolved_snapshot_id: str | None = None
	workspace_revision: str | None = None
	baseline_id: str | None = None
	manifest_version: str | None = None
	created_at: str | None = None
	available_profile_ids: tuple[str, ...] = ()
	metadata: dict[str, Any] = Field(default_factory=dict)


class MCPProfileResource(MCPResourcePayload):
	"""One profile record surfaced through the read-only MCP resource layer."""

	requested_profile_id: str
	snapshot_id: str | None = None
	resolved_snapshot_id: str | None = None
	profile_record_id: str | None = None
	profile_id: str | None = None
	defconfig_hash: str | None = None
	dotconfig_hash: str | None = None
	defconfig_path: str | None = None
	dotconfig_path: str | None = None
	app: str | None = None
	board: str | None = None
	available_profile_ids: tuple[str, ...] = ()
	metadata: dict[str, Any] = Field(default_factory=dict)


class MCPWorkspaceSummaryResource(MCPResourcePayload):
	"""Summary-only workspace projection resource."""

	requested_snapshot_id: str
	snapshot_id: str | None = None
	resolved_snapshot_id: str | None = None
	schema_version: str | None = None
	workspace_root: str | None = None
	inventory_hash: str | None = None
	generated_at: str | None = None
	summary: dict[str, Any] = Field(default_factory=dict)
	view_names: tuple[str, ...] = ()
	view_summaries: dict[str, str] = Field(default_factory=dict)
	metadata: dict[str, Any] = Field(default_factory=dict)


class MCPWorkspaceTreeResource(MCPResourcePayload):
	"""Tree-focused workspace projection resource."""

	requested_snapshot_id: str
	snapshot_id: str | None = None
	resolved_snapshot_id: str | None = None
	schema_version: str | None = None
	workspace_root: str | None = None
	inventory_hash: str | None = None
	generated_at: str | None = None
	summary: dict[str, Any] = Field(default_factory=dict)
	workspace_tree: dict[str, Any] | None = None
	metadata: dict[str, Any] = Field(default_factory=dict)


class MCPEntityResource(MCPResourcePayload):
	"""Logical entity view surfaced through a read-only MCP resource."""

	requested_entity_id: str
	snapshot_id: str | None = None
	resolved_snapshot_id: str | None = None
	entity_id: str | None = None
	source_index: SourceIndex | None = None
	entity_type: str | None = None
	name: str | None = None
	qualified_name: str | None = None
	path: str | None = None
	profile_id: str | None = None
	start_line: int | None = None
	end_line: int | None = None
	replaced_from: tuple[str, ...] = ()
	metadata: dict[str, Any] = Field(default_factory=dict)


class MCPEvidenceResource(MCPResourcePayload):
	"""Logical evidence view surfaced through a read-only MCP resource."""

	requested_evidence_id: str
	snapshot_id: str | None = None
	resolved_snapshot_id: str | None = None
	evidence_id: str | None = None
	source_index: SourceIndex | None = None
	object_type: str | None = None
	object_id: str | None = None
	profile_id: str | None = None
	citation_label: str | None = None
	start_line: int | None = None
	end_line: int | None = None
	replaced_from: tuple[str, ...] = ()
	evidence_ref: EvidenceRef | None = None
	metadata: dict[str, Any] = Field(default_factory=dict)


class MCPIndexStatusResource(MCPResourcePayload):
	"""Current index validation and job-status resource."""

	requested_snapshot_id: str
	snapshot_id: str | None = None
	resolved_snapshot_id: str | None = None
	validation: dict[str, Any] = Field(default_factory=dict)
	recent_jobs: tuple[dict[str, Any], ...] = ()
	job_status_counts: dict[str, int] = Field(default_factory=dict)


class MCPOpsToolResult(BaseModel):
	"""Stable response envelope shared by gated operational MCP tools."""

	operation: str
	status: OpsToolStatus
	summary: str
	warnings: tuple[Warning, ...] = ()
	payload: dict[str, Any] = Field(default_factory=dict)
	items: tuple[dict[str, Any], ...] = ()
	diagnostics: dict[str, Any] = Field(default_factory=dict)


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
