"""Code indexing boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from active_knowledge_server.config.schema import ActiveKnowledgeConfig
from active_knowledge_server.connectors.workspace import (
    FileInventoryEntry,
    WorkspaceConnector,
    WorkspaceInventory,
    WorkspaceWarning,
)
from active_knowledge_server.indexing.parallel import (
    parallel_map_ordered,
    resolve_indexing_workers,
)
from active_knowledge_server.indexing.progress import (
    IndexProgressCallback,
    IndexProgressEvent,
    noop_progress_callback,
    utc_timestamp,
)
from active_knowledge_server.indexing.snapshot import CURRENT_SNAPSHOT_ID
from active_knowledge_server.parsers.ctags import (
    CodeParseWarning,
    ParsedCodeComment,
    ParsedCodeFile,
    ParsedCodeSymbol,
    parse_c_family_file,
)
from active_knowledge_server.parsers.makefiles import (
    MakefileParseWarning,
    ParsedBuildModule,
    ParsedMakefile,
    parse_makefile,
)
from active_knowledge_server.security.secret_scan import SecretScanner
from active_knowledge_server.storage import (
    ALL_SCOPE,
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    RelationRecord,
    SourceRecord,
    StorageWriter,
)

CODE_INDEXER_SCHEMA_VERSION: Final = "code_indexer.v1"
_WORKSPACE_SOURCE_ID: Final = "workspace"
_WORKSPACE_LANGUAGE_SET: Final[frozenset[str]] = frozenset(
    {"c", "cpp", "c-header", "cpp-header", "makefile"}
)
_C_FAMILY_LANGUAGE_SET: Final[frozenset[str]] = frozenset({"c", "cpp", "c-header", "cpp-header"})
_SYMBOL_ENTITY_TYPE: Final[dict[str, str]] = {
    "function": "Function",
    "macro": "Macro",
    "type": "Type",
}
_RELATION_CONFIDENCE: Final[dict[str, float]] = {
    "contains.directory_file": 0.96,
    "contains.directory_module": 0.92,
    "contains.module_file": 0.91,
    "belongs_to_module": 0.91,
    "guarded_by_macro": 0.88,
}


@dataclass(frozen=True)
class CodeIndexingWarning:
    """Structured non-fatal warning emitted by the code indexer."""

    code: str
    message: str
    relative_path: str
    level: str = "caution"
    details: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable warning."""

        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "relative_path": self.relative_path,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class IndexedCode:
    """Collected code-index records ready for storage writes."""

    schema_version: str
    snapshot_id: str
    workspace_inventory: WorkspaceInventory
    source_records: tuple[SourceRecord, ...]
    file_records: tuple[FileRecord, ...]
    chunk_records: tuple[ChunkRecord, ...]
    entity_records: tuple[EntityRecord, ...]
    relation_records: tuple[RelationRecord, ...]
    evidence_records: tuple[EvidenceRecord, ...]
    warnings: tuple[CodeIndexingWarning, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable summary."""

        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "inventory_hash": self.workspace_inventory.inventory_hash,
            "source_count": len(self.source_records),
            "file_count": len(self.file_records),
            "chunk_count": len(self.chunk_records),
            "entity_count": len(self.entity_records),
            "relation_count": len(self.relation_records),
            "evidence_count": len(self.evidence_records),
            "warnings": [warning.to_dict() for warning in self.warnings],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class _CollectedCodeEntry:
    """Records and parser outputs collected from one workspace file."""

    relative_path: str
    file_record: FileRecord
    text: str
    parsed_code: ParsedCodeFile | None = None
    parsed_makefile: ParsedMakefile | None = None
    warnings: tuple[CodeIndexingWarning, ...] = ()


class CodeIndexer:
    """Parse workspace structure into code entities, chunks, and relations."""

    def __init__(
        self,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        workspace_connector: WorkspaceConnector | None = None,
        secret_scanner: SecretScanner | None = None,
    ) -> None:
        self._config = config
        self._cwd = (cwd or Path.cwd()).expanduser()
        self._connector = workspace_connector or WorkspaceConnector.from_config(
            config, cwd=self._cwd
        )
        self._secret_scanner = secret_scanner or SecretScanner.from_config(config)

    @classmethod
    def from_config(
        cls,
        config: ActiveKnowledgeConfig,
        *,
        cwd: Path | None = None,
        workspace_connector: WorkspaceConnector | None = None,
        secret_scanner: SecretScanner | None = None,
    ) -> CodeIndexer:
        """Build a code indexer from validated runtime config."""

        return cls(
            config,
            cwd=cwd,
            workspace_connector=workspace_connector,
            secret_scanner=secret_scanner,
        )

    def collect(
        self,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        workspace_inventory: WorkspaceInventory | None = None,
        include_paths: Sequence[str] | None = None,
        progress_callback: IndexProgressCallback | None = None,
    ) -> IndexedCode:
        """Collect code entities, chunks, relations, and evidence without persisting them."""

        callback = progress_callback or noop_progress_callback
        inventory = workspace_inventory or self._connector.scan()
        warnings: list[CodeIndexingWarning] = [
            _warning_from_workspace_warning(item) for item in inventory.warnings
        ]
        workspace_root = Path(inventory.workspace_root)
        include = None if include_paths is None else set(include_paths)

        indexed_entries = tuple(
            entry
            for entry in inventory.files
            if entry.language in _WORKSPACE_LANGUAGE_SET
            and (include is None or entry.relative_path in include)
        )
        physical_file_records: dict[str, FileRecord] = {}
        file_texts: dict[str, str] = {}
        code_parses: dict[str, ParsedCodeFile] = {}
        makefile_parses: dict[str, ParsedMakefile] = {}
        stage_total = len(indexed_entries)
        started_at = utc_timestamp()
        workers = resolve_indexing_workers(
            self._config.indexing.workers,
            task_count=stage_total,
            phase="code",
        )

        def report_progress(path: str, done: int) -> None:
            callback(
                IndexProgressEvent(
                    phase="code_collect",
                    stage_total=stage_total,
                    stage_done=done,
                    current_path=path,
                    message="Collecting code files",
                    warnings_count=len(warnings),
                    started_at=started_at,
                    updated_at=utc_timestamp(),
                )
            )

        results = parallel_map_ordered(
            indexed_entries,
            key=lambda entry: entry.relative_path,
            mapper=lambda entry: self._collect_code_entry(
                snapshot_id=snapshot_id,
                workspace_root=workspace_root,
                entry=entry,
            ),
            workers=workers,
            callback=report_progress,
        )
        for result in results:
            if result.error is not None:
                warnings.append(
                    CodeIndexingWarning(
                        code="code_indexer.collect_failed",
                        message="A code file failed during collect and was skipped.",
                        relative_path=result.key,
                        details={"error": str(result.error)},
                    )
                )
                continue
            if result.value is None:
                continue
            physical_file_records[result.value.relative_path] = result.value.file_record
            file_texts[result.value.relative_path] = result.value.text
            if result.value.parsed_code is not None:
                code_parses[result.value.relative_path] = result.value.parsed_code
            if result.value.parsed_makefile is not None:
                makefile_parses[result.value.relative_path] = result.value.parsed_makefile
            warnings.extend(result.value.warnings)
        collected_entries = tuple(
            entry for entry in indexed_entries if entry.relative_path in physical_file_records
        )

        directory_anchor_records: dict[str, FileRecord] = {}
        for relative_path in physical_file_records:
            directory_path = _parent_directory(relative_path)
            if directory_path is None:
                continue
            directory_anchor_records[directory_path] = _make_directory_anchor_file_record(
                snapshot_id,
                directory_path,
            )
        for parsed_makefile in makefile_parses.values():
            for module in parsed_makefile.modules:
                if module.module_path:
                    directory_anchor_records.setdefault(
                        module.module_path,
                        _make_directory_anchor_file_record(snapshot_id, module.module_path),
                    )

        source_record = SourceRecord(
            source_id=_WORKSPACE_SOURCE_ID,
            source_type="workspace",
            display_name=f"workspace:{Path(inventory.workspace_root).name}",
            root_path=inventory.workspace_root,
            revision=inventory.inventory_hash,
            metadata={
                "inventory_hash": inventory.inventory_hash,
                "repository_count": len(inventory.repositories),
                "indexed_file_count": len(indexed_entries),
            },
        )

        all_file_records = {
            **physical_file_records,
            **{path: record for path, record in directory_anchor_records.items()},
        }

        entity_records: list[EntityRecord] = []
        relation_records: list[RelationRecord] = []
        chunk_records: list[ChunkRecord] = []
        evidence_records: list[EvidenceRecord] = []

        directory_entity_ids: dict[str, str] = {}
        file_entity_ids: dict[str, str] = {}
        macro_guard_entity_ids: dict[tuple[str, str], str] = {}

        for directory_path in sorted(directory_anchor_records):
            directory_file = directory_anchor_records[directory_path]
            directory_entity = EntityRecord(
                entity_id=_stable_id("entity", "Directory", directory_path),
                snapshot_id=snapshot_id,
                file_id=directory_file.file_id,
                entity_type="Directory",
                name=Path(directory_path).name,
                qualified_name=directory_path,
                path=directory_path,
                source_scope=_source_scope_for_path(directory_path),
                profile_id=ALL_SCOPE,
                metadata={
                    "summary": f"Directory {directory_path}",
                    "synthetic_anchor": True,
                },
            )
            directory_entity_ids[directory_path] = directory_entity.entity_id
            entity_records.append(directory_entity)

        for entry in collected_entries:
            file_record = physical_file_records[entry.relative_path]
            parsed_code = code_parses.get(entry.relative_path)
            parsed_makefile = makefile_parses.get(entry.relative_path)
            file_summary = _file_summary(entry.relative_path, parsed_code, parsed_makefile)
            file_entity = EntityRecord(
                entity_id=_stable_id("entity", "File", entry.relative_path),
                snapshot_id=snapshot_id,
                file_id=file_record.file_id,
                entity_type="File",
                name=Path(entry.relative_path).name,
                qualified_name=entry.relative_path,
                path=entry.relative_path,
                source_scope=file_record.source_scope,
                profile_id=ALL_SCOPE,
                metadata={
                    "summary": file_summary,
                    "language": entry.language,
                    "area": entry.area,
                    "is_symlink": entry.is_symlink,
                },
            )
            file_entity_ids[entry.relative_path] = file_entity.entity_id
            entity_records.append(file_entity)

            directory_path = _parent_directory(entry.relative_path)
            if directory_path is not None and directory_path in directory_entity_ids:
                relation_records.append(
                    _make_relation(
                        snapshot_id=snapshot_id,
                        relation_type="contains",
                        src_entity_id=directory_entity_ids[directory_path],
                        dst_entity_id=file_entity.entity_id,
                        source_scope=file_record.source_scope,
                        extractor="workspace_inventory",
                        confidence=_RELATION_CONFIDENCE["contains.directory_file"],
                    )
                )

            if parsed_code is None:
                continue

            ordinal = 0
            if parsed_code.file_header is not None and parsed_code.file_header.text.strip():
                file_header_chunk = ChunkRecord(
                    chunk_id=_stable_id("chunk", file_record.file_id, "file_header"),
                    snapshot_id=snapshot_id,
                    file_id=file_record.file_id,
                    content_hash=_hash_text(parsed_code.file_header.text),
                    chunk_type="code.file_header",
                    ordinal=ordinal,
                    text=parsed_code.file_header.text,
                    source_scope=file_record.source_scope,
                    profile_id=ALL_SCOPE,
                    start_line=parsed_code.file_header.start_line,
                    end_line=parsed_code.file_header.end_line,
                    metadata={
                        "path": entry.relative_path,
                        "language": parsed_code.language,
                        "extractor": parsed_code.file_header.extractor,
                        "confidence": parsed_code.file_header.confidence,
                        "include_targets": list(parsed_code.file_header.include_targets),
                        "macro_names": list(parsed_code.file_header.macro_names),
                    },
                )
                chunk_records.append(file_header_chunk)
                evidence_records.append(
                    _make_chunk_evidence(
                        snapshot_id=snapshot_id,
                        object_id=file_entity.entity_id,
                        file_record=file_record,
                        chunk_record=file_header_chunk,
                        secret_scanner=self._secret_scanner,
                        citation_title=entry.relative_path,
                    )
                )
                ordinal += 1

            comments = tuple(parsed_code.comments)
            for symbol in parsed_code.symbols:
                if symbol.symbol_kind != "macro" and not symbol.is_definition:
                    continue
                symbol_entity = EntityRecord(
                    entity_id=_stable_id(
                        "entity",
                        entry.relative_path,
                        symbol.symbol_kind,
                        symbol.name,
                        symbol.start_line,
                    ),
                    snapshot_id=snapshot_id,
                    file_id=file_record.file_id,
                    entity_type=_SYMBOL_ENTITY_TYPE[symbol.symbol_kind],
                    name=symbol.name,
                    qualified_name=f"{entry.relative_path}::{symbol.name}",
                    path=f"{entry.relative_path}#{symbol.name}",
                    source_scope=file_record.source_scope,
                    profile_id=ALL_SCOPE,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    metadata={
                        "summary": symbol.signature or f"{symbol.symbol_kind} {symbol.name}",
                        "symbol_kind": symbol.symbol_kind,
                        "extractor": symbol.extractor,
                        "confidence": symbol.confidence,
                        "is_definition": symbol.is_definition,
                        "signature": symbol.signature,
                        "raw_kind": symbol.raw_kind,
                    },
                )
                entity_records.append(symbol_entity)
                symbol_text, start_line, end_line = _build_symbol_chunk(
                    file_texts[entry.relative_path],
                    symbol,
                    comments,
                )
                symbol_chunk = ChunkRecord(
                    chunk_id=_stable_id(
                        "chunk",
                        file_record.file_id,
                        symbol.symbol_kind,
                        symbol.name,
                        symbol.start_line,
                    ),
                    snapshot_id=snapshot_id,
                    file_id=file_record.file_id,
                    content_hash=_hash_text(symbol_text),
                    chunk_type=f"code.{symbol.symbol_kind}",
                    ordinal=ordinal,
                    text=symbol_text,
                    source_scope=file_record.source_scope,
                    profile_id=ALL_SCOPE,
                    start_line=start_line,
                    end_line=end_line,
                    metadata={
                        "path": entry.relative_path,
                        "entity_id": symbol_entity.entity_id,
                        "symbol_kind": symbol.symbol_kind,
                        "extractor": symbol.extractor,
                        "confidence": symbol.confidence,
                        "signature": symbol.signature,
                    },
                )
                chunk_records.append(symbol_chunk)
                relation_records.append(
                    _make_relation(
                        snapshot_id=snapshot_id,
                        relation_type="defines",
                        src_entity_id=file_entity.entity_id,
                        dst_entity_id=symbol_entity.entity_id,
                        source_scope=file_record.source_scope,
                        extractor=symbol.extractor,
                        confidence=symbol.confidence,
                        start_line=symbol.start_line,
                        end_line=symbol.end_line,
                    )
                )
                evidence_records.append(
                    _make_chunk_evidence(
                        snapshot_id=snapshot_id,
                        object_id=symbol_entity.entity_id,
                        file_record=file_record,
                        chunk_record=symbol_chunk,
                        secret_scanner=self._secret_scanner,
                        citation_title=f"{entry.relative_path} > {symbol.name}",
                    )
                )
                ordinal += 1

        for makefile_path, parsed_makefile in makefile_parses.items():
            makefile_file_record = physical_file_records[makefile_path]
            makefile_entity_id = file_entity_ids[makefile_path]
            makefile_text = file_texts[makefile_path]
            for module in parsed_makefile.modules:
                module_entity = EntityRecord(
                    entity_id=_stable_id(
                        "entity",
                        "Module",
                        makefile_path,
                        module.name,
                        module.module_path,
                    ),
                    snapshot_id=snapshot_id,
                    file_id=makefile_file_record.file_id,
                    entity_type="Module",
                    name=module.name,
                    qualified_name=module.logical_name or module.name,
                    path=f"{makefile_path}#module:{module.name}",
                    source_scope=_source_scope_for_path(
                        module.module_path, fallback=makefile_file_record.source_scope
                    ),
                    profile_id=ALL_SCOPE,
                    start_line=module.start_line,
                    end_line=module.end_line,
                    metadata={
                        "summary": _module_summary(module),
                        "aliases": []
                        if module.logical_name is None or module.logical_name == module.name
                        else [module.logical_name],
                        "module_path": module.module_path,
                        "makefile_path": module.makefile_path,
                        "definition_kind": module.definition_kind,
                        "components": list(module.components),
                        "config_paths": list(module.config_paths),
                        "condition_macros": list(module.condition_macros),
                    },
                )
                entity_records.append(module_entity)
                module_excerpt = _line_excerpt(makefile_text, module.start_line, module.end_line)
                evidence_records.append(
                    EvidenceRecord(
                        evidence_id=_stable_id("evidence", "module", module_entity.entity_id),
                        snapshot_id=snapshot_id,
                        object_type="entity",
                        object_id=module_entity.entity_id,
                        file_id=makefile_file_record.file_id,
                        source_scope=module_entity.source_scope,
                        profile_id=ALL_SCOPE,
                        chunk_id=None,
                        excerpt=self._secret_scanner.sanitize_excerpt(
                            _summary_from_text(module_excerpt, limit=220)
                        ),
                        citation_label=f"{makefile_path}:{module.start_line}",
                        start_line=module.start_line,
                        end_line=module.end_line,
                        metadata={
                            "path": makefile_path,
                            "entity_type": "Module",
                            "title": module.name,
                        },
                    )
                )
                relation_records.append(
                    _make_relation(
                        snapshot_id=snapshot_id,
                        relation_type="defines",
                        src_entity_id=makefile_entity_id,
                        dst_entity_id=module_entity.entity_id,
                        source_scope=module_entity.source_scope,
                        extractor="makefile_parser",
                        confidence=0.89,
                        start_line=module.start_line,
                        end_line=module.end_line,
                    )
                )

                if module.module_path in directory_entity_ids:
                    relation_records.append(
                        _make_relation(
                            snapshot_id=snapshot_id,
                            relation_type="contains",
                            src_entity_id=directory_entity_ids[module.module_path],
                            dst_entity_id=module_entity.entity_id,
                            source_scope=module_entity.source_scope,
                            extractor="makefile_parser",
                            confidence=_RELATION_CONFIDENCE["contains.directory_module"],
                            start_line=module.start_line,
                            end_line=module.end_line,
                        )
                    )

                for parsed_file in module.files:
                    target_file_id = file_entity_ids.get(parsed_file.path)
                    if target_file_id is None:
                        warnings.append(
                            CodeIndexingWarning(
                                code="code_indexer.file_missing",
                                message=(
                                    "Build module references a file that is not present in the "
                                    "indexed workspace inventory."
                                ),
                                relative_path=makefile_path,
                                details={
                                    "module": module.name,
                                    "target_file": parsed_file.path,
                                    "line_number": parsed_file.line_number,
                                },
                            )
                        )
                        continue
                    relation_records.append(
                        _make_relation(
                            snapshot_id=snapshot_id,
                            relation_type="contains",
                            src_entity_id=module_entity.entity_id,
                            dst_entity_id=target_file_id,
                            source_scope=module_entity.source_scope,
                            extractor="makefile_parser",
                            confidence=_RELATION_CONFIDENCE["contains.module_file"],
                            start_line=parsed_file.line_number,
                            end_line=parsed_file.line_number,
                            condition_expr=parsed_file.condition_expr,
                            condition_macros=parsed_file.condition_macros,
                        )
                    )
                    relation_records.append(
                        _make_relation(
                            snapshot_id=snapshot_id,
                            relation_type="belongs_to_module",
                            src_entity_id=target_file_id,
                            dst_entity_id=module_entity.entity_id,
                            source_scope=module_entity.source_scope,
                            extractor="makefile_parser",
                            confidence=_RELATION_CONFIDENCE["belongs_to_module"],
                            start_line=parsed_file.line_number,
                            end_line=parsed_file.line_number,
                            condition_expr=parsed_file.condition_expr,
                            condition_macros=parsed_file.condition_macros,
                        )
                    )
                    for macro in parsed_file.condition_macros:
                        macro_entity_id = _ensure_guard_macro_entity(
                            macro_guard_entity_ids,
                            entity_records,
                            snapshot_id=snapshot_id,
                            file_record=makefile_file_record,
                            anchor_path=makefile_path,
                            macro_name=macro,
                        )
                        relation_records.append(
                            _make_relation(
                                snapshot_id=snapshot_id,
                                relation_type="guarded_by_macro",
                                src_entity_id=target_file_id,
                                dst_entity_id=macro_entity_id,
                                source_scope=module_entity.source_scope,
                                extractor="makefile_parser",
                                confidence=_RELATION_CONFIDENCE["guarded_by_macro"],
                                start_line=parsed_file.line_number,
                                end_line=parsed_file.line_number,
                                condition_expr=parsed_file.condition_expr,
                                condition_macros=(macro,),
                            )
                        )

                for relation in module.relations:
                    if relation.relation_type != "guarded_by_macro":
                        continue
                    macro_entity_id = _ensure_guard_macro_entity(
                        macro_guard_entity_ids,
                        entity_records,
                        snapshot_id=snapshot_id,
                        file_record=makefile_file_record,
                        anchor_path=makefile_path,
                        macro_name=relation.target,
                    )
                    relation_records.append(
                        _make_relation(
                            snapshot_id=snapshot_id,
                            relation_type="guarded_by_macro",
                            src_entity_id=module_entity.entity_id,
                            dst_entity_id=macro_entity_id,
                            source_scope=module_entity.source_scope,
                            extractor="makefile_parser",
                            confidence=_RELATION_CONFIDENCE["guarded_by_macro"],
                            start_line=relation.line_number,
                            end_line=relation.line_number,
                            condition_expr=relation.condition_expr,
                            condition_macros=relation.condition_macros,
                        )
                    )

        return IndexedCode(
            schema_version=CODE_INDEXER_SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            workspace_inventory=inventory,
            source_records=(source_record,),
            file_records=tuple(_sorted_file_records(all_file_records.values())),
            chunk_records=tuple(chunk_records),
            entity_records=tuple(entity_records),
            relation_records=tuple(relation_records),
            evidence_records=tuple(evidence_records),
            warnings=tuple(warnings),
            metadata={"collect_workers": workers.to_dict()},
        )

    def _collect_code_entry(
        self,
        *,
        snapshot_id: str,
        workspace_root: Path,
        entry: FileInventoryEntry,
    ) -> _CollectedCodeEntry:
        absolute_path = workspace_root / entry.relative_path
        text = absolute_path.read_text(encoding="utf-8", errors="replace")
        file_record = FileRecord(
            file_id=_stable_id("file", snapshot_id, entry.relative_path),
            snapshot_id=snapshot_id,
            source_id=_WORKSPACE_SOURCE_ID,
            relative_path=entry.relative_path,
            content_hash=entry.content_hash or _hash_text(text),
            source_scope=_source_scope_for_path(entry.relative_path, fallback=entry.area),
            profile_id=ALL_SCOPE,
            language=entry.language,
            metadata={
                "area": entry.area,
                "indexed_kind": "physical",
                "is_symlink": entry.is_symlink,
            },
        )
        warnings: list[CodeIndexingWarning] = []
        parsed_code = None
        parsed_makefile = None
        if entry.language in _C_FAMILY_LANGUAGE_SET:
            parsed_code = parse_c_family_file(
                absolute_path,
                text,
                compile_db_path=None,
                prefer_ctags=self._config.indexing.code.enable_ctags,
            )
            warnings.extend(
                _warning_from_code_parse_warning(entry.relative_path, warning)
                for warning in parsed_code.warnings
            )
        elif entry.language == "makefile":
            sibling_paths = ()
            try:
                sibling_paths = tuple(child.name for child in absolute_path.parent.iterdir())
            except OSError as exc:
                warnings.append(
                    CodeIndexingWarning(
                        code="code_indexer.sibling_scan_failed",
                        message=f"Failed to enumerate makefile siblings: {exc}",
                        relative_path=entry.relative_path,
                        details={"path": entry.relative_path},
                    )
                )
            parsed_makefile = parse_makefile(
                Path(entry.relative_path),
                text,
                sibling_paths=sibling_paths,
            )
            warnings.extend(
                _warning_from_makefile_parse_warning(entry.relative_path, warning)
                for warning in parsed_makefile.warnings
            )
        return _CollectedCodeEntry(
            relative_path=entry.relative_path,
            file_record=file_record,
            text=text,
            parsed_code=parsed_code,
            parsed_makefile=parsed_makefile,
            warnings=tuple(warnings),
        )

    def collect_and_store(
        self,
        writer: StorageWriter,
        *,
        snapshot_id: str = CURRENT_SNAPSHOT_ID,
        workspace_inventory: WorkspaceInventory | None = None,
        progress_callback: IndexProgressCallback | None = None,
    ) -> IndexedCode:
        """Collect code records and persist them through the metadata writer."""

        indexed = self.collect(
            snapshot_id=snapshot_id,
            workspace_inventory=workspace_inventory,
            progress_callback=progress_callback,
        )
        for record in indexed.source_records:
            writer.upsert_source(record)
        for record in indexed.file_records:
            writer.upsert_file(record)
        for record in indexed.chunk_records:
            writer.upsert_chunk(record)
        for record in indexed.entity_records:
            writer.upsert_entity(record)
        for record in indexed.relation_records:
            writer.upsert_relation(record)
        for record in indexed.evidence_records:
            writer.upsert_evidence(record)
        writer.flush()
        return indexed


def count_indexable_workspace_files(
    inventory: WorkspaceInventory,
    *,
    include_paths: Sequence[str] | None = None,
) -> int:
    """Return the number of workspace files scanned by the code indexer."""

    include = None if include_paths is None else set(include_paths)
    return sum(
        1
        for entry in inventory.files
        if entry.language in _WORKSPACE_LANGUAGE_SET
        and (include is None or entry.relative_path in include)
    )


def _warning_from_workspace_warning(warning: WorkspaceWarning) -> CodeIndexingWarning:
    return CodeIndexingWarning(
        code=warning.code,
        message=warning.message,
        relative_path=warning.display_path,
        details=warning.details,
    )


def _warning_from_code_parse_warning(
    relative_path: str,
    warning: CodeParseWarning,
) -> CodeIndexingWarning:
    details = dict(warning.details)
    if warning.line_number is not None:
        details["line_number"] = warning.line_number
    return CodeIndexingWarning(
        code=warning.code,
        message=warning.message,
        relative_path=relative_path,
        details=details,
    )


def _warning_from_makefile_parse_warning(
    relative_path: str,
    warning: MakefileParseWarning,
) -> CodeIndexingWarning:
    details = dict(warning.details)
    if warning.line_number is not None:
        details["line_number"] = warning.line_number
    return CodeIndexingWarning(
        code=warning.code,
        message=warning.message,
        relative_path=relative_path,
        details=details,
    )


def _source_scope_for_path(path: str, *, fallback: str | None = None) -> str:
    normalized = path.strip("/")
    if not normalized:
        return fallback or ALL_SCOPE
    head = normalized.split("/", maxsplit=1)[0]
    return head or fallback or ALL_SCOPE


def _parent_directory(relative_path: str) -> str | None:
    parent = Path(relative_path).parent.as_posix()
    if parent in {"", "."}:
        return None
    return parent


def _make_directory_anchor_file_record(snapshot_id: str, directory_path: str) -> FileRecord:
    return FileRecord(
        file_id=_stable_id("file", snapshot_id, "directory", directory_path),
        snapshot_id=snapshot_id,
        source_id=_WORKSPACE_SOURCE_ID,
        relative_path=directory_path,
        content_hash=_hash_jsonable({"directory_path": directory_path}),
        source_scope=_source_scope_for_path(directory_path),
        profile_id=ALL_SCOPE,
        language="directory",
        metadata={
            "indexed_kind": "synthetic_directory_anchor",
            "synthetic_anchor": True,
        },
    )


def _sorted_file_records(records: Sequence[FileRecord]) -> list[FileRecord]:
    return sorted(records, key=lambda record: (record.relative_path, record.file_id))


def _file_summary(
    relative_path: str,
    parsed_code: ParsedCodeFile | None,
    parsed_makefile: ParsedMakefile | None,
) -> str:
    if parsed_code is not None and parsed_code.file_header is not None:
        return _summary_from_text(parsed_code.file_header.text)
    if parsed_makefile is not None and parsed_makefile.modules:
        names = ", ".join(module.name for module in parsed_makefile.modules[:3])
        return f"Build file for modules: {names}"
    return relative_path


def _module_summary(module: ParsedBuildModule) -> str:
    file_count = len(module.files)
    if module.condition_macros:
        return (
            f"Build module {module.name} in {module.module_path} with {file_count} files; "
            f"guarded by {', '.join(module.condition_macros)}."
        )
    return f"Build module {module.name} in {module.module_path} with {file_count} files."


def _build_symbol_chunk(
    text: str,
    symbol: ParsedCodeSymbol,
    comments: Sequence[ParsedCodeComment],
) -> tuple[str, int, int]:
    start_line = symbol.start_line
    end_line = symbol.end_line
    for comment in comments:
        if comment.end_line == symbol.start_line - 1:
            start_line = min(start_line, comment.start_line)
            break
    chunk_text = _line_excerpt(text, start_line, end_line)
    return chunk_text, start_line, end_line


def _line_excerpt(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    start_index = max(start_line - 1, 0)
    end_index = min(end_line, len(lines))
    excerpt = "\n".join(lines[start_index:end_index]).strip()
    return excerpt or "\n".join(lines[start_index:end_index])


def _make_chunk_evidence(
    *,
    snapshot_id: str,
    object_id: str,
    file_record: FileRecord,
    chunk_record: ChunkRecord,
    secret_scanner: SecretScanner,
    citation_title: str,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=_stable_id("evidence", object_id, chunk_record.chunk_id),
        snapshot_id=snapshot_id,
        object_type="entity",
        object_id=object_id,
        file_id=file_record.file_id,
        source_scope=file_record.source_scope,
        profile_id=ALL_SCOPE,
        chunk_id=chunk_record.chunk_id,
        excerpt=secret_scanner.sanitize_excerpt(_summary_from_text(chunk_record.text, limit=220)),
        citation_label=f"{citation_title}:{chunk_record.start_line}",
        start_line=chunk_record.start_line,
        end_line=chunk_record.end_line,
        metadata={
            "path": file_record.relative_path,
            "chunk_type": chunk_record.chunk_type,
            "title": citation_title,
        },
    )


def _make_relation(
    *,
    snapshot_id: str,
    relation_type: str,
    src_entity_id: str,
    dst_entity_id: str,
    source_scope: str,
    extractor: str,
    confidence: float,
    start_line: int | None = None,
    end_line: int | None = None,
    condition_expr: str | None = None,
    condition_macros: Sequence[str] = (),
) -> RelationRecord:
    metadata: dict[str, object] = {
        "extractor": extractor,
        "confidence": confidence,
    }
    if start_line is not None:
        metadata["start_line"] = start_line
    if end_line is not None:
        metadata["end_line"] = end_line
    if condition_expr is not None:
        metadata["condition_expr"] = condition_expr
    if condition_macros:
        metadata["condition_macros"] = list(condition_macros)
    return RelationRecord(
        relation_id=_stable_id(
            "relation",
            relation_type,
            src_entity_id,
            dst_entity_id,
            start_line,
            end_line,
            condition_expr,
            tuple(condition_macros),
        ),
        snapshot_id=snapshot_id,
        relation_type=relation_type,
        src_entity_id=src_entity_id,
        dst_entity_id=dst_entity_id,
        source_scope=source_scope,
        profile_id=ALL_SCOPE,
        metadata=metadata,
    )


def _ensure_guard_macro_entity(
    macro_guard_entity_ids: dict[tuple[str, str], str],
    entity_records: list[EntityRecord],
    *,
    snapshot_id: str,
    file_record: FileRecord,
    anchor_path: str,
    macro_name: str,
) -> str:
    key = (anchor_path, macro_name)
    existing = macro_guard_entity_ids.get(key)
    if existing is not None:
        return existing
    entity = EntityRecord(
        entity_id=_stable_id("entity", anchor_path, "guard_macro", macro_name),
        snapshot_id=snapshot_id,
        file_id=file_record.file_id,
        entity_type="Macro",
        name=macro_name,
        qualified_name=f"{anchor_path}::{macro_name}",
        path=f"{anchor_path}#macro:{macro_name}",
        source_scope=file_record.source_scope,
        profile_id=ALL_SCOPE,
        metadata={
            "summary": f"Build guard macro {macro_name}",
            "macro_role": "guard",
            "extractor": "makefile_parser",
        },
    )
    entity_records.append(entity)
    macro_guard_entity_ids[key] = entity.entity_id
    return entity.entity_id


def _summary_from_text(text: str | None, *, limit: int = 180) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _hash_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _hash_jsonable(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
