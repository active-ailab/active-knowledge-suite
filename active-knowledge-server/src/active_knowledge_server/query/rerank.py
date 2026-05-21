"""Reranking boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

from active_knowledge_server.models.query import QueryIntent

RerankMode = Literal["none", "lightweight", "cross_encoder"]

_RRF_K: Final = 60.0
_AUTHORITY_SCORES: Final[dict[str, float]] = {
	"source_doc": 1.0,
	"api_doc": 0.95,
	"widget_doc": 0.92,
	"workspace_code": 0.82,
	"source_code": 0.80,
	"workspace_map": 0.68,
	"derived": 0.40,
	"synthetic": 0.28,
	"unknown": 0.20,
}
_CODE_INTENTS: Final[set[QueryIntent]] = {
	"code_exact",
	"code_concept",
	"call_trace",
	"runtime_flow",
	"profile_diff",
	"workspace_nav",
	"evidence_lookup",
}
_DOC_INTENTS: Final[set[QueryIntent]] = {
	"api_lookup",
	"widget_lookup",
	"product_context",
	"project_context",
	"unknown",
}


@dataclass(frozen=True)
class RetrievalSignal:
	"""One retriever contribution kept for explainable fusion traces."""

	retriever: str
	rank: int
	weight: float
	raw_score: float
	rrf_score: float
	match_reason: str

	def to_dict(self) -> dict[str, object]:
		return {
			"retriever": self.retriever,
			"rank": self.rank,
			"weight": self.weight,
			"raw_score": self.raw_score,
			"rrf_score": self.rrf_score,
			"match_reason": self.match_reason,
		}


@dataclass(frozen=True)
class FusionCandidate:
	"""Common ranked candidate shape used across fusion and rerank."""

	candidate_id: str
	object_type: str
	title: str | None
	snippet: str | None
	relative_path: str | None
	profile_id: str
	raw_score: float
	source_index: str | None = None
	source_scope: str | None = None
	module_names: tuple[str, ...] = ()
	authority_level: str = "unknown"
	freshness_ts: str | None = None
	graph_depth: int | None = None
	graph_proximity: float = 0.0
	evidence_keys: tuple[str, ...] = ()
	match_reasons: tuple[str, ...] = ()
	retrieval_signals: tuple[RetrievalSignal, ...] = ()
	metadata: dict[str, object] = field(default_factory=dict)
	fused_score: float = 0.0
	rerank_score: float = 0.0

	def to_dict(self) -> dict[str, object]:
		return {
			"candidate_id": self.candidate_id,
			"object_type": self.object_type,
			"title": self.title,
			"snippet": self.snippet,
			"relative_path": self.relative_path,
			"profile_id": self.profile_id,
			"raw_score": self.raw_score,
			"source_index": self.source_index,
			"source_scope": self.source_scope,
			"module_names": list(self.module_names),
			"authority_level": self.authority_level,
			"freshness_ts": self.freshness_ts,
			"graph_depth": self.graph_depth,
			"graph_proximity": self.graph_proximity,
			"evidence_keys": list(self.evidence_keys),
			"match_reasons": list(self.match_reasons),
			"retrieval_signals": [item.to_dict() for item in self.retrieval_signals],
			"metadata": dict(self.metadata),
			"fused_score": self.fused_score,
			"rerank_score": self.rerank_score,
		}


class CandidateReranker(Protocol):
	"""Small reranker protocol used by the query service."""

	mode: RerankMode

	def rerank(
		self,
		candidates: Sequence[FusionCandidate],
		*,
		intent: QueryIntent,
		requested_profile_id: str,
	) -> tuple[FusionCandidate, ...]:
		"""Return candidates in final display order."""


@dataclass(frozen=True)
class NoopReranker:
	"""Pass-through reranker used when reranking is disabled."""

	mode: RerankMode = "none"

	def rerank(
		self,
		candidates: Sequence[FusionCandidate],
		*,
		intent: QueryIntent,
		requested_profile_id: str,
	) -> tuple[FusionCandidate, ...]:
		return tuple(
			replace(candidate, rerank_score=round(candidate.fused_score, 6))
			for candidate in sorted(
				candidates,
				key=lambda item: (-item.fused_score, item.candidate_id),
			)
		)


@dataclass(frozen=True)
class LightweightReranker:
	"""Rule-based reranker that prefers authoritative, fresh, and profile-close evidence."""

	now: datetime | None = None
	mode: RerankMode = "lightweight"

	def rerank(
		self,
		candidates: Sequence[FusionCandidate],
		*,
		intent: QueryIntent,
		requested_profile_id: str,
	) -> tuple[FusionCandidate, ...]:
		clock = self.now or datetime.now(UTC)
		reranked: list[FusionCandidate] = []
		for candidate in candidates:
			authority_score = resolve_authority_score(candidate.authority_level)
			profile_match_score = resolve_profile_match_score(
				candidate.profile_id,
				requested_profile_id,
			)
			freshness_score = resolve_freshness_score(candidate.freshness_ts, now=clock)
			graph_proximity = max(
				candidate.graph_proximity,
				0.0 if candidate.graph_depth is None else 1.0 / float(candidate.graph_depth + 1),
			)
			source_coverage = min(1.0, len(candidate.retrieval_signals) / 3.0)
			intent_bonus = 0.0
			if intent in _CODE_INTENTS and any(
				item.retriever == "symbol" for item in candidate.retrieval_signals
			):
				intent_bonus += 0.10
			if intent in _DOC_INTENTS and any(
				item.retriever == "vector" for item in candidate.retrieval_signals
			):
				intent_bonus += 0.08
			if intent in {"call_trace", "runtime_flow", "workspace_nav", "profile_diff"}:
				intent_bonus += 0.08 * graph_proximity

			bonus_multiplier = (
				1.0
				+ 0.20 * authority_score
				+ 0.15 * profile_match_score
				+ 0.15 * freshness_score
				+ 0.15 * graph_proximity
				+ 0.05 * source_coverage
				+ intent_bonus
			)
			metadata = dict(candidate.metadata)
			metadata["rerank_features"] = {
				"authority": round(authority_score, 6),
				"profile_match": round(profile_match_score, 6),
				"freshness": round(freshness_score, 6),
				"graph_proximity": round(graph_proximity, 6),
				"source_coverage": round(source_coverage, 6),
				"intent_bonus": round(intent_bonus, 6),
				"mode": self.mode,
			}
			reranked.append(
				replace(
					candidate,
					metadata=metadata,
					rerank_score=round(candidate.fused_score * bonus_multiplier, 6),
				)
			)
		return tuple(
			sorted(
				reranked,
				key=lambda item: (-item.rerank_score, -item.fused_score, item.candidate_id),
			)
		)


def build_reranker(mode: RerankMode) -> CandidateReranker:
	"""Resolve the configured reranker implementation."""

	if mode == "none":
		return NoopReranker()
	return LightweightReranker(mode=mode)


def fuse_ranked_candidates(
	ranked_candidates_by_source: Mapping[str, Sequence[FusionCandidate]],
	*,
	weights: Mapping[str, float],
	rrf_k: float = _RRF_K,
) -> tuple[FusionCandidate, ...]:
	"""Fuse multiple ranked lists using weighted reciprocal rank fusion."""

	if rrf_k <= 0.0:
		raise ValueError("rrf_k must be > 0")

	fused: dict[str, FusionCandidate] = {}
	for retriever, candidates in ranked_candidates_by_source.items():
		weight = max(0.0, float(weights.get(retriever, 0.0)))
		if weight == 0.0:
			continue
		for rank, candidate in enumerate(candidates, start=1):
			rrf_score = weight / (rrf_k + float(rank))
			signal = RetrievalSignal(
				retriever=retriever,
				rank=rank,
				weight=round(weight, 6),
				raw_score=round(candidate.raw_score, 6),
				rrf_score=round(rrf_score, 6),
				match_reason="; ".join(candidate.match_reasons),
			)
			existing = fused.get(candidate.candidate_id)
			if existing is None:
				fused[candidate.candidate_id] = replace(
					candidate,
					match_reasons=_dedupe_text(candidate.match_reasons),
					retrieval_signals=(signal,),
					fused_score=round(rrf_score, 6),
					rerank_score=round(rrf_score, 6),
				)
				continue
			fused[candidate.candidate_id] = _merge_candidates(
				existing,
				candidate,
				signal=signal,
				rrf_score=rrf_score,
			)
	return tuple(
		sorted(
			fused.values(),
			key=lambda item: (-item.fused_score, -item.graph_proximity, item.candidate_id),
		)
	)


def resolve_authority_score(authority_level: str | None) -> float:
	key = (authority_level or "unknown").strip().lower()
	return _AUTHORITY_SCORES.get(key, _AUTHORITY_SCORES["unknown"])


def resolve_profile_match_score(candidate_profile_id: str, requested_profile_id: str) -> float:
	requested = requested_profile_id.strip().lower()
	candidate = candidate_profile_id.strip().lower()
	if requested in {"", "auto", "all"}:
		return 0.6 if candidate == "all" else 0.8
	if candidate == requested:
		return 1.0
	if candidate == "all":
		return 0.45
	return 0.0


def resolve_freshness_score(value: str | None, *, now: datetime) -> float:
	if value is None or not value.strip():
		return 0.0
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return 0.0
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=UTC)
	age_seconds = max(0.0, (now - parsed.astimezone(UTC)).total_seconds())
	age_days = age_seconds / 86400.0
	if age_days <= 7.0:
		return 1.0
	if age_days <= 30.0:
		return 0.8
	if age_days <= 90.0:
		return 0.55
	if age_days <= 365.0:
		return 0.25
	return 0.0


def _merge_candidates(
	existing: FusionCandidate,
	new: FusionCandidate,
	*,
	signal: RetrievalSignal,
	rrf_score: float,
) -> FusionCandidate:
	authority_level = existing.authority_level
	if resolve_authority_score(new.authority_level) > resolve_authority_score(existing.authority_level):
		authority_level = new.authority_level
	freshness_ts = _prefer_newer_timestamp(existing.freshness_ts, new.freshness_ts)
	return replace(
		existing,
		title=existing.title or new.title,
		snippet=_prefer_snippet(existing.snippet, new.snippet),
		relative_path=existing.relative_path or new.relative_path,
		profile_id=_prefer_profile(existing.profile_id, new.profile_id),
		raw_score=max(existing.raw_score, new.raw_score),
		source_index=_prefer_source_index(existing.source_index, new.source_index),
		source_scope=existing.source_scope or new.source_scope,
		module_names=_dedupe_text((*existing.module_names, *new.module_names)),
		authority_level=authority_level,
		freshness_ts=freshness_ts,
		graph_depth=_prefer_graph_depth(existing.graph_depth, new.graph_depth),
		graph_proximity=max(existing.graph_proximity, new.graph_proximity),
		evidence_keys=_dedupe_text((*existing.evidence_keys, *new.evidence_keys)),
		match_reasons=_dedupe_text((*existing.match_reasons, *new.match_reasons)),
		retrieval_signals=(*existing.retrieval_signals, signal),
		metadata={**new.metadata, **existing.metadata},
		fused_score=round(existing.fused_score + rrf_score, 6),
		rerank_score=round(existing.fused_score + rrf_score, 6),
	)


def _prefer_source_index(current: str | None, candidate: str | None) -> str | None:
	priority = {None: 0, "baseline": 1, "merged": 2, "overlay": 3, "derived": 4}
	if priority.get(candidate, 0) > priority.get(current, 0):
		return candidate
	return current


def _prefer_profile(current: str, candidate: str) -> str:
	if current == "all" and candidate != "all":
		return candidate
	return current


def _prefer_graph_depth(current: int | None, candidate: int | None) -> int | None:
	if current is None:
		return candidate
	if candidate is None:
		return current
	return min(current, candidate)


def _prefer_snippet(current: str | None, candidate: str | None) -> str | None:
	if current is None:
		return candidate
	if candidate is None:
		return current
	if len(candidate) > len(current):
		return candidate
	return current


def _prefer_newer_timestamp(current: str | None, candidate: str | None) -> str | None:
	if current is None:
		return candidate
	if candidate is None:
		return current
	try:
		current_dt = datetime.fromisoformat(current.replace("Z", "+00:00"))
		candidate_dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
	except ValueError:
		return current
	return candidate if candidate_dt >= current_dt else current


def _dedupe_text(values: Sequence[str]) -> tuple[str, ...]:
	return tuple(dict.fromkeys(value for value in values if value))
