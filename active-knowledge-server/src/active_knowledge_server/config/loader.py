"""Configuration loading and precedence helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

import yaml

from active_knowledge_server.config.defaults import (
    DEFAULT_CONFIG_FILE,
    DEFAULT_LOCAL_CONFIG_NAME,
    DEFAULT_WORKDIR,
    default_config,
)

ConfigScalar: TypeAlias = str | int | float | bool | None
ConfigValue: TypeAlias = ConfigScalar | list["ConfigValue"] | dict[str, "ConfigValue"]
ConfigDict: TypeAlias = dict[str, ConfigValue]

_ENV_PATHS: Mapping[str, tuple[str, ...]] = {
    "ACTIVE_KB_WORKDIR": ("runtime", "workdir"),
    "ACTIVE_KB_WORKSPACE": ("project", "workspace_root"),
    "ACTIVE_KB_SOURCE_DOCS_ROOT": ("runtime", "source_docs_root"),
    "ACTIVE_KB_PROFILE": ("project", "default_profile"),
    "ACTIVE_KB_TRANSPORT": ("server", "transport"),
    "ACTIVE_KB_HTTP_HOST": ("server", "http", "host"),
    "ACTIVE_KB_HTTP_PORT": ("server", "http", "port"),
    "ACTIVE_KB_LOG_LEVEL": ("runtime", "log_level"),
}


class ConfigError(ValueError):
    """Raised when a configuration file or override cannot be interpreted."""


@dataclass(frozen=True)
class ResolvedConfig:
    """A merged config plus the file paths used to build it."""

    data: ConfigDict
    baseline_config_path: Path | None
    local_config_path: Path
    loaded_files: tuple[Path, ...]
    source_order: tuple[str, ...] = (
        "defaults",
        "baseline_config",
        "local_config",
        "environment",
        "cli",
    )

    def get(self, dotted_path: str, default: ConfigValue | None = None) -> ConfigValue | None:
        """Return a config value by dotted path."""

        current: ConfigValue | None = self.data
        for part in dotted_path.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


def normalize_config_path(path: str | Path, cwd: Path | None = None) -> Path:
    """Return an expanded, absolute config path without reading it."""

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return (cwd or Path.cwd()) / candidate


def resolve_config(
    *,
    config_path: str | Path | None = None,
    local_config_path: str | Path | None = None,
    cli_overrides: ConfigDict | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> ResolvedConfig:
    """Merge config sources using CLI > env > local > baseline > defaults priority."""

    root = cwd or Path.cwd()
    environment = env or os.environ
    cli_data = cli_overrides or {}
    env_data = env_overrides(environment)

    baseline_path = select_baseline_config_path(config_path, environment, root)
    baseline_data = (
        load_yaml_config(baseline_path) if baseline_path and baseline_path.exists() else {}
    )

    pre_local = merge_configs(default_config_data(), baseline_data, env_data, cli_data)
    local_path = select_local_config_path(local_config_path, environment, pre_local, root)
    local_data = load_yaml_config(local_path) if local_path.exists() else {}

    data = merge_configs(default_config_data(), baseline_data, local_data, env_data, cli_data)
    data = apply_derived_runtime_paths(data)

    loaded_files = tuple(path for path in (baseline_path, local_path) if path and path.exists())
    return ResolvedConfig(
        data=data,
        baseline_config_path=baseline_path,
        local_config_path=local_path,
        loaded_files=loaded_files,
    )


def default_config_data() -> ConfigDict:
    """Return built-in defaults as a typed config dictionary."""

    return coerce_config_value(default_config(), source="defaults")


def select_baseline_config_path(
    config_path: str | Path | None,
    env: Mapping[str, str],
    cwd: Path,
) -> Path | None:
    """Choose the baseline/static config file path, if any."""

    if config_path is not None:
        return normalize_config_path(config_path, cwd)

    env_path = env.get("ACTIVE_KB_CONFIG")
    if env_path:
        return normalize_config_path(env_path, cwd)

    repository_config = normalize_config_path(DEFAULT_CONFIG_FILE, cwd)
    if repository_config.exists():
        return repository_config

    baseline_config = normalize_config_path(
        Path(DEFAULT_WORKDIR) / "baseline" / "config" / "baseline.yaml",
        cwd,
    )
    if baseline_config.exists():
        return baseline_config

    return None


def select_local_config_path(
    local_config_path: str | Path | None,
    env: Mapping[str, str],
    pre_local_config: ConfigDict,
    cwd: Path,
) -> Path:
    """Choose the user-local config path."""

    if local_config_path is not None:
        return normalize_config_path(local_config_path, cwd)

    env_path = env.get("ACTIVE_KB_LOCAL_CONFIG")
    if env_path:
        return normalize_config_path(env_path, cwd)

    workdir = config_path_value(pre_local_config, "runtime.workdir", DEFAULT_WORKDIR)
    return resolve_runtime_path(workdir, cwd) / "local" / "config" / DEFAULT_LOCAL_CONFIG_NAME


def env_overrides(env: Mapping[str, str]) -> ConfigDict:
    """Convert supported ACTIVE_KB_* environment variables into config overrides."""

    overrides: ConfigDict = {}
    for name, path in _ENV_PATHS.items():
        raw = env.get(name)
        if raw is None:
            continue
        value: ConfigValue = parse_env_value(name, raw)
        if name == "ACTIVE_KB_TRANSPORT":
            value = normalize_transport(raw)
        set_nested(overrides, path, value)
    return overrides


def parse_env_value(name: str, raw: str) -> ConfigValue:
    """Parse a supported environment variable value."""

    if name != "ACTIVE_KB_HTTP_PORT":
        return raw
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer port, got {raw!r}") from exc


def load_yaml_config(path: Path) -> ConfigDict:
    """Load a YAML config file as a dictionary."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in config file {path}: {exc}") from exc

    if raw is None:
        return {}
    return coerce_config_value(raw, source=str(path))


def coerce_config_value(value: object, *, source: str) -> ConfigDict:
    """Validate that a raw value is a supported config dictionary."""

    coerced = _coerce_value(value, source=source)
    if not isinstance(coerced, dict):
        raise ConfigError(f"{source} must contain a YAML mapping at the top level")
    return coerced


def merge_configs(*configs: Mapping[str, ConfigValue]) -> ConfigDict:
    """Deep-merge config dictionaries from lowest to highest priority."""

    merged: ConfigDict = {}
    for config in configs:
        merged = _deep_merge(merged, config)
    return merged


def set_nested(config: ConfigDict, path: tuple[str, ...], value: ConfigValue) -> None:
    """Set a nested config value, creating intermediate mappings."""

    current = config
    for part in path[:-1]:
        existing = current.get(part)
        if not isinstance(existing, dict):
            existing = {}
            current[part] = existing
        current = existing
    current[path[-1]] = value


def apply_derived_runtime_paths(config: ConfigDict) -> ConfigDict:
    """Fill runtime paths derived from runtime.workdir when callers omit them."""

    merged = merge_configs(config)
    runtime = merged.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigError("runtime config must be a mapping")

    workdir = str(runtime.get("workdir") or DEFAULT_WORKDIR)
    runtime.setdefault("baseline_dir", str(Path(workdir) / "baseline"))
    runtime.setdefault("local_dir", str(Path(workdir) / "local"))
    return merged


def config_path_value(config: Mapping[str, ConfigValue], dotted_path: str, default: str) -> str:
    """Return a string config value by dotted path."""

    current: ConfigValue | None = cast(ConfigDict, dict(config))
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    if isinstance(current, str):
        return current
    return default


def resolve_runtime_path(path: str | Path, cwd: Path) -> Path:
    """Resolve a runtime path relative to a working directory."""

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate
    return cwd / candidate


def normalize_transport(transport: str) -> str:
    """Normalize common transport aliases to the config contract vocabulary."""

    return "streamable-http" if transport == "http" else transport


def _deep_merge(
    base: Mapping[str, ConfigValue],
    overlay: Mapping[str, ConfigValue],
) -> ConfigDict:
    merged: ConfigDict = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _coerce_value(value: object, *, source: str) -> ConfigValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_coerce_value(item, source=source) for item in value]
    if isinstance(value, dict):
        coerced: ConfigDict = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigError(f"{source} contains a non-string key: {key!r}")
            coerced[key] = _coerce_value(item, source=source)
        return coerced
    raise ConfigError(f"{source} contains unsupported value {value!r}")
