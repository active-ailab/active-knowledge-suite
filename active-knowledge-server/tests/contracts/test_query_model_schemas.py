from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_knowledge_server.models import Candidate, EvidenceRef, QueryRequest, QueryResult, Warning

SNAPSHOT_DIR = Path(__file__).with_name("snapshots")


@pytest.mark.parametrize(
    ("snapshot_name", "model"),
    [
        ("query_request", QueryRequest),
        ("query_result", QueryResult),
        ("warning", Warning),
        ("evidence_ref", EvidenceRef),
        ("candidate", Candidate),
    ],
)
def test_query_contract_schema_snapshots(snapshot_name: str, model: type[object]) -> None:
    expected = json.loads(
        (SNAPSHOT_DIR / f"{snapshot_name}.schema.json").read_text(encoding="utf-8")
    )

    assert model.model_json_schema() == expected