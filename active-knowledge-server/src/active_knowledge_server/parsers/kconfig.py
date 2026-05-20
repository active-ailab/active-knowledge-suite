"""Kconfig, Config.in, defconfig, and .config parser contracts."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

KCONFIG_PARSER_SCHEMA_VERSION: Final = "kconfig_parser.v1"

_CONFIG_SYMBOL_PATTERN: Final = r"(?:CONFIG_[A-Za-z0-9_]+|[A-Za-z][A-Za-z0-9_]+)"
_CONFIG_ASSIGNMENT_RE: Final = re.compile(rf"^({_CONFIG_SYMBOL_PATTERN})=(.*)$")
_CONFIG_NOT_SET_RE: Final = re.compile(rf"^#\s*({_CONFIG_SYMBOL_PATTERN})\s+is\s+not\s+set\s*$")
_CONFIG_STRING_VALUE_RE: Final = re.compile(r'^".*"$')
_CONFIG_INTEGER_RE: Final = re.compile(r"^-?[0-9]+$")

_KCONFIG_SYMBOL_START_RE: Final = re.compile(r"^(config|menuconfig)\s+([A-Za-z0-9_]+)\s*$")
_KCONFIG_TYPE_RE: Final = re.compile(
	r'^(bool|tristate|string|int|hex)(?:\s+"(?P<prompt>(?:[^"\\]|\\.)*)")?(?:\s+if\s+.+)?\s*$'
)
_KCONFIG_PROMPT_RE: Final = re.compile(r'^prompt\s+"(?P<prompt>(?:[^"\\]|\\.)*)"(?:\s+if\s+.+)?\s*$')
_KCONFIG_DEPENDS_RE: Final = re.compile(r"^depends\s+on\s+(.+)$")
_KCONFIG_SELECT_RE: Final = re.compile(r"^select\s+([A-Za-z0-9_]+)(?:\s+if\s+(.+))?$")
_KCONFIG_IF_RE: Final = re.compile(r"^if\s+(.+)$")
_TOP_LEVEL_KCONFIG_KEYWORDS: Final = {
	"choice",
	"comment",
	"config",
	"endchoice",
	"endif",
	"endmenu",
	"if",
	"mainmenu",
	"menu",
	"menuconfig",
	"orsource",
	"osource",
	"rsource",
	"source",
}

_APP_VALUE_SYMBOLS: Final = {"APP", "APP_NAME", "APPLICATION", "APPLICATION_NAME"}
_BOARD_VALUE_SYMBOLS: Final = {"BOARD", "BOARD_NAME", "BUILD_BOARD", "HMI_BUILD_BOARD"}
_BOARD_VARIANT_VALUE_SYMBOLS: Final = {"PRODUCT_CUSTOMIZE_DIR", "HMI_PRODUCT_CUSTOMIZE_DIR"}


@dataclass(frozen=True)
class KconfigParseWarning:
	"""Non-fatal issue encountered while parsing one config artifact."""

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
class ParsedMacroAssignment:
	"""One CONFIG_* assignment parsed from defconfig or .config."""

	macro_name: str
	symbol: str
	value: str
	raw_value: str
	value_type: str
	enabled: bool
	line_number: int
	source_kind: str

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable macro assignment."""

		return {
			"macro_name": self.macro_name,
			"symbol": self.symbol,
			"value": self.value,
			"raw_value": self.raw_value,
			"value_type": self.value_type,
			"enabled": self.enabled,
			"line_number": self.line_number,
			"source_kind": self.source_kind,
		}


@dataclass(frozen=True)
class ParsedProfileClues:
	"""App, board, and feature hints inferred from config macros."""

	app: str | None
	board: str | None
	features: tuple[str, ...]
	app_candidates: tuple[str, ...] = ()
	board_candidates: tuple[str, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable clue payload."""

		return {
			"app": self.app,
			"board": self.board,
			"features": list(self.features),
			"app_candidates": list(self.app_candidates),
			"board_candidates": list(self.board_candidates),
		}


@dataclass(frozen=True)
class ParsedConfigFile:
	"""Parsed representation of one defconfig or .config file."""

	schema_version: str
	source_path: str
	source_kind: str
	assignments: tuple[ParsedMacroAssignment, ...]
	clues: ParsedProfileClues
	macro_hash: str
	warnings: tuple[KconfigParseWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable config payload."""

		return {
			"schema_version": self.schema_version,
			"source_path": self.source_path,
			"source_kind": self.source_kind,
			"assignments": [assignment.to_dict() for assignment in self.assignments],
			"clues": self.clues.to_dict(),
			"macro_hash": self.macro_hash,
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass(frozen=True)
class ParsedKconfigSelect:
	"""One select edge declared by a Kconfig symbol."""

	target: str
	condition: str | None
	line_number: int

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable select payload."""

		return {
			"target": self.target,
			"condition": self.condition,
			"line_number": self.line_number,
		}


@dataclass(frozen=True)
class ParsedKconfigSymbol:
	"""One symbol definition parsed from Kconfig or Config.in."""

	name: str
	definition_kind: str
	value_type: str | None
	prompt: str | None
	depends_on: tuple[str, ...]
	selects: tuple[ParsedKconfigSelect, ...]
	start_line: int
	end_line: int

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable symbol payload."""

		return {
			"name": self.name,
			"definition_kind": self.definition_kind,
			"value_type": self.value_type,
			"prompt": self.prompt,
			"depends_on": list(self.depends_on),
			"selects": [select.to_dict() for select in self.selects],
			"start_line": self.start_line,
			"end_line": self.end_line,
		}


@dataclass(frozen=True)
class ParsedKconfigFile:
	"""Parsed representation of one Kconfig or Config.in file."""

	schema_version: str
	source_path: str
	symbols: tuple[ParsedKconfigSymbol, ...]
	warnings: tuple[KconfigParseWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable Kconfig payload."""

		return {
			"schema_version": self.schema_version,
			"source_path": self.source_path,
			"symbols": [symbol.to_dict() for symbol in self.symbols],
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass(frozen=True)
class ParsedProfileConfig:
	"""Resolved profile summary built from defconfig and .config inputs."""

	schema_version: str
	defconfig: ParsedConfigFile | None
	dotconfig: ParsedConfigFile | None
	merged_assignments: tuple[ParsedMacroAssignment, ...]
	clues: ParsedProfileClues
	macro_summary_hash: str
	warnings: tuple[KconfigParseWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable profile payload."""

		return {
			"schema_version": self.schema_version,
			"defconfig": None if self.defconfig is None else self.defconfig.to_dict(),
			"dotconfig": None if self.dotconfig is None else self.dotconfig.to_dict(),
			"merged_assignments": [assignment.to_dict() for assignment in self.merged_assignments],
			"clues": self.clues.to_dict(),
			"macro_summary_hash": self.macro_summary_hash,
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass
class _KconfigSymbolBuilder:
	name: str
	definition_kind: str
	start_line: int
	value_type: str | None = None
	prompt: str | None = None
	depends_on: list[str] = field(default_factory=list)
	selects: list[ParsedKconfigSelect] = field(default_factory=list)
	end_line: int | None = None

	def build(self, *, end_line: int) -> ParsedKconfigSymbol:
		depends_on = tuple(_unique_strings(self.depends_on))
		selects = tuple(self.selects)
		return ParsedKconfigSymbol(
			name=self.name,
			definition_kind=self.definition_kind,
			value_type=self.value_type,
			prompt=self.prompt,
			depends_on=depends_on,
			selects=selects,
			start_line=self.start_line,
			end_line=end_line,
		)


def parse_defconfig(source_path: Path, text: str) -> ParsedConfigFile:
	"""Parse one defconfig file into macro assignments and profile clues."""

	return _parse_config(source_path, text, source_kind="defconfig")


def parse_dotconfig(source_path: Path, text: str) -> ParsedConfigFile:
	"""Parse one .config file into macro assignments and profile clues."""

	return _parse_config(source_path, text, source_kind="dotconfig")


def parse_kconfig(source_path: Path, text: str) -> ParsedKconfigFile:
	"""Parse one Kconfig or Config.in file into symbol definitions."""

	warnings: list[KconfigParseWarning] = []
	symbols: list[ParsedKconfigSymbol] = []
	if_stack: list[str] = []
	current: _KconfigSymbolBuilder | None = None
	last_line = 0

	for line_number, raw_line in _iter_logical_lines(text):
		last_line = line_number
		stripped = raw_line.strip()
		if not stripped or stripped.startswith("#"):
			continue

		symbol_match = _KCONFIG_SYMBOL_START_RE.match(stripped)
		if symbol_match is not None:
			if current is not None:
				symbols.append(current.build(end_line=_symbol_end_line(current, fallback_line=line_number - 1)))
			current = _KconfigSymbolBuilder(
				name=symbol_match.group(2),
				definition_kind=symbol_match.group(1),
				start_line=line_number,
				depends_on=list(if_stack),
			)
			continue

		if_match = _KCONFIG_IF_RE.match(stripped)
		if if_match is not None:
			if current is not None:
				symbols.append(current.build(end_line=_symbol_end_line(current, fallback_line=line_number - 1)))
				current = None
			if_stack.append(if_match.group(1).strip())
			continue

		if stripped == "endif":
			if current is not None:
				symbols.append(current.build(end_line=_symbol_end_line(current, fallback_line=line_number - 1)))
				current = None
			if if_stack:
				if_stack.pop()
			else:
				warnings.append(
					KconfigParseWarning(
						code="kconfig.unmatched_endif",
						message="Encountered endif without a matching if block.",
						line_number=line_number,
					)
				)
			continue

		if current is None:
			continue

		if _looks_like_top_level_directive(stripped):
			symbols.append(current.build(end_line=_symbol_end_line(current, fallback_line=line_number - 1)))
			current = None
			continue

		type_match = _KCONFIG_TYPE_RE.match(stripped)
		if type_match is not None:
			current.value_type = type_match.group(1)
			prompt = type_match.group("prompt")
			if prompt:
				current.prompt = _unescape_kconfig_string(prompt)
			current.end_line = line_number
			continue

		prompt_match = _KCONFIG_PROMPT_RE.match(stripped)
		if prompt_match is not None:
			current.prompt = _unescape_kconfig_string(prompt_match.group("prompt"))
			current.end_line = line_number
			continue

		depends_match = _KCONFIG_DEPENDS_RE.match(stripped)
		if depends_match is not None:
			current.depends_on.append(depends_match.group(1).strip())
			current.end_line = line_number
			continue

		select_match = _KCONFIG_SELECT_RE.match(stripped)
		if select_match is not None:
			current.selects.append(
				ParsedKconfigSelect(
					target=select_match.group(1),
					condition=None if select_match.group(2) is None else select_match.group(2).strip(),
					line_number=line_number,
				)
			)
			current.end_line = line_number
			continue

		current.end_line = line_number

	if current is not None:
		symbols.append(current.build(end_line=_symbol_end_line(current, fallback_line=last_line)))

	if if_stack:
		warnings.append(
			KconfigParseWarning(
				code="kconfig.unclosed_if",
				message="One or more if blocks were not closed by endif.",
				details={"conditions": list(if_stack)},
			)
		)

	return ParsedKconfigFile(
		schema_version=KCONFIG_PARSER_SCHEMA_VERSION,
		source_path=source_path.as_posix(),
		symbols=tuple(symbols),
		warnings=tuple(warnings),
	)


def parse_profile_config(
	*,
	defconfig_path: Path | None = None,
	defconfig_text: str | None = None,
	dotconfig_path: Path | None = None,
	dotconfig_text: str | None = None,
) -> ParsedProfileConfig:
	"""Merge parsed defconfig and .config facts into one stable profile summary."""

	if defconfig_text is None and dotconfig_text is None:
		raise ValueError("parse_profile_config() requires defconfig_text or dotconfig_text")

	defconfig = (
		None
		if defconfig_text is None
		else parse_defconfig(defconfig_path or Path("defconfig"), defconfig_text)
	)
	dotconfig = (
		None
		if dotconfig_text is None
		else parse_dotconfig(dotconfig_path or Path(".config"), dotconfig_text)
	)
	merged_assignments = merge_macro_assignments(defconfig, dotconfig)
	clues = _merge_profile_clues(
		() if defconfig is None else (defconfig.clues,),
		() if dotconfig is None else (dotconfig.clues,),
	)
	macro_summary_hash = compute_profile_macro_summary_hash(merged_assignments, clues)
	warnings = tuple(
		warning
		for parsed in (defconfig, dotconfig)
		if parsed is not None
		for warning in parsed.warnings
	)
	return ParsedProfileConfig(
		schema_version=KCONFIG_PARSER_SCHEMA_VERSION,
		defconfig=defconfig,
		dotconfig=dotconfig,
		merged_assignments=merged_assignments,
		clues=clues,
		macro_summary_hash=macro_summary_hash,
		warnings=warnings,
	)


def merge_macro_assignments(*parsed_configs: ParsedConfigFile | None) -> tuple[ParsedMacroAssignment, ...]:
	"""Merge macro assignments with later parsed configs overriding earlier ones."""

	merged: dict[str, ParsedMacroAssignment] = {}
	for parsed in parsed_configs:
		if parsed is None:
			continue
		for assignment in parsed.assignments:
			merged[assignment.macro_name] = assignment
	return tuple(sorted(merged.values(), key=lambda assignment: assignment.macro_name))


def compute_profile_macro_summary_hash(
	assignments: Sequence[ParsedMacroAssignment],
	clues: ParsedProfileClues,
) -> str:
	"""Compute a deterministic hash from resolved profile macros and inferred clues."""

	payload = {
		"schema_version": KCONFIG_PARSER_SCHEMA_VERSION,
		"app": clues.app,
		"board": clues.board,
		"features": list(clues.features),
		"assignments": [
			{
				"macro_name": assignment.macro_name,
				"value": assignment.value,
				"value_type": assignment.value_type,
				"enabled": assignment.enabled,
			}
			for assignment in sorted(assignments, key=lambda item: item.macro_name)
		],
	}
	encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
	return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _parse_config(source_path: Path, text: str, *, source_kind: str) -> ParsedConfigFile:
	assignments: list[ParsedMacroAssignment] = []
	warnings: list[KconfigParseWarning] = []

	for line_number, raw_line in enumerate(text.splitlines(), start=1):
		stripped = raw_line.strip()
		if not stripped:
			continue

		not_set_match = _CONFIG_NOT_SET_RE.match(stripped)
		if not_set_match is not None:
			macro_name = not_set_match.group(1)
			assignments.append(
				ParsedMacroAssignment(
					macro_name=macro_name,
					symbol=_symbol_from_macro_name(macro_name),
					value="n",
					raw_value="n",
					value_type="bool",
					enabled=False,
					line_number=line_number,
					source_kind=source_kind,
				)
			)
			continue

		assignment_match = _CONFIG_ASSIGNMENT_RE.match(stripped)
		if assignment_match is None:
			if not stripped.startswith("#"):
				warnings.append(
					KconfigParseWarning(
						code="config.unparsed_line",
						message="Skipping config line that does not match supported defconfig/.config assignment syntax.",
						line_number=line_number,
						details={"source_kind": source_kind, "line": stripped},
					)
				)
			continue

		macro_name = assignment_match.group(1)
		raw_value = assignment_match.group(2).strip()
		value, value_type, enabled = _normalize_config_value(raw_value)
		assignments.append(
			ParsedMacroAssignment(
				macro_name=macro_name,
				symbol=_symbol_from_macro_name(macro_name),
				value=value,
				raw_value=raw_value,
				value_type=value_type,
				enabled=enabled,
				line_number=line_number,
				source_kind=source_kind,
			)
		)

	ordered_assignments = tuple(sorted(assignments, key=lambda assignment: assignment.macro_name))
	clues = _extract_profile_clues(ordered_assignments, source_path=source_path)
	macro_hash = _compute_config_hash(ordered_assignments)
	return ParsedConfigFile(
		schema_version=KCONFIG_PARSER_SCHEMA_VERSION,
		source_path=source_path.as_posix(),
		source_kind=source_kind,
		assignments=ordered_assignments,
		clues=clues,
		macro_hash=macro_hash,
		warnings=tuple(warnings),
	)


def _normalize_config_value(raw_value: str) -> tuple[str, str, bool]:
	value = raw_value.strip()
	if value in {"y", "n"}:
		return value, "bool", value == "y"
	if value == "m":
		return value, "tristate", True
	if _CONFIG_STRING_VALUE_RE.match(value):
		return _decode_config_string(value), "string", True
	if value.lower().startswith("0x"):
		return value.lower(), "hex", value != "0x0"
	if _CONFIG_INTEGER_RE.match(value):
		return value, "int", value != "0"
	return value, "literal", bool(value)


def _decode_config_string(raw_value: str) -> str:
	try:
		decoded = ast.literal_eval(raw_value)
	except (SyntaxError, ValueError):
		return raw_value[1:-1]
	if isinstance(decoded, str):
		return decoded
	return raw_value[1:-1]


def _extract_profile_clues(
	assignments: Sequence[ParsedMacroAssignment],
	*,
	source_path: Path,
) -> ParsedProfileClues:
	app_candidates: list[str] = []
	board_candidates: list[str] = []
	feature_candidates: list[str] = []
	board_base_candidates: list[str] = []
	board_variant_candidates: list[str] = []
	path_hint = _infer_path_clue(source_path)
	if path_hint["app"] is not None:
		app_candidates.append(path_hint["app"])
	if path_hint["board"] is not None:
		board_candidates.append(path_hint["board"])

	for assignment in assignments:
		symbol = assignment.symbol
		if symbol in _APP_VALUE_SYMBOLS and assignment.value_type in {"string", "literal"}:
			candidate = _normalize_clue_value(assignment.value)
			if candidate:
				app_candidates.append(candidate)
		if symbol in _BOARD_VALUE_SYMBOLS and assignment.value_type in {"string", "literal"}:
			candidate = _normalize_clue_value(assignment.value)
			if candidate:
				board_candidates.append(candidate)
				board_base_candidates.append(candidate)
		if symbol in _BOARD_VARIANT_VALUE_SYMBOLS and assignment.value_type in {"string", "literal"}:
			candidate = _normalize_clue_value(assignment.value)
			if candidate:
				board_variant_candidates.append(candidate)

		if not assignment.enabled:
			continue

		if symbol.startswith("APP_"):
			candidate = _normalize_clue_value(symbol.removeprefix("APP_"))
			if candidate:
				app_candidates.append(candidate)
		elif symbol.startswith("APPLICATION_"):
			candidate = _normalize_clue_value(symbol.removeprefix("APPLICATION_"))
			if candidate:
				app_candidates.append(candidate)

		if symbol.startswith("BOARD_") and assignment.value_type in {"bool", "tristate"}:
			candidate = _normalize_clue_value(symbol.removeprefix("BOARD_"))
			if candidate:
				board_candidates.append(candidate)
		elif symbol.startswith("HMI_BOARD_") and assignment.value_type in {"bool", "tristate"}:
			candidate = _normalize_clue_value(symbol.removeprefix("HMI_BOARD_"))
			if candidate:
				board_candidates.append(candidate)

		if symbol.startswith("FEATURE_"):
			candidate = _normalize_clue_value(symbol.removeprefix("FEATURE_"))
			if candidate:
				feature_candidates.append(candidate)
		elif "_FEATURE_" in symbol:
			candidate = _normalize_clue_value(symbol.split("_FEATURE_", maxsplit=1)[1])
			if candidate:
				feature_candidates.append(candidate)

	for board_base in board_base_candidates:
		for board_variant in board_variant_candidates:
			composed = _normalize_clue_value(f"{board_base}_{board_variant}")
			if composed:
				board_candidates.append(composed)

	unique_apps = tuple(_unique_strings(app_candidates))
	unique_boards = _filter_board_candidates(tuple(_unique_strings(board_candidates)), preferred_hint=path_hint["board"])
	unique_features = tuple(_unique_strings(feature_candidates))
	return ParsedProfileClues(
		app=unique_apps[0] if unique_apps else None,
		board=_select_preferred_board(unique_boards, preferred_hint=path_hint["board"]),
		features=unique_features,
		app_candidates=unique_apps,
		board_candidates=unique_boards,
	)


def _merge_profile_clues(
	defconfig_clues: Sequence[ParsedProfileClues],
	dotconfig_clues: Sequence[ParsedProfileClues],
) -> ParsedProfileClues:
	app_candidates = [
		candidate
		for clue in (*dotconfig_clues, *defconfig_clues)
		for candidate in clue.app_candidates
	]
	board_candidates = [
		candidate
		for clue in (*dotconfig_clues, *defconfig_clues)
		for candidate in clue.board_candidates
	]
	features = [
		feature
		for clue in (*dotconfig_clues, *defconfig_clues)
		for feature in clue.features
	]
	unique_apps = tuple(_unique_strings(app_candidates))
	unique_boards = tuple(_unique_strings(board_candidates))
	unique_features = tuple(_unique_strings(features))
	return ParsedProfileClues(
		app=unique_apps[0] if unique_apps else None,
		board=unique_boards[0] if unique_boards else None,
		features=unique_features,
		app_candidates=unique_apps,
		board_candidates=unique_boards,
	)


def _compute_config_hash(assignments: Sequence[ParsedMacroAssignment]) -> str:
	payload = {
		"schema_version": KCONFIG_PARSER_SCHEMA_VERSION,
		"assignments": [
			{
				"macro_name": assignment.macro_name,
				"value": assignment.value,
				"value_type": assignment.value_type,
				"enabled": assignment.enabled,
			}
			for assignment in assignments
		],
	}
	encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
	return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _symbol_end_line(builder: _KconfigSymbolBuilder, *, fallback_line: int) -> int:
	if builder.end_line is not None:
		return builder.end_line
	return max(builder.start_line, fallback_line)


def _iter_logical_lines(text: str) -> tuple[tuple[int, str], ...]:
	logical_lines: list[tuple[int, str]] = []
	start_line: int | None = None
	buffer = ""
	for line_number, raw_line in enumerate(text.splitlines(), start=1):
		line = raw_line.rstrip()
		if start_line is None:
			start_line = line_number
			buffer = line
		else:
			buffer = f"{buffer} {line.lstrip()}"

		if line.endswith("\\"):
			buffer = buffer[:-1].rstrip()
			continue

		logical_lines.append((start_line, buffer))
		start_line = None
		buffer = ""

	if start_line is not None:
		logical_lines.append((start_line, buffer))

	return tuple(logical_lines)


def _looks_like_top_level_directive(line: str) -> bool:
	keyword = line.split(maxsplit=1)[0]
	return keyword in _TOP_LEVEL_KCONFIG_KEYWORDS


def _unescape_kconfig_string(value: str) -> str:
	return value.replace(r'\"', '"').replace(r"\\", "\\")


def _symbol_from_macro_name(macro_name: str) -> str:
	return macro_name.removeprefix("CONFIG_") if macro_name.startswith("CONFIG_") else macro_name


def _infer_path_clue(source_path: Path) -> dict[str, str | None]:
	name = source_path.name
	if not name.endswith("_defconfig"):
		return {"app": None, "board": None}
	stem = _normalize_clue_value(name[: -len("_defconfig")])
	if not stem:
		return {"app": None, "board": None}
	parts = {part.lower() for part in source_path.parts}
	if "apps" in parts or "app" in parts:
		return {"app": stem, "board": None}
	return {"app": None, "board": stem}


def _normalize_clue_value(value: str) -> str:
	normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower())
	return normalized.strip("_")


def _select_preferred_board(candidates: Sequence[str], *, preferred_hint: str | None = None) -> str | None:
	if not candidates:
		return None
	if preferred_hint is not None:
		for candidate in candidates:
			if candidate == preferred_hint:
				return candidate
	return max(candidates, key=lambda candidate: (candidate.count("_"), len(candidate), candidate))


def _filter_board_candidates(candidates: Sequence[str], *, preferred_hint: str | None) -> tuple[str, ...]:
	if preferred_hint is None:
		return tuple(candidates)
	filtered = tuple(candidate for candidate in candidates if _matches_board_hint(candidate, preferred_hint))
	return filtered or tuple(candidates)


def _matches_board_hint(candidate: str, preferred_hint: str) -> bool:
	return (
		candidate == preferred_hint
		or preferred_hint.startswith(f"{candidate}_")
		or candidate.startswith(f"{preferred_hint}_")
	)


def _unique_strings(values: Sequence[str]) -> list[str]:
	return list(dict.fromkeys(value for value in values if value))
