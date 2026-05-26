#!/usr/bin/env python3
"""Run repeated hybrid-query stability checks against a local Active KB index."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import cycle
from pathlib import Path
from typing import Any, Literal

import yaml

from active_knowledge_server.config.loader import ConfigDict, resolve_config, set_nested
from active_knowledge_server.eval.cases import load_eval_suite
from active_knowledge_server.models.query import QueryRequest
from active_knowledge_server.models.responses import QueryResult
from active_knowledge_server.query import QueryService
from active_knowledge_server.query.retrievers import (
    FullTextMatchResult,
    FullTextSearchRequest,
    FullTextSearchResult,
)
from active_knowledge_server.storage import ALL_SCOPE, FTSMatch, FTSQuery, QueryScope
from active_knowledge_server.storage.lancedb_store import LanceDBVectorAdapter
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    configured_sqlite_paths,
)

VectorMode = Literal["auto", "configured", "off"]
ToggleMode = Literal["configured", "off"]


@dataclass(frozen=True)
class QueryCase:
    case_id: str
    request: QueryRequest


class FastFullTextRetriever:
    """Storage FTS retriever that avoids building the full logical entity catalog."""

    def __init__(self, reader: Any) -> None:
        self._reader = reader

    def search(self, request: FullTextSearchRequest) -> FullTextSearchResult:
        best_matches: dict[tuple[str, str], FullTextMatchResult] = {}
        for index_name in request.indexes:
            fts_matches = self._reader.search_fts(
                FTSQuery(
                    index_name=index_name,
                    query=request.normalized_query or request.query.strip(),
                    scope=request.scope,
                    top_k=max(request.top_k * 4, 24),
                    domain=request.domain,
                    doc_type=request.doc_type,
                    source_index=request.source_index,
                )
            )
            for rank, match in enumerate(fts_matches, start=1):
                candidate = fts_match_to_result(match, rank=rank)
                key = (candidate.logical_object_id, candidate.object_type)
                current = best_matches.get(key)
                if current is None or candidate.score > current.score:
                    best_matches[key] = candidate

        ordered = sorted(
            best_matches.values(),
            key=lambda item: (-item.score, item.logical_object_id),
        )
        return FullTextSearchResult(request=request, matches=tuple(ordered[: request.top_k]))


class FastEvidencePackager:
    """No-op evidence packaging for the repeated stability probe."""

    def bundle_for_query(
        self,
        *,
        scope: QueryScope,
        candidates: Any,
    ) -> tuple[tuple[Any, ...], list[dict[str, object]]]:
        del scope, candidates
        return (), []

    def bundle_for_entity(
        self,
        entity_id: str,
        *,
        snapshot_id: str = "current",
        profile_id: str = ALL_SCOPE,
    ) -> tuple[Any, ...]:
        del entity_id, snapshot_id, profile_id
        return ()


def main() -> int:
    args = parse_args()
    started_at = utc_now()
    started = time.perf_counter()

    resolved = resolve_config(
        config_path=args.config,
        local_config_path=args.local_config,
        cli_overrides=common_overrides(args),
    )
    config = resolved.model
    cwd = Path.cwd()
    metadata_adapter = SQLiteStorageAdapter.from_config(config, cwd=cwd)
    sqlite_paths = configured_sqlite_paths(config, cwd=cwd)
    vector_enabled, vector_reason = resolve_vector_enabled(
        sqlite_paths=sqlite_paths,
        scope=QueryScope(snapshot_id=args.snapshot_id, profile_id=ALL_SCOPE),
        mode=args.vector_mode,
    )
    vector_adapter = (
        LanceDBVectorAdapter.from_config(config, cwd=cwd, metadata_adapter=metadata_adapter)
        if vector_enabled
        else None
    )
    service = QueryService.from_config(
        config,
        cwd=cwd,
        metadata_adapter=metadata_adapter,
        vector_adapter=vector_adapter,
        fulltext_retriever=FastFullTextRetriever(metadata_adapter.reader()),
        evidence_packager=FastEvidencePackager(),
    )

    cases = load_cases(args, sqlite_paths=sqlite_paths)
    rng = random.Random(args.seed)
    shuffled_cases = list(cases)
    rng.shuffle(shuffled_cases)

    report = run_probe(
        service=service,
        cases=tuple(shuffled_cases),
        total_runs=args.count,
        excluded_statuses=frozenset(args.exclude_status),
        threshold=args.threshold,
        started_at=started_at,
        duration_seconds=time.perf_counter() - started,
        vector_enabled=vector_enabled,
        vector_reason=vector_reason,
        symbol_mode=args.symbol_mode,
        graph_mode=args.graph_mode,
        seed=args.seed,
        config_path=args.config,
    )

    if args.report is not None:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run repeated QueryService hybrid searches against the configured local index. "
            "blocked/zero_result are excluded from the success-rate denominator by default."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Active KB config file. Defaults to ../examples/local-single-user.yaml when present.",
    )
    parser.add_argument("--local-config", type=Path, help="Optional local config override file.")
    parser.add_argument("--workdir", type=Path, help="Override runtime.workdir.")
    parser.add_argument("--workspace", type=Path, help="Override project.workspace_root.")
    parser.add_argument(
        "--source-docs-root",
        type=Path,
        help="Override runtime.source_docs_root.",
    )
    parser.add_argument("--profile", help="Override project.default_profile.")
    parser.add_argument(
        "--snapshot-id",
        default="current",
        help="Snapshot used for generated queries.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=500,
        help="Total query executions.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.99,
        help="Required eligible success rate.",
    )
    parser.add_argument(
        "--exclude-status",
        action="append",
        default=["blocked", "zero_result"],
        help="Result status excluded from the denominator. May be repeated.",
    )
    parser.add_argument(
        "--queries",
        type=Path,
        help="Optional eval_cases.v1 YAML or simple YAML with a top-level queries list.",
    )
    parser.add_argument(
        "--generated-case-limit",
        type=int,
        default=200,
        help="Maximum generated cases when --queries is omitted.",
    )
    parser.add_argument("--seed", type=int, default=20260526, help="Deterministic shuffle seed.")
    parser.add_argument(
        "--vector-mode",
        choices=("auto", "configured", "off"),
        default="auto",
        help=(
            "auto uses vector retrieval only when vector_ref rows exist; configured always follows "
            "the config; off disables the vector adapter for this probe."
        ),
    )
    parser.add_argument(
        "--symbol-mode",
        choices=("configured", "off"),
        default="off",
        help=(
            "configured follows query.hybrid.enable_symbol; off disables symbol retrieval for a "
            "sustainable 500-run local-index stability probe."
        ),
    )
    parser.add_argument(
        "--graph-mode",
        choices=("configured", "off"),
        default="off",
        help="configured follows query.hybrid.enable_graph_expand; off disables graph expansion.",
    )
    parser.add_argument("--report", type=Path, help="Optional JSON report path.")
    return parser.parse_args()


def default_config_path() -> Path | None:
    for candidate in (
        Path("../examples/local-single-user.yaml"),
        Path("examples/local-single-user.yaml"),
    ):
        if candidate.exists():
            return candidate
    return None


def common_overrides(args: argparse.Namespace) -> ConfigDict:
    overrides: ConfigDict = {}
    for attr, path in (
        ("workdir", ("runtime", "workdir")),
        ("workspace", ("project", "workspace_root")),
        ("source_docs_root", ("runtime", "source_docs_root")),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            set_nested(overrides, path, str(value))
    if args.profile is not None:
        set_nested(overrides, ("project", "default_profile"), str(args.profile))
    if args.symbol_mode == "off":
        set_nested(overrides, ("query", "hybrid", "enable_symbol"), False)
    if args.graph_mode == "off":
        set_nested(overrides, ("query", "hybrid", "enable_graph_expand"), False)
    return overrides


def resolve_vector_enabled(
    *,
    sqlite_paths: dict[str, Path],
    scope: QueryScope,
    mode: VectorMode,
) -> tuple[bool, str]:
    if mode == "off":
        return False, "disabled_by_flag"
    if mode == "configured":
        return True, "configured"
    try:
        has_vector_refs = has_any_vector_refs(sqlite_paths=sqlite_paths, scope=scope)
    except Exception as exc:  # noqa: BLE001 - probe should continue with readable text indexes.
        return False, f"vector_ref_probe_failed:{exc.__class__.__name__}"
    if has_vector_refs:
        return True, "vector_refs_present"
    return False, "no_vector_refs"


def load_cases(args: argparse.Namespace, *, sqlite_paths: dict[str, Path]) -> tuple[QueryCase, ...]:
    if args.queries is not None:
        cases = load_cases_from_yaml(args.queries)
    else:
        cases = generate_cases_from_index(
            sqlite_paths=sqlite_paths,
            snapshot_id=args.snapshot_id,
            limit=max(args.generated_case_limit, 1),
        )
    if not cases:
        raise SystemExit("no query cases available; provide --queries or index code/docs first")
    return cases


def load_cases_from_yaml(path: Path) -> tuple[QueryCase, ...]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("schema_version") == "eval_cases.v1":
        suite = load_eval_suite(path)
        return tuple(
            QueryCase(case.case_id, QueryRequest.model_validate(case.request.to_dict()))
            for case in suite.cases
            if case.execution_mode == "query_quality"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("queries"), list):
        raise ValueError("query YAML must be eval_cases.v1 or contain a top-level queries list")

    cases: list[QueryCase] = []
    for index, item in enumerate(payload["queries"]):
        if not isinstance(item, dict):
            raise ValueError(f"queries[{index}] must be a mapping")
        case_id = str(item.get("id") or item.get("case_id") or f"query_{index:04d}")
        request_payload = {
            key: value for key, value in item.items() if key not in {"id", "case_id"}
        }
        request_payload.setdefault("caller_tool", request_payload.get("tool", "kb_search"))
        request_payload.pop("tool", None)
        request_payload.setdefault("domain", "auto")
        request_payload.setdefault("view", "auto")
        request_payload.setdefault("granularity", "auto")
        request_payload.setdefault("profile_id", "auto")
        request_payload.setdefault("snapshot_id", "current")
        request_payload.setdefault("client_context", {})
        cases.append(QueryCase(case_id, QueryRequest.model_validate(request_payload)))
    return tuple(cases)


def generate_cases_from_index(
    *,
    sqlite_paths: dict[str, Path],
    snapshot_id: str,
    limit: int,
) -> tuple[QueryCase, ...]:
    cases: list[QueryCase] = []
    seen_queries: set[str] = set()

    for row in sample_rows(
        sqlite_paths,
        """
        SELECT name, entity_type
        FROM entity
        WHERE snapshot_id = ?
          AND entity_type IN ('Function', 'Macro', 'Type')
          AND length(name) >= 3
        ORDER BY rowid ASC
        LIMIT ?
        """,
        (snapshot_id, max(limit, 1)),
    ):
        name = str(row["name"]).strip()
        if name and name not in seen_queries:
            seen_queries.add(name)
            add_case(
                cases,
                QueryCase(
                    f"symbol_{len(cases):04d}",
                    QueryRequest(
                        query=f"{name} 在哪里定义？",
                        domain="code",
                        view="code",
                        granularity="symbol",
                        profile_id="auto",
                        snapshot_id=snapshot_id,
                        caller_tool="code_resolve",
                        client_context={},
                    ),
                ),
                limit=limit,
            )
        kb_query = f"kb:{name}"
        if name and kb_query not in seen_queries:
            seen_queries.add(kb_query)
            add_case(
                cases,
                QueryCase(
                    f"kb_symbol_{len(cases):04d}",
                    QueryRequest(
                        query=name,
                        domain="auto",
                        view="evidence",
                        granularity="auto",
                        profile_id="auto",
                        snapshot_id=snapshot_id,
                        caller_tool="kb_search",
                        client_context={},
                    ),
                ),
                limit=limit,
            )
        if len(cases) >= limit:
            break

    if len(cases) < limit:
        remaining = max(limit - len(cases), 1)
        for row in sample_rows(
            sqlite_paths,
            """
            SELECT relative_path
            FROM file
            WHERE snapshot_id = ?
              AND length(relative_path) >= 3
            ORDER BY rowid ASC
            LIMIT ?
            """,
            (snapshot_id, remaining),
        ):
            relative_path = str(row["relative_path"]).strip()
            if not relative_path or relative_path in seen_queries:
                continue
            seen_queries.add(relative_path)
            add_case(
                cases,
                QueryCase(
                    f"file_{len(cases):04d}",
                    QueryRequest(
                        query=f"{relative_path} 这个文件属于哪个模块？",
                        domain="code",
                        view="code",
                        granularity="file",
                        profile_id="auto",
                        snapshot_id=snapshot_id,
                        caller_tool="code_resolve",
                        client_context={},
                    ),
                ),
                limit=limit,
            )
            if len(cases) >= limit:
                break

    if len(cases) < limit:
        remaining = max(limit - len(cases), 1)
        for row in sample_rows(
            sqlite_paths,
            """
            SELECT text
            FROM chunk
            WHERE snapshot_id = ?
              AND length(text) >= 12
            ORDER BY rowid ASC
            LIMIT ?
            """,
            (snapshot_id, remaining),
        ):
            text = compact_text(str(row["text"]))
            text_key = f"text:{text}"
            if not text or text_key in seen_queries:
                continue
            seen_queries.add(text_key)
            add_case(
                cases,
                QueryCase(
                    f"kb_text_{len(cases):04d}",
                    QueryRequest(
                        query=text,
                        domain="auto",
                        view="evidence",
                        granularity="auto",
                        profile_id="auto",
                        snapshot_id=snapshot_id,
                        caller_tool="kb_search",
                        client_context={},
                    ),
                ),
                limit=limit,
            )
            if len(cases) >= limit:
                break

    return tuple(cases)


def has_any_vector_refs(*, sqlite_paths: dict[str, Path], scope: QueryScope) -> bool:
    for path in metadata_paths(sqlite_paths):
        if not path.exists():
            continue
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM vector_ref
                WHERE (profile_id = ? OR profile_id = ?)
                  AND (source_scope = ? OR source_scope = ?)
                LIMIT 1
                """,
                (scope.profile_id, ALL_SCOPE, scope.source_scope, ALL_SCOPE),
            ).fetchone()
            if row is not None:
                return True
    return False


def sample_rows(
    sqlite_paths: dict[str, Path],
    query: str,
    params: tuple[object, ...],
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    for path in metadata_paths(sqlite_paths):
        if not path.exists():
            continue
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            rows.extend(connection.execute(query, params).fetchall())
    return rows


def metadata_paths(sqlite_paths: dict[str, Path]) -> tuple[Path, ...]:
    return (
        sqlite_paths["overlay_metadata"],
        sqlite_paths["baseline_metadata"],
    )


def fts_match_to_result(match: FTSMatch, *, rank: int) -> FullTextMatchResult:
    return FullTextMatchResult(
        logical_object_id=match.logical_object_id,
        physical_object_id=match.physical_object_id,
        object_type=match.object_type,
        primary_index=match.index_name,
        matched_indexes=(match.index_name,),
        source_index=match.source_index,
        score=float(match.score) + (0.01 / float(max(rank, 1))),
        match_reason=f"matched {match.index_name}",
        relative_path=match.relative_path,
        title=match.title,
        snippet=match.snippet,
        file_id=match.file_id,
        chunk_id=match.chunk_id,
        entity_id=match.entity_id,
        profile_id=match.profile_id,
        source_scope=match.source_scope,
        domain=match.domain,
        doc_type=match.doc_type,
        module_names=metadata_string_tuple(match.metadata.get("module_names")),
        metadata=dict(match.metadata),
    )


def metadata_string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def add_case(cases: list[QueryCase], case: QueryCase, *, limit: int) -> None:
    if len(cases) < limit:
        cases.append(case)


def compact_text(text: str, *, max_chars: int = 96) -> str:
    compacted = " ".join(text.split())
    if len(compacted) <= max_chars:
        return compacted
    return compacted[:max_chars].rsplit(" ", 1)[0]


def run_probe(
    *,
    service: QueryService,
    cases: tuple[QueryCase, ...],
    total_runs: int,
    excluded_statuses: frozenset[str],
    threshold: float,
    started_at: str,
    duration_seconds: float,
    vector_enabled: bool,
    vector_reason: str,
    symbol_mode: ToggleMode,
    graph_mode: ToggleMode,
    seed: int,
    config_path: Path | None,
) -> dict[str, Any]:
    success_count = 0
    eligible_runs = 0
    exception_count = 0
    failures: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    excluded_status_counts: Counter[str] = Counter()
    latencies: list[float] = []

    for run_index, case in zip(range(max(total_runs, 1)), cycle(cases), strict=False):
        query_started = time.perf_counter()
        try:
            result = service.search(case.request)
            latency = time.perf_counter() - query_started
            latencies.append(latency)
            status_counts[result.result_status] += 1
            valid_schema = schema_compliant(result)
            if result.result_status in excluded_statuses:
                excluded_status_counts[result.result_status] += 1
                continue
            eligible_runs += 1
            if valid_schema and result.result_status != "error":
                success_count += 1
                continue
            failures.append(
                {
                    "run_index": run_index,
                    "case_id": case.case_id,
                    "result_status": result.result_status,
                    "schema_compliant": valid_schema,
                    "query": case.request.query,
                }
            )
        except Exception as exc:  # noqa: BLE001 - stability probe records failures and continues.
            latency = time.perf_counter() - query_started
            latencies.append(latency)
            exception_count += 1
            eligible_runs += 1
            status_counts["exception"] += 1
            failures.append(
                {
                    "run_index": run_index,
                    "case_id": case.case_id,
                    "error_kind": exc.__class__.__name__,
                    "error": str(exc),
                    "query": case.request.query,
                }
            )

    success_rate = 1.0 if eligible_runs == 0 else success_count / eligible_runs
    finished_at = utc_now()
    return {
        "schema_version": "hybrid_query_stability.v1",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds + sum(latencies), 6),
        "config_path": None if config_path is None else str(config_path),
        "seed": seed,
        "case_count": len(cases),
        "configured_runs": max(total_runs, 1),
        "total_runs": max(total_runs, 1),
        "eligible_runs": eligible_runs,
        "success_count": success_count,
        "success_rate": round(success_rate, 6),
        "threshold": threshold,
        "passed": success_rate >= threshold,
        "exception_count": exception_count,
        "failure_count": len(failures),
        "excluded_statuses": sorted(excluded_statuses),
        "excluded_status_counts": dict(sorted(excluded_status_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "latency_seconds": {
            "min": round(min(latencies, default=0.0), 6),
            "max": round(max(latencies, default=0.0), 6),
            "mean": round(sum(latencies) / len(latencies), 6) if latencies else 0.0,
        },
        "vector": {
            "enabled": vector_enabled,
            "reason": vector_reason,
        },
        "retriever_modes": {
            "symbol": symbol_mode,
            "graph": graph_mode,
            "evidence_packaging": "fast_noop",
        },
        "failures": failures[:20],
    }


def schema_compliant(result: QueryResult) -> bool:
    try:
        QueryResult.model_validate(result.to_dict())
    except Exception:
        return False
    return True


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
