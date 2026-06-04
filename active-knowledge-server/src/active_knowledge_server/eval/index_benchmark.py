"""Index benchmark reporting helpers for Phase 4 acceptance gates."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean


def parse_positive_int_csv(raw: str | None, *, default: tuple[int, ...]) -> tuple[int, ...]:
    """Parse one comma-separated positive integer list with stable deduplication."""

    if raw is None or not raw.strip():
        return default
    values: list[int] = []
    seen: set[int] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        value = int(candidate)
        if value <= 0:
            raise ValueError("values must be positive integers")
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    if not values:
        raise ValueError("at least one positive integer is required")
    return tuple(values)


@dataclass(frozen=True)
class MetricSummary:
    """Compact summary for one numeric metric across repeated samples."""

    sample_count: int
    min_value: float
    p50: float
    p95: float
    mean_value: float
    max_value: float

    @classmethod
    def from_samples(cls, samples: tuple[float, ...]) -> MetricSummary:
        if not samples:
            raise ValueError("metric summaries require at least one sample")
        ordered = tuple(sorted(float(sample) for sample in samples))
        return cls(
            sample_count=len(ordered),
            min_value=ordered[0],
            p50=_percentile(ordered, 0.50),
            p95=_percentile(ordered, 0.95),
            mean_value=float(mean(ordered)),
            max_value=ordered[-1],
        )

    def to_dict(self) -> dict[str, float | int]:
        return {
            "sample_count": self.sample_count,
            "min": self.min_value,
            "p50": self.p50,
            "p95": self.p95,
            "mean": self.mean_value,
            "max": self.max_value,
        }


@dataclass(frozen=True)
class IndexBenchmarkScenarioKey:
    """One unique benchmark scenario across worker and writer settings."""

    mode: str
    target: str
    source: str
    cache_mode: str
    workers_requested: str
    parallel_mode: str
    writer_batch_size: int
    writer_max_files_per_transaction: int
    writer_max_records_per_transaction: int
    writer_commit_interval_ms: int
    sqlite_journal_mode: str
    sqlite_synchronous: str
    sqlite_wal_autocheckpoint_pages: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "target": self.target,
            "source": self.source,
            "cache_mode": self.cache_mode,
            "workers_requested": self.workers_requested,
            "parallel_mode": self.parallel_mode,
            "writer_batch_size": self.writer_batch_size,
            "writer_max_files_per_transaction": self.writer_max_files_per_transaction,
            "writer_max_records_per_transaction": self.writer_max_records_per_transaction,
            "writer_commit_interval_ms": self.writer_commit_interval_ms,
            "sqlite_journal_mode": self.sqlite_journal_mode,
            "sqlite_synchronous": self.sqlite_synchronous,
            "sqlite_wal_autocheckpoint_pages": self.sqlite_wal_autocheckpoint_pages,
        }


@dataclass(frozen=True)
class IndexBenchmarkScenarioSummary:
    """Aggregated benchmark measurements and derived risks for one scenario."""

    key: IndexBenchmarkScenarioKey
    wall_seconds: MetricSummary
    cpu_seconds: MetricSummary
    rss_delta_bytes: MetricSummary
    result_status_counts: dict[str, int]
    warning_code_counts: dict[str, int]
    max_warning_count: int
    metadata_db_bytes_max: int
    metadata_wal_bytes_max: int
    metadata_shm_bytes_max: int
    speedup_vs_reference_p50: float | None
    memory_multiplier_vs_reference_p95: float | None
    risk_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.key.to_dict(),
            "wall_seconds": self.wall_seconds.to_dict(),
            "cpu_seconds": self.cpu_seconds.to_dict(),
            "rss_delta_bytes": self.rss_delta_bytes.to_dict(),
            "result_status_counts": dict(self.result_status_counts),
            "warning_code_counts": dict(self.warning_code_counts),
            "max_warning_count": self.max_warning_count,
            "storage_files": {
                "metadata_db_bytes_max": self.metadata_db_bytes_max,
                "metadata_wal_bytes_max": self.metadata_wal_bytes_max,
                "metadata_shm_bytes_max": self.metadata_shm_bytes_max,
            },
            "speedup_vs_reference_p50": self.speedup_vs_reference_p50,
            "memory_multiplier_vs_reference_p95": self.memory_multiplier_vs_reference_p95,
            "risk_flags": list(self.risk_flags),
        }


@dataclass(frozen=True)
class IndexBenchmarkRecommendation:
    """One stable recommendation derived from the observed scenario matrix."""

    key: IndexBenchmarkScenarioKey
    rationale: str
    speedup_vs_reference_p50: float | None
    memory_multiplier_vs_reference_p95: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.key.to_dict(),
            "rationale": self.rationale,
            "speedup_vs_reference_p50": self.speedup_vs_reference_p50,
            "memory_multiplier_vs_reference_p95": self.memory_multiplier_vs_reference_p95,
        }


@dataclass(frozen=True)
class IndexBenchmarkReport:
    """Release-gate oriented summary over index benchmark JSONL records."""

    dataset: dict[str, object]
    machine: dict[str, object]
    git_commit: str | None
    scenario_summaries: tuple[IndexBenchmarkScenarioSummary, ...]
    recommendations: tuple[IndexBenchmarkRecommendation, ...]
    observed_risks: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "index_benchmark_report.v1",
            "dataset": self.dataset,
            "machine": self.machine,
            "git_commit": self.git_commit,
            "scenario_summaries": [item.to_dict() for item in self.scenario_summaries],
            "recommendations": [item.to_dict() for item in self.recommendations],
            "observed_risks": list(self.observed_risks),
        }


def load_index_benchmark_records(path: Path) -> tuple[dict[str, object], ...]:
    """Load one JSONL benchmark run artifact."""

    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError("benchmark JSONL records must decode to objects")
        records.append(payload)
    return tuple(records)


def summarize_index_benchmark_records(
    records: tuple[dict[str, object], ...] | list[dict[str, object]],
) -> IndexBenchmarkReport:
    """Summarize raw benchmark samples into a release-gate friendly report."""

    normalized = tuple(records)
    if not normalized:
        raise ValueError("at least one benchmark record is required")

    grouped: dict[IndexBenchmarkScenarioKey, list[dict[str, object]]] = defaultdict(list)
    for record in normalized:
        grouped[_scenario_key_from_record(record)].append(record)

    reference_by_family = _reference_keys_by_family(tuple(grouped))
    scenario_summaries: list[IndexBenchmarkScenarioSummary] = []
    for key, samples in sorted(grouped.items(), key=lambda item: _scenario_sort_tuple(item[0])):
        reference_key = reference_by_family[_family_key_for_scenario(key)]
        reference_samples = grouped[reference_key]
        summary = _build_scenario_summary(
            key,
            samples,
            reference_key=reference_key,
            reference_samples=reference_samples,
        )
        scenario_summaries.append(summary)

    recommendations: list[IndexBenchmarkRecommendation] = []
    observed_risks = sorted({risk for item in scenario_summaries for risk in item.risk_flags})
    scenarios_by_family: dict[tuple[object, ...], list[IndexBenchmarkScenarioSummary]] = defaultdict(list)
    for summary in scenario_summaries:
        scenarios_by_family[_family_key_for_scenario(summary.key)].append(summary)
    for family_key in sorted(scenarios_by_family):
        recommendation = _recommend_family_scenario(scenarios_by_family[family_key])
        if recommendation is not None:
            recommendations.append(recommendation)

    return IndexBenchmarkReport(
        dataset=_common_mapping(normalized, "dataset"),
        machine=_common_mapping(normalized, "machine"),
        git_commit=_common_text(normalized, "git_commit"),
        scenario_summaries=tuple(scenario_summaries),
        recommendations=tuple(recommendations),
        observed_risks=tuple(observed_risks),
    )


def render_index_benchmark_markdown(report: IndexBenchmarkReport) -> str:
    """Render one compact Markdown report for doc/ or tests/perf/results/."""

    lines = [
        "# Index Benchmark Report",
        "",
        f"- Dataset tier: {report.dataset.get('dataset_tier', 'unknown')}",
        f"- Workspace files: {report.dataset.get('workspace_file_count', 'unknown')}",
        f"- Source docs: {report.dataset.get('source_doc_file_count', 'unknown')}",
        f"- CPU count: {report.machine.get('cpu_count', 'unknown')}",
        f"- Git commit: {report.git_commit or 'unknown'}",
        "",
        "## Recommendations",
    ]
    if not report.recommendations:
        lines.extend([
            "",
            "No stable recommendation could be derived from the provided scenarios.",
        ])
    else:
        lines.append("")
        for recommendation in report.recommendations:
            scenario = recommendation.key
            lines.append(
                "- "
                f"workers={scenario.workers_requested}, "
                f"parallel_mode={scenario.parallel_mode}, "
                f"batch_size={scenario.writer_batch_size}, "
                f"max_files_per_transaction={scenario.writer_max_files_per_transaction}, "
                f"max_records_per_transaction={scenario.writer_max_records_per_transaction}, "
                f"commit_interval_ms={scenario.writer_commit_interval_ms}, "
                f"sqlite={scenario.sqlite_journal_mode}/{scenario.sqlite_synchronous}: "
                f"{recommendation.rationale}"
            )

    lines.extend(
        [
            "",
            "## Scenario Summary",
            "",
            "| Scenario | p50 wall (s) | p95 wall (s) | Speedup vs ref | RSS p95 multiplier | Warnings | Risks |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for summary in report.scenario_summaries:
        scenario = summary.key
        speedup = (
            "-"
            if summary.speedup_vs_reference_p50 is None
            else f"{summary.speedup_vs_reference_p50:.2f}x"
        )
        memory = (
            "-"
            if summary.memory_multiplier_vs_reference_p95 is None
            else f"{summary.memory_multiplier_vs_reference_p95:.2f}x"
        )
        risks = ", ".join(summary.risk_flags) if summary.risk_flags else "none"
        lines.append(
            "| "
            f"w={scenario.workers_requested}, m={scenario.parallel_mode}, "
            f"b={scenario.writer_batch_size}, "
            f"mf={scenario.writer_max_files_per_transaction}, "
            f"mr={scenario.writer_max_records_per_transaction}, "
            f"c={scenario.writer_commit_interval_ms}, {scenario.cache_mode}, "
            f"{scenario.sqlite_journal_mode}/{scenario.sqlite_synchronous}"
            " | "
            f"{summary.wall_seconds.p50:.3f}"
            " | "
            f"{summary.wall_seconds.p95:.3f}"
            " | "
            f"{speedup}"
            " | "
            f"{memory}"
            " | "
            f"{summary.max_warning_count}"
            " | "
                f"{risks} |"
        )

    if report.observed_risks:
        lines.extend(["", "## Observed Risks", ""])
        for risk in report.observed_risks:
            lines.append(f"- {risk}")
    return "\n".join(lines).rstrip() + "\n"


def _scenario_key_from_record(record: dict[str, object]) -> IndexBenchmarkScenarioKey:
    writer = _mapping(record.get("writer"))
    sqlite_payload = _mapping(record.get("sqlite"))
    configured_sqlite = _mapping(sqlite_payload.get("configured"))
    return IndexBenchmarkScenarioKey(
        mode=str(record.get("mode", "unknown")),
        target=str(record.get("target", "unknown")),
        source=str(record.get("source", "unknown")),
        cache_mode=str(record.get("cache_mode", "unknown")),
        workers_requested=str(record.get("workers_requested", "unknown")),
        parallel_mode=str(_mapping(record.get("parallel")).get("mode", "thread")),
        writer_batch_size=int(writer.get("batch_size", 0)),
        writer_max_files_per_transaction=int(
            writer.get("max_files_per_transaction", writer.get("batch_size", 0))
        ),
        writer_max_records_per_transaction=int(writer.get("max_records_per_transaction", 0)),
        writer_commit_interval_ms=int(writer.get("commit_interval_ms", 0)),
        sqlite_journal_mode=str(configured_sqlite.get("journal_mode", "unknown")),
        sqlite_synchronous=str(configured_sqlite.get("synchronous", "unknown")),
        sqlite_wal_autocheckpoint_pages=_optional_int(
            configured_sqlite.get("wal_autocheckpoint_pages")
        ),
    )


def _family_key_for_scenario(key: IndexBenchmarkScenarioKey) -> tuple[object, ...]:
    return (
        key.mode,
        key.target,
        key.source,
        key.cache_mode,
        key.parallel_mode,
    )


def _reference_keys_by_family(
    keys: tuple[IndexBenchmarkScenarioKey, ...],
) -> dict[tuple[object, ...], IndexBenchmarkScenarioKey]:
    families: dict[tuple[object, ...], list[IndexBenchmarkScenarioKey]] = defaultdict(list)
    for key in keys:
        families[_family_key_for_scenario(key)].append(key)
    return {
        family: min(
            members,
            key=lambda item: (
                _worker_order(item.workers_requested),
                item.parallel_mode,
                item.writer_batch_size,
                _optional_positive_sort_key(item.writer_max_files_per_transaction),
                _optional_positive_sort_key(item.writer_max_records_per_transaction),
                item.writer_commit_interval_ms,
            ),
        )
        for family, members in families.items()
    }


def _build_scenario_summary(
    key: IndexBenchmarkScenarioKey,
    samples: list[dict[str, object]],
    *,
    reference_key: IndexBenchmarkScenarioKey,
    reference_samples: list[dict[str, object]],
) -> IndexBenchmarkScenarioSummary:
    wall_samples = tuple(float(record.get("wall_seconds", 0.0)) for record in samples)
    cpu_samples = tuple(float(record.get("cpu_seconds", 0.0)) for record in samples)
    rss_samples = tuple(float(record.get("rss_delta_bytes", 0.0)) for record in samples)
    reference_wall = MetricSummary.from_samples(
        tuple(float(record.get("wall_seconds", 0.0)) for record in reference_samples)
    )
    reference_rss = MetricSummary.from_samples(
        tuple(float(record.get("rss_delta_bytes", 0.0)) for record in reference_samples)
    )
    result_status_counts = Counter(str(record.get("result_status", "unknown")) for record in samples)
    warning_code_counts = Counter()
    max_warning_count = 0
    metadata_db_bytes_max = 0
    metadata_wal_bytes_max = 0
    metadata_shm_bytes_max = 0
    for record in samples:
        max_warning_count = max(max_warning_count, int(record.get("warning_count", 0)))
        warning_code_counts.update(str(code) for code in record.get("warning_codes", []))
        storage_files = _mapping(record.get("storage_files"))
        metadata_db_bytes_max = max(metadata_db_bytes_max, int(storage_files.get("metadata_db_bytes", 0)))
        metadata_wal_bytes_max = max(
            metadata_wal_bytes_max,
            int(storage_files.get("metadata_wal_bytes", 0)),
        )
        metadata_shm_bytes_max = max(
            metadata_shm_bytes_max,
            int(storage_files.get("metadata_shm_bytes", 0)),
        )

    wall_summary = MetricSummary.from_samples(wall_samples)
    cpu_summary = MetricSummary.from_samples(cpu_samples)
    rss_summary = MetricSummary.from_samples(rss_samples)
    speedup = None
    if reference_wall.p50 > 0:
        speedup = reference_wall.p50 / wall_summary.p50
    memory_multiplier = None
    if reference_rss.p95 > 0:
        memory_multiplier = rss_summary.p95 / reference_rss.p95
    risk_flags = _risk_flags_for_summary(
        key,
        result_status_counts=result_status_counts,
        warning_code_counts=warning_code_counts,
        max_warning_count=max_warning_count,
        metadata_db_bytes_max=metadata_db_bytes_max,
        metadata_wal_bytes_max=metadata_wal_bytes_max,
        speedup_vs_reference=speedup,
        memory_multiplier_vs_reference=memory_multiplier,
        is_reference=key == reference_key,
    )
    return IndexBenchmarkScenarioSummary(
        key=key,
        wall_seconds=wall_summary,
        cpu_seconds=cpu_summary,
        rss_delta_bytes=rss_summary,
        result_status_counts=dict(sorted(result_status_counts.items())),
        warning_code_counts=dict(sorted(warning_code_counts.items())),
        max_warning_count=max_warning_count,
        metadata_db_bytes_max=metadata_db_bytes_max,
        metadata_wal_bytes_max=metadata_wal_bytes_max,
        metadata_shm_bytes_max=metadata_shm_bytes_max,
        speedup_vs_reference_p50=speedup,
        memory_multiplier_vs_reference_p95=memory_multiplier,
        risk_flags=risk_flags,
    )


def _risk_flags_for_summary(
    key: IndexBenchmarkScenarioKey,
    *,
    result_status_counts: Counter[str],
    warning_code_counts: Counter[str],
    max_warning_count: int,
    metadata_db_bytes_max: int,
    metadata_wal_bytes_max: int,
    speedup_vs_reference: float | None,
    memory_multiplier_vs_reference: float | None,
    is_reference: bool,
) -> tuple[str, ...]:
    risks: list[str] = []
    if result_status_counts.get("failed", 0) > 0:
        risks.append("failed_result")
    if result_status_counts.get("blocked", 0) > 0:
        risks.append("blocked_result")
    if max_warning_count > 0:
        risks.append("warnings_present")
    if any("lock" in code for code in warning_code_counts):
        risks.append("sqlite_lock_warning")
    if any(code.startswith("vector.") or code.startswith("embedding.") for code in warning_code_counts):
        risks.append("vector_write_warning")
    if key.sqlite_journal_mode == "wal" and metadata_wal_bytes_max > metadata_db_bytes_max > 0:
        risks.append("wal_larger_than_db")
    if not is_reference and speedup_vs_reference is not None and speedup_vs_reference < 1.0:
        risks.append("slower_than_reference")
    if memory_multiplier_vs_reference is not None and memory_multiplier_vs_reference > 2.0:
        risks.append("memory_gt_2x_reference")
    return tuple(sorted(set(risks)))


def _recommend_family_scenario(
    scenarios: list[IndexBenchmarkScenarioSummary],
) -> IndexBenchmarkRecommendation | None:
    hard_risks = {
        "failed_result",
        "blocked_result",
        "sqlite_lock_warning",
        "vector_write_warning",
        "memory_gt_2x_reference",
    }
    eligible = [
        summary
        for summary in scenarios
        if not hard_risks.intersection(summary.risk_flags)
    ]
    if not eligible:
        return None
    fastest = min(eligible, key=lambda item: item.wall_seconds.p50)
    near_fastest = [
        item
        for item in eligible
        if item.wall_seconds.p50 <= fastest.wall_seconds.p50 * 1.05
    ]
    chosen = min(
        near_fastest,
        key=lambda item: (
            _worker_order(item.key.workers_requested),
            item.key.parallel_mode,
            item.key.writer_batch_size,
            _optional_positive_sort_key(item.key.writer_max_files_per_transaction),
            _optional_positive_sort_key(item.key.writer_max_records_per_transaction),
            item.key.writer_commit_interval_ms,
            item.wall_seconds.p50,
        ),
    )
    rationale = (
        "fastest stable scenario within 5% of the minimum p50 wall time, "
        "preferring lower worker and writer pressure"
    )
    return IndexBenchmarkRecommendation(
        key=chosen.key,
        rationale=rationale,
        speedup_vs_reference_p50=chosen.speedup_vs_reference_p50,
        memory_multiplier_vs_reference_p95=chosen.memory_multiplier_vs_reference_p95,
    )


def _common_mapping(records: tuple[dict[str, object], ...], key: str) -> dict[str, object]:
    first = _mapping(records[0].get(key))
    if all(_mapping(record.get(key)) == first for record in records[1:]):
        return first
    return {"mixed": True}


def _common_text(records: tuple[dict[str, object], ...], key: str) -> str | None:
    first = records[0].get(key)
    if all(record.get(key) == first for record in records[1:]):
        return None if first is None else str(first)
    return None


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_positive_sort_key(value: int) -> int:
    return value if value > 0 else 2**31 - 1


def _worker_order(value: str) -> tuple[int, str]:
    return (0, value) if value.isdigit() else (1, value)


def _scenario_sort_tuple(key: IndexBenchmarkScenarioKey) -> tuple[object, ...]:
    return (
        key.mode,
        key.target,
        key.source,
        key.cache_mode,
        key.parallel_mode,
        _worker_order(key.workers_requested),
        key.writer_batch_size,
        _optional_positive_sort_key(key.writer_max_files_per_transaction),
        _optional_positive_sort_key(key.writer_max_records_per_transaction),
        key.writer_commit_interval_ms,
        key.sqlite_journal_mode,
        key.sqlite_synchronous,
        -1 if key.sqlite_wal_autocheckpoint_pages is None else key.sqlite_wal_autocheckpoint_pages,
    )


def _percentile(samples: tuple[float, ...], quantile: float) -> float:
    if len(samples) == 1:
        return samples[0]
    position = (len(samples) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(samples) - 1)
    fraction = position - lower
    return samples[lower] + (samples[upper] - samples[lower]) * fraction
