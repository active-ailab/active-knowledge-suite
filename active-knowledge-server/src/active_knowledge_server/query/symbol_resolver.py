"""Thin symbol-resolution wrapper over SymbolRetriever."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from active_knowledge_server.query.retrievers import (
	SymbolCandidate,
	SymbolRetriever,
	SymbolSearchRequest,
)
from active_knowledge_server.storage import ALL_SCOPE, QueryScope, StorageAdapter

SymbolResolutionStatus = Literal["resolved", "multi_result", "zero_result"]


@dataclass(frozen=True)
class SymbolResolution:
	"""Resolved symbol entity or a disambiguation payload."""

	status: SymbolResolutionStatus
	query: str
	selected: SymbolCandidate | None = None
	candidates: tuple[SymbolCandidate, ...] = ()
	reason: str | None = None

	def to_dict(self) -> dict[str, object]:
		return {
			"status": self.status,
			"query": self.query,
			"selected": None if self.selected is None else self.selected.to_dict(),
			"candidates": [item.to_dict() for item in self.candidates],
			"reason": self.reason,
		}


class SymbolResolver:
	"""Resolve one code entity or return stable disambiguation candidates."""

	def __init__(self, retriever: SymbolRetriever) -> None:
		self._retriever = retriever

	@classmethod
	def from_storage(cls, adapter: StorageAdapter) -> SymbolResolver:
		return cls(SymbolRetriever.from_storage(adapter))

	def resolve(
		self,
		name: str,
		*,
		entity_type: str | None = None,
		profile_id: str | None = None,
		snapshot_id: str | None = None,
		source_scope: str = ALL_SCOPE,
		top_k: int = 5,
	) -> SymbolResolution:
		request = SymbolSearchRequest(
			query=name,
			entity_type=entity_type,
			top_k=top_k,
			scope=QueryScope(
				snapshot_id=snapshot_id or "current",
				profile_id=profile_id or ALL_SCOPE,
				source_scope=source_scope,
			),
		)
		result = self._retriever.search(request)
		if not result.candidates:
			return SymbolResolution(
				status="zero_result",
				query=name,
				reason="No symbol candidates matched the requested scope.",
			)

		if len(result.candidates) == 1:
			return SymbolResolution(
				status="resolved",
				query=name,
				selected=result.candidates[0],
				candidates=result.candidates,
				reason="One symbol candidate matched the request.",
			)

		exact_candidates = [item for item in result.candidates if "exact" in item.match_kinds]
		if len(exact_candidates) == 1:
			return SymbolResolution(
				status="resolved",
				query=name,
				selected=exact_candidates[0],
				candidates=result.candidates,
				reason="One exact symbol candidate matched the request.",
			)

		top_candidate = result.candidates[0]
		second_candidate = result.candidates[1]
		if top_candidate.score - second_candidate.score >= 0.15 and "exact" in top_candidate.match_kinds:
			return SymbolResolution(
				status="resolved",
				query=name,
				selected=top_candidate,
				candidates=result.candidates,
				reason="Top exact candidate is clearly ahead of the remaining matches.",
			)

		return SymbolResolution(
			status="multi_result",
			query=name,
			candidates=result.candidates,
			reason="Multiple symbol candidates require disambiguation.",
		)
