from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from active_knowledge_server.parsers.ctags import (
	C_FAMILY_PARSER_SCHEMA_VERSION,
	parse_c_family_file,
)


def test_parse_c_family_file_extracts_symbols_header_comments_and_compile_db_warning() -> None:
	parsed = parse_c_family_file(
		Path("core/main.c"),
		dedent(
			"""\
			/*
			 * Demo file header
			 */
			#include <stdio.h>
			#include "demo.h"
			#define DEMO_STACK_SIZE 1024

			typedef struct {
			    int argc;
			    char **argv;
			} demo_args_t;

			// single line note
			extern void board_init(void);

			static int helper(int value)
			{
			    return value + 1;
			}
			"""
		),
	)

	includes = {include.target: include for include in parsed.includes}
	symbols = {(symbol.name, symbol.symbol_kind, symbol.is_definition): symbol for symbol in parsed.symbols}
	comment_texts = [comment.text for comment in parsed.comments]

	assert parsed.schema_version == C_FAMILY_PARSER_SCHEMA_VERSION
	assert parsed.language == "c"
	assert parsed.extractor_used == "heuristic"
	assert [warning.code for warning in parsed.warnings] == ["compile_db.missing"]
	assert set(includes) == {"stdio.h", "demo.h"}
	assert includes["stdio.h"].is_system is True
	assert includes["demo.h"].is_system is False
	assert parsed.file_header is not None
	assert parsed.file_header.start_line == 1
	assert parsed.file_header.end_line == 6
	assert parsed.file_header.include_targets == ("stdio.h", "demo.h")
	assert parsed.file_header.macro_names == ("DEMO_STACK_SIZE",)
	assert any("Demo file header" in text for text in comment_texts)
	assert any("single line note" in text for text in comment_texts)
	assert ("DEMO_STACK_SIZE", "macro", True) in symbols
	assert symbols[("DEMO_STACK_SIZE", "macro", True)].extractor == "heuristic"
	assert ("demo_args_t", "type", True) in symbols
	assert ("board_init", "function", False) in symbols
	assert ("helper", "function", True) in symbols
	assert symbols[("helper", "function", True)].confidence < 0.8


def test_parse_c_family_file_supports_header_guards_cpp_headers_and_compile_db_path() -> None:
	parsed = parse_c_family_file(
		Path("include/demo.hpp"),
		dedent(
			"""\
			#ifndef DEMO_HEADER_HPP
			#define DEMO_HEADER_HPP

			#include "dep.hpp"

			class DemoClient {
			public:
			    void connect();
			};

			typedef unsigned int demo_flags_t;

			#endif
			"""
		),
		compile_db_path=Path("build/compile_commands.json"),
	)

	symbols = {(symbol.name, symbol.symbol_kind): symbol for symbol in parsed.symbols}

	assert parsed.language == "cpp-header"
	assert parsed.compile_db_path == "build/compile_commands.json"
	assert parsed.warnings == ()
	assert parsed.file_header is not None
	assert parsed.file_header.macro_names == ("DEMO_HEADER_HPP",)
	assert parsed.file_header.include_targets == ("dep.hpp",)
	assert parsed.includes[0].target == "dep.hpp"
	assert ("DemoClient", "type") in symbols
	assert ("demo_flags_t", "type") in symbols