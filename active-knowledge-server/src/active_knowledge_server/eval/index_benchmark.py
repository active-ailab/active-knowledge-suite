"""Index benchmark reporting helpers for Phase 4 acceptance gates."""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from active_knowledge_server.indexing.progress import IndexProgressEvent

BENCHMARK_PHASE_ORDER = (
    "discover",
    "code_collect",
    "code_finalize",
    "code_apply",
    "doc_collect",
    "doc_finalize",
    "doc_apply",
    "vector_apply",
    "profile_relations",
    "workspace_map",
)

BENCHMARK_PHASE_ALIASES = {
    "vectors_apply": "vector_apply",
}


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
class ProgressPhaseTimingSnapshot:
    """One benchmark-safe summary derived from observed progress events."""

    phase_timings: dict[str, float]
    phase_event_counts: dict[str, int]
    observed_phases: tuple[str, ...]
    event_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "phase_timings": dict(self.phase_timings),
            "phase_event_counts": dict(self.phase_event_counts),
            "observed_phases": list(self.observed_phases),
            "event_count": self.event_count,
        }


class ProgressPhaseTimingCollector:
    """Aggregate contiguous progress-event wall time into per-phase timings."""

    def __init__(self) -> None:
        self._phase_timings: dict[str, float] = {}
        self._phase_event_counts: Counter[str] = Counter()
        self._current_phase: str | None = None
        self._last_observed_at: float | None = None
        self._event_count = 0

    def observe(self, event: IndexProgressEvent, *, observed_at: float | None = None) -> None:
        now = time.perf_counter() if observed_at is None else observed_at
        if self._current_phase is not None and self._last_observed_at is not None:
            elapsed_seconds = max(now - self._last_observed_at, 0.0)
            self._phase_timings[self._current_phase] = round(
                self._phase_timings.get(self._current_phase, 0.0) + elapsed_seconds,
                6,
            )
        phase = _benchmark_phase_name(str(event.phase))
        self._current_phase = phase
        self._last_observed_at = now
        self._phase_event_counts[phase] += 1
        self._event_count += 1

    def finish(self, *, observed_at: float | None = None) -> ProgressPhaseTimingSnapshot:
        now = time.perf_counter() if observed_at is None else observed_at
        if self._current_phase is not None and self._last_observed_at is not None:
            elapsed_seconds = max(now - self._last_observed_at, 0.0)
            self._phase_timings[self._current_phase] = round(
                self._phase_timings.get(self._current_phase, 0.0) + elapsed_seconds,
                6,
            )
            self._last_observed_at = now
        ordered_phases = tuple(
            phase for phase in BENCHMARK_PHASE_ORDER if phase in self._phase_event_counts
        ) + tuple(
            phase
            for phase in sorted(self._phase_event_counts)
            if phase not in BENCHMARK_PHASE_ORDER
        )
        return ProgressPhaseTimingSnapshot(
            phase_timings=dict(sorted(self._phase_timings.items())),
            phase_event_counts=dict(sorted(self._phase_event_counts.items())),
            observed_phases=ordered_phases,
            event_count=self._event_count,
        )


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
    resume_kind: str
    resume_mode: str
    interrupt_after_task_percent: int | None
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
            "resume_kind": self.resume_kind,
            "resume_mode": self.resume_mode,
            "interrupt_after_task_percent": self.interrupt_after_task_percent,
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
    validate_status_counts: dict[str, int]
    warning_code_counts: dict[str, int]
    max_warning_count: int
    phase_timings: dict[str, MetricSummary]
    task_stats: dict[str, MetricSummary]
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
            "validate_status_counts": dict(self.validate_status_counts),
            "warning_code_counts": dict(self.warning_code_counts),
            "max_warning_count": self.max_warning_count,
            "phase_timings": {
                phase: summary.to_dict() for phase, summary in sorted(self.phase_timings.items())
            },
            "task_stats": {
                name: summary.to_dict() for name, summary in sorted(self.task_stats.items())
            },
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
    resume_comparisons: tuple[dict[str, object], ...]
    observed_risks: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "index_benchmark_report.v3",
            "dataset": self.dataset,
            "machine": self.machine,
            "git_commit": self.git_commit,
            "scenario_summaries": [item.to_dict() for item in self.scenario_summaries],
            "recommendations": [item.to_dict() for item in self.recommendations],
            "resume_comparisons": list(self.resume_comparisons),
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
    resume_comparisons = _build_resume_comparisons(tuple(scenario_summaries))

    return IndexBenchmarkReport(
        dataset=_common_mapping(normalized, "dataset"),
        machine=_common_mapping(normalized, "machine"),
        git_commit=_common_text(normalized, "git_commit"),
        scenario_summaries=tuple(scenario_summaries),
        recommendations=tuple(recommendations),
        resume_comparisons=resume_comparisons,
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
                f"resume={scenario.resume_kind}/{scenario.resume_mode}, "
                f"interrupt={_interrupt_label(scenario.interrupt_after_task_percent)}, "
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
            "| Scenario | p50 wall (s) | p95 wall (s) | Speedup vs ref | Top phases (p50) | Tasks p50 (a/s/r) | Warnings | Risks |",
            "| --- | ---: | ---: | ---: | --- | --- | ---: | --- |",
        ]
    )
    for summary in report.scenario_summaries:
        scenario = summary.key
        speedup = (
            "-"
            if summary.speedup_vs_reference_p50 is None
            else f"{summary.speedup_vs_reference_p50:.2f}x"
        )
        top_phases = _top_phase_summary(summary.phase_timings)
        task_stats = _task_stats_summary(summary.task_stats)
        risks = ", ".join(summary.risk_flags) if summary.risk_flags else "none"
        lines.append(
            "| "
            f"{scenario.resume_kind}/{scenario.resume_mode}, "
            f"i={_interrupt_label(scenario.interrupt_after_task_percent)}, "
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
            f"{top_phases}"
            " | "
            f"{task_stats}"
            " | "
            f"{summary.max_warning_count}"
            " | "
            f"{risks} |"
        )

    if report.resume_comparisons:
        lines.extend(
            [
                "",
                "## Resume Summary",
                "",
                "| Scenario | Interrupt | Fresh p50 wall (s) | Resumed p50 wall (s) | Expected remaining (s) | Resume/remaining | Resumed tasks p50 (a/s/r) | Replay overhead | Validate |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
            ]
        )
        for item in report.resume_comparisons:
            lines.append(
                "| "
                f"{item['scenario']}"
                " | "
                f"{item['interrupt_after_task_percent']}%"
                " | "
                f"{float(item['fresh_wall_p50']):.3f}"
                " | "
                f"{float(item['resumed_wall_p50']):.3f}"
                " | "
                f"{float(item['expected_remaining_wall_p50']):.3f}"
                " | "
                f"{item['resume_vs_remaining']}"
                " | "
                f"{item['resumed_task_stats']}"
                " | "
                f"{item['replay_overhead']}"
                " | "
                f"{item['validate_status']}"
                " |"
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
    job = _mapping(record.get("job"))
    resume_policy = _mapping(job.get("resume_policy"))
    return IndexBenchmarkScenarioKey(
        mode=str(record.get("mode", "unknown")),
        target=str(record.get("target", "unknown")),
        source=str(record.get("source", "unknown")),
        cache_mode=str(record.get("cache_mode", "unknown")),
        resume_kind="resumed" if bool(job.get("resumed", False)) else "fresh",
        resume_mode=str(resume_policy.get("mode", "disabled")),
        interrupt_after_task_percent=_optional_int(
            _mapping(record.get("interrupt")).get("after_task_percent")
        ),
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
        key.resume_kind,
        key.resume_mode,
        key.interrupt_after_task_percent,
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
    validate_status_counts = Counter(
        str(_mapping(record.get("validate")).get("status", "not_run")) for record in samples
    )
    warning_code_counts = Counter()
    max_warning_count = 0
    phase_timings = _metric_summaries_by_key(samples, "phase_timings")
    task_stats = _metric_summaries_by_key(samples, "task_stats")
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
        validate_status_counts=validate_status_counts,
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
        validate_status_counts=dict(sorted(validate_status_counts.items())),
        warning_code_counts=dict(sorted(warning_code_counts.items())),
        max_warning_count=max_warning_count,
        phase_timings=phase_timings,
        task_stats=task_stats,
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
    validate_status_counts: Counter[str],
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
    if sum(
        count for status, count in validate_status_counts.items() if status not in {"ok", "not_run"}
    ) > 0:
        risks.append("validate_failed")
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
        "validate_failed",
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


def _metric_summaries_by_key(
    samples: list[dict[str, object]],
    record_key: str,
) -> dict[str, MetricSummary]:
    values_by_key: dict[str, list[float]] = defaultdict(list)
    for record in samples:
        payload = _mapping(record.get(record_key))
        for key, value in payload.items():
            try:
                values_by_key[str(key)].append(float(value))
            except (TypeError, ValueError):
                continue
    return {
        key: MetricSummary.from_samples(tuple(values))
        for key, values in sorted(values_by_key.items())
        if values
    }


def _top_phase_summary(phase_timings: dict[str, MetricSummary]) -> str:
    if not phase_timings:
        return "-"
    ranked = sorted(
        phase_timings.items(),
        key=lambda item: (-item[1].p50, _phase_sort_key(item[0])),
    )
    return ", ".join(f"{phase}={summary.p50:.3f}s" for phase, summary in ranked[:3])


def _task_stats_summary(task_stats: dict[str, MetricSummary]) -> str:
    applied = task_stats.get("applied")
    skipped = task_stats.get("skipped")
    replayed = task_stats.get("replayed")
    if applied is None and skipped is None and replayed is None:
        return "-"
    return (
        f"{0.0 if applied is None else applied.p50:.1f}/"
        f"{0.0 if skipped is None else skipped.p50:.1f}/"
        f"{0.0 if replayed is None else replayed.p50:.1f}"
    )


def _build_resume_comparisons(
    scenarios: tuple[IndexBenchmarkScenarioSummary, ...],
) -> tuple[dict[str, object], ...]:
    fresh_by_key: dict[tuple[object, ...], IndexBenchmarkScenarioSummary] = {}
    resumed_summaries: list[IndexBenchmarkScenarioSummary] = []
    for summary in scenarios:
        family_key = _resume_family_key(summary.key)
        if summary.key.resume_kind == "fresh":
            fresh_by_key[family_key] = summary
        elif summary.key.resume_kind == "resumed":
            resumed_summaries.append(summary)
    comparisons: list[dict[str, object]] = []
    for resumed in sorted(resumed_summaries, key=lambda item: _scenario_sort_tuple(item.key)):
        interrupt_percent = resumed.key.interrupt_after_task_percent
        fresh = fresh_by_key.get(_resume_family_key(resumed.key))
        if fresh is None:
            continue
        replayed = resumed.task_stats.get("replayed")
        applied = resumed.task_stats.get("applied")
        skipped = resumed.task_stats.get("skipped")
        expected_remaining_wall = fresh.wall_seconds.p50
        if interrupt_percent is not None:
            expected_remaining_wall = fresh.wall_seconds.p50 * (
                max(0.0, 100.0 - float(interrupt_percent)) / 100.0
            )
        resume_vs_remaining = "-"
        if expected_remaining_wall > 0:
            resume_vs_remaining = f"{resumed.wall_seconds.p50 / expected_remaining_wall:.2f}x"
        replay_overhead = "-"
        if replayed is not None and applied is not None and applied.p50 > 0:
            replay_overhead = f"{replayed.p50 / applied.p50:.2%} of applied tasks"
        elif replayed is not None:
            replay_overhead = f"{replayed.p50:.1f} replayed tasks"
        comparisons.append(
            {
                "scenario": _resume_comparison_label(resumed.key),
                "interrupt_after_task_percent": 0 if interrupt_percent is None else interrupt_percent,
                "fresh_wall_p50": fresh.wall_seconds.p50,
                "resumed_wall_p50": resumed.wall_seconds.p50,
                "expected_remaining_wall_p50": expected_remaining_wall,
                "resume_vs_remaining": resume_vs_remaining,
                "resumed_task_stats": (
                    f"{0.0 if applied is None else applied.p50:.1f}/"
                    f"{0.0 if skipped is None else skipped.p50:.1f}/"
                    f"{0.0 if replayed is None else replayed.p50:.1f}"
                ),
                "replay_overhead": replay_overhead,
                "validate_status": _validate_status_summary(resumed.validate_status_counts),
            }
        )
    return tuple(comparisons)


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


def _benchmark_phase_name(value: str) -> str:
    return BENCHMARK_PHASE_ALIASES.get(value, value)


def _phase_sort_key(value: str) -> tuple[int, str]:
    try:
        return (BENCHMARK_PHASE_ORDER.index(value), value)
    except ValueError:
        return (len(BENCHMARK_PHASE_ORDER), value)


def _resume_family_key(key: IndexBenchmarkScenarioKey) -> tuple[object, ...]:
    return (
        key.mode,
        key.target,
        key.source,
        key.cache_mode,
        key.parallel_mode,
        key.workers_requested,
        key.writer_batch_size,
        key.writer_max_files_per_transaction,
        key.writer_max_records_per_transaction,
        key.writer_commit_interval_ms,
        key.sqlite_journal_mode,
        key.sqlite_synchronous,
        key.sqlite_wal_autocheckpoint_pages,
    )


def _resume_comparison_label(key: IndexBenchmarkScenarioKey) -> str:
    return (
        f"w={key.workers_requested}, m={key.parallel_mode}, "
        f"b={key.writer_batch_size}, mf={key.writer_max_files_per_transaction}, "
        f"mr={key.writer_max_records_per_transaction}, c={key.writer_commit_interval_ms}, "
        f"{key.cache_mode}, {key.sqlite_journal_mode}/{key.sqlite_synchronous}"
    )


def _scenario_sort_tuple(key: IndexBenchmarkScenarioKey) -> tuple[object, ...]:
    return (
        key.mode,
        key.target,
        key.source,
        key.cache_mode,
        key.resume_kind,
        key.resume_mode,
        -1 if key.interrupt_after_task_percent is None else key.interrupt_after_task_percent,
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


def _interrupt_label(value: int | None) -> str:
    return "-" if value is None else f"{value}%"


def _validate_status_summary(counts: dict[str, int]) -> str:
    if not counts:
        return "not_run"
    if len(counts) == 1:
        status, count = next(iter(sorted(counts.items())))
        return f"{status} ({count})"
    return ", ".join(f"{status} ({count})" for status, count in sorted(counts.items()))
