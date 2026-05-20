"""ctags parser boundary."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

C_FAMILY_PARSER_SCHEMA_VERSION: Final = "c_family_parser.v1"

CodeLanguage = Literal["c", "cpp", "c-header", "cpp-header", "unknown"]
CodeExtractor = Literal["ctags", "heuristic"]
CodeSymbolKind = Literal["function", "macro", "type"]

_LANGUAGE_BY_SUFFIX: Final[dict[str, CodeLanguage]] = {
	".c": "c",
	".cc": "cpp",
	".cpp": "cpp",
	".cxx": "cpp",
	".h": "c-header",
	".hh": "cpp-header",
	".hpp": "cpp-header",
	".hxx": "cpp-header",
}
_COMMENT_RE: Final = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
_INCLUDE_RE: Final = re.compile(r'^\s*#\s*include\s*(?P<target><[^>]+>|"[^"]+")')
_DEFINE_RE: Final = re.compile(r'^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)\b')
_COMPOSITE_TYPE_RE: Final = re.compile(
	r"(?ms)^\s*(?:typedef\s+)?(?P<kind>struct|enum|union|class)\s+"
	r"(?P<tag>[A-Za-z_]\w+)?\s*\{.*?\}\s*(?P<alias>[A-Za-z_]\w+)?\s*;"
)
_TYPEDEF_ALIAS_RE: Final = re.compile(
	r"(?m)^\s*typedef\s+(?!struct\b|enum\b|union\b|class\b)"
	r"[^;{]+?\b(?P<alias>[A-Za-z_]\w+)\s*;"
)
_FORWARD_TYPE_RE: Final = re.compile(
	r"(?m)^\s*(?P<kind>struct|enum|union|class)\s+(?P<name>[A-Za-z_]\w+)\s*;"
)
_IDENTIFIER_RE: Final = re.compile(r"([A-Za-z_]\w*)\s*$")
_CTAGS_SUPPORTED_KINDS: Final[frozenset[str]] = frozenset(
	{"class", "enum", "function", "macro", "prototype", "struct", "typedef", "union"}
)
_CONTROL_KEYWORDS: Final[frozenset[str]] = frozenset(
	{"case", "catch", "do", "else", "for", "if", "return", "sizeof", "switch", "while"}
)
_SYMBOL_CONFIDENCE: Final[dict[tuple[CodeExtractor, CodeSymbolKind, bool], float]] = {
	("ctags", "function", True): 0.72,
	("ctags", "function", False): 0.68,
	("ctags", "macro", True): 0.76,
	("ctags", "macro", False): 0.76,
	("ctags", "type", True): 0.70,
	("ctags", "type", False): 0.70,
	("heuristic", "function", True): 0.63,
	("heuristic", "function", False): 0.58,
	("heuristic", "macro", True): 0.82,
	("heuristic", "macro", False): 0.82,
	("heuristic", "type", True): 0.61,
	("heuristic", "type", False): 0.61,
}


@dataclass(frozen=True)
class CodeParseWarning:
	"""Non-fatal issue encountered while parsing one C-family file."""

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
class ParsedCodeInclude:
	"""One include directive parsed from a C-family file."""

	target: str
	is_system: bool
	line_number: int
	extractor: CodeExtractor
	confidence: float

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable include payload."""

		return {
			"target": self.target,
			"is_system": self.is_system,
			"line_number": self.line_number,
			"extractor": self.extractor,
			"confidence": self.confidence,
		}


@dataclass(frozen=True)
class ParsedCodeComment:
	"""One line or block comment extracted from a C-family file."""

	comment_kind: Literal["line", "block"]
	text: str
	start_line: int
	end_line: int
	extractor: CodeExtractor
	confidence: float

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable comment payload."""

		return {
			"comment_kind": self.comment_kind,
			"text": self.text,
			"start_line": self.start_line,
			"end_line": self.end_line,
			"extractor": self.extractor,
			"confidence": self.confidence,
		}


@dataclass(frozen=True)
class ParsedCodeSymbol:
	"""One function, macro, or type symbol parsed from a C-family file."""

	name: str
	symbol_kind: CodeSymbolKind
	start_line: int
	end_line: int
	extractor: CodeExtractor
	confidence: float
	is_definition: bool = True
	signature: str | None = None
	raw_kind: str | None = None

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable symbol payload."""

		return {
			"name": self.name,
			"symbol_kind": self.symbol_kind,
			"start_line": self.start_line,
			"end_line": self.end_line,
			"extractor": self.extractor,
			"confidence": self.confidence,
			"is_definition": self.is_definition,
			"signature": self.signature,
			"raw_kind": self.raw_kind,
		}


@dataclass(frozen=True)
class ParsedFileHeader:
	"""Leading file-header chunk containing comments, includes, and key macros."""

	text: str
	start_line: int
	end_line: int
	include_targets: tuple[str, ...]
	macro_names: tuple[str, ...]
	extractor: CodeExtractor
	confidence: float

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable file-header payload."""

		return {
			"text": self.text,
			"start_line": self.start_line,
			"end_line": self.end_line,
			"include_targets": list(self.include_targets),
			"macro_names": list(self.macro_names),
			"extractor": self.extractor,
			"confidence": self.confidence,
		}


@dataclass(frozen=True)
class ParsedCodeFile:
	"""Parsed representation of one C, C++, or header source file."""

	schema_version: str
	source_path: str
	language: CodeLanguage
	extractor_used: CodeExtractor
	compile_db_path: str | None
	symbols: tuple[ParsedCodeSymbol, ...]
	includes: tuple[ParsedCodeInclude, ...]
	comments: tuple[ParsedCodeComment, ...]
	file_header: ParsedFileHeader | None
	warnings: tuple[CodeParseWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable code-parser payload."""

		return {
			"schema_version": self.schema_version,
			"source_path": self.source_path,
			"language": self.language,
			"extractor_used": self.extractor_used,
			"compile_db_path": self.compile_db_path,
			"symbols": [symbol.to_dict() for symbol in self.symbols],
			"includes": [include.to_dict() for include in self.includes],
			"comments": [comment.to_dict() for comment in self.comments],
			"file_header": None if self.file_header is None else self.file_header.to_dict(),
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass(frozen=True)
class _Span:
	start_offset: int
	end_offset: int
	text: str


def parse_c_family_file(
	source_path: Path,
	text: str,
	*,
	compile_db_path: Path | None = None,
	prefer_ctags: bool = True,
) -> ParsedCodeFile:
	"""Parse one C-family file into basic symbol, include, comment, and header facts."""

	language = _detect_language(source_path)
	line_starts = _build_line_starts(text)
	comments = _extract_comments(text, line_starts)
	includes = _extract_includes(text)
	file_header = _extract_file_header(text)
	warnings: list[CodeParseWarning] = []
	if compile_db_path is None:
		warnings.append(
			CodeParseWarning(
				code="compile_db.missing",
				message="Missing compile DB; C/C++ structure is heuristic and cross-translation-unit relations stay degraded.",
				details={
					"source_path": source_path.as_posix(),
					"language": language,
				},
			)
		)

	ctags_symbols = _parse_symbols_with_ctags(source_path) if prefer_ctags else None
	if ctags_symbols:
		symbols = ctags_symbols
		extractor_used: CodeExtractor = "ctags"
	else:
		symbols = _parse_symbols_heuristically(text, line_starts)
		extractor_used = "heuristic"

	return ParsedCodeFile(
		schema_version=C_FAMILY_PARSER_SCHEMA_VERSION,
		source_path=source_path.as_posix(),
		language=language,
		extractor_used=extractor_used,
		compile_db_path=None if compile_db_path is None else compile_db_path.as_posix(),
		symbols=symbols,
		includes=includes,
		comments=comments,
		file_header=file_header,
		warnings=tuple(warnings),
	)


def _parse_symbols_with_ctags(source_path: Path) -> tuple[ParsedCodeSymbol, ...] | None:
	ctags_path = shutil.which("ctags")
	if ctags_path is None:
		return None

	result = subprocess.run(
		[
			ctags_path,
			"--output-format=json",
			"--fields=+nK",
			"--sort=no",
			"-f",
			"-",
			source_path.as_posix(),
		],
		capture_output=True,
		text=True,
		check=False,
	)
	if result.returncode != 0:
		return None

	symbols: list[ParsedCodeSymbol] = []
	seen: set[tuple[str, CodeSymbolKind, int, bool]] = set()
	for raw_line in result.stdout.splitlines():
		if not raw_line.strip():
			continue
		try:
			payload = json.loads(raw_line)
		except json.JSONDecodeError:
			return None
		if payload.get("_type") != "tag":
			continue
		raw_kind_obj = payload.get("kind")
		line_number_obj = payload.get("line")
		name_obj = payload.get("name")
		if not isinstance(raw_kind_obj, str) or not isinstance(line_number_obj, int) or not isinstance(name_obj, str):
			continue
		if raw_kind_obj not in _CTAGS_SUPPORTED_KINDS:
			continue
		symbol_kind, is_definition = _map_ctags_kind(raw_kind_obj)
		key = (name_obj, symbol_kind, line_number_obj, is_definition)
		if key in seen:
			continue
		seen.add(key)
		signature_obj = payload.get("signature")
		signature = signature_obj if isinstance(signature_obj, str) else None
		symbols.append(
			ParsedCodeSymbol(
				name=name_obj,
				symbol_kind=symbol_kind,
				start_line=line_number_obj,
				end_line=line_number_obj,
				extractor="ctags",
				confidence=_symbol_confidence("ctags", symbol_kind, is_definition=is_definition),
				is_definition=is_definition,
				signature=signature,
				raw_kind=raw_kind_obj,
			)
		)
	return tuple(sorted(symbols, key=lambda symbol: (symbol.start_line, symbol.symbol_kind, symbol.name)))


def _parse_symbols_heuristically(text: str, line_starts: Sequence[int]) -> tuple[ParsedCodeSymbol, ...]:
	masked_text = _mask_comments(text)
	symbols: list[ParsedCodeSymbol] = []
	seen: set[tuple[str, CodeSymbolKind, int, bool]] = set()

	for line_number, raw_line in enumerate(text.splitlines(), start=1):
		define_match = _DEFINE_RE.match(raw_line)
		if define_match is None:
			continue
		name = define_match.group("name")
		_add_symbol(
			symbols,
			seen,
			ParsedCodeSymbol(
				name=name,
				symbol_kind="macro",
				start_line=line_number,
				end_line=line_number,
				extractor="heuristic",
				confidence=_symbol_confidence("heuristic", "macro", is_definition=True),
				is_definition=True,
				signature=raw_line.strip(),
				raw_kind="define",
			),
		)

	for match in _COMPOSITE_TYPE_RE.finditer(masked_text):
		start_line = _offset_to_line(line_starts, match.start())
		end_line = _offset_to_line(line_starts, match.end() - 1)
		tag = match.group("tag")
		alias = match.group("alias")
		kind = match.group("kind")
		if tag:
			_add_symbol(
				symbols,
				seen,
				ParsedCodeSymbol(
					name=tag,
					symbol_kind="type",
					start_line=start_line,
					end_line=end_line,
					extractor="heuristic",
					confidence=_symbol_confidence("heuristic", "type", is_definition=True),
					is_definition=True,
					signature=_normalize_whitespace(masked_text[match.start() : match.end()]),
					raw_kind=kind,
				),
			)
		if alias:
			_add_symbol(
				symbols,
				seen,
				ParsedCodeSymbol(
					name=alias,
					symbol_kind="type",
					start_line=start_line,
					end_line=end_line,
					extractor="heuristic",
					confidence=_symbol_confidence("heuristic", "type", is_definition=True),
					is_definition=True,
					signature=_normalize_whitespace(masked_text[match.start() : match.end()]),
					raw_kind="typedef",
				),
			)

	for match in _TYPEDEF_ALIAS_RE.finditer(masked_text):
		start_line = _offset_to_line(line_starts, match.start())
		end_line = _offset_to_line(line_starts, match.end() - 1)
		_add_symbol(
			symbols,
			seen,
			ParsedCodeSymbol(
				name=match.group("alias"),
				symbol_kind="type",
				start_line=start_line,
				end_line=end_line,
				extractor="heuristic",
				confidence=_symbol_confidence("heuristic", "type", is_definition=True),
				is_definition=True,
				signature=_normalize_whitespace(masked_text[match.start() : match.end()]),
				raw_kind="typedef",
			),
		)

	for match in _FORWARD_TYPE_RE.finditer(masked_text):
		start_line = _offset_to_line(line_starts, match.start())
		end_line = _offset_to_line(line_starts, match.end() - 1)
		_add_symbol(
			symbols,
			seen,
			ParsedCodeSymbol(
				name=match.group("name"),
				symbol_kind="type",
				start_line=start_line,
				end_line=end_line,
				extractor="heuristic",
				confidence=_symbol_confidence("heuristic", "type", is_definition=False),
				is_definition=False,
				signature=_normalize_whitespace(masked_text[match.start() : match.end()]),
				raw_kind=match.group("kind"),
			),
		)

	for span in _iter_top_level_statements(masked_text):
		statement = span.text.strip()
		if not statement or statement.startswith("#"):
			continue
		if statement.startswith(("typedef ", "struct ", "enum ", "union ", "class ")):
			continue
		if "(" not in statement:
			continue

		open_paren = statement.find("(")
		left_side = statement[:open_paren].rstrip()
		name_match = _IDENTIFIER_RE.search(left_side)
		if name_match is None:
			continue
		name = name_match.group(1)
		if name in _CONTROL_KEYWORDS:
			continue
		prefix = left_side[: name_match.start(1)].strip()
		if not prefix or prefix.endswith(")") or prefix.startswith("#") or "=" in prefix:
			continue

		terminator = statement[-1]
		is_definition = terminator == "{"
		start_line = _offset_to_line(line_starts, span.start_offset)
		end_line = _offset_to_line(line_starts, span.end_offset - 1)
		_add_symbol(
			symbols,
			seen,
			ParsedCodeSymbol(
				name=name,
				symbol_kind="function",
				start_line=start_line,
				end_line=end_line,
				extractor="heuristic",
				confidence=_symbol_confidence("heuristic", "function", is_definition=is_definition),
				is_definition=is_definition,
				signature=_normalize_whitespace(statement),
				raw_kind="definition" if is_definition else "prototype",
			),
		)

	return tuple(sorted(symbols, key=lambda symbol: (symbol.start_line, symbol.symbol_kind, symbol.name)))


def _extract_comments(text: str, line_starts: Sequence[int]) -> tuple[ParsedCodeComment, ...]:
	comments: list[ParsedCodeComment] = []
	for match in _COMMENT_RE.finditer(text):
		comment_text = match.group(0).strip()
		if not comment_text:
			continue
		start_line = _offset_to_line(line_starts, match.start())
		end_line = _offset_to_line(line_starts, match.end() - 1)
		comments.append(
			ParsedCodeComment(
				comment_kind="block" if comment_text.startswith("/*") else "line",
				text=comment_text,
				start_line=start_line,
				end_line=end_line,
				extractor="heuristic",
				confidence=0.98,
			)
		)
	return tuple(comments)


def _extract_includes(text: str) -> tuple[ParsedCodeInclude, ...]:
	includes: list[ParsedCodeInclude] = []
	for line_number, raw_line in enumerate(text.splitlines(), start=1):
		include_match = _INCLUDE_RE.match(raw_line)
		if include_match is None:
			continue
		target = include_match.group("target")
		includes.append(
			ParsedCodeInclude(
				target=target[1:-1],
				is_system=target.startswith("<"),
				line_number=line_number,
				extractor="heuristic",
				confidence=0.99,
			)
		)
	return tuple(includes)


def _extract_file_header(text: str) -> ParsedFileHeader | None:
	lines = text.splitlines()
	start_line: int | None = None
	end_line = 0
	in_block_comment = False
	include_targets: list[str] = []
	macro_names: list[str] = []

	for line_number, raw_line in enumerate(lines, start=1):
		stripped = raw_line.strip()
		if start_line is None:
			if not stripped:
				continue
			start_line = line_number

		if in_block_comment:
			end_line = line_number
			if "*/" in stripped:
				in_block_comment = False
			continue

		if not stripped:
			end_line = line_number
			continue

		if stripped.startswith("/*"):
			end_line = line_number
			in_block_comment = "*/" not in stripped
			continue

		if stripped.startswith("//"):
			end_line = line_number
			continue

		include_match = _INCLUDE_RE.match(raw_line)
		if include_match is not None:
			include_targets.append(include_match.group("target")[1:-1])
			end_line = line_number
			continue

		define_match = _DEFINE_RE.match(raw_line)
		if define_match is not None:
			macro_names.append(define_match.group("name"))
			end_line = line_number
			continue

		if stripped.startswith("#"):
			end_line = line_number
			continue

		break

	if start_line is None or end_line < start_line:
		return None

	while end_line >= start_line and not lines[end_line - 1].strip():
		end_line -= 1
	chunk_text = "\n".join(lines[start_line - 1 : end_line]).strip()
	if not chunk_text:
		return None
	return ParsedFileHeader(
		text=chunk_text,
		start_line=start_line,
		end_line=end_line,
		include_targets=tuple(include_targets),
		macro_names=tuple(macro_names),
		extractor="heuristic",
		confidence=0.91,
	)


def _iter_top_level_statements(masked_text: str) -> tuple[_Span, ...]:
	spans: list[_Span] = []
	start_offset: int | None = None
	paren_depth = 0
	brace_depth = 0
	in_string: str | None = None
	escaped = False

	for index, character in enumerate(masked_text):
		if start_offset is None:
			if character.isspace():
				continue
			start_offset = index

		if in_string is not None:
			if escaped:
				escaped = False
			elif character == "\\":
				escaped = True
			elif character == in_string:
				in_string = None
			continue

		if character in {'"', "'"}:
			in_string = character
			continue

		if character == "(":
			paren_depth += 1
			continue
		if character == ")":
			paren_depth = max(paren_depth - 1, 0)
			continue
		if character == "{":
			if paren_depth == 0 and brace_depth == 0 and start_offset is not None:
				spans.append(_Span(start_offset=start_offset, end_offset=index + 1, text=masked_text[start_offset : index + 1]))
				start_offset = None
			brace_depth += 1
			continue
		if character == "}":
			brace_depth = max(brace_depth - 1, 0)
			if brace_depth == 0:
				start_offset = None
			continue
		if character == ";" and paren_depth == 0 and brace_depth == 0 and start_offset is not None:
			spans.append(_Span(start_offset=start_offset, end_offset=index + 1, text=masked_text[start_offset : index + 1]))
			start_offset = None

	return tuple(spans)


def _detect_language(source_path: Path) -> CodeLanguage:
	return _LANGUAGE_BY_SUFFIX.get(source_path.suffix, "unknown")


def _mask_comments(text: str) -> str:
	characters = list(text)
	for match in _COMMENT_RE.finditer(text):
		for index in range(match.start(), match.end()):
			if characters[index] != "\n":
				characters[index] = " "
	return "".join(characters)


def _build_line_starts(text: str) -> list[int]:
	line_starts = [0]
	for index, character in enumerate(text):
		if character == "\n":
			line_starts.append(index + 1)
	return line_starts


def _offset_to_line(line_starts: Sequence[int], offset: int) -> int:
	return bisect_right(line_starts, offset)


def _map_ctags_kind(raw_kind: str) -> tuple[CodeSymbolKind, bool]:
	if raw_kind in {"function", "prototype"}:
		return "function", raw_kind == "function"
	if raw_kind == "macro":
		return "macro", True
	return "type", True


def _symbol_confidence(extractor: CodeExtractor, symbol_kind: CodeSymbolKind, *, is_definition: bool) -> float:
	return _SYMBOL_CONFIDENCE[(extractor, symbol_kind, is_definition)]


def _normalize_whitespace(value: str) -> str:
	return re.sub(r"\s+", " ", value).strip()


def _add_symbol(
	symbols: list[ParsedCodeSymbol],
	seen: set[tuple[str, CodeSymbolKind, int, bool]],
	symbol: ParsedCodeSymbol,
) -> None:
	key = (symbol.name, symbol.symbol_kind, symbol.start_line, symbol.is_definition)
	if key in seen:
		return
	seen.add(key)
	symbols.append(symbol)
