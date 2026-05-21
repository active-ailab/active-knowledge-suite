from __future__ import annotations

from datetime import UTC, datetime

from active_knowledge_server.query.rerank import (
    FusionCandidate,
    LightweightReranker,
    fuse_ranked_candidates,
)


def test_weighted_rrf_prefers_stronger_weighted_rank_and_merges_signals() -> None:
    alpha = FusionCandidate(
        candidate_id="entity:alpha",
        object_type="entity",
        title="alpha",
        snippet="alpha symbol",
        relative_path="src/alpha.c",
        profile_id="all",
        raw_score=0.91,
        match_reasons=("symbol exact",),
    )
    beta = FusionCandidate(
        candidate_id="entity:beta",
        object_type="entity",
        title="beta",
        snippet="beta symbol",
        relative_path="src/beta.c",
        profile_id="all",
        raw_score=0.87,
        match_reasons=("symbol exact",),
    )
    fused = fuse_ranked_candidates(
        {
            "symbol": (alpha, beta),
            "fts": (
                FusionCandidate(
                    candidate_id="entity:beta",
                    object_type="entity",
                    title="beta",
                    snippet="beta fulltext",
                    relative_path="src/beta.c",
                    profile_id="all",
                    raw_score=0.95,
                    match_reasons=("fts hit",),
                ),
                FusionCandidate(
                    candidate_id="entity:alpha",
                    object_type="entity",
                    title="alpha",
                    snippet="alpha fulltext",
                    relative_path="src/alpha.c",
                    profile_id="all",
                    raw_score=0.76,
                    match_reasons=("fts hit",),
                ),
            ),
        },
        weights={"symbol": 0.6, "fts": 0.4},
    )

    assert [item.candidate_id for item in fused] == ["entity:alpha", "entity:beta"]
    assert len(fused[0].retrieval_signals) == 2
    assert {signal.retriever for signal in fused[0].retrieval_signals} == {"symbol", "fts"}
    assert fused[0].fused_score > fused[1].fused_score


def test_lightweight_reranker_boosts_authority_freshness_profile_and_graph() -> None:
    reranker = LightweightReranker(now=datetime(2026, 5, 21, tzinfo=UTC))
    strong_doc = FusionCandidate(
        candidate_id="doc:authoritative",
        object_type="doc",
        title="Authoritative API",
        snippet="recent source doc",
        relative_path="knowledge-sources/api/authoritative.md",
        profile_id="watch",
        raw_score=0.88,
        authority_level="source_doc",
        freshness_ts="2026-05-19T00:00:00Z",
        graph_depth=1,
        graph_proximity=0.5,
        match_reasons=("doc hit",),
        fused_score=0.0100,
        rerank_score=0.0100,
    )
    weak_doc = FusionCandidate(
        candidate_id="doc:stale-derived",
        object_type="doc",
        title="Derived Notes",
        snippet="older derived notes",
        relative_path="knowledge-sources/api/derived.md",
        profile_id="all",
        raw_score=0.92,
        authority_level="derived",
        freshness_ts="2024-01-01T00:00:00Z",
        graph_depth=4,
        graph_proximity=0.2,
        match_reasons=("doc hit",),
        fused_score=0.0103,
        rerank_score=0.0103,
    )

    reranked = reranker.rerank(
        (weak_doc, strong_doc),
        intent="api_lookup",
        requested_profile_id="watch",
    )

    assert [item.candidate_id for item in reranked] == ["doc:authoritative", "doc:stale-derived"]
    features = reranked[0].metadata["rerank_features"]
    assert features["authority"] > features["freshness"] - 0.3
    assert features["profile_match"] == 1.0
    assert features["graph_proximity"] == 0.5