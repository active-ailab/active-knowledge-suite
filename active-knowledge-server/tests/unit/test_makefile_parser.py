from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from active_knowledge_server.parsers.makefiles import parse_makefile


def test_parse_makefile_extracts_module_files_macros_and_config_links() -> None:
	parsed = parse_makefile(
		Path("components/health/module.mk"),
		dedent(
			"""\
			LOCAL_MODULE := health_core
			LOCAL_SRCS += main.c \
			    ui/screen.c
			LOCAL_SRCS-$(CONFIG_HEALTH_BT) += bt.c
			health_core-y += renderer.o
			ifeq ($(CONFIG_HEALTH_SENSOR),y)
			LOCAL_SRCS += sensor.c
			endif
			"""
		),
		sibling_paths=("Config.in", "Kconfig"),
	)

	assert parsed.schema_version == "makefile_parser.v1"
	assert parsed.warnings == ()
	assert len(parsed.modules) == 1

	module = parsed.modules[0]
	files = {item.path: item for item in module.files}
	relations = {(relation.relation_type, relation.target) for relation in module.relations}

	assert module.name == "health_core"
	assert module.logical_name is None
	assert module.module_path == "components/health"
	assert module.definition_kind == "explicit_variable"
	assert module.components == ()
	assert module.condition_macros == ("CONFIG_HEALTH_BT", "CONFIG_HEALTH_SENSOR")
	assert module.config_paths == (
		"components/health/Config.in",
		"components/health/Kconfig",
	)
	assert set(files) == {
		"components/health/main.c",
		"components/health/ui/screen.c",
		"components/health/bt.c",
		"components/health/renderer.o",
		"components/health/sensor.c",
	}
	assert files["components/health/bt.c"].condition_macros == ("CONFIG_HEALTH_BT",)
	assert files["components/health/sensor.c"].condition_macros == ("CONFIG_HEALTH_SENSOR",)
	assert files["components/health/renderer.o"].artifact_kind == "object"
	assert ("contained_in_directory", "components/health") in relations
	assert ("configured_by", "components/health/Config.in") in relations
	assert ("contains_file", "components/health/main.c") in relations
	assert ("guarded_by_macro", "CONFIG_HEALTH_BT") in relations


def test_parse_makefile_uses_directory_name_fallback_and_reports_unclosed_if() -> None:
	parsed = parse_makefile(
		Path("apps/demo/Makefile"),
		dedent(
			"""\
			SRCS += main.c
			ifdef CONFIG_DEMO_EXTRA
			SRCS += extra.c
			"""
		),
	)

	assert len(parsed.modules) == 1
	module = parsed.modules[0]
	files = {item.path: item for item in module.files}

	assert module.name == "demo"
	assert module.logical_name is None
	assert module.definition_kind == "directory_name"
	assert module.condition_macros == ("CONFIG_DEMO_EXTRA",)
	assert files["apps/demo/main.c"].condition_macros == ()
	assert files["apps/demo/extra.c"].condition_macros == ("CONFIG_DEMO_EXTRA",)
	assert [warning.code for warning in parsed.warnings] == ["makefile.unclosed_if"]
	assert parsed.warnings[0].line_number == 2


def test_parse_makefile_supports_zeppos_name_module_components_and_bare_macros() -> None:
	parsed = parse_makefile(
		Path("core/module.mk"),
		dedent(
			"""\
			NAME := hmos_core
			MODULE := rtos
			$(NAME)_COMPONENTS += env uiframework
			$(NAME)_COMPONENTS-$(HMI_COMP_ZRPC) += zrpc
			$(NAME)_SOURCES += freertos/tasks.c \
			    freertos/timers.c
			$(NAME)_SOURCES-$(HMI_MCU_MHS003) += freertos/portable/GCC/mhs003/port.c
			ifeq ($(HMI_CORE_TEST), y)
			$(NAME)_SOURCES += test/test_os_status.c
			else ifeq ($(HMI_MCU_SIMX86), y)
			$(NAME)_SOURCES += simx86_only.c
			endif
			"""
		),
		sibling_paths=("Config.in",),
	)

	assert parsed.warnings == ()
	assert len(parsed.modules) == 1

	module = parsed.modules[0]
	files = {item.path: item for item in module.files}
	relations = {(relation.relation_type, relation.target): relation for relation in module.relations}

	assert module.name == "hmos_core"
	assert module.logical_name == "rtos"
	assert module.definition_kind == "explicit_variable"
	assert module.components == ("env", "uiframework", "zrpc")
	assert module.condition_macros == (
		"HMI_COMP_ZRPC",
		"HMI_CORE_TEST",
		"HMI_MCU_MHS003",
		"HMI_MCU_SIMX86",
	)
	assert files["core/freertos/portable/GCC/mhs003/port.c"].condition_macros == ("HMI_MCU_MHS003",)
	assert files["core/simx86_only.c"].condition_macros == ("HMI_CORE_TEST", "HMI_MCU_SIMX86")
	assert relations[("aliases_module", "rtos")].target_type == "module"
	assert relations[("configured_by", "core/Config.in")].target_type == "config"
	assert relations[("depends_on_component", "zrpc")].condition_macros == ("HMI_COMP_ZRPC",)