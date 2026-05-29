from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_knowledge_server.eval.index_benchmark import (
    load_index_benchmark_records,
    parse_positive_int_csv,
    render_index_benchmark_markdown,
    summarize_index_benchmark_records,
)


def _record(
    *,
    workers: int | str,
    parallel_mode: str = "thread",
    batch_size: int,
    commit_interval_ms: int,
    wall_seconds: float,
    rss_delta_bytes: int,
    warning_codes: tuple[str, ...] = (),
    journal_mode: str = "delete",
    metadata_db_bytes: int = 1_000,
    metadata_wal_bytes: int = 0,
) -> dict[str, object]:
    return {
        "schema_version": "index_benchmark_record.v1",
        "mode": "incremental",
        "target": "local",
        "source": "all",
        "cache_mode": "cold",
        "workers_requested": workers,
        "parallel": {
            "mode": parallel_mode,
        },
        "result_status": "ready",
        "wall_seconds": wall_seconds,
        "cpu_seconds": wall_seconds / 2.0,
        "rss_delta_bytes": rss_delta_bytes,
        "warning_count": len(warning_codes),
        "warning_codes": list(warning_codes),
        "writer": {
            "batch_size": batch_size,
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
    assert recommendation.key.parallel_mode == "thread"
    assert recommendation.key.writer_batch_size == 100
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
        ]
    )

    markdown = render_index_benchmark_markdown(report)

    assert "# Index Benchmark Report" in markdown
    assert "workers=4, parallel_mode=thread, batch_size=100, commit_interval_ms=500" in markdown
    assert "wal_larger_than_db" in markdown


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
