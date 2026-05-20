"""API documentation parser wrapper."""

from __future__ import annotations

from pathlib import Path

from active_knowledge_server.parsers.markdown import ParsedDocument, parse_source_document


def parse_api_doc(path: str | Path, *, text: str | None = None) -> ParsedDocument:
	"""Parse an API source document using API-specific front matter normalization."""

	return parse_source_document(path, text=text, category="api")
