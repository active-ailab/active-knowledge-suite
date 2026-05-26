"""Pydantic configuration schema for Active Knowledge Server."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic import (
    ConfigDict as PydanticConfigDict,
)

from active_knowledge_server.config.defaults import DEFAULT_SCHEMA_VERSION

DeploymentMode = Literal["local_single_user", "remote_shared"]
Transport = Literal["stdio", "streamable-http"]
StoreMode = Literal["readonly", "readwrite"]
RerankMode = Literal["none", "lightweight", "cross_encoder"]

_MASK = "***REDACTED***"
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_SENSITIVE_CONTEXT_ALLOWED = {"auth_provider", "env", "header", "scheme"}


@dataclass(frozen=True)
class ConfigSchemaInfo:
    """Current config schema metadata."""

    version: str = DEFAULT_SCHEMA_VERSION


class ConfigModel(BaseModel):
    """Base class that preserves future config keys while validating known ones."""

    model_config = PydanticConfigDict(extra="allow")


class HttpTokenConfig(ConfigModel):
    """Token auth indirection config."""

    env: str | None = None
    header: str = "Authorization"
    scheme: str = "Bearer"


class HttpConfig(ConfigModel):
    """HTTP transport configuration."""

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    mcp_path: str = "/mcp"
    require_auth: bool = False
    auth_provider: str = "none"
    token: HttpTokenConfig | None = None
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1"])
    trust_reverse_proxy: bool = False


class ServerConfig(ConfigModel):
    """Server and MCP facade configuration."""

    name: str = "active-knowledge-server"
    transport: Transport = "stdio"
    expose_ops_tools: bool = False
    http: HttpConfig = Field(default_factory=HttpConfig)


class LogRotationConfig(ConfigModel):
    """Rotating file log policy."""

    enabled: bool = True
    max_bytes: int = Field(default=10_485_760, ge=1)
    backup_count: int = Field(default=5, ge=0)


class LoggingConfig(ConfigModel):
    """Runtime log file configuration."""

    rotation: LogRotationConfig = Field(default_factory=LogRotationConfig)


class RuntimeConfig(ConfigModel):
    """Runtime filesystem and logging configuration."""

    source_root: str = "."
    workdir: str
    baseline_dir: str
    local_dir: str
    source_docs_root: str
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class ProjectConfig(ConfigModel):
    """Target engineering project configuration."""

    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    branch_strategy: str = "baseline-first"
    baseline_branch: str | None = None
    default_snapshot: str = "current"
    default_profile: str = "auto"


class PathsConfig(ConfigModel):
    """Workspace include and exclude path patterns."""

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class ProfileDiscoveryConfig(ConfigModel):
    """Profile discovery path hints."""

    defconfig_roots: list[str] = Field(default_factory=list)
    dotconfig_candidates: list[str] = Field(default_factory=list)


class KnownProfileConfig(ConfigModel):
    """Configured profile seed."""

    id: str = Field(min_length=1)
    dotconfig: str | None = None
    defconfig: str | None = None
    app: str | None = None
    board: str | None = None
    priority: int | None = None


class ProfilesConfig(ConfigModel):
    """Profile discovery and known profile configuration."""

    discovery: ProfileDiscoveryConfig = Field(default_factory=ProfileDiscoveryConfig)
    known: list[KnownProfileConfig] = Field(default_factory=list)


class BaselineStorageConfig(ConfigModel):
    """Baseline manifest location."""

    manifest: str


class StoreConfig(ConfigModel):
    """Local storage adapter config."""

    backend: str = Field(min_length=1)
    path: str = Field(min_length=1)
    mode: StoreMode = "readwrite"


class SQLiteTuningConfig(ConfigModel):
    """SQLite journal and checkpoint tuning for metadata-side writers."""

    journal_mode: Literal["delete", "wal"] = "delete"
    synchronous: Literal["full", "normal"] = "full"
    wal_autocheckpoint_pages: int | None = Field(default=None, ge=1)
    assume_local_filesystem: bool = False

    @model_validator(mode="after")
    def validate_wal_guards(self) -> SQLiteTuningConfig:
        """Gate WAL behind an explicit local-filesystem acknowledgement."""

        if self.journal_mode == "wal" and not self.assume_local_filesystem:
            raise ValueError(
                "storage.sqlite.assume_local_filesystem must be true when "
                "storage.sqlite.journal_mode=wal"
            )
        if self.journal_mode != "wal" and self.wal_autocheckpoint_pages is not None:
            raise ValueError(
                "storage.sqlite.wal_autocheckpoint_pages requires "
                "storage.sqlite.journal_mode=wal"
            )
        return self


class StorageConfig(ConfigModel):
    """Metadata, vector, artifact, and cache storage config."""

    baseline: BaselineStorageConfig
    metadata: StoreConfig
    overlay: StoreConfig
    jobs: StoreConfig
    vector: StoreConfig
    vector_delta: StoreConfig
    sqlite: SQLiteTuningConfig = Field(default_factory=SQLiteTuningConfig)
    artifacts_root: str
    local_artifacts_root: str
    cache_root: str


class CodeIndexingConfig(ConfigModel):
    """Code indexing feature switches."""

    enable_full_code_scan: bool = True
    enable_ctags: bool = True
    enable_tree_sitter: bool = True
    enable_clang_index: bool = False
    compile_db_candidates: list[str] = Field(default_factory=list)


class DocsIndexingConfig(ConfigModel):
    """Document indexing feature switches."""

    enable_markdown: bool = True
    enable_html: bool = True
    enable_pdf: bool = False


class EmbeddingsConfig(ConfigModel):
    """Embedding job configuration."""

    enabled: bool = True
    provider: str = "local"
    model: str = "bge-m3"
    batch_size: int = Field(default=32, ge=1)


class IndexWriterConfig(ConfigModel):
    """Single-writer batching policy for index apply phases."""

    batch_size: int = Field(default=64, ge=1)
    commit_interval_ms: int = Field(default=1000, ge=1)


class LearnedCardsConfig(ConfigModel):
    """Learned card ingestion switches."""

    enabled: bool = False
    require_review: bool = True


class IndexingConfig(ConfigModel):
    """Indexing pipeline configuration."""

    mode: str = "local"
    incremental: bool = True
    reuse_baseline: bool = True
    write_target: Literal["local_overlay", "baseline"] = "local_overlay"
    workers: int | Literal["auto"] = "auto"
    code: CodeIndexingConfig = Field(default_factory=CodeIndexingConfig)
    docs: DocsIndexingConfig = Field(default_factory=DocsIndexingConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    writer: IndexWriterConfig = Field(default_factory=IndexWriterConfig)
    learned_cards: LearnedCardsConfig = Field(default_factory=LearnedCardsConfig)

    @field_validator("workers")
    @classmethod
    def validate_workers(cls, value: int | Literal["auto"]) -> int | Literal["auto"]:
        """Allow auto or a positive worker count."""

        if value == "auto" or value > 0:
            return value
        raise ValueError("workers must be 'auto' or a positive integer")


class HybridQueryConfig(ConfigModel):
    """Hybrid retrieval switches."""

    enable_fts: bool = True
    enable_vector: bool = True
    enable_symbol: bool = True
    enable_graph_expand: bool = True
    rerank: RerankMode = "lightweight"


class QueryConfig(ConfigModel):
    """Query service configuration."""

    default_top_k: int = Field(default=12, ge=1)
    max_evidence_items: int = Field(default=20, ge=1)
    hybrid: HybridQueryConfig = Field(default_factory=HybridQueryConfig)
    evidence_required: bool = True


class SecretScanConfig(ConfigModel):
    """Secret scan configuration."""

    enabled: bool = True
    deny_patterns: list[str] = Field(default_factory=list)


class AuditConfig(ConfigModel):
    """Audit log configuration."""

    enabled: bool = True


class SecurityConfig(ConfigModel):
    """Security and audit configuration."""

    path_allowlist: list[str] = Field(default_factory=list)
    secret_scan: SecretScanConfig = Field(default_factory=SecretScanConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)


class ActiveKnowledgeConfig(ConfigModel):
    """Complete Active Knowledge Server runtime configuration."""

    config_schema_version: str = DEFAULT_SCHEMA_VERSION
    deployment_mode: DeploymentMode = "local_single_user"
    server: ServerConfig
    runtime: RuntimeConfig
    project: ProjectConfig
    paths: PathsConfig = Field(default_factory=PathsConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    storage: StorageConfig
    indexing: IndexingConfig
    query: QueryConfig
    security: SecurityConfig

    def to_config_dict(self) -> dict[str, Any]:
        """Return a serializable config dictionary."""

        return self.model_dump(mode="json", exclude_none=True)


def validate_config_dict(data: Mapping[str, Any], *, source: str) -> ActiveKnowledgeConfig:
    """Validate a merged config dictionary and raise an actionable ValueError."""

    try:
        return ActiveKnowledgeConfig.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(format_validation_error(error) for error in exc.errors())
        raise ValueError(f"invalid {source}: {details}") from exc


def format_validation_error(error: Mapping[str, Any]) -> str:
    """Format a Pydantic validation error as a compact dotted-path message."""

    location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
    message = str(error.get("msg", "invalid value"))
    return f"{location}: {message}"


def summarize_config(
    config: ActiveKnowledgeConfig,
    *,
    cwd: Path,
    loaded_files: tuple[Path, ...] = (),
    local_config_path: Path | None = None,
) -> dict[str, Any]:
    """Return a compact non-sensitive config summary for CLI and ops output."""

    return {
        "config_schema_version": config.config_schema_version,
        "deployment_mode": config.deployment_mode,
        "workdir": shorten_path(config.runtime.workdir, cwd),
        "local_dir": shorten_path(config.runtime.local_dir, cwd),
        "baseline_dir": shorten_path(config.runtime.baseline_dir, cwd),
        "source_docs_root": shorten_path(config.runtime.source_docs_root, cwd),
        "workspace_root": shorten_path(config.project.workspace_root, cwd),
        "profile": config.project.default_profile,
        "transport": config.server.transport,
        "http": {
            "host": config.server.http.host,
            "port": config.server.http.port,
            "require_auth": config.server.http.require_auth,
            "auth_provider": config.server.http.auth_provider,
        },
        "loaded_config_files": [shorten_path(path, cwd) for path in loaded_files],
        "local_config_path": shorten_path(local_config_path, cwd) if local_config_path else None,
    }


def safe_config_dump(config: ActiveKnowledgeConfig) -> dict[str, Any]:
    """Return a full config dump with sensitive scalar values redacted."""

    return cast(dict[str, Any], redact_sensitive(config.to_config_dict()))


def redact_sensitive(value: Any, *, parent_sensitive: bool = False) -> Any:
    """Recursively redact secret-bearing scalar fields."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = key.lower()
            sensitive = parent_sensitive or key_lower in _SENSITIVE_KEYS
            child_sensitive = sensitive and key_lower not in _SENSITIVE_CONTEXT_ALLOWED
            if child_sensitive and not isinstance(item, dict | list):
                redacted[key] = _MASK
            else:
                redacted[key] = redact_sensitive(item, parent_sensitive=child_sensitive)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item, parent_sensitive=parent_sensitive) for item in value]
    if parent_sensitive:
        return _MASK
    return value


def shorten_path(path: str | Path, cwd: Path) -> str:
    """Shorten absolute paths under cwd or home while preserving exact relative paths."""

    raw_path = str(path)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return raw_path

    cwd = cwd.expanduser().resolve()
    try:
        return f"./{candidate.resolve().relative_to(cwd)}"
    except ValueError:
        pass

    home = Path.home().resolve()
    try:
        return f"~/{candidate.resolve().relative_to(home)}"
    except ValueError:
        return raw_path
