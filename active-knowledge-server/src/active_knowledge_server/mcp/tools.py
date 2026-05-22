"""Bootstrap and V1 query MCP tools for the FastMCP facade."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.connectors.workspace import WorkspaceConnector
from active_knowledge_server.indexing.workspace_map import WorkspaceMapArtifact, WorkspaceMapBuilder, WorkspaceViewItem
from active_knowledge_server.mcp.annotations import readonly_annotations
from active_knowledge_server.mcp.schemas import (
	MCPAppContext,
	MCPPingResult,
	MCPServerInfoResult,
	QUERY_TOOL_NAMES,
	RegisteredTool,
)
from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.models.query import QueryDomain, QueryGranularity, QueryIntent, QueryRequest, QueryView
from active_knowledge_server.models.responses import QueryResult, Warning
from active_knowledge_server.query.service import QueryService
from active_knowledge_server.security.path_guard import PathBlockedError
from active_knowledge_server.storage.base import ALL_SCOPE, StorageReader
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import (
	SQLiteStorageAdapter,
	configured_sqlite_paths,
	migrate_sqlite_store,
)

DocsSearchType = Literal["api", "widget", "product", "project"]
WorkspaceViewName = Literal["workspace", "layer", "domain", "feature", "profile"]
_QUERY_TOOL_TAGS = ("query", "v1", "readonly")


@dataclass
class LazyQueryToolRuntime:
	"""Lazy query runtime shared by all M6-02 MCP query tools."""

	context: MCPAppContext
	_metadata_adapter: SQLiteStorageAdapter | None = None
	_vector_adapter: LanceDBVectorAdapter | None = None
	_query_service: QueryService | None = None
	_workspace_connector: WorkspaceConnector | None = None
	_workspace_map_builder: WorkspaceMapBuilder | None = None
	_readonly_metadata_adapter: SQLiteStorageAdapter | None = None
	_readonly_workspace_connector: WorkspaceConnector | None = None
	_readonly_workspace_map_builder: WorkspaceMapBuilder | None = None

	def search_query(self, request: QueryRequest) -> QueryResult:
		"""Run one routed hybrid query using the shared service."""

		self._ensure_initialized()
		assert self._query_service is not None
		return self._query_service.search(request)

	def bundle_evidence_for_entity(
		self,
		entity_id: str,
		*,
		snapshot_id: str,
		profile_id: str,
	) -> tuple[EvidenceRef, ...]:
		"""Package stable evidence refs for one explicit entity."""

		self._ensure_initialized()
		assert self._query_service is not None
		return self._query_service.bundle_evidence_for_entity(
			entity_id,
			snapshot_id=snapshot_id,
			profile_id=profile_id,
		)

	def collect_workspace_artifact(self, *, snapshot_id: str) -> WorkspaceMapArtifact:
		"""Build a live workspace projection artifact for workspace_view."""

		self._ensure_initialized()
		assert self._metadata_adapter is not None
		assert self._workspace_connector is not None
		assert self._workspace_map_builder is not None
		reader = self._metadata_adapter.reader()
		inventory = self._workspace_connector.scan()
		return self._workspace_map_builder.collect(
			snapshot_id=snapshot_id,
			workspace_inventory=inventory,
			reader=reader,
			profiles=reader.iter_profiles(snapshot_id=snapshot_id),
		)

	def resource_reader(self) -> StorageReader:
		"""Return a read-only metadata reader without triggering migrations."""

		self._ensure_readonly_initialized()
		assert self._readonly_metadata_adapter is not None
		return self._readonly_metadata_adapter.reader()

	def collect_workspace_artifact_readonly(self, *, snapshot_id: str) -> WorkspaceMapArtifact:
		"""Build one workspace projection artifact without triggering storage migrations."""

		self._ensure_readonly_initialized()
		assert self._readonly_metadata_adapter is not None
		assert self._readonly_workspace_connector is not None
		assert self._readonly_workspace_map_builder is not None
		reader = self._readonly_metadata_adapter.reader()
		inventory = self._readonly_workspace_connector.scan()
		return self._readonly_workspace_map_builder.collect(
			snapshot_id=snapshot_id,
			workspace_inventory=inventory,
			reader=reader,
			profiles=reader.iter_profiles(snapshot_id=snapshot_id),
		)

	def _ensure_initialized(self) -> None:
		if self._query_service is not None:
			return

		paths = configured_sqlite_paths(self.context.config, cwd=self.context.cwd)
		migrate_sqlite_store(paths["baseline_metadata"], target="baseline_metadata")
		migrate_sqlite_store(paths["overlay_metadata"], target="overlay_metadata")
		migrate_sqlite_store(paths["jobs"], target="jobs")

		metadata_adapter = SQLiteStorageAdapter.from_config(
			self.context.config,
			cwd=self.context.cwd,
		)
		vector_adapter = LanceDBVectorAdapter.from_config(
			self.context.config,
			cwd=self.context.cwd,
			metadata_adapter=metadata_adapter,
		)
		self._metadata_adapter = metadata_adapter
		self._vector_adapter = vector_adapter
		self._query_service = QueryService.from_config(
			self.context.config,
			cwd=self.context.cwd,
			metadata_adapter=metadata_adapter,
			vector_adapter=vector_adapter,
		)
		self._workspace_connector = WorkspaceConnector.from_config(
			self.context.config,
			cwd=self.context.cwd,
		)
		self._workspace_map_builder = WorkspaceMapBuilder.from_config(
			self.context.config,
			cwd=self.context.cwd,
		)

	def _ensure_readonly_initialized(self) -> None:
		if self._readonly_metadata_adapter is not None:
			return

		self._readonly_metadata_adapter = SQLiteStorageAdapter.from_config(
			self.context.config,
			cwd=self.context.cwd,
		)
		self._readonly_workspace_connector = WorkspaceConnector.from_config(
			self.context.config,
			cwd=self.context.cwd,
		)
		self._readonly_workspace_map_builder = WorkspaceMapBuilder.from_config(
			self.context.config,
			cwd=self.context.cwd,
		)


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


def register_query_tools(
	mcp: Any,
	context: MCPAppContext,
	*,
	runtime: LazyQueryToolRuntime,
) -> tuple[RegisteredTool, ...]:
	"""Register the V1 read-only query tool surface required by M6-02."""

	@mcp.tool(
		name="kb_search",
		annotations=readonly_annotations(title="Active Knowledge Search"),
		tags={"query", "v1", "search"},
	)
	def kb_search(
		query: str,
		*,
		domain: QueryDomain = "auto",
		view: QueryView = "auto",
		granularity: QueryGranularity = "auto",
		profile_id: str | None = "auto",
		snapshot_id: str | None = "current",
	) -> QueryResult:
		"""Run the generic hybrid retrieval entry point across code, docs, and profiles."""

		request = QueryRequest(
			query=query,
			domain=domain,
			view=view,
			granularity=granularity,
			profile_id=profile_id,
			snapshot_id=snapshot_id,
			caller_tool="kb_search",
		)
		return _execute_search_tool(
			context=context,
			runtime=runtime,
			tool_name="kb_search",
			intent="unknown",
			request=request,
		)

	@mcp.tool(
		name="docs_search",
		annotations=readonly_annotations(title="Active Documentation Search"),
		tags={"query", "v1", "docs"},
	)
	def docs_search(
		query: str,
		*,
		domain: QueryDomain = "auto",
		doc_type: DocsSearchType | None = None,
		view: QueryView = "auto",
		granularity: QueryGranularity = "doc_section",
		profile_id: str | None = "auto",
		snapshot_id: str | None = "current",
	) -> QueryResult:
		"""Search API, widget, product, or project documentation."""

		request = QueryRequest(
			query=query,
			domain=_resolve_docs_domain(domain=domain, doc_type=doc_type),
			view=view,
			granularity=granularity,
			profile_id=profile_id,
			snapshot_id=snapshot_id,
			caller_tool="docs_search",
			client_context={"doc_type": doc_type} if doc_type is not None else {},
		)
		return _execute_search_tool(
			context=context,
			runtime=runtime,
			tool_name="docs_search",
			intent="api_lookup",
			request=request,
			details={"doc_type": doc_type},
		)

	@mcp.tool(
		name="code_resolve",
		annotations=readonly_annotations(title="Active Code Resolve"),
		tags={"query", "v1", "code"},
	)
	def code_resolve(
		query: str,
		*,
		granularity: QueryGranularity = "symbol",
		profile_id: str | None = "auto",
		snapshot_id: str | None = "current",
	) -> QueryResult:
		"""Resolve one symbol, macro, path, or file anchor inside the indexed workspace."""

		request = QueryRequest(
			query=query,
			domain="code",
			view="code",
			granularity=granularity,
			profile_id=profile_id,
			snapshot_id=snapshot_id,
			caller_tool="code_resolve",
		)
		return _execute_search_tool(
			context=context,
			runtime=runtime,
			tool_name="code_resolve",
			intent="code_exact",
			request=request,
		)

	@mcp.tool(
		name="code_context",
		annotations=readonly_annotations(title="Active Code Context"),
		tags={"query", "v1", "code"},
	)
	def code_context(
		query: str,
		*,
		view: QueryView = "code",
		granularity: QueryGranularity = "module",
		profile_id: str | None = "auto",
		snapshot_id: str | None = "current",
	) -> QueryResult:
		"""Explain module-level code context around one concept, file, or subsystem."""

		request = QueryRequest(
			query=query,
			domain="code",
			view=view,
			granularity=granularity,
			profile_id=profile_id,
			snapshot_id=snapshot_id,
			caller_tool="code_context",
		)
		return _execute_search_tool(
			context=context,
			runtime=runtime,
			tool_name="code_context",
			intent="code_concept",
			request=request,
		)

	@mcp.tool(
		name="code_trace",
		annotations=readonly_annotations(title="Active Code Trace"),
		tags={"query", "v1", "trace"},
	)
	def code_trace(
		query: str,
		*,
		view: QueryView = "runtime",
		granularity: QueryGranularity = "flow",
		profile_id: str | None = "auto",
		snapshot_id: str | None = "current",
	) -> QueryResult:
		"""Trace one call path or runtime flow using the shared graph-aware query service."""

		request = QueryRequest(
			query=query,
			domain="code",
			view=view,
			granularity=granularity,
			profile_id=profile_id,
			snapshot_id=snapshot_id,
			caller_tool="code_trace",
		)
		return _execute_search_tool(
			context=context,
			runtime=runtime,
			tool_name="code_trace",
			intent="call_trace",
			request=request,
		)

	@mcp.tool(
		name="config_impact",
		annotations=readonly_annotations(title="Active Config Impact"),
		tags={"query", "v1", "profile"},
	)
	def config_impact(
		query: str,
		*,
		compare_to: str | None = None,
		profile_id: str | None = "auto",
		snapshot_id: str | None = "current",
	) -> QueryResult:
		"""Inspect profile, macro, and configuration impact across one or more profiles."""

		client_context = {"compare_to": compare_to} if compare_to is not None else {}
		request = QueryRequest(
			query=query,
			view="profile",
			granularity="profile",
			profile_id=profile_id,
			snapshot_id=snapshot_id,
			caller_tool="config_impact",
			client_context=client_context,
		)
		return _execute_search_tool(
			context=context,
			runtime=runtime,
			tool_name="config_impact",
			intent="profile_diff",
			request=request,
			details={"compare_to": compare_to},
		)

	@mcp.tool(
		name="workspace_view",
		annotations=readonly_annotations(title="Active Workspace View"),
		tags={"query", "v1", "workspace"},
	)
	def workspace_view(
		*,
		view: WorkspaceViewName = "workspace",
		query: str | None = None,
		profile_id: str | None = "auto",
		snapshot_id: str | None = "current",
		limit: int | None = None,
	) -> QueryResult:
		"""Return one live workspace projection view filtered by an optional query string."""

		request_id = str(uuid4())
		resolved_snapshot_id = snapshot_id or "current"
		resolved_profile_id = _response_profile_id(profile_id)
		tool_query = query.strip() if isinstance(query, str) and query.strip() else None
		with context.audit_logger.tool_call(
			tool="workspace_view",
			query=tool_query,
			profile_id=profile_id,
			snapshot_id=resolved_snapshot_id,
			caller="mcp.query",
			request_id=request_id,
			details={"view": view, "limit": limit},
		) as scope:
			try:
				artifact = runtime.collect_workspace_artifact(snapshot_id=resolved_snapshot_id)
				projection = artifact.views[view]
				matching_items = tuple(
					item
					for item in projection.items
					if _workspace_item_matches(item, tool_query)
				)
				result = _workspace_view_result(
					artifact=artifact,
					view=view,
					projection_items=matching_items,
					projection_summary=projection.summary,
					projection_metadata=projection.metadata,
					query=tool_query,
					profile_id=resolved_profile_id,
					snapshot_id=resolved_snapshot_id,
					limit=limit or context.config.query.default_top_k,
				)
			except PathBlockedError as exc:
				result = _blocked_result(
					tool_name="workspace_view",
					intent="workspace_nav",
					summary="Workspace view access was blocked by the configured path guard.",
					snapshot_id=resolved_snapshot_id,
					profile_id=resolved_profile_id,
					blocked_reason="security.path_blocked",
					message=str(exc),
				)
			except Exception as exc:
				result = _error_result(
					tool_name="workspace_view",
					intent="workspace_nav",
					summary="Workspace view failed before a stable projection could be returned.",
					snapshot_id=resolved_snapshot_id,
					profile_id=resolved_profile_id,
					request_id=request_id,
					exc=exc,
				)
			scope.set_result(
				result_count=_result_count(result),
				result_status=result.result_status,
				warning_codes=[warning.code for warning in result.warnings],
				warning_levels=[warning.level for warning in result.warnings],
			)
			return result

	@mcp.tool(
		name="evidence_bundle",
		annotations=readonly_annotations(title="Active Evidence Bundle"),
		tags={"query", "v1", "evidence"},
	)
	def evidence_bundle(
		query: str | None = None,
		*,
		entity_id: str | None = None,
		profile_id: str | None = "auto",
		snapshot_id: str | None = "current",
	) -> QueryResult:
		"""Package stable evidence refs for either a routed query or one explicit entity."""

		if entity_id is None and (query is None or not query.strip()):
			return _blocked_result(
				tool_name="evidence_bundle",
				intent="evidence_lookup",
				summary="Provide either query or entity_id before requesting an evidence bundle.",
				snapshot_id=snapshot_id or "current",
				profile_id=_response_profile_id(profile_id),
				blocked_reason="input.missing_locator",
				message="evidence_bundle requires query or entity_id.",
			)

		if entity_id is None:
			request = QueryRequest(
				query=(query or "").strip(),
				view="evidence",
				profile_id=profile_id,
				snapshot_id=snapshot_id,
				caller_tool="evidence_bundle",
			)
			return _execute_search_tool(
				context=context,
				runtime=runtime,
				tool_name="evidence_bundle",
				intent="evidence_lookup",
				request=request,
			)

		request_id = str(uuid4())
		resolved_snapshot_id = snapshot_id or "current"
		resolved_profile_id = _response_profile_id(profile_id)
		bundle_profile_id = resolved_profile_id if resolved_profile_id != "not_required" else ALL_SCOPE
		with context.audit_logger.tool_call(
			tool="evidence_bundle",
			query=query,
			profile_id=profile_id,
			snapshot_id=resolved_snapshot_id,
			caller="mcp.query",
			request_id=request_id,
			details={"entity_id": entity_id},
		) as scope:
			try:
				evidence_refs = runtime.bundle_evidence_for_entity(
					entity_id,
					snapshot_id=resolved_snapshot_id,
					profile_id=bundle_profile_id,
				)
				result = _entity_evidence_result(
					entity_id=entity_id,
					evidence_refs=evidence_refs,
					snapshot_id=resolved_snapshot_id,
					profile_id=resolved_profile_id,
				)
			except PathBlockedError as exc:
				result = _blocked_result(
					tool_name="evidence_bundle",
					intent="evidence_lookup",
					summary="Evidence packaging was blocked by the configured path guard.",
					snapshot_id=resolved_snapshot_id,
					profile_id=resolved_profile_id,
					blocked_reason="security.path_blocked",
					message=str(exc),
				)
			except Exception as exc:
				result = _error_result(
					tool_name="evidence_bundle",
					intent="evidence_lookup",
					summary="Evidence packaging failed before a stable bundle could be returned.",
					snapshot_id=resolved_snapshot_id,
					profile_id=resolved_profile_id,
					request_id=request_id,
					exc=exc,
				)
			scope.set_result(
				result_count=_result_count(result),
				result_status=result.result_status,
				warning_codes=[warning.code for warning in result.warnings],
				warning_levels=[warning.level for warning in result.warnings],
			)
			return result

	return tuple(
		RegisteredTool(
			name=name,
			description=description,
			handler=handler,
			tags=_QUERY_TOOL_TAGS,
		)
		for name, description, handler in (
			(
				"kb_search",
				"Run the generic hybrid retrieval entry point across code, docs, and profiles.",
				kb_search,
			),
			(
				"docs_search",
				"Search API, widget, product, or project documentation.",
				docs_search,
			),
			(
				"code_resolve",
				"Resolve one symbol, macro, path, or file anchor inside the indexed workspace.",
				code_resolve,
			),
			(
				"code_context",
				"Explain module-level code context around one concept, file, or subsystem.",
				code_context,
			),
			(
				"code_trace",
				"Trace one call path or runtime flow using the shared graph-aware query service.",
				code_trace,
			),
			(
				"config_impact",
				"Inspect profile, macro, and configuration impact across one or more profiles.",
				config_impact,
			),
			(
				"workspace_view",
				"Return one live workspace projection view filtered by an optional query string.",
				workspace_view,
			),
			(
				"evidence_bundle",
				"Package stable evidence refs for either a routed query or one explicit entity.",
				evidence_bundle,
			),
		)
	)


def _execute_search_tool(
	*,
	context: MCPAppContext,
	runtime: LazyQueryToolRuntime,
	tool_name: str,
	intent: QueryIntent,
	request: QueryRequest,
	details: Mapping[str, object | None] | None = None,
) -> QueryResult:
	"""Execute one routed query tool with shared audit and error handling."""

	request_id = str(uuid4())
	with context.audit_logger.tool_call(
		tool=tool_name,
		query=request.query,
		profile_id=request.profile_id,
		snapshot_id=request.snapshot_id,
		caller="mcp.query",
		request_id=request_id,
		details={
			"domain": request.domain,
			"view": request.view,
			"granularity": request.granularity,
			**{key: value for key, value in (details or {}).items() if value is not None},
		},
	) as scope:
		try:
			result = _normalize_query_result(runtime.search_query(request), tool_name=tool_name)
		except PathBlockedError as exc:
			result = _blocked_result(
				tool_name=tool_name,
				intent=intent,
				summary=f"{tool_name} was blocked by the configured path guard.",
				snapshot_id=request.snapshot_id or "current",
				profile_id=_response_profile_id(request.profile_id),
				blocked_reason="security.path_blocked",
				message=str(exc),
			)
		except Exception as exc:
			result = _error_result(
				tool_name=tool_name,
				intent=intent,
				summary=f"{tool_name} failed before a stable query result could be returned.",
				snapshot_id=request.snapshot_id or "current",
				profile_id=_response_profile_id(request.profile_id),
				request_id=request_id,
				exc=exc,
			)
		scope.set_result(
			result_count=_result_count(result),
			result_status=result.result_status,
			warning_codes=[warning.code for warning in result.warnings],
			warning_levels=[warning.level for warning in result.warnings],
		)
		return result


def _normalize_query_result(result: QueryResult, *, tool_name: str) -> QueryResult:
	"""Rewrite the outward tool_name to the invoked MCP wrapper name."""

	if result.tool_name == tool_name:
		return result
	return result.model_copy(update={"tool_name": tool_name})


def _resolve_docs_domain(*, domain: QueryDomain, doc_type: DocsSearchType | None) -> QueryDomain:
	"""Resolve the effective domain for docs_search without losing explicit specialization."""

	if doc_type is not None:
		return doc_type
	if domain != "auto":
		return domain
	return "docs"


def _response_profile_id(profile_id: str | None) -> str:
	"""Normalize profile IDs for response envelopes that do not require one."""

	if profile_id is None:
		return "not_required"
	value = profile_id.strip()
	if not value or value == "auto":
		return "not_required"
	return value


def _result_count(result: QueryResult) -> int:
	"""Return one stable audit result-count across all query result shapes."""

	return max(
		len(result.items),
		len(result.candidates),
		len(result.evidence_refs),
		len(result.entities),
		len(result.relations),
	)


def _zero_result_warning(message: str) -> Warning:
	"""Build the shared zero-result warning required by the contract."""

	return Warning(
		level="caution",
		code="retrieval.zero_result",
		message=message,
		actionable=True,
		suggested_action="Narrow or broaden the query with a symbol, path, profile_id, or view filter.",
	)


def _blocked_result(
	*,
	tool_name: str,
	intent: QueryIntent,
	summary: str,
	snapshot_id: str,
	profile_id: str,
	blocked_reason: str,
	message: str,
) -> QueryResult:
	"""Build a stable blocked response for MCP query tools."""

	return QueryResult.blocked(
		tool_name=tool_name,
		summary=summary,
		warnings=(
			Warning(
				level="blocked",
				code=blocked_reason,
				message=message,
				actionable=True,
				suggested_action="Adjust the request inputs or server path-guard configuration and retry.",
			),
		),
		next_queries=("Retry with a narrower path, symbol, or profile filter.",),
		diagnostics={"blocked_reason": blocked_reason},
		query_intent=intent,
		snapshot_id=snapshot_id,
		profile_id=profile_id,
	)


def _error_result(
	*,
	tool_name: str,
	intent: QueryIntent,
	summary: str,
	snapshot_id: str,
	profile_id: str,
	request_id: str,
	exc: Exception,
) -> QueryResult:
	"""Build a stable error envelope for unexpected MCP query tool failures."""

	return QueryResult(
		tool_name=tool_name,
		result_status="error",
		confidence=0.0,
		query_intent=intent,
		snapshot_id=snapshot_id,
		profile_id=profile_id,
		summary=summary,
		diagnostics={
			"request_id": request_id,
			"error_kind": exc.__class__.__name__,
			"error_summary": str(exc),
		},
	)


def _workspace_item_matches(item: WorkspaceViewItem, query: str | None) -> bool:
	"""Return whether one workspace projection item matches the optional filter string."""

	if query is None:
		return True
	lower_query = query.lower()
	haystacks: Sequence[str] = (
		item.item_id,
		item.kind,
		item.name,
		item.summary,
		*item.source_paths,
		*item.module_names,
		*item.entity_ids,
		*item.related_items,
		*(str(value) for value in item.metadata.values()),
	)
	return any(lower_query in value.lower() for value in haystacks)


def _workspace_view_result(
	*,
	artifact: WorkspaceMapArtifact,
	view: WorkspaceViewName,
	projection_items: tuple[WorkspaceViewItem, ...],
	projection_summary: str,
	projection_metadata: Mapping[str, object],
	query: str | None,
	profile_id: str,
	snapshot_id: str,
	limit: int,
) -> QueryResult:
	"""Build a stable QueryResult from one workspace projection view."""

	returned_items = projection_items[: max(1, limit)]
	total_matches = len(projection_items)
	if not returned_items:
		return QueryResult(
			tool_name="workspace_view",
			result_status="zero_result",
			confidence=0.0,
			query_intent="workspace_nav",
			snapshot_id=snapshot_id,
			profile_id=profile_id,
			summary=f"No {view} projection items matched the requested workspace filter.",
			warnings=(_zero_result_warning("workspace_view returned no matching projection items."),),
			next_queries=(
				"Retry workspace_view without the query filter.",
				"Try workspace_view(view='workspace') to inspect a broader projection.",
			),
			diagnostics={
				"view": view,
				"artifact_summary": dict(artifact.summary),
				"view_metadata": dict(projection_metadata),
				"matched_items": 0,
			},
		)

	truncated = len(returned_items) < total_matches
	summary = projection_summary
	if query is not None:
		summary = f"Matched {len(returned_items)} {view} projection items for '{query}'."
	if truncated:
		summary = f"{summary} Showing {len(returned_items)} of {total_matches} matches."
	return QueryResult(
		tool_name="workspace_view",
		result_status="ok",
		confidence=1.0 if query is None else 0.88,
		query_intent="workspace_nav",
		snapshot_id=snapshot_id,
		profile_id=profile_id,
		summary=summary,
		items=tuple(item.to_dict() for item in returned_items),
		diagnostics={
			"view": view,
			"artifact_summary": dict(artifact.summary),
			"view_metadata": dict(projection_metadata),
			"total_matches": total_matches,
			"returned_items": len(returned_items),
			"truncated": truncated,
			"artifact_metadata": dict(artifact.metadata),
		},
	)


def _entity_evidence_result(
	*,
	entity_id: str,
	evidence_refs: tuple[EvidenceRef, ...],
	snapshot_id: str,
	profile_id: str,
) -> QueryResult:
	"""Build a stable entity-scoped evidence bundle result."""

	if not evidence_refs:
		return QueryResult(
			tool_name="evidence_bundle",
			result_status="zero_result",
			confidence=0.0,
			query_intent="evidence_lookup",
			snapshot_id=snapshot_id,
			profile_id=profile_id,
			summary=f"No evidence refs were packaged for entity {entity_id}.",
			warnings=(_zero_result_warning("evidence_bundle returned no evidence for the requested entity."),),
			next_queries=(
				"Retry evidence_bundle with a broader query instead of entity_id.",
				"Verify the entity_id through code_resolve before retrying.",
			),
			diagnostics={"entity_id": entity_id},
		)
	return QueryResult(
		tool_name="evidence_bundle",
		result_status="ok",
		confidence=1.0,
		query_intent="evidence_lookup",
		snapshot_id=snapshot_id,
		profile_id=profile_id,
		summary=f"Packaged {len(evidence_refs)} evidence refs for entity {entity_id}.",
		items=(
			{
				"entity_id": entity_id,
				"bundle_type": "entity",
				"evidence_count": len(evidence_refs),
			},
		),
		evidence_refs=evidence_refs,
		diagnostics={"entity_id": entity_id},
	)
