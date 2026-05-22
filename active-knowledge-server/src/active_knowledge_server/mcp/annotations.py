"""MCP annotation helpers."""

from __future__ import annotations

def tool_annotations(
	*,
	title: str,
	read_only: bool,
	idempotent: bool,
	destructive: bool = False,
) -> dict[str, object]:
	"""Build one stable annotation payload for FastMCP tools/resources."""

	return {
		"title": title,
		"readOnlyHint": read_only,
		"idempotentHint": idempotent,
		"destructiveHint": destructive,
		"openWorldHint": False,
	}


def readonly_annotations(
	*,
	title: str,
) -> dict[str, object]:
	"""Build one stable read-only annotation payload for FastMCP tools/resources."""

	return tool_annotations(title=title, read_only=True, idempotent=True)
