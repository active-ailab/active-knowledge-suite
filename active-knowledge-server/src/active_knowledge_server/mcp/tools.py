"""Bootstrap, query, and gated ops MCP tools for the FastMCP facade."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from active_knowledge_server.connectors.workspace import WorkspaceConnector
from active_knowledge_server.indexing.jobs import (
	INDEX_JOB_LOCK_ID,
	JobLockConflictError,
	JobStateTransitionError,
	SQLiteJobStore,
	decode_task_checkpoint,
)
from active_knowledge_server.indexing.workspace_map import (
	WorkspaceMapArtifact,
	WorkspaceMapBuilder,
	WorkspaceViewItem,
)
from active_knowledge_server.mcp.annotations import readonly_annotations, tool_annotations
from active_knowledge_server.mcp.schemas import (
	MCPAppContext,
	MCPOpsToolResult,
	MCPPingResult,
	MCPServerInfoResult,
	RegisteredTool,
)
from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.models.query import (
	QueryDomain,
	QueryGranularity,
	QueryIntent,
	QueryRequest,
	QueryView,
)
from active_knowledge_server.models.responses import QueryResult, Warning
from active_knowledge_server.query.service import QueryService
from active_knowledge_server.security.config import validate_startup_security
from active_knowledge_server.security.path_guard import PathBlockedError
from active_knowledge_server.storage.base import ALL_SCOPE, StorageReader
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import (
	SQLiteStorageAdapter,
	configured_sqlite_paths,
	migrate_sqlite_store,
)
from active_knowledge_server.storage.validation import validate_storage_consistency

DocsSearchType = Literal["api", "widget", "product", "project"]
WorkspaceViewName = Literal["workspace", "layer", "domain", "feature", "profile"]
OpsIndexMode = Literal["incremental", "full"]
OpsIndexSource = Literal["all", "code", "docs"]
OpsIndexResume = Literal["auto", "disabled"]
_QUERY_TOOL_TAGS = ("query", "v1", "readonly")
_OPS_TOOL_TAGS = ("ops", "v1")
_ACTIVE_INDEX_JOB_STATUSES = (
	"pending",
	"discovering",
	"parsing",
	"extracting",
	"embedding",
	"reporting",
)
_INDEX_JOB_CONTRACT_SCHEMA_VERSION = "index_job_contract.v1"
_INDEX_RESUME_POLICY_SCHEMA_VERSION = "index_resume_policy.v1"


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
	_job_store: SQLiteJobStore | None = None

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

	def ops_reader(self) -> StorageReader:
		"""Return the read-only metadata reader reused by gated ops tools."""

		return self.resource_reader()

	def active_index_job(self) -> Any | None:
		"""Return the newest non-terminal index job when one exists."""

		for job in self.ops_reader().iter_jobs():
			if job.job_type == "index" and job.status in _ACTIVE_INDEX_JOB_STATUSES:
				return job
		return None

	def recent_jobs(self, *, limit: int = 10) -> tuple[Any, ...]:
		"""Return recent persisted jobs in descending update order."""

		return self.ops_reader().iter_jobs()[: max(1, limit)]

	def job_checkpoints(self, job_id: str) -> dict[str, str]:
		"""Return persisted checkpoints for one job."""

		return self._ensure_job_store().get_checkpoints(job_id)

	def create_index_job(
		self,
		*,
		mode: OpsIndexMode,
		source: OpsIndexSource,
		profile_id: str,
		snapshot_id: str,
		resume: OpsIndexResume,
	) -> Any:
		"""Persist a scheduler-owned index job request for the gated ops surface."""

		active_job = self.active_index_job()
		if active_job is not None:
			raise JobLockConflictError(
				"index job "
				f"{active_job.job_id!r} is already active with status {active_job.status!r}"
			)
		store = self._ensure_job_store()
		metadata = _ops_index_job_metadata(
			mode=mode,
			source=source,
			profile_id=profile_id,
			snapshot_id=snapshot_id,
			resume=resume,
			requested_by="mcp.ops_start_index",
		)
		if resume == "auto":
			resumable = self._find_ops_resumable_job(
				mode=mode,
				source=source,
				profile_id=profile_id,
				snapshot_id=snapshot_id,
			)
			if resumable is not None:
				if resumable.status in {"failed", "partial_ready"}:
					resumable = store.retry_job(resumable.job_id)
				resumed = store.resume_job(
					resumable.job_id,
					increment_resume_count=True,
				).job
				return store.transition_or_update_running_metadata(
					resumed.job_id,
					metadata_update={**metadata, "execution_state": "scheduled"},
				)
		return store.create_job(
			snapshot_id=snapshot_id,
			profile_id=profile_id,
			metadata=metadata,
		)

	def resume_index_job(self, job_id: str) -> Any:
		"""Mark one resumable scheduler-owned index job pending for a later runner."""

		store = self._ensure_job_store()
		job = store.get_job(job_id)
		if job is None:
			raise KeyError(f"job {job_id!r} does not exist")
		if job.job_type != "index":
			raise JobStateTransitionError(f"job {job_id!r} is not an index job")
		if bool(job.metadata.get("cancelled")):
			raise JobStateTransitionError(f"cancelled job {job_id!r} cannot be resumed")
		if job.metadata.get("execution_state") == "superseded":
			raise JobStateTransitionError(f"superseded job {job_id!r} cannot be resumed")
		if job.status == "ready":
			raise JobStateTransitionError(f"ready job {job_id!r} cannot be resumed")
		if job.status in _ACTIVE_INDEX_JOB_STATUSES:
			return job
		if job.status in {"failed", "partial_ready"}:
			job = store.retry_job(job_id)
			resumed = store.resume_job(job.job_id, increment_resume_count=True).job
			return store.transition_or_update_running_metadata(
				resumed.job_id,
				metadata_update={
					"execution_state": "scheduled",
					"resume_policy": {
						"schema_version": _INDEX_RESUME_POLICY_SCHEMA_VERSION,
						"mode": "job_id",
						"resume_enabled": True,
						"resume_job_id": job_id,
					},
				},
			)
		raise JobStateTransitionError(f"job {job_id!r} is not resumable from {job.status!r}")

	def cancel_index_job(self, job_id: str) -> Any:
		"""Cancel one active or pending index job through the jobs store."""

		store = self._ensure_job_store()
		job = store.get_job(job_id)
		if job is None:
			raise KeyError(f"job {job_id!r} does not exist")
		if job.job_type != "index":
			raise JobStateTransitionError(f"job {job_id!r} is not an index job")
		if job.status == "failed" and bool(job.metadata.get("cancelled")):
			return job
		if job.status not in _ACTIVE_INDEX_JOB_STATUSES:
			raise JobStateTransitionError(
				f"job {job_id!r} is not cancelable from status {job.status!r}"
			)
		updated = store.transition_job(
			job_id,
			"failed",
			error_summary="cancelled by ops_cancel_index",
			metadata_update={
				"cancelled": True,
				"cancelled_by": "mcp.ops_cancel_index",
			},
		)
		store.release_lock(INDEX_JOB_LOCK_ID, owner_job_id=job_id)
		return updated

	def _find_ops_resumable_job(
		self,
		*,
		mode: OpsIndexMode,
		source: OpsIndexSource,
		profile_id: str,
		snapshot_id: str,
	) -> Any | None:
		"""Return the newest compatible MCP-scheduled job that can be retried/resumed."""

		for job in self.recent_jobs(limit=50):
			if job.job_type != "index":
				continue
			if job.status not in {"failed", "partial_ready"}:
				continue
			if bool(job.metadata.get("cancelled")):
				continue
			if job.metadata.get("execution_state") == "superseded":
				continue
			if job.snapshot_id != snapshot_id or job.profile_id != profile_id:
				continue
			if job.metadata.get("requested_mode") != mode:
				continue
			if job.metadata.get("requested_target") != "overlay":
				continue
			if job.metadata.get("requested_source") != source:
				continue
			return job
		return None

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

	def _ensure_job_store(self) -> SQLiteJobStore:
		if self._job_store is not None:
			return self._job_store

		paths = configured_sqlite_paths(self.context.config, cwd=self.context.cwd)
		migrate_sqlite_store(paths["jobs"], target="jobs")
		self._job_store = SQLiteJobStore(paths["jobs"])
		return self._job_store


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
				expose_ops_tools=context.ops_tools_enabled(),
				audit_enabled=context.config.security.audit.enabled,
				workspace_root=str(context.workspace_root),
				workdir=str(context.layout.workdir),
				source_docs_root=str(context.source_docs_root),
				ops_tools=context.exposed_ops_tools(),
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


def register_ops_tools(
	mcp: Any,
	context: MCPAppContext,
	*,
	runtime: LazyQueryToolRuntime,
) -> tuple[RegisteredTool, ...]:
	"""Register gated operational tools for local single-user deployments only."""

	if not context.ops_tools_enabled():
		return ()

	@mcp.tool(
		name="ops_get_config",
		annotations=readonly_annotations(title="Active Ops Get Config"),
		tags={"ops", "config", "status"},
	)
	def ops_get_config() -> MCPOpsToolResult:
		"""Return the current non-sensitive config and effective ops exposure state."""

		with context.audit_logger.tool_call(tool="ops_get_config", caller="mcp.ops") as scope:
			result = _execute_ops(
				context=context,
				operation="ops_get_config",
				handler=lambda: MCPOpsToolResult(
					operation="ops_get_config",
					status="ok",
					summary="Returned the effective non-sensitive config and ops exposure state.",
					payload={
						"config": dict(context.config_summary),
						"deployment_mode": context.config.deployment_mode,
						"transport": context.config.server.transport,
						"expose_ops_tools": context.ops_tools_enabled(),
						"ops_tools": list(context.exposed_ops_tools()),
					},
				),
			)
			_finalize_ops_scope(scope, result)
			return result

	@mcp.tool(
		name="ops_validate_setup",
		annotations=readonly_annotations(title="Active Ops Validate Setup"),
		tags={"ops", "validate", "status"},
	)
	def ops_validate_setup(*, strict: bool = False) -> MCPOpsToolResult:
		"""Validate local path readiness, fail-safe security, and storage consistency."""

		with context.audit_logger.tool_call(
			tool="ops_validate_setup",
			caller="mcp.ops",
			details={"strict": strict},
		) as scope:
			result = _execute_ops(
				context=context,
				operation="ops_validate_setup",
				handler=lambda: _validate_setup_result(context=context, strict=strict),
			)
			_finalize_ops_scope(scope, result)
			return result

	@mcp.tool(
		name="ops_index_status",
		annotations=readonly_annotations(title="Active Ops Index Status"),
		tags={"ops", "index", "status"},
	)
	def ops_index_status(*, limit: int = 10) -> MCPOpsToolResult:
		"""Return storage validation plus recent persisted index jobs."""

		with context.audit_logger.tool_call(
			tool="ops_index_status",
			caller="mcp.ops",
			details={"limit": limit},
		) as scope:
			result = _execute_ops(
				context=context,
				operation="ops_index_status",
				handler=lambda: _index_status_result(context=context, runtime=runtime, limit=limit),
			)
			_finalize_ops_scope(scope, result)
			return result

	@mcp.tool(
		name="ops_start_index",
		annotations=tool_annotations(
			title="Active Ops Start Index",
			read_only=False,
			idempotent=False,
		),
		tags={"ops", "index", "write"},
	)
	def ops_start_index(
		*,
		mode: OpsIndexMode = "incremental",
		source: OpsIndexSource = "all",
		profile_id: str = ALL_SCOPE,
		snapshot_id: str = "current",
		resume: OpsIndexResume = "auto",
	) -> MCPOpsToolResult:
		"""Create one scheduler-owned index job request when no active job exists."""

		with context.audit_logger.tool_call(
			tool="ops_start_index",
			profile_id=profile_id,
			snapshot_id=snapshot_id,
			caller="mcp.ops",
			details={"mode": mode, "source": source, "resume": resume},
		) as scope:
			result = _execute_ops(
				context=context,
				operation="ops_start_index",
				handler=lambda: _start_index_result(
					runtime=runtime,
					mode=mode,
					source=source,
					profile_id=profile_id,
					snapshot_id=snapshot_id,
					resume=resume,
				),
			)
			_finalize_ops_scope(scope, result)
			return result

	@mcp.tool(
		name="ops_cancel_index",
		annotations=tool_annotations(
			title="Active Ops Cancel Index",
			read_only=False,
			idempotent=False,
			destructive=True,
		),
		tags={"ops", "index", "write"},
	)
	def ops_cancel_index(job_id: str) -> MCPOpsToolResult:
		"""Cancel one active or pending index job."""

		with context.audit_logger.tool_call(
			tool="ops_cancel_index",
			caller="mcp.ops",
			details={"job_id": job_id},
		) as scope:
			result = _execute_ops(
				context=context,
				operation="ops_cancel_index",
				handler=lambda: _cancel_index_result(runtime=runtime, job_id=job_id),
			)
			_finalize_ops_scope(scope, result)
			return result

	@mcp.tool(
		name="ops_resume_index",
		annotations=tool_annotations(
			title="Active Ops Resume Index",
			read_only=False,
			idempotent=False,
		),
		tags={"ops", "index", "write"},
	)
	def ops_resume_index(job_id: str) -> MCPOpsToolResult:
		"""Resume or retry one persisted index job request."""

		with context.audit_logger.tool_call(
			tool="ops_resume_index",
			caller="mcp.ops",
			details={"job_id": job_id},
		) as scope:
			result = _execute_ops(
				context=context,
				operation="ops_resume_index",
				handler=lambda: _resume_index_result(runtime=runtime, job_id=job_id),
			)
			_finalize_ops_scope(scope, result)
			return result

	@mcp.tool(
		name="ops_list_profiles",
		annotations=readonly_annotations(title="Active Ops List Profiles"),
		tags={"ops", "profile", "status"},
	)
	def ops_list_profiles(*, snapshot_id: str | None = None) -> MCPOpsToolResult:
		"""List indexed profiles from read-only metadata without triggering migrations."""

		with context.audit_logger.tool_call(
			tool="ops_list_profiles",
			snapshot_id=snapshot_id,
			caller="mcp.ops",
		) as scope:
			result = _execute_ops(
				context=context,
				operation="ops_list_profiles",
				handler=lambda: _list_profiles_result(runtime=runtime, snapshot_id=snapshot_id),
			)
			_finalize_ops_scope(scope, result)
			return result

	@mcp.tool(
		name="ops_list_sources",
		annotations=readonly_annotations(title="Active Ops List Sources"),
		tags={"ops", "source", "status"},
	)
	def ops_list_sources() -> MCPOpsToolResult:
		"""List indexed source roots from read-only metadata without triggering migrations."""

		with context.audit_logger.tool_call(tool="ops_list_sources", caller="mcp.ops") as scope:
			result = _execute_ops(
				context=context,
				operation="ops_list_sources",
				handler=lambda: _list_sources_result(runtime=runtime),
			)
			_finalize_ops_scope(scope, result)
			return result

	return tuple(
		RegisteredTool(
			name=name,
			description=description,
			handler=handler,
			tags=_OPS_TOOL_TAGS,
		)
		for name, description, handler in (
			("ops_get_config", "Return the effective non-sensitive config and ops exposure state.", ops_get_config),
			("ops_validate_setup", "Validate local path readiness, fail-safe security, and storage consistency.", ops_validate_setup),
			("ops_index_status", "Return storage validation plus recent persisted index jobs.", ops_index_status),
			("ops_start_index", "Create one scheduler-owned index job request when no active job exists.", ops_start_index),
			("ops_cancel_index", "Cancel one active or pending index job.", ops_cancel_index),
			("ops_resume_index", "Resume or retry one persisted index job request.", ops_resume_index),
			("ops_list_profiles", "List indexed profiles from read-only metadata without triggering migrations.", ops_list_profiles),
			("ops_list_sources", "List indexed source roots from read-only metadata without triggering migrations.", ops_list_sources),
		)
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
		started_at = time.perf_counter()
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
			context.observability_store.record_query_run(
				tool_name="workspace_view",
				result=result,
				latency_seconds=max(time.perf_counter() - started_at, 0.0),
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
		started_at = time.perf_counter()
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
			context.observability_store.record_query_run(
				tool_name="evidence_bundle",
				result=result,
				latency_seconds=max(time.perf_counter() - started_at, 0.0),
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


def _execute_ops(
	*,
	context: MCPAppContext,
	operation: str,
	handler: Any,
) -> MCPOpsToolResult:
	"""Execute one gated ops handler with consistent safety enforcement."""

	blocked = _ops_access_blocked(context=context, operation=operation)
	if blocked is not None:
		return blocked
	try:
		return handler()
	except Exception as exc:  # noqa: BLE001 - ops tools surface stable envelopes on unexpected failures.
		return _ops_error_result(
			operation=operation,
			summary=f"{operation} failed before returning a stable operational response.",
			exc=exc,
		)


def _finalize_ops_scope(scope: Any, result: MCPOpsToolResult) -> None:
	"""Write one stable audit summary for an ops tool result."""

	scope.set_result(
		result_count=_ops_result_count(result),
		result_status=result.status,
		warning_codes=[warning.code for warning in result.warnings],
		warning_levels=[warning.level for warning in result.warnings],
	)


def _validate_setup_result(*, context: MCPAppContext, strict: bool) -> MCPOpsToolResult:
	"""Build the stable validate-setup response used by the gated ops surface."""

	checks = _ops_validation_checks(context=context, strict=strict)
	storage_report = validate_storage_consistency(context.config, cwd=context.cwd)
	security_result = validate_startup_security(context.config)
	warnings = [
		*[_warning_from_validation_check(check) for check in checks if check["level"] != "ok"],
		*(warning.to_warning() for warning in security_result.warnings),
		*[_warning_from_storage_check(check) for check in storage_report.checks if check.severity != "info"],
	]
	error_checks = [check for check in checks if check["level"] == "error"]
	status = (
		"blocked"
		if strict and (error_checks or security_result.blocked or storage_report.status == "blocked")
		else "ok"
	)
	summary = (
		"Local setup validation completed without blocking issues."
		if status == "ok"
		else "Local setup validation found blocking issues under strict mode."
	)
	return MCPOpsToolResult(
		operation="ops_validate_setup",
		status=status,
		summary=summary,
		warnings=tuple(warnings),
		items=tuple(dict(check) for check in checks),
		payload={
			"strict": strict,
			"storage_report": storage_report.to_dict(),
			"security": {
				"ok": security_result.ok,
				"warnings": [warning.to_dict() for warning in security_result.warnings],
			},
		},
	)


def _index_status_result(
	*,
	context: MCPAppContext,
	runtime: LazyQueryToolRuntime,
	limit: int,
) -> MCPOpsToolResult:
	"""Build the stable index-status response with recent jobs and storage checks."""

	storage_report = validate_storage_consistency(context.config, cwd=context.cwd)
	recent_jobs = runtime.recent_jobs(limit=limit)
	job_status_counts = Counter(job.status for job in recent_jobs)
	job_checkpoints = {job.job_id: runtime.job_checkpoints(job.job_id) for job in recent_jobs}
	task_status_counts = Counter[str]()
	for job in recent_jobs:
		stats = _job_task_stats(job, checkpoints=job_checkpoints[job.job_id])
		for status_name, count in stats["checkpoint_counts"].items():
			task_status_counts[status_name] += int(count)
	warnings = tuple(
		_warning_from_storage_check(check)
		for check in storage_report.checks
		if check.severity != "info"
	)
	return MCPOpsToolResult(
		operation="ops_index_status",
		status="ok",
		summary="Returned recent index jobs plus current storage validation.",
		warnings=warnings,
		items=tuple(
			_job_payload(job, checkpoints=job_checkpoints[job.job_id])
			for job in recent_jobs
		),
		payload={
			"validation": storage_report.to_dict(),
			"job_status_counts": dict(job_status_counts),
			"task_status_counts": dict(task_status_counts),
		},
	)


def _start_index_result(
	*,
	runtime: LazyQueryToolRuntime,
	mode: OpsIndexMode,
	source: OpsIndexSource,
	profile_id: str,
	snapshot_id: str,
	resume: OpsIndexResume,
) -> MCPOpsToolResult:
	"""Create a stable accepted/conflict response for index-job scheduling."""

	try:
		job = runtime.create_index_job(
			mode=mode,
			source=source,
			profile_id=_normalize_scope_value(profile_id),
			snapshot_id=snapshot_id or "current",
			resume=resume,
		)
	except JobLockConflictError as exc:
		active_job = runtime.active_index_job()
		return MCPOpsToolResult(
			operation="ops_start_index",
			status="conflict",
			summary="Another index job is already active; the new job request was not accepted.",
			warnings=(
				Warning(
					level="caution",
					code="ops.index_job_active",
					message=str(exc),
					actionable=True,
					suggested_action="Inspect ops_index_status or cancel the active job before retrying.",
				),
			),
			payload={"active_job": _job_payload(active_job) if active_job is not None else None},
		)
	return MCPOpsToolResult(
		operation="ops_start_index",
		status="accepted",
		summary="Index job request accepted and persisted for scheduler-owned execution.",
		payload={
			"job": _job_payload(job),
			"execution_state": "scheduled",
			"requested_mode": mode,
			"requested_source": source,
			"resume": resume,
		},
	)


def _cancel_index_result(*, runtime: LazyQueryToolRuntime, job_id: str) -> MCPOpsToolResult:
	"""Cancel one index job and return a stable result envelope."""

	try:
		job = runtime.cancel_index_job(job_id)
	except KeyError:
		return MCPOpsToolResult(
			operation="ops_cancel_index",
			status="not_found",
			summary="The requested index job was not found.",
			warnings=(
				Warning(
					level="caution",
					code="ops.job_not_found",
					message=f"job {job_id!r} does not exist",
					actionable=True,
					suggested_action="Use ops_index_status to discover a valid job_id before retrying.",
				),
			),
		)
	except JobStateTransitionError as exc:
		return MCPOpsToolResult(
			operation="ops_cancel_index",
			status="conflict",
			summary="The requested index job is not cancelable in its current state.",
			warnings=(
				Warning(
					level="caution",
					code="ops.job_not_cancelable",
					message=str(exc),
					actionable=True,
					suggested_action="Inspect ops_index_status and retry only for pending or running index jobs.",
				),
			),
		)
	return MCPOpsToolResult(
		operation="ops_cancel_index",
		status="ok",
		summary="The requested index job was marked as cancelled.",
		payload={"job": _job_payload(job)},
	)


def _resume_index_result(*, runtime: LazyQueryToolRuntime, job_id: str) -> MCPOpsToolResult:
	"""Resume one index job and return a stable accepted/conflict envelope."""

	try:
		job = runtime.resume_index_job(job_id)
	except KeyError:
		return MCPOpsToolResult(
			operation="ops_resume_index",
			status="not_found",
			summary="The requested index job was not found.",
			warnings=(
				Warning(
					level="caution",
					code="ops.job_not_found",
					message=f"job {job_id!r} does not exist",
					actionable=True,
					suggested_action="Use ops_index_status to discover a valid job_id before retrying.",
				),
			),
		)
	except JobStateTransitionError as exc:
		return MCPOpsToolResult(
			operation="ops_resume_index",
			status="conflict",
			summary="The requested index job is not resumable in its current state.",
			warnings=(
				Warning(
					level="caution",
					code="ops.job_not_resumable",
					message=str(exc),
					actionable=True,
					suggested_action="Inspect ops_index_status or start a fresh job request.",
				),
			),
		)
	return MCPOpsToolResult(
		operation="ops_resume_index",
		status="accepted",
		summary="Index job resume request accepted and persisted for scheduler-owned execution.",
		payload={"job": _job_payload(job), "execution_state": "scheduled"},
	)


def _list_profiles_result(
	*,
	runtime: LazyQueryToolRuntime,
	snapshot_id: str | None,
) -> MCPOpsToolResult:
	"""List persisted profile records through the read-only metadata adapter."""

	filter_snapshot = None if snapshot_id in {None, "", "current"} else snapshot_id
	profiles = runtime.ops_reader().iter_profiles(snapshot_id=filter_snapshot)
	return MCPOpsToolResult(
		operation="ops_list_profiles",
		status="ok",
		summary=f"Returned {len(profiles)} indexed profiles.",
		items=tuple(_record_payload(profile) for profile in profiles),
		payload={"snapshot_id": filter_snapshot},
	)


def _list_sources_result(*, runtime: LazyQueryToolRuntime) -> MCPOpsToolResult:
	"""List persisted source records through the read-only metadata adapter."""

	sources = runtime.ops_reader().iter_sources()
	return MCPOpsToolResult(
		operation="ops_list_sources",
		status="ok",
		summary=f"Returned {len(sources)} indexed sources.",
		items=tuple(_record_payload(source) for source in sources),
		payload={"source_count": len(sources)},
	)


def _ops_access_blocked(
	*,
	context: MCPAppContext,
	operation: str,
) -> MCPOpsToolResult | None:
	"""Return a stable blocked result when ops tools are not effectively exposed."""

	if context.ops_tools_enabled():
		return None
	code = (
		"security.ops_exposure_blocked"
		if context.config.deployment_mode != "local_single_user"
		else "security.ops_tools_disabled"
	)
	message = (
		"Operational tools are never exposed outside local_single_user deployments."
		if context.config.deployment_mode != "local_single_user"
		else "Operational tools are disabled until server.expose_ops_tools=true is set."
	)
	suggested_action = (
		"Use local_single_user deployment mode for local operational access."
		if context.config.deployment_mode != "local_single_user"
		else "Enable server.expose_ops_tools in a trusted local_single_user deployment and retry."
	)
	return MCPOpsToolResult(
		operation=operation,
		status="blocked",
		summary="Operational tools are blocked by the effective server exposure policy.",
		warnings=(
			Warning(
				level="blocked",
				code=code,
				message=message,
				actionable=True,
				suggested_action=suggested_action,
			),
		),
		diagnostics={
			"deployment_mode": context.config.deployment_mode,
			"configured_expose_ops_tools": context.config.server.expose_ops_tools,
		},
	)


def _ops_error_result(*, operation: str, summary: str, exc: Exception) -> MCPOpsToolResult:
	"""Build a stable error envelope for unexpected ops-tool failures."""

	request_id = str(uuid4())
	return MCPOpsToolResult(
		operation=operation,
		status="error",
		summary=summary,
		warnings=(
			Warning(
				level="degraded",
				code="ops.unexpected_error",
				message=str(exc),
				actionable=True,
				suggested_action="Inspect the diagnostics, fix the underlying runtime issue, and retry.",
			),
		),
		diagnostics={
			"request_id": request_id,
			"error_kind": exc.__class__.__name__,
			"error_summary": str(exc),
		},
	)


def _ops_result_count(result: MCPOpsToolResult) -> int:
	"""Return a stable audit result-count across all ops response shapes."""

	return max(len(result.items), 1 if result.payload else 0)


def _ops_validation_checks(
	*,
	context: MCPAppContext,
	strict: bool,
) -> list[dict[str, str]]:
	"""Build setup validation checks reused by ops_validate_setup."""

	checks: list[dict[str, str]] = []
	for name, info in _ops_path_status(context).items():
		exists = bool(info["exists"])
		missing_is_error = strict and name in {"workspace_root", "source_docs_root", "workdir"}
		if exists:
			checks.append({"name": name, "level": "ok", "message": f"{info['path']} exists"})
		else:
			checks.append(
				{
					"name": name,
					"level": "error" if missing_is_error else "warning",
					"message": f"{info['path']} does not exist",
				}
			)
	checks.append(
		{
			"name": "server.transport",
			"level": "ok",
			"message": str(context.config.server.transport),
		}
	)
	security_result = validate_startup_security(context.config)
	if security_result.ok:
		checks.append(
			{
				"name": "security.fail_safe",
				"level": "ok",
				"message": "fail-safe startup security checks passed",
			}
		)
	else:
		for warning in security_result.warnings:
			checks.append({"name": warning.code, "level": "error", "message": warning.message})
	return checks


def _ops_path_status(context: MCPAppContext) -> dict[str, dict[str, str | bool]]:
	"""Return existence status for important local runtime paths."""

	paths = {
		"workspace_root": context.workspace_root,
		"source_docs_root": context.source_docs_root,
		"workdir": context.layout.workdir,
		"baseline_dir": context.layout.baseline_dir,
		"local_dir": context.layout.local_dir,
		"local_config": context.layout.local_config_path,
	}
	return {
		name: {"path": str(path), "exists": path.exists(), "kind": _ops_path_kind(path)}
		for name, path in paths.items()
	}


def _ops_path_kind(path: Path) -> str:
	"""Classify an existing or missing path for ops status payloads."""

	if path.is_dir():
		return "directory"
	if path.is_file():
		return "file"
	return "missing"


def _warning_from_validation_check(check: Mapping[str, str]) -> Warning:
	"""Convert one ops validation check into the shared warning contract."""

	level = "blocked" if check["level"] == "error" else "caution"
	return Warning(
		level=level,
		code=f"validation.{check['name']}",
		message=check["message"],
		actionable=True,
		suggested_action="Create the missing path or adjust the local server configuration before retrying.",
	)


def _warning_from_storage_check(check: Any) -> Warning:
	"""Convert one storage validation finding into the shared warning contract."""

	return Warning(
		level=check.severity,
		code=check.check_code,
		message=check.message,
		details=dict(check.details),
		actionable=check.suggested_action is not None,
		suggested_action=check.suggested_action,
		affected_sources=tuple(check.affected_objects),
	)


def _record_payload(record: Any) -> dict[str, Any]:
	"""Return one stable JSON payload for storage-backed dataclass records."""

	return dict(asdict(record))


def _job_payload(
	job: Any | None,
	*,
	checkpoints: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
	"""Return one stable JSON payload for a persisted job record."""

	if job is None:
		return None
	payload = _record_payload(job)
	payload["task_stats"] = _job_task_stats(job, checkpoints=checkpoints or {})
	return payload


def _ops_index_job_metadata(
	*,
	mode: OpsIndexMode,
	source: OpsIndexSource,
	profile_id: str,
	snapshot_id: str,
	resume: OpsIndexResume,
	requested_by: str,
) -> dict[str, Any]:
	"""Build MCP-owned index job metadata using the CLI job contract field names."""

	resume_policy = {
		"schema_version": _INDEX_RESUME_POLICY_SCHEMA_VERSION,
		"mode": "auto" if resume == "auto" else "disabled",
		"resume_enabled": resume == "auto",
	}
	return {
		"schema_version": _INDEX_JOB_CONTRACT_SCHEMA_VERSION,
		"execution_state": "scheduled",
		"requested_mode": mode,
		"requested_target": "overlay",
		"requested_source": source,
		"requested_profile_id": profile_id,
		"requested_snapshot_id": snapshot_id,
		"requested_by": requested_by,
		"resume_policy": resume_policy,
		"tasks_total": 0,
		"tasks_applied": 0,
		"tasks_skipped": 0,
		"tasks_failed": 0,
	}


def _job_task_stats(job: Any, *, checkpoints: Mapping[str, str]) -> dict[str, Any]:
	"""Return task-level stats from CLI metadata, falling back to checkpoint KV."""

	metadata = getattr(job, "metadata", {})
	checkpoint_counts: Counter[str] = Counter()
	applied_by_phase: Counter[str] = Counter()
	collected_by_phase: Counter[str] = Counter()
	for key, value in checkpoints.items():
		if not key.startswith("task:"):
			continue
		checkpoint = decode_task_checkpoint(value)
		if checkpoint is None:
			continue
		checkpoint_counts[checkpoint.status] += 1
		if checkpoint.status == "applied":
			applied_by_phase[checkpoint.phase] += 1
		elif checkpoint.status == "collected":
			collected_by_phase[checkpoint.phase] += 1

	applied = _metadata_int(metadata.get("tasks_applied"))
	if applied is None:
		applied = checkpoint_counts["applied"]
	else:
		applied = max(applied, checkpoint_counts["applied"])
	return {
		"tasks_total": _metadata_int(metadata.get("tasks_total")),
		"tasks_applied": applied,
		"tasks_skipped": _metadata_int(metadata.get("tasks_skipped")) or 0,
		"tasks_failed": _metadata_int(metadata.get("tasks_failed")) or 0,
		"tasks_required": _metadata_int(metadata.get("tasks_required")),
		"tasks_by_phase": _metadata_mapping(metadata.get("tasks_by_phase")),
		"tasks_by_source_kind": _metadata_mapping(metadata.get("tasks_by_source_kind")),
		"last_task_key": metadata.get("last_task_key"),
		"last_phase": metadata.get("last_phase"),
		"checkpoint_counts": dict(sorted(checkpoint_counts.items())),
		"applied_by_phase": dict(sorted(applied_by_phase.items())),
		"collected_by_phase": dict(sorted(collected_by_phase.items())),
	}


def _metadata_int(value: object) -> int | None:
	if isinstance(value, int):
		return value
	if isinstance(value, str) and value.isdecimal():
		return int(value)
	return None


def _metadata_mapping(value: object) -> dict[str, Any]:
	if not isinstance(value, Mapping):
		return {}
	return {str(key): item for key, item in value.items()}


def _normalize_scope_value(value: str | None) -> str:
	"""Normalize optional scope-like input values for persisted ops job metadata."""

	if value is None:
		return ALL_SCOPE
	resolved = value.strip()
	if not resolved or resolved == "auto":
		return ALL_SCOPE
	return resolved


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
	started_at = time.perf_counter()
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
		context.observability_store.record_query_run(
			tool_name=tool_name,
			result=result,
			latency_seconds=max(time.perf_counter() - started_at, 0.0),
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
