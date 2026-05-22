from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.indexing.workspace_map import (
    WorkspaceMapArtifact,
    WorkspaceProjectionView,
    WorkspaceTreeNode,
    WorkspaceViewItem,
)
from active_knowledge_server.mcp import create_fastmcp_app
from active_knowledge_server.mcp.schemas import OPS_TOOL_NAMES, QUERY_TOOL_NAMES
from active_knowledge_server.models import QueryRequest
from active_knowledge_server.models.responses import QueryResult
from active_knowledge_server.query import QueryRouter

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "skill_routing_examples.yaml"


@dataclass(frozen=True)
class StubProfileResolution:
    requested: str | None
    status: str
    resolved_profile_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "status": self.status,
            "resolved_profile_id": self.resolved_profile_id,
            "profile_record_id": None,
            "source": "stub",
            "confidence": None,
            "candidates": [],
            "warnings": [],
        }


@dataclass(frozen=True)
class StubCollectedProfiles:
    resolution: StubProfileResolution


class StubProfileCollector:
    def collect(
        self,
        snapshot_id: str | None = None,
        *,
        requested_profile_id: str | None = None,
        build_outputs_manifest: object | None = None,
        client_context: dict[str, object] | None = None,
    ) -> StubCollectedProfiles:
        del snapshot_id, build_outputs_manifest, client_context
        if requested_profile_id not in (None, "auto"):
            return StubCollectedProfiles(
                resolution=StubProfileResolution(
                    requested=requested_profile_id,
                    status="resolved",
                    resolved_profile_id=requested_profile_id,
                )
            )
        return StubCollectedProfiles(
            resolution=StubProfileResolution(
                requested="auto",
                status="unresolved",
            )
        )


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


def _resolve_model(tmp_path: Path) -> object:
    resolved = _resolved_config(tmp_path)
    return resolved.model


def _load_spec() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


def _fake_query_result(request: QueryRequest) -> QueryResult:
    profile_id = request.profile_id if request.profile_id not in (None, "", "auto") else "not_required"
    snapshot_id = request.snapshot_id or "current"
    return QueryResult(
        tool_name=request.caller_tool,
        result_status="ok",
        confidence=0.9,
        query_intent="unknown",
        snapshot_id=snapshot_id,
        profile_id=profile_id,
        summary=f"smoke result for {request.caller_tool}",
        items=({"query": request.query},),
    )


def _workspace_artifact(workspace_root: Path) -> WorkspaceMapArtifact:
    item = WorkspaceViewItem(
        item_id="module:demo",
        kind="module",
        name="demo",
        summary="Demo workspace item",
        source_paths=("packages/services/demo",),
    )
    views = {
        name: WorkspaceProjectionView(
            view_name=name,
            summary=f"{name} summary",
            items=(item,),
        )
        for name in ("workspace", "layer", "domain", "feature", "profile")
    }
    return WorkspaceMapArtifact(
        schema_version="workspace_map.v1",
        snapshot_id="current",
        workspace_root=str(workspace_root),
        inventory_hash="inv-1",
        generated_at="2026-05-22T00:00:00Z",
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
            module_count=1,
            children=(),
        ),
        views=views,
        metadata={"builder": "skill-routing-smoke"},
    )


def test_skill_routing_examples_reference_registered_query_tools(tmp_path: Path, monkeypatch) -> None:
    spec = _load_spec()
    resolved = _resolved_config(tmp_path)
    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}

    assert len(spec["examples"]) >= 10
    assert len(spec["route_matrix"]) >= 8

    monkeypatch.setattr(runtime.query_runtime, "search_query", _fake_query_result)
    monkeypatch.setattr(
        runtime.query_runtime,
        "collect_workspace_artifact",
        lambda **_: _workspace_artifact(tmp_path / "workspace"),
    )

    registered_tool_names = set(runtime.inventory.tool_names)
    allowed_query_tools = set(QUERY_TOOL_NAMES)
    forbidden_ops_tools = set(OPS_TOOL_NAMES)

    for route in spec["route_matrix"]:
        assert route["primary_tool"] in allowed_query_tools
        assert route["primary_tool"] in registered_tool_names
        assert route["primary_tool"] not in forbidden_ops_tools
        for tool_name in route.get("fallback_tools", []):
            assert tool_name in allowed_query_tools
            assert tool_name not in forbidden_ops_tools

    for example in spec["examples"]:
        for tool_call in example["tool_calls"]:
            tool_name = tool_call["tool"]
            assert tool_name in allowed_query_tools
            assert tool_name in registered_tool_names
            assert tool_name not in forbidden_ops_tools

        first_call = example["tool_calls"][0]
        result = handlers[first_call["tool"]](**first_call.get("args", {}))
        assert result.tool_name == first_call["tool"]
        assert result.result_status in {"ok", "zero_result"}


def test_skill_routing_warning_examples_match_router_behavior(tmp_path: Path) -> None:
    spec = _load_spec()
    router = QueryRouter.from_config(
        _resolve_model(tmp_path),
        cwd=tmp_path,
        profile_collector=StubProfileCollector(),
    )

    for case in spec["warning_examples"]:
        decision = router.route(QueryRequest(query=case["query"]))
        warning_codes = {warning.code for warning in decision.warnings}

        assert decision.intent == case["intent"]
        assert decision.tool_plan.primary_tool == case["primary_tool"]
        assert set(case["expected_warning_codes"]).issubset(warning_codes)


def test_skill_routing_examples_keep_skill_off_ops_and_storage_internals() -> None:
    spec = _load_spec()

    assert tuple(spec["forbidden_dependencies"]["ops_tools"]) == OPS_TOOL_NAMES

    storage_terms = tuple(spec["forbidden_dependencies"]["storage_terms"])
    for route in spec["route_matrix"]:
        serialized = json.dumps(route, ensure_ascii=False).lower()
        for term in storage_terms:
            assert term not in serialized

    for example in spec["examples"]:
        for tool_call in example["tool_calls"]:
            serialized = json.dumps(tool_call.get("args", {}), ensure_ascii=False).lower()
            for term in storage_terms:
                assert term not in serialized