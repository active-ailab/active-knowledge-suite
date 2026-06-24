from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from active_knowledge_server.cli import main
from active_knowledge_server.eval.cases import load_eval_suite
from active_knowledge_server.feedback import (
    build_feedback_evidence_annotations,
    build_feedback_record,
    load_feedback_record,
    load_query_result_payload,
    write_eval_draft,
    write_feedback_record,
    write_learned_seed_draft,
)


def _query_result_payload() -> dict[str, object]:
    return {
        "schema_version": "query_result.v1",
        "tool_name": "code_resolve",
        "result_status": "ok",
        "confidence": 0.92,
        "confidence_band": "high",
        "query_intent": "code_exact",
        "snapshot_id": "current",
        "profile_id": "not_required",
        "summary": "Found the requested symbol definition.",
        "items": (
            {
                "candidate_id": "entity:health_service_publish_event",
                "object_type": "entity",
                "title": "health_service_publish_event",
                "snippet": "void health_service_publish_event(...);",
                "relative_path": "framework/health/service.c",
                "profile_id": "all",
                "module_names": ["health.service"],
                "score": 0.97,
                "fused_score": 0.95,
                "authority_level": "workspace_code",
            },
        ),
        "evidence_refs": (
            {
                "evidence_id": "ev-code",
                "type": "code",
                "path": "framework/health/service.c",
                "start_line": 42,
                "end_line": 58,
                "authority_level": "workspace_code",
                "excerpt": "void health_service_publish_event(...)",
                "source_index": "overlay",
            },
            {
                "evidence_id": "ev-doc",
                "type": "doc",
                "path": "knowledge-sources/engineering/health-events.md",
                "start_line": 10,
                "end_line": 18,
                "authority_level": "official",
                "excerpt": "Event publishing flow",
                "source_index": "baseline",
            },
        ),
        "warnings": (),
        "diagnostics": {
            "route": {
                "intent": "code_exact",
                "selected_view": "code",
                "selected_granularity": "symbol",
                "tool_plan": {
                    "route_mode": "direct",
                    "primary_tool": "code_resolve",
                },
            }
        },
    }


def _write_feedback_config(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workdir = tmp_path / ".active-kb"
    workspace.mkdir()
    source_docs.mkdir()
    config_path = tmp_path / "feedback.yaml"
    payload = {
        "deployment_mode": "local_single_user",
        "server": {
            "transport": "stdio",
            "http": {
                "host": "127.0.0.1",
                "port": 8765,
                "require_auth": False,
                "auth_provider": "none",
                "allowed_origins": ["http://127.0.0.1"],
            },
        },
        "runtime": {
            "source_root": str(tmp_path),
            "workdir": str(workdir),
            "baseline_dir": str(workdir / "baseline"),
            "local_dir": str(workdir / "local"),
            "source_docs_root": str(source_docs),
        },
        "project": {
            "id": "active",
            "display_name": "Active",
            "workspace_root": str(workspace),
        },
        "storage": {
            "baseline": {"manifest": str(workdir / "baseline" / "manifest.json")},
            "metadata": {
                "backend": "sqlite",
                "path": str(workdir / "baseline" / "db" / "metadata.db"),
                "mode": "readonly",
            },
            "overlay": {
                "backend": "sqlite",
                "path": str(workdir / "local" / "db" / "overlay.db"),
            },
            "jobs": {
                "backend": "sqlite",
                "path": str(workdir / "local" / "db" / "jobs.db"),
            },
            "vector": {
                "backend": "lancedb",
                "path": str(workdir / "baseline" / "vectors" / "lancedb"),
                "mode": "readonly",
            },
            "vector_delta": {
                "backend": "lancedb",
                "path": str(workdir / "local" / "vectors" / "lancedb-delta"),
            },
            "artifacts_root": str(workdir / "baseline" / "artifacts"),
            "local_artifacts_root": str(workdir / "local" / "artifacts"),
            "cache_root": str(workdir / "local" / "cache"),
        },
        "indexing": {
            "embeddings": {
                "enabled": False,
                "model": "disabled",
            },
        },
        "query": {
            "hybrid": {
                "rerank": "lightweight",
            },
        },
        "security": {
            "audit": {
                "enabled": True,
            },
        },
    }
    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return config_path


def test_feedback_artifacts_generate_eval_and_seed_drafts(tmp_path: Path) -> None:
    result_path = tmp_path / "query-result.json"
    result_path.write_text(
        json.dumps(_query_result_payload(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result, route = load_query_result_payload(result_path)
    annotations = build_feedback_evidence_annotations(
        result,
        useful_ids=["ev-code"],
        accepted_ids=["ev-doc"],
    )
    record = build_feedback_record(
        query="health_service_publish_event() 在哪里定义？",
        tool_name="code_resolve",
        query_intent="code_exact",
        profile_id=result.profile_id,
        snapshot_id=result.snapshot_id,
        result_status=result.result_status,
        result_summary=result.summary,
        source_refs=["smoke:zeppos"],
        route=route,
        evidence_feedback=annotations,
        returned_evidence_refs=result.evidence_refs,
        query_result_path=str(result_path),
    )

    artifact_root = tmp_path / "feedback"
    record_path = write_feedback_record(artifact_root, record)
    loaded = load_feedback_record(artifact_root, record.feedback_id)
    eval_path = write_eval_draft(artifact_root, loaded)
    seed_path = write_learned_seed_draft(artifact_root, loaded)

    assert record_path.is_file()
    assert loaded.feedback_id == record.feedback_id
    assert load_eval_suite(eval_path).suite_id == f"feedback-draft-{record.feedback_id}"
    seed_text = seed_path.read_text(encoding="utf-8")
    assert "review_status: pending" in seed_text
    assert "knowledge-sources/" not in str(seed_path.parent)


def test_feedback_cli_record_and_drafts(tmp_path: Path, capsys) -> None:
    config_path = _write_feedback_config(tmp_path)
    result_path = tmp_path / "query-result.json"
    result_path.write_text(
        json.dumps(_query_result_payload(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "feedback",
            "record",
            "--config",
            str(config_path),
            "--query",
            "health_service_publish_event() 在哪里定义？",
            "--result-file",
            str(result_path),
            "--evidence-useful",
            "ev-code",
            "--missed-symbol",
            "health_service_publish_event",
            "--source-ref",
            "smoke:zeppos",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    feedback_id = payload["feedback_id"]

    assert exit_code == 0
    assert payload["record"]["tool_name"] == "code_resolve"
    assert payload["record"]["source_refs"] == ["smoke:zeppos"]

    exit_code = main(
        [
            "feedback",
            "draft-eval",
            "--config",
            str(config_path),
            "--feedback-id",
            feedback_id,
            "--format",
            "json",
        ]
    )
    eval_payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert Path(eval_payload["draft_path"]).is_file()

    exit_code = main(
        [
            "feedback",
            "draft-seed",
            "--config",
            str(config_path),
            "--feedback-id",
            feedback_id,
            "--format",
            "json",
        ]
    )
    seed_payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert Path(seed_payload["draft_path"]).is_file()
    assert seed_payload["review_required"] is True


def test_feedback_subcommand_has_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "feedback", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "usage: active-kb feedback" in result.stdout
    assert "draft-eval" in result.stdout
    assert "draft-seed" in result.stdout
