"""Bootstrap MCP resources for the FastMCP facade."""

from __future__ import annotations

from collections import Counter
from typing import Any

from active_knowledge_server.mcp.annotations import readonly_annotations
from active_knowledge_server.mcp.schemas import (
	MCPEntityResource,
	MCPEvidenceResource,
	MCPAppContext,
	MCPConfigSummaryResource,
	MCPIndexStatusResource,
	MCPProfileResource,
	MCPServerRuntimeResource,
	MCPSnapshotResource,
	MCPWorkspaceSummaryResource,
	MCPWorkspaceTreeResource,
	RegisteredResource,
	serialize_json_resource,
)
from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.query.evidence_packager import (
	_authority_level,
	_evidence_type,
	_hash_text,
	_mapping_text,
	_summary_from_text,
)
from active_knowledge_server.security.path_guard import PathBlockedError
from active_knowledge_server.security.secret_scan import SecretScanner
from active_knowledge_server.storage import QueryScope
from active_knowledge_server.storage.validation import validate_storage_consistency

_RESOURCE_EXCERPT_LIMIT = 220


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
				expose_ops_tools=context.ops_tools_enabled(),
				audit_enabled=context.config.security.audit.enabled,
				ops_tools=context.exposed_ops_tools(),
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


def register_query_resources(
	mcp: Any,
	context: MCPAppContext,
	*,
	runtime: Any,
) -> tuple[RegisteredResource, ...]:
	"""Register the read-only MCP resources required by M6-03."""

	secret_scanner = SecretScanner.from_config(context.config)

	@mcp.resource(
		"active://snapshot/current",
		name="ActiveKnowledgeCurrentSnapshot",
		description="Current snapshot metadata and available profiles.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Current Snapshot"),
		tags={"query", "resource", "snapshot"},
	)
	def current_snapshot() -> str:
		"""Return the current snapshot alias plus resolved stable snapshot metadata."""

		return serialize_json_resource(
			_build_snapshot_resource(runtime=runtime, requested_snapshot_id="current")
		)

	@mcp.resource(
		"active://profile/{profile_id}",
		name="ActiveKnowledgeProfile",
		description="Current-snapshot profile metadata resolved by profile_id.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Profile Resource"),
		tags={"query", "resource", "profile"},
	)
	def current_profile(profile_id: str) -> str:
		"""Return one current-snapshot profile record by profile_id."""

		return serialize_json_resource(
			_build_profile_resource(
				runtime=runtime,
				requested_snapshot_id="current",
				requested_profile_id=profile_id,
			)
		)

	@mcp.resource(
		"active://workspace/current/summary",
		name="ActiveKnowledgeWorkspaceSummary",
		description="Current workspace projection summary without the full tree payload.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Workspace Summary"),
		tags={"query", "resource", "workspace"},
	)
	def workspace_summary() -> str:
		"""Return the current workspace summary and available projection views."""

		return serialize_json_resource(
			_build_workspace_summary_resource(
				runtime=runtime,
				requested_snapshot_id="current",
			)
		)

	@mcp.resource(
		"active://workspace/current/tree",
		name="ActiveKnowledgeWorkspaceTree",
		description="Current workspace tree projection for navigation surfaces.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Workspace Tree"),
		tags={"query", "resource", "workspace"},
	)
	def workspace_tree() -> str:
		"""Return the current workspace tree projection."""

		return serialize_json_resource(
			_build_workspace_tree_resource(
				runtime=runtime,
				requested_snapshot_id="current",
			)
		)

	@mcp.resource(
		"active://entity/{entity_id}",
		name="ActiveKnowledgeEntity",
		description="Logical entity view for one current-snapshot entity_id.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Entity Resource"),
		tags={"query", "resource", "entity"},
	)
	def entity(entity_id: str) -> str:
		"""Return one logical entity view without mutating runtime state."""

		return serialize_json_resource(
			_build_entity_resource(
				runtime=runtime,
				requested_snapshot_id="current",
				requested_entity_id=entity_id,
			)
		)

	@mcp.resource(
		"active://evidence/{evidence_id}",
		name="ActiveKnowledgeEvidence",
		description="Logical evidence view and stable evidence reference for one evidence_id.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Evidence Resource"),
		tags={"query", "resource", "evidence"},
	)
	def evidence(evidence_id: str) -> str:
		"""Return one logical evidence view and sanitized evidence reference."""

		return serialize_json_resource(
			_build_evidence_resource(
				runtime=runtime,
				requested_snapshot_id="current",
				requested_evidence_id=evidence_id,
				secret_scanner=secret_scanner,
			)
		)

	@mcp.resource(
		"active://index/status",
		name="ActiveKnowledgeIndexStatus",
		description="Current index validation status and recent job summary.",
		mime_type="application/json",
		annotations=readonly_annotations(title="Index Status"),
		tags={"query", "resource", "status", "index"},
	)
	def index_status() -> str:
		"""Return the current index validation report and recent jobs."""

		return serialize_json_resource(
			_build_index_status_resource(
				context=context,
				runtime=runtime,
				requested_snapshot_id="current",
			)
		)

	return (
		RegisteredResource(
			uri="active://snapshot/current",
			name="ActiveKnowledgeCurrentSnapshot",
			description="Current snapshot metadata and available profiles.",
			handler=current_snapshot,
			tags=("query", "resource", "snapshot"),
		),
		RegisteredResource(
			uri="active://profile/{profile_id}",
			name="ActiveKnowledgeProfile",
			description="Current-snapshot profile metadata resolved by profile_id.",
			handler=current_profile,
			tags=("query", "resource", "profile"),
		),
		RegisteredResource(
			uri="active://workspace/current/summary",
			name="ActiveKnowledgeWorkspaceSummary",
			description="Current workspace projection summary without the full tree payload.",
			handler=workspace_summary,
			tags=("query", "resource", "workspace"),
		),
		RegisteredResource(
			uri="active://workspace/current/tree",
			name="ActiveKnowledgeWorkspaceTree",
			description="Current workspace tree projection for navigation surfaces.",
			handler=workspace_tree,
			tags=("query", "resource", "workspace"),
		),
		RegisteredResource(
			uri="active://entity/{entity_id}",
			name="ActiveKnowledgeEntity",
			description="Logical entity view for one current-snapshot entity_id.",
			handler=entity,
			tags=("query", "resource", "entity"),
		),
		RegisteredResource(
			uri="active://evidence/{evidence_id}",
			name="ActiveKnowledgeEvidence",
			description="Logical evidence view and stable evidence reference for one evidence_id.",
			handler=evidence,
			tags=("query", "resource", "evidence"),
		),
		RegisteredResource(
			uri="active://index/status",
			name="ActiveKnowledgeIndexStatus",
			description="Current index validation status and recent job summary.",
			handler=index_status,
			tags=("query", "resource", "status", "index"),
		),
	)


def _build_snapshot_resource(*, runtime: Any, requested_snapshot_id: str) -> MCPSnapshotResource:
	reader, snapshot, resolved_snapshot_id = _snapshot_lookup(runtime=runtime, requested_snapshot_id=requested_snapshot_id)
	profile_ids = _profile_ids(reader, resolved_snapshot_id)
	requested_uri = "active://snapshot/current"
	if snapshot is None:
		return MCPSnapshotResource(
			status="missing",
			requested_uri=requested_uri,
			requested_snapshot_id=requested_snapshot_id,
			resolved_snapshot_id=resolved_snapshot_id,
			available_profile_ids=profile_ids,
			message="No snapshot metadata is indexed for the requested snapshot alias.",
		)
	return MCPSnapshotResource(
		requested_uri=requested_uri,
		requested_snapshot_id=requested_snapshot_id,
		snapshot_id=snapshot.snapshot_id,
		resolved_snapshot_id=resolved_snapshot_id,
		workspace_revision=snapshot.workspace_revision,
		baseline_id=snapshot.baseline_id,
		manifest_version=snapshot.manifest_version,
		created_at=snapshot.created_at,
		available_profile_ids=profile_ids,
		metadata=dict(snapshot.metadata),
	)


def _build_profile_resource(
	*,
	runtime: Any,
	requested_snapshot_id: str,
	requested_profile_id: str,
) -> MCPProfileResource:
	reader, snapshot, resolved_snapshot_id = _snapshot_lookup(runtime=runtime, requested_snapshot_id=requested_snapshot_id)
	profiles = tuple(reader.iter_profiles(snapshot_id=resolved_snapshot_id))
	available_profile_ids = tuple(dict.fromkeys(record.profile_id for record in profiles))
	profile = next((record for record in profiles if record.profile_id == requested_profile_id), None)
	requested_uri = f"active://profile/{requested_profile_id}"
	if profile is None:
		return MCPProfileResource(
			status="missing",
			requested_uri=requested_uri,
			requested_profile_id=requested_profile_id,
			snapshot_id=None if snapshot is None else snapshot.snapshot_id,
			resolved_snapshot_id=resolved_snapshot_id,
			available_profile_ids=available_profile_ids,
			message="The requested profile_id is not indexed for the current snapshot.",
		)
	return MCPProfileResource(
		requested_uri=requested_uri,
		requested_profile_id=requested_profile_id,
		snapshot_id=profile.snapshot_id,
		resolved_snapshot_id=resolved_snapshot_id,
		profile_record_id=profile.profile_record_id,
		profile_id=profile.profile_id,
		defconfig_hash=profile.defconfig_hash,
		dotconfig_hash=profile.dotconfig_hash,
		defconfig_path=profile.defconfig_path,
		dotconfig_path=profile.dotconfig_path,
		app=profile.app,
		board=profile.board,
		available_profile_ids=available_profile_ids,
		metadata=dict(profile.metadata),
	)


def _build_workspace_summary_resource(
	*,
	runtime: Any,
	requested_snapshot_id: str,
) -> MCPWorkspaceSummaryResource:
	_, snapshot, resolved_snapshot_id = _snapshot_lookup(runtime=runtime, requested_snapshot_id=requested_snapshot_id)
	requested_uri = "active://workspace/current/summary"
	try:
		artifact = runtime.collect_workspace_artifact_readonly(snapshot_id=resolved_snapshot_id)
	except PathBlockedError as exc:
		return MCPWorkspaceSummaryResource(
			status="blocked",
			requested_uri=requested_uri,
			requested_snapshot_id=requested_snapshot_id,
			snapshot_id=None if snapshot is None else snapshot.snapshot_id,
			resolved_snapshot_id=resolved_snapshot_id,
			message=str(exc),
		)
	except Exception as exc:
		return MCPWorkspaceSummaryResource(
			status="error",
			requested_uri=requested_uri,
			requested_snapshot_id=requested_snapshot_id,
			snapshot_id=None if snapshot is None else snapshot.snapshot_id,
			resolved_snapshot_id=resolved_snapshot_id,
			message=str(exc),
		)
	return MCPWorkspaceSummaryResource(
		requested_uri=requested_uri,
		requested_snapshot_id=requested_snapshot_id,
		snapshot_id=artifact.snapshot_id,
		resolved_snapshot_id=resolved_snapshot_id,
		schema_version=artifact.schema_version,
		workspace_root=artifact.workspace_root,
		inventory_hash=artifact.inventory_hash,
		generated_at=artifact.generated_at,
		summary=dict(artifact.summary),
		view_names=tuple(sorted(artifact.views)),
		view_summaries={name: view.summary for name, view in sorted(artifact.views.items())},
		metadata=dict(artifact.metadata),
	)


def _build_workspace_tree_resource(
	*,
	runtime: Any,
	requested_snapshot_id: str,
) -> MCPWorkspaceTreeResource:
	_, snapshot, resolved_snapshot_id = _snapshot_lookup(runtime=runtime, requested_snapshot_id=requested_snapshot_id)
	requested_uri = "active://workspace/current/tree"
	try:
		artifact = runtime.collect_workspace_artifact_readonly(snapshot_id=resolved_snapshot_id)
	except PathBlockedError as exc:
		return MCPWorkspaceTreeResource(
			status="blocked",
			requested_uri=requested_uri,
			requested_snapshot_id=requested_snapshot_id,
			snapshot_id=None if snapshot is None else snapshot.snapshot_id,
			resolved_snapshot_id=resolved_snapshot_id,
			message=str(exc),
		)
	except Exception as exc:
		return MCPWorkspaceTreeResource(
			status="error",
			requested_uri=requested_uri,
			requested_snapshot_id=requested_snapshot_id,
			snapshot_id=None if snapshot is None else snapshot.snapshot_id,
			resolved_snapshot_id=resolved_snapshot_id,
			message=str(exc),
		)
	return MCPWorkspaceTreeResource(
		requested_uri=requested_uri,
		requested_snapshot_id=requested_snapshot_id,
		snapshot_id=artifact.snapshot_id,
		resolved_snapshot_id=resolved_snapshot_id,
		schema_version=artifact.schema_version,
		workspace_root=artifact.workspace_root,
		inventory_hash=artifact.inventory_hash,
		generated_at=artifact.generated_at,
		summary=dict(artifact.summary),
		workspace_tree=artifact.workspace_tree.to_dict(),
		metadata=dict(artifact.metadata),
	)


def _build_entity_resource(
	*,
	runtime: Any,
	requested_snapshot_id: str,
	requested_entity_id: str,
) -> MCPEntityResource:
	reader, snapshot, resolved_snapshot_id = _snapshot_lookup(runtime=runtime, requested_snapshot_id=requested_snapshot_id)
	requested_uri = f"active://entity/{requested_entity_id}"
	scope = QueryScope(snapshot_id=resolved_snapshot_id)
	logical = next(
		(item for item in reader.logical_entities(scope) if item.logical_object_id == requested_entity_id),
		None,
	)
	if logical is None:
		return MCPEntityResource(
			status="missing",
			requested_uri=requested_uri,
			requested_entity_id=requested_entity_id,
			snapshot_id=None if snapshot is None else snapshot.snapshot_id,
			resolved_snapshot_id=resolved_snapshot_id,
			message="The requested entity_id is not available in the current logical view.",
		)
	record = logical.record
	file_record = reader.get_file(record.file_id)
	path = record.path or (None if file_record is None else file_record.relative_path)
	return MCPEntityResource(
		requested_uri=requested_uri,
		requested_entity_id=requested_entity_id,
		snapshot_id=record.snapshot_id,
		resolved_snapshot_id=resolved_snapshot_id,
		entity_id=logical.logical_object_id,
		source_index=logical.source_index,
		entity_type=record.entity_type,
		name=record.name,
		qualified_name=record.qualified_name,
		path=path,
		profile_id=record.profile_id,
		start_line=record.start_line,
		end_line=record.end_line,
		replaced_from=logical.replaced_from,
		metadata=dict(record.metadata),
	)


def _build_evidence_resource(
	*,
	runtime: Any,
	requested_snapshot_id: str,
	requested_evidence_id: str,
	secret_scanner: SecretScanner,
) -> MCPEvidenceResource:
	reader, snapshot, resolved_snapshot_id = _snapshot_lookup(runtime=runtime, requested_snapshot_id=requested_snapshot_id)
	requested_uri = f"active://evidence/{requested_evidence_id}"
	scope = QueryScope(snapshot_id=resolved_snapshot_id)
	logical = next(
		(item for item in reader.logical_evidence(scope) if item.logical_object_id == requested_evidence_id),
		None,
	)
	if logical is None:
		return MCPEvidenceResource(
			status="missing",
			requested_uri=requested_uri,
			requested_evidence_id=requested_evidence_id,
			snapshot_id=None if snapshot is None else snapshot.snapshot_id,
			resolved_snapshot_id=resolved_snapshot_id,
			message="The requested evidence_id is not available in the current logical view.",
		)
	record = logical.record
	object_scope = QueryScope(
		snapshot_id=record.snapshot_id,
		profile_id=record.profile_id,
		source_scope=record.source_scope,
	)
	resolved_object_id = reader.resolve_replacement(
		record.object_type,
		record.object_id,
		object_scope,
	).resolved_object_id
	return MCPEvidenceResource(
		requested_uri=requested_uri,
		requested_evidence_id=requested_evidence_id,
		snapshot_id=record.snapshot_id,
		resolved_snapshot_id=resolved_snapshot_id,
		evidence_id=logical.logical_object_id,
		source_index=logical.source_index,
		object_type=record.object_type,
		object_id=resolved_object_id,
		profile_id=record.profile_id,
		citation_label=record.citation_label,
		start_line=record.start_line,
		end_line=record.end_line,
		replaced_from=logical.replaced_from,
		evidence_ref=_evidence_ref_from_logical(
			logical=logical,
			reader=reader,
			secret_scanner=secret_scanner,
		),
		metadata=dict(record.metadata),
	)


def _build_index_status_resource(
	*,
	context: MCPAppContext,
	runtime: Any,
	requested_snapshot_id: str,
) -> MCPIndexStatusResource:
	reader, snapshot, resolved_snapshot_id = _snapshot_lookup(runtime=runtime, requested_snapshot_id=requested_snapshot_id)
	requested_uri = "active://index/status"
	validation = validate_storage_consistency(
		context.config,
		cwd=context.cwd,
		scope=QueryScope(snapshot_id=resolved_snapshot_id),
	).to_dict()
	recent_jobs_raw = tuple(reader.iter_jobs())[:10]
	recent_jobs = tuple(
		{
			"job_id": job.job_id,
			"job_type": job.job_type,
			"status": job.status,
			"write_target": job.write_target,
			"created_at": job.created_at,
			"updated_at": job.updated_at,
			"snapshot_id": job.snapshot_id,
			"profile_id": job.profile_id,
			"error_summary": job.error_summary,
			"metadata": dict(job.metadata),
		}
		for job in recent_jobs_raw
	)
	job_status_counts = dict(sorted(Counter(job.status for job in recent_jobs_raw).items()))
	status = validation.get("status")
	resource_status = "ok" if status == "ok" else status
	return MCPIndexStatusResource(
		status="missing" if snapshot is None and not recent_jobs and not validation.get("checks") else resource_status,
		requested_uri=requested_uri,
		requested_snapshot_id=requested_snapshot_id,
		snapshot_id=None if snapshot is None else snapshot.snapshot_id,
		resolved_snapshot_id=resolved_snapshot_id,
		validation=validation,
		recent_jobs=recent_jobs,
		job_status_counts=job_status_counts,
		message=(
			"No snapshot metadata is indexed for the requested snapshot alias."
			if snapshot is None and not recent_jobs and not validation.get("checks")
			else None
		),
	)


def _snapshot_lookup(*, runtime: Any, requested_snapshot_id: str) -> tuple[Any, Any | None, str]:
	reader = runtime.resource_reader()
	snapshot = reader.get_snapshot(requested_snapshot_id)
	resolved_snapshot_id = requested_snapshot_id
	if snapshot is not None:
		resolved_snapshot_id = _resolved_snapshot_id(snapshot)
	return reader, snapshot, resolved_snapshot_id


def _resolved_snapshot_id(snapshot: Any) -> str:
	resolved = snapshot.metadata.get("resolved_snapshot_id")
	if isinstance(resolved, str) and resolved.strip():
		return resolved.strip()
	return snapshot.snapshot_id


def _profile_ids(reader: Any, snapshot_id: str) -> tuple[str, ...]:
	return tuple(
		dict.fromkeys(record.profile_id for record in reader.iter_profiles(snapshot_id=snapshot_id))
	)


def _evidence_ref_from_logical(
	*,
	logical: Any,
	reader: Any,
	secret_scanner: SecretScanner,
) -> EvidenceRef | None:
	record = logical.record
	chunk = reader.get_chunk(record.chunk_id) if record.chunk_id else None
	file_record = reader.get_file(record.file_id)
	entity = reader.get_entity(record.object_id) if record.object_type == "entity" else None
	path = (
		_mapping_text(record.metadata, "path")
		or (None if file_record is None else file_record.relative_path)
		or record.citation_label
	)
	if path is None:
		return None
	start_line = record.start_line
	if start_line is None and chunk is not None:
		start_line = chunk.start_line
	if start_line is None and entity is not None:
		start_line = entity.start_line
	end_line = record.end_line
	if end_line is None and chunk is not None:
		end_line = chunk.end_line
	if end_line is None and entity is not None:
		end_line = entity.end_line
	excerpt_source = record.excerpt
	if excerpt_source is None and chunk is not None:
		excerpt_source = chunk.text
	if excerpt_source is None and entity is not None:
		excerpt_source = entity.qualified_name
	excerpt = _finalize_excerpt(excerpt_source, secret_scanner=secret_scanner)
	content_hash = (
		_mapping_text(record.metadata, "content_hash")
		or (None if chunk is None else chunk.content_hash)
		or (None if file_record is None else file_record.content_hash)
		or _hash_text(excerpt)
	)
	return EvidenceRef(
		evidence_id=record.evidence_id,
		type=_evidence_type(path=path, object_type=record.object_type),
		path=path,
		start_line=start_line,
		end_line=end_line,
		authority_level=_authority_level(path=path, metadata=record.metadata),
		excerpt=excerpt,
		content_hash=content_hash,
		source_index=logical.source_index,
	)


def _finalize_excerpt(text: str | None, *, secret_scanner: SecretScanner) -> str | None:
	summary = _summary_from_text(text, limit=_RESOURCE_EXCERPT_LIMIT)
	if not summary:
		return None
	sanitized = secret_scanner.sanitize_excerpt(summary)
	if len(sanitized) <= _RESOURCE_EXCERPT_LIMIT:
		return sanitized
	return _summary_from_text(sanitized, limit=_RESOURCE_EXCERPT_LIMIT)
