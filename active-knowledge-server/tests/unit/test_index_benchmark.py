from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_knowledge_server.eval.index_benchmark import (
    ProgressPhaseTimingCollector,
    load_index_benchmark_records,
    parse_positive_int_csv,
    render_index_benchmark_markdown,
    summarize_index_benchmark_records,
)
from active_knowledge_server.indexing.progress import IndexProgressEvent


def _record(
    *,
    workers: int | str,
    parallel_mode: str = "thread",
    batch_size: int,
    max_files_per_transaction: int | None = None,
    max_records_per_transaction: int | None = None,
    commit_interval_ms: int,
    wall_seconds: float,
    rss_delta_bytes: int,
    resumed: bool = False,
    resume_mode: str = "disabled",
    warning_codes: tuple[str, ...] = (),
    journal_mode: str = "delete",
    metadata_db_bytes: int = 1_000,
    metadata_wal_bytes: int = 0,
    replayed_tasks: int = 0,
    interrupt_after_task_percent: int | None = None,
    validate_status: str = "not_run",
    readonly_probe_mode: str = "none",
    readonly_latency_p50_ms: float = 0.0,
    readonly_latency_p95_ms: float = 0.0,
    readonly_busy_count: int = 0,
    readonly_error_count: int = 0,
) -> dict[str, object]:
    return {
        "schema_version": "index_benchmark_record.v4",
        "mode": "incremental",
        "target": "local",
        "source": "all",
        "cache_mode": "cold",
        "workers_requested": workers,
        "job": {
            "resumed": resumed,
            "resume_policy": {
                "mode": resume_mode,
            },
        },
        "parallel": {
            "mode": parallel_mode,
        },
        "result_status": "ready",
        "wall_seconds": wall_seconds,
        "cpu_seconds": wall_seconds / 2.0,
        "rss_delta_bytes": rss_delta_bytes,
        "warning_count": len(warning_codes),
        "warning_codes": list(warning_codes),
        "phase_timings": {
            "discover": 0.5,
            "code_collect": wall_seconds / 3.0,
            "code_apply": wall_seconds / 4.0,
            "workspace_map": 0.1,
        },
        "task_stats": {
            "total": 10,
            "applied": 8,
            "skipped": 2 if resumed else 0,
            "failed": 0,
            "replayed": replayed_tasks,
        },
        "interrupt": {
            "after_task_percent": interrupt_after_task_percent,
            "expected_remaining_wall_seconds": None,
            "resume_vs_expected_remaining_ratio": None,
        },
        "validate": {
            "ran": validate_status != "not_run",
            "status": validate_status,
        },
        "readonly_probe": {
            "enabled": readonly_probe_mode != "none",
            "mode": readonly_probe_mode,
            "success_count": 16 if readonly_probe_mode != "none" else 0,
            "busy_count": readonly_busy_count,
            "error_count": readonly_error_count,
            "startup_wait_count": 0,
            "latency_ms_p50": readonly_latency_p50_ms,
            "latency_ms_p95": readonly_latency_p95_ms,
            "latency_ms_max": max(readonly_latency_p50_ms, readonly_latency_p95_ms),
            "last_error": None,
        },
        "writer": {
            "batch_size": batch_size,
            "max_files_per_transaction": (
                batch_size if max_files_per_transaction is None else max_files_per_transaction
            ),
            "max_records_per_transaction": (
                2048
                if max_records_per_transaction is None
                else max_records_per_transaction
            ),
            "commit_interval_ms": commit_interval_ms,
        },
        "sqlite": {
            "configured": {
                "journal_mode": journal_mode,
                "synchronous": "full",
                "wal_autocheckpoint_pages": None,
                "assume_local_filesystem": journal_mode == "wal",
            }
        },
        "storage_files": {
            "metadata_db_bytes": metadata_db_bytes,
            "metadata_wal_bytes": metadata_wal_bytes,
            "metadata_shm_bytes": 0,
        },
        "dataset": {
            "workspace_file_count": 4096,
            "source_doc_file_count": 128,
            "dataset_tier": "small",
        },
        "machine": {
            "platform": "linux",
            "python": "3.11.9",
            "cpu_count": 8,
        },
        "git_commit": "abc123",
    }


def test_parse_positive_int_csv_accepts_defaults_and_deduplicates() -> None:
    assert parse_positive_int_csv(None, default=(64,)) == (64,)
    assert parse_positive_int_csv("64,128,64", default=(1,)) == (64, 128)


def test_parse_positive_int_csv_rejects_non_positive_values() -> None:
    with pytest.raises(ValueError, match="positive integers"):
        parse_positive_int_csv("0,64", default=(64,))


def test_progress_phase_timing_collector_aggregates_contiguous_phase_time() -> None:
    collector = ProgressPhaseTimingCollector()

    collector.observe(IndexProgressEvent(phase="discover"), observed_at=10.0)
    collector.observe(IndexProgressEvent(phase="code_collect"), observed_at=12.0)
    collector.observe(IndexProgressEvent(phase="code_collect"), observed_at=15.5)
    collector.observe(IndexProgressEvent(phase="code_apply"), observed_at=18.0)
    snapshot = collector.finish(observed_at=20.0)

    assert snapshot.phase_timings["discover"] == pytest.approx(2.0)
    assert snapshot.phase_timings["code_collect"] == pytest.approx(6.0)
    assert snapshot.phase_timings["code_apply"] == pytest.approx(2.0)
    assert snapshot.phase_event_counts["code_collect"] == 2
    assert snapshot.event_count == 4


def test_summarize_index_benchmark_records_recommends_fastest_stable_scenario() -> None:
    records = [
        _record(
            workers=1,
            batch_size=64,
            commit_interval_ms=1000,
            wall_seconds=10.0,
            rss_delta_bytes=100,
        ),
        _record(
            workers=4,
            batch_size=100,
            commit_interval_ms=500,
            wall_seconds=6.0,
            rss_delta_bytes=150,
        ),
        _record(
            workers=8,
            batch_size=200,
            commit_interval_ms=500,
            wall_seconds=5.8,
            rss_delta_bytes=250,
            journal_mode="wal",
            metadata_wal_bytes=2_400,
        ),
        _record(
            workers=2,
            batch_size=500,
            commit_interval_ms=250,
            wall_seconds=6.2,
            rss_delta_bytes=120,
            warning_codes=("embedding.provider_unavailable",),
        ),
    ]

    report = summarize_index_benchmark_records(records)

    assert len(report.scenario_summaries) == 4
    assert report.recommendations
    recommendation = report.recommendations[0]
    assert recommendation.key.workers_requested == "4"
    assert recommendation.key.resume_kind == "fresh"
    assert recommendation.key.interrupt_after_task_percent is None
    assert recommendation.key.parallel_mode == "thread"
    assert recommendation.key.readonly_probe_mode == "none"
    assert recommendation.key.writer_batch_size == 100
    assert recommendation.key.writer_max_files_per_transaction == 100
    assert recommendation.key.writer_max_records_per_transaction == 2048
    assert recommendation.key.writer_commit_interval_ms == 500

    by_workers = {
        summary.key.workers_requested: summary for summary in report.scenario_summaries
    }
    assert by_workers["4"].speedup_vs_reference_p50 == pytest.approx(10.0 / 6.0)
    assert "memory_gt_2x_reference" in by_workers["8"].risk_flags
    assert "wal_larger_than_db" in by_workers["8"].risk_flags
    assert "vector_write_warning" in by_workers["2"].risk_flags
    assert "warnings_present" in by_workers["2"].risk_flags


def test_render_index_benchmark_markdown_includes_recommendation_and_risks() -> None:
    report = summarize_index_benchmark_records(
        [
            _record(
                workers=1,
                batch_size=64,
                commit_interval_ms=1000,
                wall_seconds=10.0,
                rss_delta_bytes=100,
            ),
            _record(
                workers=4,
                batch_size=100,
                commit_interval_ms=500,
                wall_seconds=6.0,
                rss_delta_bytes=150,
                readonly_probe_mode="sqlite_count",
                readonly_latency_p50_ms=1.2,
                readonly_latency_p95_ms=3.4,
            ),
            _record(
                workers=8,
                batch_size=200,
                commit_interval_ms=500,
                wall_seconds=5.8,
                rss_delta_bytes=250,
                journal_mode="wal",
                metadata_wal_bytes=2_400,
                readonly_probe_mode="sqlite_count",
                readonly_latency_p50_ms=1.5,
                readonly_latency_p95_ms=4.1,
            ),
        ]
    )

    markdown = render_index_benchmark_markdown(report)

    assert "# Index Benchmark Report" in markdown
    assert (
        "resume=fresh/disabled, interrupt=-, workers=4, parallel_mode=thread, "
        "readonly_probe=sqlite_count, batch_size=100, "
        "max_files_per_transaction=100, max_records_per_transaction=2048, "
        "commit_interval_ms=500"
    ) in markdown
    assert "1.20/3.40 busy=0.0" in markdown
    assert "wal_larger_than_db" in markdown


def test_summarize_index_benchmark_records_flags_readonly_probe_busy() -> None:
    report = summarize_index_benchmark_records(
        [
            _record(
                workers=1,
                batch_size=64,
                commit_interval_ms=1000,
                wall_seconds=10.0,
                rss_delta_bytes=100,
                readonly_probe_mode="sqlite_count",
                readonly_latency_p50_ms=2.4,
                readonly_latency_p95_ms=8.8,
                readonly_busy_count=3,
            ),
        ]
    )

    summary = report.scenario_summaries[0]
    assert "readonly_busy" in summary.risk_flags


def test_render_index_benchmark_markdown_includes_resume_summary() -> None:
    report = summarize_index_benchmark_records(
        [
            _record(
                workers=4,
                batch_size=100,
                commit_interval_ms=500,
                wall_seconds=10.0,
                rss_delta_bytes=150,
            ),
            _record(
                workers=4,
                batch_size=100,
                commit_interval_ms=500,
                wall_seconds=3.0,
                rss_delta_bytes=120,
                resumed=True,
                resume_mode="auto",
                replayed_tasks=1,
                interrupt_after_task_percent=70,
                validate_status="ok",
            ),
        ]
    )

    markdown = render_index_benchmark_markdown(report)

    assert "## Resume Summary" in markdown
    assert "70%" in markdown
    assert "1.00x" in markdown
    assert "ok (1)" in markdown
    assert "1.0 replayed tasks" not in markdown
    assert "12.50% of applied tasks" in markdown


def test_summarize_index_benchmark_records_keeps_distinct_interrupt_percent_scenarios() -> None:
    report = summarize_index_benchmark_records(
        [
            _record(
                workers=4,
                batch_size=100,
                commit_interval_ms=500,
                wall_seconds=10.0,
                rss_delta_bytes=150,
            ),
            _record(
                workers=4,
                batch_size=100,
                commit_interval_ms=500,
                wall_seconds=7.0,
                rss_delta_bytes=120,
                resumed=True,
                resume_mode="auto",
                interrupt_after_task_percent=30,
                validate_status="ok",
            ),
            _record(
                workers=4,
                batch_size=100,
                commit_interval_ms=500,
                wall_seconds=3.0,
                rss_delta_bytes=120,
                resumed=True,
                resume_mode="auto",
                interrupt_after_task_percent=70,
                validate_status="ok",
            ),
        ]
    )

    resumed_summaries = [
        summary for summary in report.scenario_summaries if summary.key.resume_kind == "resumed"
    ]
    assert len(resumed_summaries) == 2
    assert {summary.key.interrupt_after_task_percent for summary in resumed_summaries} == {30, 70}
    assert len(report.resume_comparisons) == 2


def test_summarize_index_benchmark_records_backfills_legacy_writer_limits() -> None:
    legacy = _record(
        workers=1,
        batch_size=64,
        commit_interval_ms=1000,
        wall_seconds=10.0,
        rss_delta_bytes=100,
    )
    writer = legacy["writer"]
    assert isinstance(writer, dict)
    del writer["max_files_per_transaction"]
    del writer["max_records_per_transaction"]

    report = summarize_index_benchmark_records([legacy])

    summary = report.scenario_summaries[0]
    assert summary.key.writer_batch_size == 64
    assert summary.key.writer_max_files_per_transaction == 64
    assert summary.key.writer_max_records_per_transaction == 0


def test_load_index_benchmark_records_reads_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "benchmark.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record, sort_keys=True)
            for record in (
                _record(
                    workers=1,
                    batch_size=64,
                    commit_interval_ms=1000,
                    wall_seconds=10.0,
                    rss_delta_bytes=100,
                ),
                _record(
                    workers=4,
                    batch_size=100,
                    commit_interval_ms=500,
                    wall_seconds=6.0,
                    rss_delta_bytes=150,
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    records = load_index_benchmark_records(path)

    assert len(records) == 2
    assert records[0]["workers_requested"] == 1
    assert records[1]["writer"]["batch_size"] == 100
    assert records[1]["writer"]["max_files_per_transaction"] == 100
    assert records[1]["writer"]["max_records_per_transaction"] == 2048
