from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.indexing.workspace_map import WorkspaceMapArtifact, WorkspaceProjectionView, WorkspaceTreeNode, WorkspaceViewItem
from active_knowledge_server.mcp import create_fastmcp_app
from active_knowledge_server.models.evidence import EvidenceRef
from active_knowledge_server.models.query import QueryRequest
from active_knowledge_server.models.responses import QueryResult


def _resolved_config(tmp_path: Path) -> object:
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
	}
	return resolve_config(cli_overrides=overrides, cwd=tmp_path)


def test_docs_search_normalizes_returned_tool_name_and_domain(tmp_path: Path, monkeypatch) -> None:
	resolved = _resolved_config(tmp_path)
	runtime = create_fastmcp_app(resolved, cwd=tmp_path)
	handler = {tool.name: tool.handler for tool in runtime.inventory.tools}["docs_search"]
	request_seen: dict[str, QueryRequest] = {}

	def fake_search(request: QueryRequest) -> QueryResult:
		request_seen["request"] = request
		return QueryResult(
			tool_name="kb_search",
			result_status="ok",
			confidence=0.91,
			query_intent="api_lookup",
			snapshot_id=request.snapshot_id or "current",
			profile_id="not_required",
			summary="found docs",
			items=({"path": "knowledge-sources/api/foo.md"},),
		)

	monkeypatch.setattr(runtime.query_runtime, "search_query", fake_search)

	result = handler("foo api", doc_type="api")
	snapshot = runtime.context.observability_store.load_snapshot()

	assert result.tool_name == "docs_search"
	assert request_seen["request"].caller_tool == "docs_search"
	assert request_seen["request"].domain == "api"
	assert request_seen["request"].granularity == "doc_section"
	assert snapshot["metrics"]["retrieval_candidates_total"] == 1
	assert snapshot["recent_queries"][0]["tool_name"] == "docs_search"


def test_config_impact_passes_compare_to_in_client_context(tmp_path: Path, monkeypatch) -> None:
	resolved = _resolved_config(tmp_path)
	runtime = create_fastmcp_app(resolved, cwd=tmp_path)
	handler = {tool.name: tool.handler for tool in runtime.inventory.tools}["config_impact"]
	request_seen: dict[str, QueryRequest] = {}

	def fake_search(request: QueryRequest) -> QueryResult:
		request_seen["request"] = request
		return QueryResult(
			tool_name="config_impact",
			result_status="ok",
			confidence=0.94,
			query_intent="profile_diff",
			snapshot_id=request.snapshot_id or "current",
			profile_id="board_a",
			summary="profile impact",
			items=({"profile_id": "board_a"},),
		)

	monkeypatch.setattr(runtime.query_runtime, "search_query", fake_search)

	result = handler("CONFIG_FOO", compare_to="board_b", profile_id="board_a")

	assert result.tool_name == "config_impact"
	assert request_seen["request"].client_context == {"compare_to": "board_b"}
	assert request_seen["request"].view == "profile"
	assert request_seen["request"].granularity == "profile"


def test_workspace_view_returns_projection_items(tmp_path: Path, monkeypatch) -> None:
	resolved = _resolved_config(tmp_path)
	runtime = create_fastmcp_app(resolved, cwd=tmp_path)
	handler = {tool.name: tool.handler for tool in runtime.inventory.tools}["workspace_view"]

	artifact = WorkspaceMapArtifact(
		schema_version="workspace_map.v1",
		snapshot_id="current",
		workspace_root=str(tmp_path / "workspace"),
		inventory_hash="inv-1",
		generated_at="2025-01-01T00:00:00Z",
		summary={"areas": 1},
		workspace_tree=WorkspaceTreeNode(
			node_id="root",
			name="workspace",
			path="",
			role="workspace",
			layer=None,
			domain=None,
			feature=None,
			summary="root",
			direct_file_count=0,
			total_file_count=0,
			module_count=0,
			children=(),
		),
		views={
			"workspace": WorkspaceProjectionView(
				view_name="workspace",
				summary="workspace summary",
				items=(
					WorkspaceViewItem(
						item_id="module:core",
						kind="module",
						name="core",
						summary="Core module",
						source_paths=("src/core.c",),
					),
				),
			),
		},
		metadata={"builder": "test"},
	)

	monkeypatch.setattr(runtime.query_runtime, "collect_workspace_artifact", lambda **_: artifact)

	result = handler(view="workspace", query="core")

	assert result.tool_name == "workspace_view"
	assert result.result_status == "ok"
	assert result.items[0]["item_id"] == "module:core"
	assert result.diagnostics["view"] == "workspace"


def test_evidence_bundle_supports_explicit_entity_ids(tmp_path: Path, monkeypatch) -> None:
	resolved = _resolved_config(tmp_path)
	runtime = create_fastmcp_app(resolved, cwd=tmp_path)
	handler = {tool.name: tool.handler for tool in runtime.inventory.tools}["evidence_bundle"]

	evidence_refs = (
		EvidenceRef(
			evidence_id="ev:1",
			type="code",
			path="src/core.c",
			start_line=10,
			end_line=20,
			authority_level="code.primary",
			excerpt="int core(void);",
		),
		)
	monkeypatch.setattr(runtime.query_runtime, "bundle_evidence_for_entity", lambda *args, **kwargs: evidence_refs)

	result = handler(entity_id="entity:core")

	assert result.tool_name == "evidence_bundle"
	assert result.result_status == "ok"
	assert result.evidence_refs == evidence_refs
	assert result.items[0]["entity_id"] == "entity:core"
