"""MCP annotation helpers."""

from __future__ import annotations


def readonly_annotations(
	*,
	title: str | None = None,
	open_world: bool = False,
) -> dict[str, object]:
	"""Return a consistent read-only annotation payload for tools/resources."""

	annotations: dict[str, object] = {
		"readOnlyHint": True,
		"idempotentHint": True,
		"openWorldHint": open_world,
	}
	if title is not None:
		annotations["title"] = title
	return annotations
