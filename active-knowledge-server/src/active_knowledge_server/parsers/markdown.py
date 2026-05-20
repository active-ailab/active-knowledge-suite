"""Markdown, HTML, and front matter parsing for source documents."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

import yaml

DOC_PARSER_SCHEMA_VERSION: Final = "doc_parser.v1"
_MARKDOWN_SUFFIXES: Final = {".md", ".markdown", ".mdx"}
_HTML_SUFFIXES: Final = {".html", ".htm"}
_ATX_HEADING_RE: Final = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?$")
_SETEXT_H1_RE: Final = re.compile(r"^\s*=+\s*$")
_SETEXT_H2_RE: Final = re.compile(r"^\s*-+\s*$")
_FENCE_RE: Final = re.compile(r"^\s*(```+|~~~+)")

_CATEGORY_ALIASES: Final[dict[str, str]] = {
	"api": "api",
	"widget": "widgets",
	"widgets": "widgets",
	"engineering": "engineering",
	"product": "product",
	"project": "project",
	"design": "design",
	"qa": "qa",
	"release": "release",
	"learned-seeds": "learned-seeds",
}
_COMMON_FRONT_MATTER_FIELDS: Final[dict[str, str]] = {
	"title": "string",
	"authority_level": "string",
	"profiles": "string_list",
	"tags": "string_list",
}
_CATEGORY_FRONT_MATTER_FIELDS: Final[dict[str, dict[str, str]]] = {
	"api": {
		"module": "string",
		"version": "string",
		"code_symbols": "string_list",
	},
	"widgets": {
		"widget": "string",
		"ui_framework": "string",
		"code_paths": "string_list",
	},
}


@dataclass(frozen=True)
class DocumentParseWarning:
	"""Non-fatal issue encountered while parsing one document."""

	code: str
	message: str
	line_number: int | None = None
	details: Mapping[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable warning."""

		return {
			"level": "warning",
			"code": self.code,
			"message": self.message,
			"line_number": self.line_number,
			"details": dict(self.details),
		}


@dataclass(frozen=True)
class ParsedFrontMatter:
	"""Normalized front matter metadata."""

	raw: Mapping[str, object]
	known_fields: Mapping[str, object]
	extension_fields: Mapping[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable front matter object."""

		return {
			"raw": dict(self.raw),
			"known_fields": dict(self.known_fields),
			"extension_fields": dict(self.extension_fields),
		}


@dataclass(frozen=True)
class ParsedHeading:
	"""One heading in the parsed heading tree."""

	level: int
	title: str
	path: tuple[str, ...]
	anchor: str
	start_line: int

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable heading object."""

		return {
			"level": self.level,
			"title": self.title,
			"path": list(self.path),
			"anchor": self.anchor,
			"start_line": self.start_line,
		}


@dataclass(frozen=True)
class ParsedChunk:
	"""One document chunk ready for downstream indexing."""

	ordinal: int
	chunk_type: str
	text: str
	start_line: int
	end_line: int
	heading_path: tuple[str, ...] = ()
	anchor: str | None = None
	metadata: Mapping[str, object] = field(default_factory=dict)

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable chunk object."""

		return {
			"ordinal": self.ordinal,
			"chunk_type": self.chunk_type,
			"text": self.text,
			"start_line": self.start_line,
			"end_line": self.end_line,
			"heading_path": list(self.heading_path),
			"anchor": self.anchor,
			"metadata": dict(self.metadata),
		}


@dataclass(frozen=True)
class ParsedDocument:
	"""Parsed representation of one source document."""

	schema_version: str
	source_path: str
	format: str
	category: str | None
	title: str | None
	front_matter: ParsedFrontMatter | None
	headings: tuple[ParsedHeading, ...]
	chunks: tuple[ParsedChunk, ...]
	warnings: tuple[DocumentParseWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable document payload."""

		return {
			"schema_version": self.schema_version,
			"source_path": self.source_path,
			"format": self.format,
			"category": self.category,
			"title": self.title,
			"front_matter": None if self.front_matter is None else self.front_matter.to_dict(),
			"headings": [heading.to_dict() for heading in self.headings],
			"chunks": [chunk.to_dict() for chunk in self.chunks],
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass(frozen=True)
class _MarkdownHeadingMarker:
	level: int
	title: str
	line_index: int
	start_line: int


@dataclass(frozen=True)
class _HtmlBlock:
	kind: str
	text: str
	start_line: int
	end_line: int
	level: int | None = None


@dataclass
class _HtmlCapture:
	tag: str
	kind: str
	start_line: int
	level: int | None = None
	parts: list[str] = field(default_factory=list)


class _StructuredHTMLParser(HTMLParser):
	"""Small HTML parser that extracts headings, body text, and table rows."""

	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.blocks: list[_HtmlBlock] = []
		self._captures: list[_HtmlCapture] = []
		self._fallback_fragments: list[tuple[int, str]] = []
		self._current_row_cells: list[str] | None = None
		self._current_row_start: int | None = None
		self._current_cell_parts: list[str] | None = None
		self.title_text: str | None = None

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		line_number = self.getpos()[0]
		if tag == "title":
			self._captures.append(_HtmlCapture(tag=tag, kind="title", start_line=line_number))
			return
		if tag in {"p", "li"}:
			self._captures.append(_HtmlCapture(tag=tag, kind="body", start_line=line_number))
			return
		if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
			self._captures.append(
				_HtmlCapture(tag=tag, kind="heading", start_line=line_number, level=int(tag[1]))
			)
			return
		if tag == "tr":
			self._current_row_cells = []
			self._current_row_start = line_number
			return
		if tag in {"th", "td"} and self._current_row_cells is not None:
			self._current_cell_parts = []

	def handle_endtag(self, tag: str) -> None:
		line_number = self.getpos()[0]
		if tag in {"title", "p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
			capture = self._pop_capture(tag)
			if capture is None:
				return
			text = normalize_inline_text("".join(capture.parts))
			if not text:
				return
			if capture.kind == "title":
				self.title_text = text
				return
			self.blocks.append(
				_HtmlBlock(
					kind=capture.kind,
					text=text,
					start_line=capture.start_line,
					end_line=line_number,
					level=capture.level,
				)
			)
			return
		if tag in {"th", "td"} and self._current_cell_parts is not None:
			text = normalize_inline_text("".join(self._current_cell_parts))
			if text:
				assert self._current_row_cells is not None
				self._current_row_cells.append(text)
			self._current_cell_parts = None
			return
		if tag == "tr" and self._current_row_cells is not None and self._current_row_start is not None:
			if self._current_row_cells:
				self.blocks.append(
					_HtmlBlock(
						kind="table_row",
						text=" | ".join(self._current_row_cells),
						start_line=self._current_row_start,
						end_line=line_number,
					)
				)
			self._current_row_cells = None
			self._current_row_start = None

	def handle_data(self, data: str) -> None:
		line_number = self.getpos()[0]
		if self._current_cell_parts is not None:
			self._current_cell_parts.append(data)
			return
		if self._captures:
			self._captures[-1].parts.append(data)
			return
		stripped = normalize_inline_text(data)
		if stripped:
			self._fallback_fragments.append((line_number, stripped))

	def fallback_body_block(self) -> _HtmlBlock | None:
		"""Return one fallback body block when HTML has loose text only."""

		if not self._fallback_fragments:
			return None
		start_line = self._fallback_fragments[0][0]
		end_line = self._fallback_fragments[-1][0]
		text = "\n".join(fragment for _, fragment in self._fallback_fragments)
		return _HtmlBlock(kind="body", text=text, start_line=start_line, end_line=end_line)

	def _pop_capture(self, tag: str) -> _HtmlCapture | None:
		for index in range(len(self._captures) - 1, -1, -1):
			capture = self._captures[index]
			if capture.tag == tag:
				del self._captures[index]
				return capture
		return None


def parse_source_document(
	path: str | Path,
	*,
	text: str | None = None,
	category: str | None = None,
) -> ParsedDocument:
	"""Parse one source document by suffix and return a stable document contract."""

	source_path = Path(path)
	raw_text = text if text is not None else source_path.read_text(encoding="utf-8")
	normalized_category = normalize_doc_category(category or infer_source_doc_category(source_path))
	suffix = source_path.suffix.lower()
	if suffix in _MARKDOWN_SUFFIXES:
		return parse_markdown_document(source_path, raw_text, category=normalized_category)
	if suffix in _HTML_SUFFIXES:
		return parse_html_document(source_path, raw_text, category=normalized_category)
	raise ValueError(f"Unsupported document format for source parser: {source_path.suffix}")


def parse_markdown_document(
	path: str | Path,
	text: str,
	*,
	category: str | None = None,
) -> ParsedDocument:
	"""Parse Markdown front matter, heading tree, and line-addressable chunks."""

	warnings: list[DocumentParseWarning] = []
	source_path = Path(path)
	normalized_category = normalize_doc_category(category or infer_source_doc_category(source_path))
	front_matter, body_text, body_start_line = split_front_matter(text, warnings)
	parsed_front_matter = normalize_front_matter(front_matter, normalized_category, warnings)
	numbered_lines = list(enumerate(body_text.splitlines(), start=body_start_line))
	heading_markers = scan_markdown_headings(numbered_lines)
	headings = build_heading_tree(heading_markers)
	chunks = build_markdown_chunks(numbered_lines, heading_markers, headings)
	title = resolve_document_title(source_path, headings, parsed_front_matter)
	return ParsedDocument(
		schema_version=DOC_PARSER_SCHEMA_VERSION,
		source_path=str(source_path),
		format="markdown",
		category=normalized_category,
		title=title,
		front_matter=parsed_front_matter,
		headings=headings,
		chunks=chunks,
		warnings=tuple(warnings),
	)


def parse_html_document(
	path: str | Path,
	text: str,
	*,
	category: str | None = None,
) -> ParsedDocument:
	"""Parse HTML title, heading tree, body blocks, and table rows."""

	warnings: list[DocumentParseWarning] = []
	source_path = Path(path)
	normalized_category = normalize_doc_category(category or infer_source_doc_category(source_path))
	parser = _StructuredHTMLParser()
	parser.feed(text)
	parser.close()
	blocks = list(parser.blocks)
	if not blocks:
		fallback = parser.fallback_body_block()
		if fallback is not None:
			blocks.append(fallback)
	headings = build_html_headings(blocks)
	chunks = build_html_chunks(blocks, headings)
	title = parser.title_text or resolve_document_title(source_path, headings, None)
	return ParsedDocument(
		schema_version=DOC_PARSER_SCHEMA_VERSION,
		source_path=str(source_path),
		format="html",
		category=normalized_category,
		title=title,
		front_matter=None,
		headings=headings,
		chunks=chunks,
		warnings=tuple(warnings),
	)


def split_front_matter(
	text: str,
	warnings: list[DocumentParseWarning],
) -> tuple[Mapping[str, object], str, int]:
	"""Split YAML front matter from the Markdown body when present."""

	lines = text.splitlines()
	if not lines or lines[0].strip() != "---":
		return {}, text, 1
	closing_index = None
	for index in range(1, len(lines)):
		if lines[index].strip() in {"---", "..."}:
			closing_index = index
			break
	if closing_index is None:
		warnings.append(
			DocumentParseWarning(
				code="doc.front_matter_unterminated",
				message="Front matter start marker was found without a matching closing marker.",
				line_number=1,
			)
		)
		return {}, text, 1

	front_matter_text = "\n".join(lines[1:closing_index])
	body_text = "\n".join(lines[closing_index + 1 :])
	if not front_matter_text.strip():
		return {}, body_text, closing_index + 2
	try:
		payload = yaml.safe_load(front_matter_text)
	except yaml.YAMLError as exc:
		warnings.append(
			DocumentParseWarning(
				code="doc.front_matter_invalid_yaml",
				message=f"Front matter YAML could not be parsed: {exc}",
				line_number=1,
			)
		)
		return {}, body_text, closing_index + 2
	if payload is None:
		return {}, body_text, closing_index + 2
	if not isinstance(payload, Mapping):
		warnings.append(
			DocumentParseWarning(
				code="doc.front_matter_not_mapping",
				message="Front matter must decode to a YAML mapping.",
				line_number=1,
			)
		)
		return {}, body_text, closing_index + 2
	normalized = {str(key): value for key, value in payload.items()}
	return normalized, body_text, closing_index + 2


def normalize_front_matter(
	payload: Mapping[str, object],
	category: str | None,
	warnings: list[DocumentParseWarning],
) -> ParsedFrontMatter | None:
	"""Normalize category-aware front matter fields for downstream indexing."""

	if not payload:
		return None
	field_kinds = dict(_COMMON_FRONT_MATTER_FIELDS)
	if category is not None:
		field_kinds.update(_CATEGORY_FRONT_MATTER_FIELDS.get(category, {}))

	known_fields: dict[str, object] = {}
	extension_fields: dict[str, object] = {}
	for key, value in payload.items():
		kind = field_kinds.get(key)
		if kind is None:
			extension_fields[key] = value
			continue
		normalized = normalize_front_matter_value(key, value, kind, warnings)
		if normalized is None and kind == "string":
			continue
		if normalized == [] and kind == "string_list" and value is None:
			continue
		known_fields[key] = normalized
	return ParsedFrontMatter(raw=dict(payload), known_fields=known_fields, extension_fields=extension_fields)


def normalize_front_matter_value(
	key: str,
	value: object,
	kind: str,
	warnings: list[DocumentParseWarning],
) -> object | None:
	"""Normalize one front matter field to a stable primitive shape."""

	if kind == "string":
		if value is None:
			return None
		if isinstance(value, (str, int, float, bool)):
			return str(value)
		warnings.append(
			DocumentParseWarning(
				code="doc.front_matter_invalid_type",
				message=f"Front matter field {key!r} must be a string-like value.",
				details={"field": key, "expected": "string"},
			)
		)
		return None

	if kind == "string_list":
		if value is None:
			return []
		if isinstance(value, str):
			return [value]
		if isinstance(value, (list, tuple)):
			normalized: list[str] = []
			for item in value:
				if isinstance(item, (str, int, float, bool)):
					normalized.append(str(item))
					continue
				warnings.append(
					DocumentParseWarning(
						code="doc.front_matter_invalid_type",
						message=f"Front matter field {key!r} must be a list of string-like values.",
						details={"field": key, "expected": "string_list"},
					)
				)
				return []
			return normalized
		warnings.append(
			DocumentParseWarning(
				code="doc.front_matter_invalid_type",
				message=f"Front matter field {key!r} must be a list of string-like values.",
				details={"field": key, "expected": "string_list"},
			)
		)
		return []

	raise ValueError(f"Unsupported front matter field kind: {kind}")


def scan_markdown_headings(numbered_lines: list[tuple[int, str]]) -> tuple[_MarkdownHeadingMarker, ...]:
	"""Return Markdown heading markers while ignoring fenced code blocks."""

	markers: list[_MarkdownHeadingMarker] = []
	in_fence = False
	active_fence: str | None = None
	index = 0
	while index < len(numbered_lines):
		line_number, raw_line = numbered_lines[index]
		fence_match = _FENCE_RE.match(raw_line)
		if fence_match:
			fence = fence_match.group(1)[:3]
			if not in_fence:
				in_fence = True
				active_fence = fence
			elif active_fence == fence:
				in_fence = False
				active_fence = None
			index += 1
			continue
		if in_fence:
			index += 1
			continue

		atx_match = _ATX_HEADING_RE.match(raw_line)
		if atx_match:
			markers.append(
				_MarkdownHeadingMarker(
					level=len(atx_match.group(1)),
					title=normalize_inline_text(atx_match.group(2)),
					line_index=index,
					start_line=line_number,
				)
			)
			index += 1
			continue

		if index + 1 < len(numbered_lines):
			next_line = numbered_lines[index + 1][1]
			if raw_line.strip():
				if _SETEXT_H1_RE.match(next_line):
					markers.append(
						_MarkdownHeadingMarker(
							level=1,
							title=normalize_inline_text(raw_line),
							line_index=index,
							start_line=line_number,
						)
					)
					index += 2
					continue
				if _SETEXT_H2_RE.match(next_line):
					markers.append(
						_MarkdownHeadingMarker(
							level=2,
							title=normalize_inline_text(raw_line),
							line_index=index,
							start_line=line_number,
						)
					)
					index += 2
					continue
		index += 1
	return tuple(markers)


def build_heading_tree(markers: tuple[_MarkdownHeadingMarker, ...]) -> tuple[ParsedHeading, ...]:
	"""Convert raw Markdown heading markers into a nested heading path view."""

	headings: list[ParsedHeading] = []
	stack: list[tuple[int, str]] = []
	used_anchors: dict[str, int] = {}
	for marker in markers:
		while stack and stack[-1][0] >= marker.level:
			stack.pop()
		path = tuple(item[1] for item in stack) + (marker.title,)
		anchor = make_unique_anchor(marker.title, used_anchors)
		headings.append(
			ParsedHeading(
				level=marker.level,
				title=marker.title,
				path=path,
				anchor=anchor,
				start_line=marker.start_line,
			)
		)
		stack.append((marker.level, marker.title))
	return tuple(headings)


def build_markdown_chunks(
	numbered_lines: list[tuple[int, str]],
	markers: tuple[_MarkdownHeadingMarker, ...],
	headings: tuple[ParsedHeading, ...],
) -> tuple[ParsedChunk, ...]:
	"""Chunk Markdown by topologically ordered heading sections."""

	chunks: list[ParsedChunk] = []
	if not numbered_lines:
		return ()

	next_ordinal = 0
	if markers and markers[0].line_index > 0:
		lead_chunk = make_markdown_chunk(
			numbered_lines[0:markers[0].line_index],
			ordinal=next_ordinal,
			chunk_type="markdown.lead",
			heading=(),
			anchor=None,
		)
		if lead_chunk is not None:
			chunks.append(lead_chunk)
			next_ordinal += 1

	if not markers:
		only_chunk = make_markdown_chunk(
			numbered_lines,
			ordinal=next_ordinal,
			chunk_type="markdown.body",
			heading=(),
			anchor=None,
		)
		return () if only_chunk is None else (only_chunk,)

	for index, marker in enumerate(markers):
		end_index = markers[index + 1].line_index if index + 1 < len(markers) else len(numbered_lines)
		chunk = make_markdown_chunk(
			numbered_lines[marker.line_index:end_index],
			ordinal=next_ordinal,
			chunk_type="markdown.section",
			heading=headings[index].path,
			anchor=headings[index].anchor,
			metadata={"heading_level": headings[index].level},
		)
		if chunk is not None:
			chunks.append(chunk)
			next_ordinal += 1
	return tuple(chunks)


def make_markdown_chunk(
	lines: list[tuple[int, str]],
	*,
	ordinal: int,
	chunk_type: str,
	heading: tuple[str, ...],
	anchor: str | None,
	metadata: Mapping[str, object] | None = None,
) -> ParsedChunk | None:
	"""Create one Markdown chunk after trimming leading/trailing blank lines."""

	trimmed = trim_numbered_lines(lines)
	if not trimmed:
		return None
	start_line = trimmed[0][0]
	end_line = trimmed[-1][0]
	text = "\n".join(line for _, line in trimmed).strip()
	if not text:
		return None
	return ParsedChunk(
		ordinal=ordinal,
		chunk_type=chunk_type,
		text=text,
		start_line=start_line,
		end_line=end_line,
		heading_path=heading,
		anchor=anchor,
		metadata={} if metadata is None else dict(metadata),
	)


def build_html_headings(blocks: list[_HtmlBlock]) -> tuple[ParsedHeading, ...]:
	"""Build a heading tree from ordered HTML blocks."""

	headings: list[ParsedHeading] = []
	stack: list[tuple[int, str]] = []
	used_anchors: dict[str, int] = {}
	for block in blocks:
		if block.kind != "heading" or block.level is None:
			continue
		while stack and stack[-1][0] >= block.level:
			stack.pop()
		path = tuple(item[1] for item in stack) + (block.text,)
		anchor = make_unique_anchor(block.text, used_anchors)
		headings.append(
			ParsedHeading(
				level=block.level,
				title=block.text,
				path=path,
				anchor=anchor,
				start_line=block.start_line,
			)
		)
		stack.append((block.level, block.text))
	return tuple(headings)


def build_html_chunks(
	blocks: list[_HtmlBlock],
	headings: tuple[ParsedHeading, ...],
) -> tuple[ParsedChunk, ...]:
	"""Chunk HTML by ordered heading sections, preserving table rows in body text."""

	if not blocks:
		return ()

	heading_positions = [index for index, block in enumerate(blocks) if block.kind == "heading"]
	chunks: list[ParsedChunk] = []
	next_ordinal = 0
	if heading_positions and heading_positions[0] > 0:
		lead_chunk = make_html_chunk(
			blocks[0:heading_positions[0]],
			ordinal=next_ordinal,
			chunk_type="html.lead",
			heading=(),
			anchor=None,
		)
		if lead_chunk is not None:
			chunks.append(lead_chunk)
			next_ordinal += 1

	if not heading_positions:
		only_chunk = make_html_chunk(
			blocks,
			ordinal=next_ordinal,
			chunk_type="html.body",
			heading=(),
			anchor=None,
		)
		return () if only_chunk is None else (only_chunk,)

	for heading_index, block_index in enumerate(heading_positions):
		next_block_index = (
			heading_positions[heading_index + 1] if heading_index + 1 < len(heading_positions) else len(blocks)
		)
		chunk = make_html_chunk(
			blocks[block_index:next_block_index],
			ordinal=next_ordinal,
			chunk_type="html.section",
			heading=headings[heading_index].path,
			anchor=headings[heading_index].anchor,
			metadata={"heading_level": headings[heading_index].level},
		)
		if chunk is not None:
			chunks.append(chunk)
			next_ordinal += 1
	return tuple(chunks)


def make_html_chunk(
	blocks: list[_HtmlBlock],
	*,
	ordinal: int,
	chunk_type: str,
	heading: tuple[str, ...],
	anchor: str | None,
	metadata: Mapping[str, object] | None = None,
) -> ParsedChunk | None:
	"""Create one HTML chunk from ordered text and table blocks."""

	if not blocks:
		return None
	text = "\n".join(block.text for block in blocks if block.text).strip()
	if not text:
		return None
	return ParsedChunk(
		ordinal=ordinal,
		chunk_type=chunk_type,
		text=text,
		start_line=blocks[0].start_line,
		end_line=blocks[-1].end_line,
		heading_path=heading,
		anchor=anchor,
		metadata={} if metadata is None else dict(metadata),
	)


def trim_numbered_lines(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
	"""Trim blank leading and trailing lines while preserving line numbers."""

	start = 0
	end = len(lines)
	while start < end and not lines[start][1].strip():
		start += 1
	while end > start and not lines[end - 1][1].strip():
		end -= 1
	return lines[start:end]


def resolve_document_title(
	path: Path,
	headings: tuple[ParsedHeading, ...],
	front_matter: ParsedFrontMatter | None,
) -> str:
	"""Resolve document title using front matter first, then headings, then file stem."""

	if front_matter is not None:
		title = front_matter.known_fields.get("title")
		if isinstance(title, str) and title:
			return title
	if headings:
		return headings[0].title
	return path.stem


def infer_source_doc_category(path: Path) -> str | None:
	"""Infer source docs category from a knowledge-sources relative path when possible."""

	parts = path.as_posix().split("/")
	if "knowledge-sources" in parts:
		index = parts.index("knowledge-sources")
		if index + 1 < len(parts):
			return normalize_doc_category(parts[index + 1])
	if len(parts) >= 2:
		return normalize_doc_category(parts[-2])
	return None


def normalize_doc_category(category: str | None) -> str | None:
	"""Normalize category aliases to the connector/source-doc canonical form."""

	if category is None:
		return None
	return _CATEGORY_ALIASES.get(category.strip().lower(), category.strip().lower())


def normalize_inline_text(text: str) -> str:
	"""Collapse repeated inline whitespace while preserving readable content."""

	return re.sub(r"\s+", " ", text).strip()


def make_unique_anchor(title: str, seen: dict[str, int]) -> str:
	"""Return a stable Markdown-like anchor slug, uniquified within one document."""

	base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "section"
	count = seen.get(base, 0) + 1
	seen[base] = count
	if count == 1:
		return base
	return f"{base}-{count}"
