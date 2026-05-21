"""Shared evidence reference contract models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvidenceType = Literal["code", "doc", "config", "graph", "workspace", "profile"]
SourceIndex = Literal["baseline", "overlay", "merged"]


class EvidenceRef(BaseModel):
	"""Stable short evidence reference returned by query tools."""

	model_config = ConfigDict(extra="forbid")

	evidence_id: str = Field(min_length=1)
	type: EvidenceType
	path: str = Field(min_length=1)
	start_line: int | None = Field(default=None, ge=1)
	end_line: int | None = Field(default=None, ge=1)
	authority_level: str = Field(min_length=1)
	excerpt: str | None = None
	content_hash: str | None = None
	source_index: SourceIndex | None = None

	@model_validator(mode="after")
	def validate_line_range(self) -> EvidenceRef:
		"""Ensure line ranges are monotonic when both ends are present."""

		if self.start_line is not None and self.end_line is not None:
			if self.end_line < self.start_line:
				raise ValueError("end_line must be greater than or equal to start_line")
		return self

	def to_dict(self) -> dict[str, Any]:
		"""Return a JSON-serializable payload."""

		return self.model_dump(mode="json", exclude_none=True)
