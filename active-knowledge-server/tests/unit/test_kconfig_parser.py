from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from active_knowledge_server.parsers.kconfig import (
	parse_defconfig,
	parse_kconfig,
	parse_profile_config,
)


def test_parse_profile_config_merges_macros_extracts_clues_and_stabilizes_hash() -> None:
	kwargs = {
		"defconfig_path": Path("configs/apps/sleep_lab/sleep_lab_defconfig"),
		"defconfig_text": dedent(
			"""\
			CONFIG_APP_SLEEP_LAB=y
			CONFIG_BOARD_WATCH_DEV=y
			CONFIG_FEATURE_SLEEP=y
			# CONFIG_FEATURE_GPS is not set
			"""
		),
		"dotconfig_path": Path("build/.config"),
		"dotconfig_text": dedent(
			"""\
			CONFIG_APP="sleep_lab"
			CONFIG_BOARD="watch_dev"
			CONFIG_FEATURE_SLEEP=y
			# CONFIG_FEATURE_GPS is not set
			CONFIG_HEAP_MEM_POOL_SIZE=4096
			"""
		),
	}

	first = parse_profile_config(**kwargs)
	second = parse_profile_config(**kwargs)

	assignments = {assignment.macro_name: assignment for assignment in first.merged_assignments}

	assert first.clues.app == "sleep_lab"
	assert first.clues.board == "watch_dev"
	assert first.clues.features == ("sleep",)
	assert assignments["CONFIG_APP"].value == "sleep_lab"
	assert assignments["CONFIG_BOARD"].value == "watch_dev"
	assert assignments["CONFIG_FEATURE_GPS"].enabled is False
	assert assignments["CONFIG_HEAP_MEM_POOL_SIZE"].value == "4096"
	assert first.macro_summary_hash == second.macro_summary_hash


def test_parse_defconfig_uses_path_hint_and_reports_unparsed_lines() -> None:
	parsed = parse_defconfig(
		Path("configs/apps/demo/demo_defconfig"),
		dedent(
			"""\
			CONFIG_FEATURE_WEATHER=y
			BROKEN_LINE
			"""
		),
	)

	assert parsed.clues.app == "demo"
	assert parsed.clues.board is None
	assert parsed.clues.features == ("weather",)
	assert [warning.code for warning in parsed.warnings] == ["config.unparsed_line"]
	assert parsed.warnings[0].line_number == 2


def test_parse_kconfig_parses_symbols_depends_selects_and_if_context() -> None:
	parsed = parse_kconfig(
		Path("configs/Config.in"),
		dedent(
			"""\
			config FEATURE_SLEEP
			    bool "Sleep tracking"
			    depends on SENSOR && DISPLAY
			    select APP_CORE

			menuconfig APP_HEALTH
			    bool "Health app"
			    depends on FEATURE_SLEEP
			    select SENSOR_HUB if BT

			if BOARD_WATCH
			config BOARD_ACCEL
			    tristate "Board accel"
			    select ACCEL_DRIVER
			endif
			"""
		),
	)

	symbols = {symbol.name: symbol for symbol in parsed.symbols}

	assert parsed.warnings == ()
	assert [symbol.name for symbol in parsed.symbols] == ["FEATURE_SLEEP", "APP_HEALTH", "BOARD_ACCEL"]
	assert symbols["FEATURE_SLEEP"].value_type == "bool"
	assert symbols["FEATURE_SLEEP"].prompt == "Sleep tracking"
	assert symbols["FEATURE_SLEEP"].depends_on == ("SENSOR && DISPLAY",)
	assert [select.target for select in symbols["FEATURE_SLEEP"].selects] == ["APP_CORE"]
	assert symbols["APP_HEALTH"].definition_kind == "menuconfig"
	assert symbols["APP_HEALTH"].selects[0].condition == "BT"
	assert symbols["BOARD_ACCEL"].depends_on == ("BOARD_WATCH",)
	assert symbols["BOARD_ACCEL"].value_type == "tristate"
	assert (symbols["FEATURE_SLEEP"].start_line, symbols["FEATURE_SLEEP"].end_line) == (1, 4)
	assert (symbols["BOARD_ACCEL"].start_line, symbols["BOARD_ACCEL"].end_line) == (12, 14)