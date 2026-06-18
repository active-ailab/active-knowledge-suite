"""Durable embedding input cache and shared batch materialization helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from active_knowledge_server.config.loader import resolve_runtime_path
from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.indexing.embeddings import (
    EMBEDDING_PREPARATION_SCHEMA_VERSION,
    EmbeddingInput,
    EmbeddingPreparationResult,
)
from active_knowledge_server.security.secret_scan import (
    SECRET_SCAN_SCHEMA_VERSION,
    SecretScanReportEntry,
    SecretScanner,
)

INDEX_EMBEDDING_CACHE_SCHEMA_VERSION: Final = "index_embedding_cache.v1"
EMBEDDING_CACHE_DISABLED_SANITIZER_VERSION: Final = "secret_scan.disabled"
_EMBEDDING_CACHE_DIRNAME: Final = "embeddings"


@dataclass(frozen=True)
class EmbeddingCacheStats:
    """Observable cache and batching counters for one embedding materialization run."""

    cache_hits: int = 0
    cache_misses: int = 0
    cache_stores: int = 0
    deduplicated_inputs: int = 0
    batch_count: int = 0
    computed_embeddings: int = 0
    skipped_inputs: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_stores": self.cache_stores,
            "deduplicated_inputs": self.deduplicated_inputs,
            "batch_count": self.batch_count,
            "computed_embeddings": self.computed_embeddings,
            "skipped_inputs": self.skipped_inputs,
        }


@dataclass(frozen=True)
class PreparedEmbeddings:
    """Prepared embedding inputs plus resolved vectors."""

    preparation: EmbeddingPreparationResult
    embeddings_by_object_id: Mapping[str, tuple[float, ...]]
    cache_stats: EmbeddingCacheStats


@dataclass(frozen=True)
class _EmbeddingCacheKey:
    provider: str
    model: str
    object_type: str
    content_hash: str
    sanitizer_version: str
    digest: str

    @property
    def provider_directory(self) -> str:
        return _safe_directory_name(self.provider)


@dataclass(frozen=True)
class _CachedScanDecision:
    finding_count: int
    reasons: tuple[str, ...]
    secret_kinds: tuple[str, ...]
    line_numbers: tuple[int, ...]
    skip_embedding: bool

    def to_report_entry(self, *, source_path: str) -> SecretScanReportEntry:
        return SecretScanReportEntry(
            source_path=source_path,
            finding_count=self.finding_count,
            reasons=self.reasons,
            secret_kinds=self.secret_kinds,
            line_numbers=self.line_numbers,
            skip_embedding=self.skip_embedding,
        )


@dataclass(frozen=True)
class _EmbeddingCacheEntry:
    skip_embedding: bool
    scan_decision: _CachedScanDecision | None
    embedding: tuple[float, ...] | None

    def to_report_entry(self, *, source_path: str) -> SecretScanReportEntry | None:
        if self.scan_decision is None:
            return None
        return self.scan_decision.to_report_entry(source_path=source_path)


@dataclass(frozen=True)
class _EmbeddingLookup:
    item: EmbeddingInput
    key: _EmbeddingCacheKey
    resolution: str


class IndexEmbeddingCacheStore:
    """Read and write one local embedding cache namespace."""

    def __init__(self, cache_root: Path) -> None:
        self._cache_root = cache_root.expanduser()

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path,
    ) -> IndexEmbeddingCacheStore:
        return cls(resolve_runtime_path(config.storage.cache_root, cwd))

    @property
    def embedding_root(self) -> Path:
        return self._cache_root / _EMBEDDING_CACHE_DIRNAME

    def load(self, key: _EmbeddingCacheKey) -> _EmbeddingCacheEntry | None:
        path = self._path_for_key(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload_without_hash = dict(payload)
        artifact_hash = payload_without_hash.pop("artifact_hash", None)
        if not isinstance(artifact_hash, str):
            return None
        if _stable_hash(payload_without_hash) != artifact_hash:
            return None
        if (
            payload.get("schema_version") != INDEX_EMBEDDING_CACHE_SCHEMA_VERSION
            or payload.get("provider") != key.provider
            or payload.get("model") != key.model
            or payload.get("object_type") != key.object_type
            or payload.get("content_hash") != key.content_hash
            or payload.get("sanitizer_version") != key.sanitizer_version
            or payload.get("cache_key_digest") != key.digest
        ):
            return None
        skip_embedding = bool(payload.get("skip_embedding", False))
        scan_payload = payload.get("scan_decision")
        scan_decision = _decode_scan_decision(scan_payload)
        embedding_payload = payload.get("embedding")
        embedding: tuple[float, ...] | None = None
        if embedding_payload is not None:
            if not isinstance(embedding_payload, Sequence) or isinstance(
                embedding_payload, (str, bytes, bytearray)
            ):
                return None
            embedding = tuple(float(item) for item in embedding_payload)
        if skip_embedding and scan_decision is None:
            return None
        if not skip_embedding and embedding is None:
            return None
        return _EmbeddingCacheEntry(
            skip_embedding=skip_embedding,
            scan_decision=scan_decision,
            embedding=embedding,
        )

    def save_skip(self, key: _EmbeddingCacheKey, report: SecretScanReportEntry) -> bool:
        return self._save(
            key,
            skip_embedding=True,
            scan_decision=_CachedScanDecision(
                finding_count=report.finding_count,
                reasons=tuple(report.reasons),
                secret_kinds=tuple(report.secret_kinds),
                line_numbers=tuple(report.line_numbers),
                skip_embedding=report.skip_embedding,
            ),
            embedding=None,
        )

    def save_embedding(self, key: _EmbeddingCacheKey, embedding: Sequence[float]) -> bool:
        return self._save(
            key,
            skip_embedding=False,
            scan_decision=None,
            embedding=tuple(float(item) for item in embedding),
        )

    def _save(
        self,
        key: _EmbeddingCacheKey,
        *,
        skip_embedding: bool,
        scan_decision: _CachedScanDecision | None,
        embedding: tuple[float, ...] | None,
    ) -> bool:
        path = self._path_for_key(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, object] = {
                "schema_version": INDEX_EMBEDDING_CACHE_SCHEMA_VERSION,
                "provider": key.provider,
                "model": key.model,
                "object_type": key.object_type,
                "content_hash": key.content_hash,
                "sanitizer_version": key.sanitizer_version,
                "cache_key_digest": key.digest,
                "skip_embedding": skip_embedding,
                "scan_decision": (
                    None if scan_decision is None else _encode_scan_decision(scan_decision)
                ),
                "embedding": None if embedding is None else list(embedding),
                "created_at": _utc_now(),
            }
            artifact_hash = _stable_hash(payload)
            payload["artifact_hash"] = artifact_hash
            encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
            temporary_path = path.with_name(f"{path.name}.tmp")
            temporary_path.write_text(encoded, encoding="utf-8")
            temporary_path.replace(path)
        except OSError:
            return False
        return True

    def _path_for_key(self, key: _EmbeddingCacheKey) -> Path:
        return (
            self.embedding_root
            / key.provider_directory
            / key.object_type
            / key.digest[:2]
            / f"{key.digest}.json"
        )


def prepare_cached_embeddings(
    inputs: Sequence[EmbeddingInput],
    *,
    model: str,
    provider: str,
    batch_size: int,
    embed_batch: Callable[[Sequence[EmbeddingInput]], Sequence[Sequence[float]]],
    secret_scanner: SecretScanner | None = None,
    cache_store: IndexEmbeddingCacheStore | None = None,
) -> PreparedEmbeddings:
    """Resolve embeddings through one shared cache + batcher pipeline."""

    sanitizer_version = (
        SECRET_SCAN_SCHEMA_VERSION
        if secret_scanner is not None and secret_scanner.enabled
        else EMBEDDING_CACHE_DISABLED_SANITIZER_VERSION
    )
    lookups: list[_EmbeddingLookup] = []
    resolved_entries: dict[str, _EmbeddingCacheEntry] = {}
    pending_by_digest: dict[str, list[_EmbeddingLookup]] = {}
    resolution_by_digest: dict[str, str] = {}
    deduplicated_inputs = 0

    for item in inputs:
        key = make_embedding_cache_key(
            provider=provider,
            model=model,
            object_type=item.object_type,
            content=item.content,
            sanitizer_version=sanitizer_version,
        )
        existing_resolution = resolution_by_digest.get(key.digest)
        if existing_resolution is not None:
            deduplicated_inputs += 1
            lookup = _EmbeddingLookup(item=item, key=key, resolution=existing_resolution)
            if existing_resolution == "pending":
                pending_by_digest[key.digest].append(lookup)
            lookups.append(lookup)
            continue
        cached = None if cache_store is None else cache_store.load(key)
        if cached is not None:
            resolved_entries[key.digest] = cached
            resolution_by_digest[key.digest] = "store"
            lookups.append(_EmbeddingLookup(item=item, key=key, resolution="store"))
            continue
        resolution_by_digest[key.digest] = "pending"
        lookup = _EmbeddingLookup(item=item, key=key, resolution="pending")
        pending_by_digest.setdefault(key.digest, []).append(lookup)
        lookups.append(lookup)

    cache_stores = 0
    batch_count = 0
    computed_embeddings = 0
    pending_embedding_lookups: list[_EmbeddingLookup] = []
    for group in pending_by_digest.values():
        representative = group[0]
        if secret_scanner is not None and secret_scanner.enabled:
            scan_result = secret_scanner.scan_text(
                representative.item.content,
                source_path=representative.item.source_path,
            )
            if scan_result.skip_embedding:
                entry = _EmbeddingCacheEntry(
                    skip_embedding=True,
                    scan_decision=_CachedScanDecision(
                        finding_count=len(scan_result.matches),
                        reasons=tuple(
                            dict.fromkeys(match.reason for match in scan_result.matches)
                        ),
                        secret_kinds=tuple(
                            dict.fromkeys(match.secret_kind for match in scan_result.matches)
                        ),
                        line_numbers=tuple(
                            dict.fromkeys(match.line_number for match in scan_result.matches)
                        ),
                        skip_embedding=scan_result.skip_embedding,
                    ),
                    embedding=None,
                )
                resolved_entries[representative.key.digest] = entry
                if cache_store is not None:
                    stored = cache_store.save_skip(
                        representative.key,
                        scan_result.to_report_entry(),
                    )
                    cache_stores += int(stored)
                continue
        pending_embedding_lookups.append(representative)

    for start in range(0, len(pending_embedding_lookups), max(batch_size, 1)):
        batch = pending_embedding_lookups[start : start + max(batch_size, 1)]
        if not batch:
            continue
        embeddings = tuple(embed_batch(tuple(item.item for item in batch)))
        if len(embeddings) != len(batch):
            raise ValueError("embed_batch must return one embedding per input")
        batch_count += 1
        computed_embeddings += len(batch)
        for lookup, embedding in zip(batch, embeddings, strict=False):
            normalized_embedding = tuple(float(item) for item in embedding)
            entry = _EmbeddingCacheEntry(
                skip_embedding=False,
                scan_decision=None,
                embedding=normalized_embedding,
            )
            resolved_entries[lookup.key.digest] = entry
            if cache_store is not None:
                stored = cache_store.save_embedding(lookup.key, normalized_embedding)
                cache_stores += int(stored)

    accepted_inputs: list[EmbeddingInput] = []
    skipped_reports: list[SecretScanReportEntry] = []
    embeddings_by_object_id: dict[str, tuple[float, ...]] = {}
    cache_hits = 0
    cache_misses = 0
    for lookup in lookups:
        entry = resolved_entries.get(lookup.key.digest)
        if entry is None:
            raise ValueError("embedding cache resolution did not produce an entry")
        if lookup.resolution == "store":
            cache_hits += 1
        else:
            cache_misses += 1
        if entry.skip_embedding:
            report = entry.to_report_entry(source_path=lookup.item.source_path)
            if report is not None:
                skipped_reports.append(report)
            continue
        if entry.embedding is None:
            raise ValueError("accepted embedding cache entry is missing embedding payload")
        accepted_inputs.append(lookup.item)
        embeddings_by_object_id[lookup.item.object_id] = entry.embedding

    return PreparedEmbeddings(
        preparation=EmbeddingPreparationResult(
            schema_version=EMBEDDING_PREPARATION_SCHEMA_VERSION,
            accepted_inputs=tuple(accepted_inputs),
            skipped_reports=tuple(skipped_reports),
        ),
        embeddings_by_object_id=embeddings_by_object_id,
        cache_stats=EmbeddingCacheStats(
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            cache_stores=cache_stores,
            deduplicated_inputs=deduplicated_inputs,
            batch_count=batch_count,
            computed_embeddings=computed_embeddings,
            skipped_inputs=len(skipped_reports),
        ),
    )


def make_embedding_cache_key(
    *,
    provider: str,
    model: str,
    object_type: str,
    content: str,
    sanitizer_version: str,
) -> _EmbeddingCacheKey:
    content_hash = _content_hash(content)
    payload = {
        "model": model,
        "object_type": object_type,
        "content_hash": content_hash,
        "sanitizer_version": sanitizer_version,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return _EmbeddingCacheKey(
        provider=provider,
        model=model,
        object_type=object_type,
        content_hash=content_hash,
        sanitizer_version=sanitizer_version,
        digest=digest,
    )


def _content_hash(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _decode_scan_decision(payload: object) -> _CachedScanDecision | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        return None
    return _CachedScanDecision(
        finding_count=int(payload.get("finding_count", 0) or 0),
        reasons=tuple(str(item) for item in payload.get("reasons", ())),
        secret_kinds=tuple(str(item) for item in payload.get("secret_kinds", ())),
        line_numbers=tuple(int(item) for item in payload.get("line_numbers", ())),
        skip_embedding=bool(payload.get("skip_embedding", True)),
    )


def _encode_scan_decision(scan_decision: _CachedScanDecision) -> dict[str, object]:
    return {
        "finding_count": scan_decision.finding_count,
        "reasons": list(scan_decision.reasons),
        "secret_kinds": list(scan_decision.secret_kinds),
        "line_numbers": list(scan_decision.line_numbers),
        "skip_embedding": scan_decision.skip_embedding,
    }


def _safe_directory_name(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    trimmed = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in value.strip()
    )
    trimmed = trimmed.strip("-")[:48] or "provider"
    return f"{trimmed}-{digest}"


def _stable_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
