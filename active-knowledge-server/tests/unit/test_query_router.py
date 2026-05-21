from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.query import QueryRouter
from active_knowledge_server.models import QueryRequest


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


FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "query_intents.yaml"


def resolve_model(tmp_path: Path, overrides: ConfigDict | None = None) -> ActiveKnowledgeConfig:
    workspace = tmp_path / "workspace"
    docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    docs.mkdir()
    merged: ConfigDict = {
        "runtime": {
            "workdir": str(tmp_path / ".active-kb"),
            "baseline_dir": str(tmp_path / ".active-kb" / "baseline"),
            "local_dir": str(tmp_path / ".active-kb" / "local"),
            "source_docs_root": str(docs),
        },
        "project": {
            "workspace_root": str(workspace),
            "default_profile": "auto",
        },
        "storage": {
            "baseline": {
                "manifest": str(tmp_path / ".active-kb" / "baseline" / "manifest.json")
            },
            "metadata": {
                "path": str(tmp_path / ".active-kb" / "baseline" / "db" / "metadata.db"),
                "mode": "readwrite",
            },
            "overlay": {
                "path": str(tmp_path / ".active-kb" / "local" / "db" / "overlay.db"),
                "mode": "readwrite",
            },
            "jobs": {
                "path": str(tmp_path / ".active-kb" / "local" / "db" / "jobs.db"),
                "mode": "readwrite",
            },
            "vector": {
                "path": str(tmp_path / ".active-kb" / "baseline" / "vectors"),
                "mode": "readwrite",
            },
            "vector_delta": {
                "path": str(tmp_path / ".active-kb" / "local" / "vectors"),
                "mode": "readwrite",
            },
            "cache_root": str(tmp_path / ".active-kb" / "local" / "cache"),
        },
    }
    if overrides:
        merged = deep_merge(merged, overrides)
    return resolve_config(cli_overrides=merged, env={}, cwd=tmp_path).model


def load_intent_cases() -> list[dict[str, Any]]:
    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def router(tmp_path: Path) -> QueryRouter:
    config = resolve_model(tmp_path)
    return QueryRouter.from_config(
        config,
        cwd=tmp_path,
        profile_collector=StubProfileCollector(),
    )


@pytest.mark.parametrize("case", load_intent_cases(), ids=lambda case: case["id"])
def test_query_router_matches_fixture_intents(router: QueryRouter, case: dict[str, Any]) -> None:
    request = QueryRequest(**case["input"])

    decision = router.route(request)

    expected = case["expect"]
    assert decision.intent == expected["intent"]
    assert decision.confidence >= expected["min_confidence"]
    assert decision.selected_view == expected["selected_view"]
    assert decision.selected_granularity == expected["selected_granularity"]
    assert decision.tool_plan.primary_tool == expected["primary_tool"]
    assert decision.tool_plan.route_mode == expected["route_mode"]
    assert expected["forbidden_intents"] == [] or decision.intent not in expected["forbidden_intents"]
    signal_types = {signal.type for signal in decision.matched_signals}
    assert set(expected["required_signal_types"]).issubset(signal_types)


def test_query_router_downgrades_low_confidence_queries_to_unknown(router: QueryRouter) -> None:
    decision = router.route(QueryRequest(query="帮我看看这里。"))

    assert decision.intent == "unknown"
    assert decision.tool_plan.primary_tool == "kb_search"
    assert any(warning.code == "router.low_confidence" for warning in decision.warnings)


def test_query_router_marks_close_intents_as_ambiguous(router: QueryRouter) -> None:
    decision = router.route(QueryRequest(query="这个控件 API 怎么用？"))

    assert decision.intent == "api_lookup"
    assert any(warning.code == "router.ambiguous_intent" for warning in decision.warnings)


def test_query_router_uses_caller_tool_hint_for_macro_query(router: QueryRouter) -> None:
    decision = router.route(
        QueryRequest(
            query="CONFIG_BT",
            caller_tool="config_impact",
        )
    )

    assert decision.intent == "profile_diff"
    assert decision.tool_plan.primary_tool == "config_impact"


def test_query_router_resolves_profile_with_real_profile_collector(tmp_path: Path) -> None:
    config = resolve_model(tmp_path)
    workspace_root = Path(config.project.workspace_root)
    write_profile_fixture(
        workspace_root,
        defconfig_rel="configs/mhs003_watch_defconfig",
        dotconfig_rel="build/.config",
        app="watch",
        board="mhs003",
    )
    router = QueryRouter.from_config(config, cwd=tmp_path)

    decision = router.route(
        QueryRequest(query="CONFIG_BT 在 mhs003 watch profile 下影响哪些模块？")
    )

    assert decision.intent == "profile_diff"
    assert decision.selected_view == "profile"
    assert decision.profile_resolution["status"] == "resolved"
    assert decision.profile_resolution["resolved_profile_id"] == "mhs003_watch"
    assert decision.tool_plan.primary_tool == "config_impact"


def test_query_router_respects_hybrid_feature_switches(tmp_path: Path) -> None:
    config = resolve_model(
        tmp_path,
        overrides={
            "query": {
                "hybrid": {
                    "enable_vector": False,
                    "enable_graph_expand": False,
                }
            }
        },
    )
    router = QueryRouter.from_config(
        config,
        cwd=tmp_path,
        profile_collector=StubProfileCollector(),
    )

    decision = router.route(QueryRequest(query="sensor.subscribe API 怎么用？"))

    assert "vector" not in decision.retriever_weights
    assert "graph" not in decision.retriever_weights
    assert sum(decision.retriever_weights.values()) == pytest.approx(1.0)


def write_profile_fixture(
    workspace_root: Path,
    *,
    defconfig_rel: str,
    dotconfig_rel: str,
    app: str,
    board: str,
) -> None:
    defconfig_path = workspace_root / defconfig_rel
    dotconfig_path = workspace_root / dotconfig_rel
    defconfig_path.parent.mkdir(parents=True, exist_ok=True)
    dotconfig_path.parent.mkdir(parents=True, exist_ok=True)
    defconfig_path.write_text(
        f'CONFIG_APP="{app}"\nCONFIG_BOARD="{board}"\nCONFIG_BT=y\n',
        encoding="utf-8",
    )
    dotconfig_path.write_text(
        f'CONFIG_APP="{app}"\nCONFIG_BOARD="{board}"\nCONFIG_BT=y\n',
        encoding="utf-8",
    )


def deep_merge(base: ConfigDict, overrides: ConfigDict) -> ConfigDict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = merged[key]
            assert isinstance(nested, dict)
            merged[key] = deep_merge(nested, value)
        else:
            merged[key] = value
    return merged