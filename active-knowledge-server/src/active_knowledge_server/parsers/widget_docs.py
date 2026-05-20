"""Widget documentation parser wrapper."""

from __future__ import annotations

from pathlib import Path

from active_knowledge_server.parsers.markdown import ParsedDocument, parse_source_document


def parse_widget_doc(path: str | Path, *, text: str | None = None) -> ParsedDocument:
	"""Parse a widget source document using widget-specific front matter normalization."""

	return parse_source_document(path, text=text, category="widgets")
