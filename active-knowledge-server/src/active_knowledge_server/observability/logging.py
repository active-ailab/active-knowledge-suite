"""Runtime file logging setup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from logging import Handler, Logger
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from active_knowledge_server.config.schema import ActiveKnowledgeConfig, LogRotationConfig
from active_knowledge_server.config.workdir import WorkdirLayout

LogChannel = Literal["server", "indexer", "audit", "security", "eval"]

LOG_FILE_NAMES: Final[MappingProxyType[LogChannel, str]] = MappingProxyType(
    {
        "server": "server.log",
        "indexer": "indexer.log",
        "audit": "audit.log",
        "security": "security.log",
        "eval": "eval.log",
    }
)
CORE_LOG_CHANNELS: Final[tuple[LogChannel, ...]] = ("server", "indexer", "audit", "eval")
_FILE_LOGGER_NAMES: Final[MappingProxyType[LogChannel, str]] = MappingProxyType(
    {
        "server": "active_knowledge_server.server",
        "indexer": "active_knowledge_server.indexer",
        "security": "active_knowledge_server.security",
        "eval": "active_knowledge_server.eval",
    }
)
_MANAGED_HANDLER_ATTR: Final = "_active_kb_managed_handler"


@dataclass(frozen=True)
class LoggingSetup:
    """Paths and logger names configured for runtime logging."""

    log_files: MappingProxyType[LogChannel, Path]
    logger_names: MappingProxyType[LogChannel, str]


def ensure_log_files(logs_dir: Path) -> MappingProxyType[LogChannel, Path]:
    """Create the configured log files and return their paths."""

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_files = log_file_paths(logs_dir)
    for path in log_files.values():
        path.touch(exist_ok=True)
    return log_files


def log_file_paths(logs_dir: Path) -> MappingProxyType[LogChannel, Path]:
    """Return the configured log file paths without creating them."""

    log_files = {channel: logs_dir / filename for channel, filename in LOG_FILE_NAMES.items()}
    return MappingProxyType(log_files)


def configure_logging(config: ActiveKnowledgeConfig, layout: WorkdirLayout) -> LoggingSetup:
    """Configure rotating file handlers for non-audit runtime logs."""

    log_files = ensure_log_files(layout.local_logs_dir)
    level = logging_level(config.runtime.log_level)

    package_logger = logging.getLogger("active_knowledge_server")
    package_logger.setLevel(level)

    for channel, logger_name in _FILE_LOGGER_NAMES.items():
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        logger.propagate = False
        remove_managed_handlers(logger)

        handler = build_file_handler(log_files[channel], config.runtime.logging.rotation)
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        mark_managed_handler(handler)
        logger.addHandler(handler)

    return LoggingSetup(
        log_files=log_files,
        logger_names=MappingProxyType(dict(_FILE_LOGGER_NAMES)),
    )


def logging_level(name: str) -> int:
    """Map validated config strings to stdlib logging levels."""

    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }[name]


def build_file_handler(path: Path, rotation: LogRotationConfig) -> Handler:
    """Build a file handler honoring the configured rotation policy."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if rotation.enabled:
        return RotatingFileHandler(
            path,
            maxBytes=rotation.max_bytes,
            backupCount=rotation.backup_count,
            encoding="utf-8",
        )
    return logging.FileHandler(path, encoding="utf-8")


def mark_managed_handler(handler: Handler) -> None:
    """Mark a handler so repeated setup can replace it safely."""

    setattr(handler, _MANAGED_HANDLER_ATTR, True)


def remove_managed_handlers(logger: Logger) -> None:
    """Remove handlers installed by this module from a logger."""

    for handler in tuple(logger.handlers):
        if bool(getattr(handler, _MANAGED_HANDLER_ATTR, False)):
            logger.removeHandler(handler)
            handler.close()
