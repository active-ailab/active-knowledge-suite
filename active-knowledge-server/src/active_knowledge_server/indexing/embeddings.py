"""Embedding job boundary."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from active_knowledge_server.security.secret_scan import SecretScanReportEntry, SecretScanner

EMBEDDING_PREPARATION_SCHEMA_VERSION: Final = "embedding_preparation.v1"
LOCAL_EMBEDDING_DIMENSIONS: Final = 24
_LOCAL_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class EmbeddingInput:
	"""One chunk, entity, or evidence payload considered for embedding."""

	object_id: str
	object_type: Literal["chunk", "entity", "evidence"]
	source_path: str
	content: str
	metadata: Mapping[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable embedding input summary."""

		return {
			"object_id": self.object_id,
			"object_type": self.object_type,
			"source_path": self.source_path,
			"content_length": len(self.content),
			"metadata": dict(self.metadata),
		}


@dataclass(frozen=True)
class EmbeddingPreparationResult:
	"""Embedding inputs split into accepted items and secret-scan skips."""

	schema_version: str
	accepted_inputs: tuple[EmbeddingInput, ...]
	skipped_reports: tuple[SecretScanReportEntry, ...]

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable embedding preparation result."""

		return {
			"schema_version": self.schema_version,
			"accepted_inputs": [item.to_dict() for item in self.accepted_inputs],
			"skipped_reports": [report.to_dict() for report in self.skipped_reports],
		}


def embed_text_locally(
	text: str,
	*,
	dimensions: int = LOCAL_EMBEDDING_DIMENSIONS,
) -> tuple[float, ...]:
	"""Return a deterministic offline embedding shared by indexing and query."""

	if dimensions < 1:
		raise ValueError("dimensions must be >= 1")
	vector = [0.0] * dimensions
	for token in _LOCAL_TOKEN_RE.findall(text.lower()):
		digest = hashlib.sha256(token.encode("utf-8")).digest()
		primary = digest[0] % dimensions
		secondary = digest[1] % dimensions
		sign = 1.0 if digest[2] % 2 == 0 else -1.0
		weight = 1.0 + (digest[3] / 255.0)
		vector[primary] += sign * weight
		vector[secondary] += sign * 0.5
	if not any(vector):
		vector[0] = 1.0
	norm = math.sqrt(sum(component * component for component in vector))
	if norm == 0.0:
		return tuple(0.0 for _ in vector)
	return tuple(component / norm for component in vector)


def prepare_embedding_inputs(
	inputs: Sequence[EmbeddingInput],
	*,
	secret_scanner: SecretScanner | None = None,
) -> EmbeddingPreparationResult:
	"""Filter out inputs whose content contains secrets before vectorization."""

	if secret_scanner is None or not secret_scanner.enabled:
		return EmbeddingPreparationResult(
			schema_version=EMBEDDING_PREPARATION_SCHEMA_VERSION,
			accepted_inputs=tuple(inputs),
			skipped_reports=(),
		)

	accepted: list[EmbeddingInput] = []
	skipped: list[SecretScanReportEntry] = []
	for item in inputs:
		result = secret_scanner.scan_text(item.content, source_path=item.source_path)
		if result.skip_embedding:
			skipped.append(result.to_report_entry())
			continue
		accepted.append(item)

	return EmbeddingPreparationResult(
		schema_version=EMBEDDING_PREPARATION_SCHEMA_VERSION,
		accepted_inputs=tuple(accepted),
		skipped_reports=tuple(skipped),
	)
