"""Makefile and module.mk parser boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

MAKEFILE_PARSER_SCHEMA_VERSION: Final = "makefile_parser.v1"

_ASSIGNMENT_RE: Final = re.compile(
	r"^(?P<variable>[A-Za-z0-9_.$()/%+\-]+)\s*(?P<operator>\+=|:=|\?=|=)\s*(?P<value>.*)$"
)
_IF_DIRECTIVE_RE: Final = re.compile(r"^(?P<directive>ifeq|ifneq|ifdef|ifndef)\b(?P<expr>.*)$")
_ELSE_DIRECTIVE_RE: Final = re.compile(r"^else\b(?:\s+(?P<directive>ifeq|ifneq|ifdef|ifndef)\b(?P<expr>.*))?$")
_ENDIF_DIRECTIVE_RE: Final = re.compile(r"^endif\b")
_INCLUDE_DIRECTIVE_RE: Final = re.compile(r"^(?:-?include|sinclude)\s+(?P<value>.+)$")
_CONDITION_SYMBOL_RE: Final = re.compile(r"\b(?:CONFIG_[A-Za-z0-9_]+|[A-Z][A-Z0-9_]+)\b")
_CONDITIONAL_VARIABLE_RE: Final = re.compile(
	r"^(?P<base>[A-Za-z0-9_.$()/%+\-]+)-\$\((?P<macro>(?:CONFIG_[A-Za-z0-9_]+|[A-Z][A-Z0-9_]+))\)$"
)
_MODULE_MEMBER_VARIABLE_RE: Final = re.compile(
	r"^(?P<module>[A-Za-z0-9_.-]+)-(?P<suffix>y|m|obj|objs|src|srcs|source|sources)$",
	re.IGNORECASE,
)

_GENERIC_SOURCE_VARIABLES: Final = {
	"src",
	"srcs",
	"sources",
	"local_src",
	"local_srcs",
	"local_sources",
	"src_files",
	"source_files",
	"csrсs",
	"csrcs",
	"cppsrcs",
	"cxxsrcs",
	"asrcs",
	"asmsrcs",
	"objs",
	"object_files",
	"obj_files",
}
_GENERIC_COMPONENT_VARIABLES: Final = {
	"components",
	"local_components",
	"component_list",
	"sub_components",
}
_PRIMARY_MODULE_NAME_VARIABLES: Final = {
	"name",
	"module",
	"module_name",
	"mod_name",
	"local_module",
	"target_module",
	"target_name",
}
_LOGICAL_MODULE_NAME_VARIABLES: Final = {"module"}
_CONFIG_FILENAMES: Final = {"Config.in", "Kconfig"}
_FILE_SUFFIX_TO_KIND: Final = {
	".c": "c-source",
	".cc": "cpp-source",
	".cpp": "cpp-source",
	".cxx": "cpp-source",
	".s": "assembly-source",
	".S": "assembly-source",
	".asm": "assembly-source",
	".o": "object",
}
_DEFINITION_KIND_PRIORITY: Final = {
	"directory_name": 0,
	"variable_prefix": 1,
	"explicit_variable": 2,
}


@dataclass(frozen=True)
class MakefileParseWarning:
	"""Non-fatal issue encountered while parsing one Makefile artifact."""

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
class ParsedMakefileFile:
	"""One file-like token attached to a build module."""

	raw_path: str
	path: str
	artifact_kind: str
	origin_variable: str
	line_number: int
	condition_expr: str | None = None
	condition_macros: tuple[str, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable file payload."""

		return {
			"raw_path": self.raw_path,
			"path": self.path,
			"artifact_kind": self.artifact_kind,
			"origin_variable": self.origin_variable,
			"line_number": self.line_number,
			"condition_expr": self.condition_expr,
			"condition_macros": list(self.condition_macros),
		}


@dataclass(frozen=True)
class ParsedMakefileRelation:
	"""One module-centric relation hint emitted by the Makefile parser."""

	relation_type: str
	target_type: str
	target: str
	line_number: int | None = None
	condition_expr: str | None = None
	condition_macros: tuple[str, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable relation payload."""

		return {
			"relation_type": self.relation_type,
			"target_type": self.target_type,
			"target": self.target,
			"line_number": self.line_number,
			"condition_expr": self.condition_expr,
			"condition_macros": list(self.condition_macros),
		}


@dataclass(frozen=True)
class ParsedBuildModule:
	"""One build module inferred from a Makefile or module.mk file."""

	name: str
	module_path: str
	makefile_path: str
	logical_name: str | None
	definition_kind: str
	start_line: int
	end_line: int
	files: tuple[ParsedMakefileFile, ...]
	components: tuple[str, ...]
	condition_macros: tuple[str, ...]
	config_paths: tuple[str, ...] = ()
	relations: tuple[ParsedMakefileRelation, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable module payload."""

		return {
			"name": self.name,
			"module_path": self.module_path,
			"makefile_path": self.makefile_path,
			"logical_name": self.logical_name,
			"definition_kind": self.definition_kind,
			"start_line": self.start_line,
			"end_line": self.end_line,
			"files": [item.to_dict() for item in self.files],
			"components": list(self.components),
			"condition_macros": list(self.condition_macros),
			"config_paths": list(self.config_paths),
			"relations": [relation.to_dict() for relation in self.relations],
		}


@dataclass(frozen=True)
class ParsedMakefile:
	"""Parsed representation of one Makefile or module.mk artifact."""

	schema_version: str
	source_path: str
	modules: tuple[ParsedBuildModule, ...]
	warnings: tuple[MakefileParseWarning, ...] = ()

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable Makefile payload."""

		return {
			"schema_version": self.schema_version,
			"source_path": self.source_path,
			"modules": [module.to_dict() for module in self.modules],
			"warnings": [warning.to_dict() for warning in self.warnings],
		}


@dataclass(frozen=True)
class _LogicalLine:
	text: str
	start_line: int
	end_line: int


@dataclass(frozen=True)
class _ConditionFrame:
	expression: str
	macros: tuple[str, ...]
	start_line: int


@dataclass(frozen=True)
class _SourceBinding:
	module_name: str | None
	definition_kind: str
	origin_variable: str
	member_type: str = "file"
	condition_expression: str | None = None
	condition_macros: tuple[str, ...] = ()


@dataclass
class _ModuleBuilder:
	name: str
	module_path: str
	makefile_path: str
	definition_kind: str
	start_line: int
	logical_name: str | None = None
	end_line: int | None = None
	files: list[ParsedMakefileFile] = field(default_factory=list)
	config_paths: list[str] = field(default_factory=list)
	component_relations: list[ParsedMakefileRelation] = field(default_factory=list)
	guard_lines: dict[str, int] = field(default_factory=dict)
	_file_keys: set[tuple[str, str, int, str | None]] = field(default_factory=set)
	_component_keys: set[tuple[str, int, str | None]] = field(default_factory=set)

	def update_definition(self, *, definition_kind: str, line_number: int) -> None:
		"""Promote the module definition kind when a stronger signal appears."""

		current_priority = _DEFINITION_KIND_PRIORITY[self.definition_kind]
		candidate_priority = _DEFINITION_KIND_PRIORITY[definition_kind]
		if candidate_priority > current_priority:
			self.definition_kind = definition_kind
		self.start_line = min(self.start_line, line_number)

	def add_config_paths(self, paths: Sequence[str]) -> None:
		"""Attach adjacent Config.in/Kconfig evidence to the module."""

		for path in paths:
			if path and path not in self.config_paths:
				self.config_paths.append(path)

	def set_logical_name(self, logical_name: str, *, line_number: int) -> None:
		"""Attach one logical/module alias emitted by MODULE-style variables."""

		if not logical_name:
			return
		self.logical_name = logical_name
		self.end_line = line_number if self.end_line is None else max(self.end_line, line_number)

	def add_file(self, item: ParsedMakefileFile) -> None:
		"""Record a file membership edge for the module."""

		key = (item.path, item.origin_variable, item.line_number, item.condition_expr)
		if key in self._file_keys:
			return
		self._file_keys.add(key)
		self.files.append(item)
		self.end_line = item.line_number if self.end_line is None else max(self.end_line, item.line_number)
		for macro in item.condition_macros:
			self.guard_lines.setdefault(macro, item.line_number)

	def add_component(
		self,
		component_name: str,
		*,
		line_number: int,
		condition_expr: str | None,
		condition_macros: tuple[str, ...],
	) -> None:
		"""Record a component dependency edge for the module."""

		key = (component_name, line_number, condition_expr)
		if key in self._component_keys:
			return
		self._component_keys.add(key)
		self.component_relations.append(
			ParsedMakefileRelation(
				relation_type="depends_on_component",
				target_type="component",
				target=component_name,
				line_number=line_number,
				condition_expr=condition_expr,
				condition_macros=condition_macros,
			)
		)
		self.end_line = line_number if self.end_line is None else max(self.end_line, line_number)
		for macro in condition_macros:
			self.guard_lines.setdefault(macro, line_number)

	def build(self) -> ParsedBuildModule:
		"""Freeze the builder into a stable module payload."""

		condition_macros = tuple(sorted(self.guard_lines))
		components = tuple(
			_unique_in_order([relation.target for relation in self.component_relations])
		)
		relations = [
			ParsedMakefileRelation(
				relation_type="contained_in_directory",
				target_type="directory",
				target=self.module_path,
			)
		]
		if self.logical_name is not None and self.logical_name != self.name:
			relations.append(
				ParsedMakefileRelation(
					relation_type="aliases_module",
					target_type="module",
					target=self.logical_name,
				)
			)
		relations.extend(
			ParsedMakefileRelation(
				relation_type="configured_by",
				target_type="config",
				target=config_path,
			)
			for config_path in self.config_paths
		)
		relations.extend(
			ParsedMakefileRelation(
				relation_type="contains_file",
				target_type="file",
				target=item.path,
				line_number=item.line_number,
				condition_expr=item.condition_expr,
				condition_macros=item.condition_macros,
			)
			for item in self.files
		)
		relations.extend(
			ParsedMakefileRelation(
				relation_type="guarded_by_macro",
				target_type="macro",
				target=macro,
				line_number=line_number,
				condition_macros=(macro,),
			)
			for macro, line_number in sorted(self.guard_lines.items())
		)
		relations.extend(self.component_relations)
		end_line = self.end_line if self.end_line is not None else self.start_line
		return ParsedBuildModule(
			name=self.name,
			module_path=self.module_path,
			makefile_path=self.makefile_path,
			logical_name=self.logical_name,
			definition_kind=self.definition_kind,
			start_line=self.start_line,
			end_line=end_line,
			files=tuple(self.files),
			components=components,
			condition_macros=condition_macros,
			config_paths=tuple(self.config_paths),
			relations=tuple(relations),
		)


def parse_makefile(
	source_path: Path,
	text: str,
	*,
	sibling_paths: Sequence[str | Path] = (),
) -> ParsedMakefile:
	"""Parse one Makefile or module.mk file into build-module facts."""

	warnings: list[MakefileParseWarning] = []
	module_builders: dict[str, _ModuleBuilder] = {}
	default_module_name: str | None = None
	pending_logical_module_name: str | None = None
	condition_stack: list[_ConditionFrame] = []
	config_paths = _normalize_config_paths(source_path, sibling_paths)

	for logical_line in _iter_logical_lines(text):
		stripped = _strip_make_comment(logical_line.text).strip()
		if not stripped:
			continue

		if _ENDIF_DIRECTIVE_RE.match(stripped):
			if condition_stack:
				condition_stack.pop()
			else:
				warnings.append(
					MakefileParseWarning(
						code="makefile.unmatched_endif",
						message="Encountered endif without an active conditional block.",
						line_number=logical_line.start_line,
					)
				)
			continue

		else_match = _ELSE_DIRECTIVE_RE.match(stripped)
		if else_match:
			if not condition_stack:
				warnings.append(
					MakefileParseWarning(
						code="makefile.orphan_else",
						message="Encountered else without an active conditional block.",
						line_number=logical_line.start_line,
					)
				)
				continue
			previous = condition_stack.pop()
			directive = else_match.group("directive")
			extra_expr = None
			extra_macros: tuple[str, ...] = ()
			if directive is not None:
				raw_expr = "" if else_match.group("expr") is None else else_match.group("expr").strip()
				extra_expr = _build_condition_expression(directive, raw_expr)
				extra_macros = _extract_condition_macros(raw_expr)
			expression = f"not ({previous.expression})"
			if extra_expr:
				expression = f"{expression} && {extra_expr}"
			condition_stack.append(
				_ConditionFrame(
					expression=expression,
					macros=tuple(_unique_in_order((*previous.macros, *extra_macros))),
					start_line=logical_line.start_line,
				)
			)
			continue

		if_match = _IF_DIRECTIVE_RE.match(stripped)
		if if_match:
			directive = if_match.group("directive")
			expr = if_match.group("expr").strip()
			condition_stack.append(
				_ConditionFrame(
					expression=_build_condition_expression(directive, expr),
					macros=_extract_condition_macros(expr),
					start_line=logical_line.start_line,
				)
			)
			continue

		include_match = _INCLUDE_DIRECTIVE_RE.match(stripped)
		if include_match:
			config_paths = _merge_unique(
				config_paths,
				[
					path
					for token in _split_tokens(include_match.group("value"))
					if (path := _normalize_config_hint(source_path, token)) is not None
				],
			)
			continue

		assignment_match = _ASSIGNMENT_RE.match(stripped)
		if assignment_match is None:
			continue

		variable = assignment_match.group("variable")
		value = assignment_match.group("value").strip()

		if _is_logical_module_name_variable(variable):
			logical_name = _parse_module_name(value)
			if logical_name is None:
				warnings.append(
					MakefileParseWarning(
						code="makefile.module_name_unresolved",
						message="Module-name assignment could not be reduced to a stable identifier.",
						line_number=logical_line.start_line,
						details={"variable": variable, "value": value},
					)
				)
				continue
			pending_logical_module_name = logical_name
			if default_module_name is not None:
				builder = _ensure_module_builder(
					module_builders,
					module_name=default_module_name,
					source_path=source_path,
					line_number=logical_line.start_line,
					definition_kind="explicit_variable",
				)
				builder.set_logical_name(logical_name, line_number=logical_line.start_line)
				builder.add_config_paths(config_paths)
			continue

		if _is_primary_module_name_variable(variable):
			module_name = _parse_module_name(value)
			if module_name is None:
				warnings.append(
					MakefileParseWarning(
						code="makefile.module_name_unresolved",
						message="Module-name assignment could not be reduced to a stable identifier.",
						line_number=logical_line.start_line,
						details={"variable": variable, "value": value},
					)
				)
				continue
			if default_module_name is not None and default_module_name != module_name:
				warnings.append(
					MakefileParseWarning(
						code="makefile.multiple_module_names",
						message="Multiple explicit module names were declared in one makefile.",
						line_number=logical_line.start_line,
						details={
							"previous_module": default_module_name,
							"current_module": module_name,
							"variable": variable,
						},
					)
				)
			default_module_name = module_name
			builder = _ensure_module_builder(
				module_builders,
				module_name=module_name,
				source_path=source_path,
				line_number=logical_line.start_line,
				definition_kind="explicit_variable",
			)
			if pending_logical_module_name is not None:
				builder.set_logical_name(pending_logical_module_name, line_number=logical_line.start_line)
			builder.add_config_paths(config_paths)
			continue

		binding = _classify_source_binding(variable)
		if binding is None:
			continue

		module_name = binding.module_name or default_module_name or pending_logical_module_name or _fallback_module_name(source_path)
		if default_module_name is None:
			default_module_name = module_name
		builder = _ensure_module_builder(
			module_builders,
			module_name=module_name,
			source_path=source_path,
			line_number=logical_line.start_line,
			definition_kind=binding.definition_kind,
		)
		if pending_logical_module_name is not None:
			builder.set_logical_name(pending_logical_module_name, line_number=logical_line.start_line)
		builder.add_config_paths(config_paths)

		condition_expr, condition_macros = _merge_conditions(
			stack=condition_stack,
			binding_expr=binding.condition_expression,
			binding_macros=binding.condition_macros,
		)
		if binding.member_type == "component":
			parsed_components = [
				component_name
				for token in _split_tokens(value)
				if (component_name := _parse_component_token(token)) is not None
			]
			if not parsed_components and value:
				warnings.append(
					MakefileParseWarning(
						code="makefile.no_component_tokens",
						message="Component-like assignment did not contain a recognizable dependency token.",
						line_number=logical_line.start_line,
						details={"variable": variable, "value": value},
					)
				)
			for component_name in parsed_components:
				builder.add_component(
					component_name,
					line_number=logical_line.start_line,
					condition_expr=condition_expr,
					condition_macros=condition_macros,
				)
			continue

		parsed_files = [
			parsed
			for token in _split_tokens(value)
			if (parsed := _parse_member_token(token, source_path, binding.origin_variable, logical_line.start_line, condition_expr, condition_macros))
			is not None
		]
		if not parsed_files and value:
			warnings.append(
				MakefileParseWarning(
					code="makefile.no_file_tokens",
					message="Source-like assignment did not contain a recognizable file token.",
					line_number=logical_line.start_line,
					details={"variable": variable, "value": value},
				)
			)
		for parsed_file in parsed_files:
			builder.add_file(parsed_file)

	for frame in condition_stack:
		warnings.append(
			MakefileParseWarning(
				code="makefile.unclosed_if",
				message="Conditional block was not closed with endif.",
				line_number=frame.start_line,
				details={"expression": frame.expression},
			)
		)

	modules = tuple(
		builder.build()
		for builder in sorted(module_builders.values(), key=lambda item: (item.start_line, item.name))
	)
	return ParsedMakefile(
		schema_version=MAKEFILE_PARSER_SCHEMA_VERSION,
		source_path=source_path.as_posix(),
		modules=modules,
		warnings=tuple(warnings),
	)


def _ensure_module_builder(
	builders: dict[str, _ModuleBuilder],
	*,
	module_name: str,
	source_path: Path,
	line_number: int,
	definition_kind: str,
) -> _ModuleBuilder:
	module_path = source_path.parent.as_posix()
	builder = builders.get(module_name)
	if builder is None:
		builder = _ModuleBuilder(
			name=module_name,
			module_path=module_path,
			makefile_path=source_path.as_posix(),
			definition_kind=definition_kind,
			start_line=line_number,
		)
		builders[module_name] = builder
		return builder
	builder.update_definition(definition_kind=definition_kind, line_number=line_number)
	return builder


def _classify_source_binding(variable: str) -> _SourceBinding | None:
	normalized = variable.strip()

	conditional_match = _CONDITIONAL_VARIABLE_RE.match(normalized)
	if conditional_match is not None:
		base = conditional_match.group("base")
		macro = conditional_match.group("macro")
		condition_expr = f"$({macro})"
		classified = _classify_member_binding(base)
		if classified is not None:
			module_name, definition_kind, member_type = classified
			return _SourceBinding(
				module_name=module_name,
				definition_kind=definition_kind,
				origin_variable=normalized,
				member_type=member_type,
				condition_expression=condition_expr,
				condition_macros=(macro,),
			)
		return _SourceBinding(
			module_name=base,
			definition_kind="variable_prefix",
			origin_variable=normalized,
			member_type="file",
			condition_expression=condition_expr,
			condition_macros=(macro,),
		)

	classified = _classify_member_binding(normalized)
	if classified is None:
		return None
	module_name, definition_kind, member_type = classified
	return _SourceBinding(
		module_name=module_name,
		definition_kind=definition_kind,
		origin_variable=normalized,
		member_type=member_type,
	)


def _classify_member_binding(variable: str) -> tuple[str | None, str, str] | None:
	normalized = variable.strip()
	lower_variable = normalized.lower()

	module_match = _MODULE_MEMBER_VARIABLE_RE.match(normalized)
	if module_match is not None:
		return module_match.group("module"), "variable_prefix", "file"

	if _is_generic_component_variable(lower_variable):
		return None, "directory_name", "component"

	if _is_generic_source_variable(lower_variable):
		return None, "directory_name", "file"

	return None


def _is_generic_source_variable(variable: str) -> bool:
	if variable in _GENERIC_SOURCE_VARIABLES:
		return True
	return variable.endswith(("_src", "_srcs", "_sources", "_objs", "_object_files"))


def _is_generic_component_variable(variable: str) -> bool:
	if variable in _GENERIC_COMPONENT_VARIABLES:
		return True
	return variable.endswith(("_component", "_components"))


def _is_primary_module_name_variable(variable: str) -> bool:
	return variable.strip().lower() in _PRIMARY_MODULE_NAME_VARIABLES


def _is_logical_module_name_variable(variable: str) -> bool:
	return variable.strip().lower() in _LOGICAL_MODULE_NAME_VARIABLES


def _parse_module_name(value: str) -> str | None:
	for token in _split_tokens(value):
		candidate = token.strip().strip('"\'')
		if not candidate or "$" in candidate or "/" in candidate:
			continue
		return candidate
	return None


def _iter_logical_lines(text: str) -> tuple[_LogicalLine, ...]:
	physical_lines = text.splitlines()
	logical_lines: list[_LogicalLine] = []
	parts: list[str] = []
	start_line = 1
	for line_number, raw_line in enumerate(physical_lines, start=1):
		stripped_line = raw_line.rstrip()
		continued = stripped_line.endswith("\\")
		content = stripped_line[:-1] if continued else stripped_line
		if not parts:
			start_line = line_number
		parts.append(content.strip())
		if continued:
			continue
		logical_lines.append(
			_LogicalLine(
				text=" ".join(part for part in parts if part),
				start_line=start_line,
				end_line=line_number,
			)
		)
		parts = []
	if parts:
		logical_lines.append(
			_LogicalLine(
				text=" ".join(part for part in parts if part),
				start_line=start_line,
				end_line=len(physical_lines) if physical_lines else 1,
			)
		)
	return tuple(logical_lines)


def _strip_make_comment(value: str) -> str:
	escaped = False
	for index, character in enumerate(value):
		if character == "#" and not escaped:
			return value[:index]
		escaped = character == "\\" and not escaped
	return value


def _build_condition_expression(directive: str, expr: str) -> str:
	if directive == "ifdef":
		return expr or "ifdef"
	if directive == "ifndef":
		return f"not ({expr})" if expr else "ifndef"
	return f"{directive}{expr}"


def _extract_condition_macros(value: str) -> tuple[str, ...]:
	return tuple(_unique_in_order(_CONDITION_SYMBOL_RE.findall(value)))


def _merge_conditions(
	*,
	stack: Sequence[_ConditionFrame],
	binding_expr: str | None,
	binding_macros: Sequence[str],
) -> tuple[str | None, tuple[str, ...]]:
	expressions = [frame.expression for frame in stack if frame.expression]
	if binding_expr:
		expressions.append(binding_expr)
	macros = [macro for frame in stack for macro in frame.macros]
	macros.extend(binding_macros)
	unique_macros = tuple(_unique_in_order(macros))
	if not expressions:
		return None, unique_macros
	return " && ".join(expressions), unique_macros


def _split_tokens(value: str) -> tuple[str, ...]:
	cleaned = _strip_make_comment(value).strip()
	if not cleaned:
		return ()
	return tuple(token.rstrip(",") for token in re.findall(r"\S+", cleaned) if token.rstrip(","))


def _parse_member_token(
	token: str,
	source_path: Path,
	origin_variable: str,
	line_number: int,
	condition_expr: str | None,
	condition_macros: tuple[str, ...],
) -> ParsedMakefileFile | None:
	raw_path = token.strip()
	if not raw_path or raw_path.startswith("-"):
		return None
	artifact_kind = _classify_member_kind(raw_path)
	if artifact_kind is None:
		return None
	return ParsedMakefileFile(
		raw_path=raw_path,
		path=_normalize_member_path(source_path, raw_path),
		artifact_kind=artifact_kind,
		origin_variable=origin_variable,
		line_number=line_number,
		condition_expr=condition_expr,
		condition_macros=condition_macros,
	)


def _parse_component_token(token: str) -> str | None:
	raw_component = token.strip().strip('"\'')
	if not raw_component or raw_component.startswith("-"):
		return None
	if any(marker in raw_component for marker in ("$", "/", "%", "*")):
		return None
	return raw_component


def _classify_member_kind(raw_path: str) -> str | None:
	if raw_path.endswith("/"):
		return "directory"
	if raw_path.startswith("$(") and raw_path.endswith(")"):
		return None
	for suffix, artifact_kind in _FILE_SUFFIX_TO_KIND.items():
		if raw_path.endswith(suffix):
			return artifact_kind
	return None


def _normalize_member_path(source_path: Path, raw_path: str) -> str:
	if "$" in raw_path or "%" in raw_path or "*" in raw_path:
		return raw_path
	candidate = Path(raw_path.rstrip("/"))
	if candidate.is_absolute():
		return candidate.as_posix()
	return (source_path.parent / candidate).as_posix()


def _normalize_config_paths(source_path: Path, sibling_paths: Sequence[str | Path]) -> list[str]:
	return _merge_unique([], [_normalize_config_hint(source_path, sibling) for sibling in sibling_paths if sibling is not None])


def _normalize_config_hint(source_path: Path, candidate: str | Path) -> str | None:
	text = str(candidate).strip()
	if not text:
		return None
	if "$" in text:
		leaf_name = Path(text).name
		return text if leaf_name in _CONFIG_FILENAMES else None
	candidate_path = Path(text)
	if candidate_path.name not in _CONFIG_FILENAMES:
		return None
	if candidate_path.is_absolute():
		return candidate_path.as_posix()
	if candidate_path.parent == Path("."):
		return (source_path.parent / candidate_path.name).as_posix()
	return candidate_path.as_posix()


def _fallback_module_name(source_path: Path) -> str:
	if source_path.parent != Path("."):
		return source_path.parent.name
	return source_path.stem or source_path.name


def _merge_unique(existing: Sequence[str], new_items: Sequence[str | None]) -> list[str]:
	merged: list[str] = []
	seen: set[str] = set()
	for item in (*existing, *new_items):
		if item is None or item in seen:
			continue
		seen.add(item)
		merged.append(item)
	return merged


def _unique_in_order(values: Sequence[str]) -> list[str]:
	seen: set[str] = set()
	ordered: list[str] = []
	for value in values:
		if value in seen:
			continue
		seen.add(value)
		ordered.append(value)
	return ordered
