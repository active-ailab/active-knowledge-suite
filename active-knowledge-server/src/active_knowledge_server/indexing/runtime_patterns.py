"""Heuristic runtime-pattern extraction over collected code entities and chunks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from active_knowledge_server.storage import (
    ALL_SCOPE,
    ChunkRecord,
    EntityRecord,
    EvidenceRecord,
    FileRecord,
    RelationRecord,
)

RUNTIME_PATTERN_SCHEMA_VERSION = "runtime_pattern_extractor.v1"

_TASK_CREATE_APIS: dict[str, int] = {
    "osThreadNew": 0,
    "xTaskCreate": 0,
    "xTaskCreateStatic": 0,
}
_QUEUE_SEND_APIS: dict[str, int] = {
    "osMessageQueuePut": 0,
    "osMessageQueuePutHead": 0,
    "xQueueSend": 0,
    "xQueueSendToBack": 0,
    "xQueueSendToFront": 0,
    "xQueueOverwrite": 0,
}
_QUEUE_RECEIVE_APIS: dict[str, int] = {
    "osMessageQueueGet": 0,
    "xQueueReceive": 0,
    "xQueuePeek": 0,
}
_SEMAPHORE_WAIT_APIS: dict[str, int] = {
    "osSemaphoreAcquire": 0,
    "xSemaphoreTake": 0,
    "xSemaphoreTakeRecursive": 0,
}
_SEMAPHORE_SIGNAL_APIS: dict[str, int] = {
    "osSemaphoreRelease": 0,
    "xSemaphoreGive": 0,
    "xSemaphoreGiveRecursive": 0,
    "xSemaphoreGiveFromISR": 0,
}
_EVENT_WAIT_APIS: dict[str, int] = {
    "osEventFlagsWait": 0,
    "xEventGroupWaitBits": 0,
}
_EVENT_SIGNAL_APIS: dict[str, int] = {
    "osEventFlagsSet": 0,
    "xEventGroupSetBits": 0,
    "xEventGroupSetBitsFromISR": 0,
}
_TIMER_CREATE_APIS: dict[str, int] = {
    "osTimerNew": 0,
    "osTimerNewNamed": 1,
    "osTimerNewWithName": 0,
    "xTimerCreate": 4,
}
_TIMER_START_APIS: dict[str, int] = {
    "osTimerStart": 0,
    "xTimerStart": 0,
    "xTimerStartFromISR": 0,
}
_RUNTIME_CALL_APIS = {
    **_TASK_CREATE_APIS,
    **_QUEUE_SEND_APIS,
    **_QUEUE_RECEIVE_APIS,
    **_SEMAPHORE_WAIT_APIS,
    **_SEMAPHORE_SIGNAL_APIS,
    **_EVENT_WAIT_APIS,
    **_EVENT_SIGNAL_APIS,
    **_TIMER_CREATE_APIS,
    **_TIMER_START_APIS,
}
_RUNTIME_API_PATTERN = re.compile(
    r"\b("
    + "|".join(sorted((re.escape(name) for name in _RUNTIME_CALL_APIS), key=len, reverse=True))
    + r")\s*\("
)
_ISR_NAME_RE = re.compile(
    r"(?:^|_)(?:[A-Za-z0-9]+)?(?:IRQHandler|IRQ_Handler|ISR|InterruptHandler|Fault_Handler)$"
)
_FAULT_TEXT_RE = re.compile(
    (
        r"\b(hard[_ ]fault|bus[_ ]fault|mem(?:ory|_manage)?[_ ]fault|"
        r"usage[_ ]fault|panic|vector fetch)\b"
    ),
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPRESSION_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\s*(?:->|\.)\s*[A-Za-z_][A-Za-z0-9_]*)*"
)
_CONFIDENCE_BY_RELATION_TYPE = {
    "creates_task": 0.94,
    "runs_in_context": 0.9,
    "posts_to_queue": 0.92,
    "waits_on_queue": 0.92,
    "waits_on_semaphore": 0.91,
    "signals_semaphore": 0.91,
    "waits_on_event": 0.9,
    "signals_event": 0.9,
    "creates_timer": 0.93,
    "starts_timer": 0.89,
    "triggers": 0.93,
    "mapped_to_vector": 0.84,
    "reports_fault": 0.87,
}


@dataclass(frozen=True)
class IndexedRuntimePatterns:
    """Runtime entities, relations, and evidence inferred from code patterns."""

    schema_version: str
    entity_records: tuple[EntityRecord, ...]
    relation_records: tuple[RelationRecord, ...]
    evidence_records: tuple[EvidenceRecord, ...]


@dataclass(frozen=True)
class _FunctionContext:
    entity: EntityRecord
    chunk: ChunkRecord
    file: FileRecord
    scan_text: str
    scan_start_line: int


@dataclass(frozen=True)
class _CallSite:
    api_name: str
    args: tuple[str, ...]
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int


class RuntimePatternExtractor:
    """Extract runtime-specific graph nodes and relations from function chunks."""

    def collect(
        self,
        *,
        snapshot_id: str,
        file_records: Sequence[FileRecord],
        file_texts: Mapping[str, str],
        entity_records: Sequence[EntityRecord],
        chunk_records: Sequence[ChunkRecord],
    ) -> IndexedRuntimePatterns:
        file_by_id = {record.file_id: record for record in file_records}
        function_contexts = _build_function_contexts(
            file_by_id=file_by_id,
            file_texts=file_texts,
            entity_records=entity_records,
            chunk_records=chunk_records,
        )
        functions_by_name = _index_functions_by_name(function_contexts)

        runtime_entities: dict[str, EntityRecord] = {}
        runtime_relations: dict[str, RelationRecord] = {}
        runtime_evidence: dict[str, EvidenceRecord] = {}

        for context in function_contexts:
            text = context.scan_text
            path = context.file.relative_path
            source_scope = context.entity.source_scope
            chunk_base_line = context.scan_start_line

            for call in _extract_call_sites(
                text,
                context.scan_start_line,
            ):
                if call.api_name in _TASK_CREATE_APIS:
                    callback_expr = _argument_at(call.args, _TASK_CREATE_APIS[call.api_name])
                    if callback_expr is None:
                        continue
                    callback_name = _normalize_identifier(callback_expr)
                    task_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Task",
                        file_record=context.file,
                        anchor_path=path,
                        name=callback_name or _normalize_expression(callback_expr) or "task_entry",
                        metadata={
                            "summary": f"Task entry created via {call.api_name}",
                            "runtime_kind": "task",
                            "callback_expr": callback_expr,
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="creates_task",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=task_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )
                    callback_entity = _resolve_function_entity(
                        callback_name,
                        current_path=path,
                        functions_by_name=functions_by_name,
                    )
                    if callback_entity is not None:
                        _append_relation_with_evidence(
                            relations=runtime_relations,
                            evidence=runtime_evidence,
                            snapshot_id=snapshot_id,
                            relation_type="runs_in_context",
                            src_entity_id=callback_entity.entity_id,
                            dst_entity_id=task_entity.entity_id,
                            file_record=context.file,
                            function_chunk=context.chunk,
                            source_scope=source_scope,
                            extractor="runtime_pattern_extractor",
                            runtime_api=call.api_name,
                            start_line=call.start_line,
                            end_line=call.end_line,
                            excerpt=_excerpt_from_chunk(
                                context.chunk.text,
                                context.chunk.start_line or 1,
                                call,
                            ),
                        )
                elif call.api_name in _QUEUE_SEND_APIS:
                    queue_expr = _argument_at(call.args, _QUEUE_SEND_APIS[call.api_name])
                    queue_name = _normalize_expression(queue_expr)
                    if queue_name is None:
                        continue
                    queue_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Queue",
                        file_record=context.file,
                        anchor_path=path,
                        name=queue_name,
                        metadata={
                            "summary": f"Queue endpoint referenced via {call.api_name}",
                            "runtime_kind": "queue",
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="posts_to_queue",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=queue_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )
                elif call.api_name in _QUEUE_RECEIVE_APIS:
                    queue_expr = _argument_at(call.args, _QUEUE_RECEIVE_APIS[call.api_name])
                    queue_name = _normalize_expression(queue_expr)
                    if queue_name is None:
                        continue
                    queue_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Queue",
                        file_record=context.file,
                        anchor_path=path,
                        name=queue_name,
                        metadata={
                            "summary": f"Queue endpoint referenced via {call.api_name}",
                            "runtime_kind": "queue",
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="waits_on_queue",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=queue_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )
                elif call.api_name in _SEMAPHORE_WAIT_APIS:
                    sem_expr = _argument_at(call.args, _SEMAPHORE_WAIT_APIS[call.api_name])
                    sem_name = _normalize_expression(sem_expr)
                    if sem_name is None:
                        continue
                    sem_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Semaphore",
                        file_record=context.file,
                        anchor_path=path,
                        name=sem_name,
                        metadata={
                            "summary": f"Semaphore endpoint referenced via {call.api_name}",
                            "runtime_kind": "semaphore",
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="waits_on_semaphore",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=sem_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )
                elif call.api_name in _SEMAPHORE_SIGNAL_APIS:
                    sem_expr = _argument_at(call.args, _SEMAPHORE_SIGNAL_APIS[call.api_name])
                    sem_name = _normalize_expression(sem_expr)
                    if sem_name is None:
                        continue
                    sem_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Semaphore",
                        file_record=context.file,
                        anchor_path=path,
                        name=sem_name,
                        metadata={
                            "summary": f"Semaphore endpoint referenced via {call.api_name}",
                            "runtime_kind": "semaphore",
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="signals_semaphore",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=sem_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )
                elif call.api_name in _EVENT_WAIT_APIS:
                    event_expr = _argument_at(call.args, _EVENT_WAIT_APIS[call.api_name])
                    event_name = _normalize_expression(event_expr)
                    if event_name is None:
                        continue
                    event_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Event",
                        file_record=context.file,
                        anchor_path=path,
                        name=event_name,
                        metadata={
                            "summary": f"Event endpoint referenced via {call.api_name}",
                            "runtime_kind": "event",
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="waits_on_event",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=event_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )
                elif call.api_name in _EVENT_SIGNAL_APIS:
                    event_expr = _argument_at(call.args, _EVENT_SIGNAL_APIS[call.api_name])
                    event_name = _normalize_expression(event_expr)
                    if event_name is None:
                        continue
                    event_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Event",
                        file_record=context.file,
                        anchor_path=path,
                        name=event_name,
                        metadata={
                            "summary": f"Event endpoint referenced via {call.api_name}",
                            "runtime_kind": "event",
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="signals_event",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=event_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )
                elif call.api_name in _TIMER_CREATE_APIS:
                    callback_expr = _argument_at(call.args, _TIMER_CREATE_APIS[call.api_name])
                    callback_name = _normalize_identifier(callback_expr)
                    timer_name = (
                        _infer_timer_name(context.chunk.text, call) or callback_name or "timer"
                    )
                    timer_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Timer",
                        file_record=context.file,
                        anchor_path=path,
                        name=timer_name,
                        metadata={
                            "summary": f"Timer created via {call.api_name}",
                            "runtime_kind": "timer",
                            "callback_expr": callback_expr,
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="creates_timer",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=timer_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )
                    callback_entity = _resolve_function_entity(
                        callback_name,
                        current_path=path,
                        functions_by_name=functions_by_name,
                    )
                    if callback_entity is not None:
                        _append_relation_with_evidence(
                            relations=runtime_relations,
                            evidence=runtime_evidence,
                            snapshot_id=snapshot_id,
                            relation_type="triggers",
                            src_entity_id=timer_entity.entity_id,
                            dst_entity_id=callback_entity.entity_id,
                            file_record=context.file,
                            function_chunk=context.chunk,
                            source_scope=source_scope,
                            extractor="runtime_pattern_extractor",
                            runtime_api=call.api_name,
                            start_line=call.start_line,
                            end_line=call.end_line,
                            excerpt=_excerpt_from_chunk(
                                context.chunk.text,
                                chunk_base_line,
                                call,
                            ),
                        )
                elif call.api_name in _TIMER_START_APIS:
                    timer_expr = _argument_at(call.args, _TIMER_START_APIS[call.api_name])
                    timer_name = _normalize_expression(timer_expr)
                    if timer_name is None:
                        continue
                    timer_entity = _ensure_runtime_entity(
                        runtime_entities,
                        snapshot_id=snapshot_id,
                        entity_type="Timer",
                        file_record=context.file,
                        anchor_path=path,
                        name=timer_name,
                        metadata={
                            "summary": f"Timer referenced via {call.api_name}",
                            "runtime_kind": "timer",
                            "extractor": "runtime_pattern_extractor",
                        },
                    )
                    _append_relation_with_evidence(
                        relations=runtime_relations,
                        evidence=runtime_evidence,
                        snapshot_id=snapshot_id,
                        relation_type="starts_timer",
                        src_entity_id=context.entity.entity_id,
                        dst_entity_id=timer_entity.entity_id,
                        file_record=context.file,
                        function_chunk=context.chunk,
                        source_scope=source_scope,
                        extractor="runtime_pattern_extractor",
                        runtime_api=call.api_name,
                        start_line=call.start_line,
                        end_line=call.end_line,
                        excerpt=_excerpt_from_chunk(context.chunk.text, chunk_base_line, call),
                    )

            if _looks_like_isr(context.entity.name):
                isr_entity = _ensure_runtime_entity(
                    runtime_entities,
                    snapshot_id=snapshot_id,
                    entity_type="ISR",
                    file_record=context.file,
                    anchor_path=path,
                    name=context.entity.name,
                    metadata={
                        "summary": f"Interrupt handler {context.entity.name}",
                        "runtime_kind": "isr",
                        "extractor": "runtime_pattern_extractor",
                    },
                )
                _append_relation_with_evidence(
                    relations=runtime_relations,
                    evidence=runtime_evidence,
                    snapshot_id=snapshot_id,
                    relation_type="runs_in_context",
                    src_entity_id=context.entity.entity_id,
                    dst_entity_id=isr_entity.entity_id,
                    file_record=context.file,
                    function_chunk=context.chunk,
                    source_scope=source_scope,
                    extractor="runtime_pattern_extractor",
                    runtime_api="interrupt_handler",
                    start_line=context.chunk.start_line,
                    end_line=context.chunk.start_line,
                    excerpt=_signature_excerpt(context.chunk.text),
                )
                vector_entity = _ensure_runtime_entity(
                    runtime_entities,
                    snapshot_id=snapshot_id,
                    entity_type="Vector",
                    file_record=context.file,
                    anchor_path=path,
                    name=context.entity.name,
                    metadata={
                        "summary": f"Interrupt vector for {context.entity.name}",
                        "runtime_kind": "vector",
                        "extractor": "runtime_pattern_extractor",
                    },
                )
                _append_relation_with_evidence(
                    relations=runtime_relations,
                    evidence=runtime_evidence,
                    snapshot_id=snapshot_id,
                    relation_type="mapped_to_vector",
                    src_entity_id=isr_entity.entity_id,
                    dst_entity_id=vector_entity.entity_id,
                    file_record=context.file,
                    function_chunk=context.chunk,
                    source_scope=source_scope,
                    extractor="runtime_pattern_extractor",
                    runtime_api="interrupt_handler",
                    start_line=context.chunk.start_line,
                    end_line=context.chunk.start_line,
                    excerpt=_signature_excerpt(context.chunk.text),
                )

            for fault_name, start_line, end_line, excerpt in _fault_matches(context):
                fault_entity = _ensure_runtime_entity(
                    runtime_entities,
                    snapshot_id=snapshot_id,
                    entity_type="Fault",
                    file_record=context.file,
                    anchor_path=path,
                    name=fault_name,
                    metadata={
                        "summary": f"Fault handling path for {fault_name}",
                        "runtime_kind": "fault",
                        "extractor": "runtime_pattern_extractor",
                    },
                )
                _append_relation_with_evidence(
                    relations=runtime_relations,
                    evidence=runtime_evidence,
                    snapshot_id=snapshot_id,
                    relation_type="reports_fault",
                    src_entity_id=context.entity.entity_id,
                    dst_entity_id=fault_entity.entity_id,
                    file_record=context.file,
                    function_chunk=context.chunk,
                    source_scope=source_scope,
                    extractor="runtime_pattern_extractor",
                    runtime_api="fault_pattern",
                    start_line=start_line,
                    end_line=end_line,
                    excerpt=excerpt,
                )

        return IndexedRuntimePatterns(
            schema_version=RUNTIME_PATTERN_SCHEMA_VERSION,
            entity_records=tuple(
                sorted(
                    runtime_entities.values(),
                    key=lambda record: (record.entity_type, record.path),
                )
            ),
            relation_records=tuple(
                sorted(runtime_relations.values(), key=lambda record: record.relation_id)
            ),
            evidence_records=tuple(
                sorted(runtime_evidence.values(), key=lambda record: record.evidence_id)
            ),
        )


def _build_function_contexts(
    *,
    file_by_id: Mapping[str, FileRecord],
    file_texts: Mapping[str, str],
    entity_records: Sequence[EntityRecord],
    chunk_records: Sequence[ChunkRecord],
) -> tuple[_FunctionContext, ...]:
    entities_by_id = {
        record.entity_id: record
        for record in entity_records
        if record.entity_type == "Function"
    }
    contexts: list[_FunctionContext] = []
    for chunk in chunk_records:
        if chunk.chunk_type != "code.function":
            continue
        entity_id = chunk.metadata.get("entity_id")
        if not isinstance(entity_id, str):
            continue
        entity = entities_by_id.get(entity_id)
        if entity is None:
            continue
        file_record = file_by_id.get(entity.file_id)
        if file_record is None:
            continue
        full_text = file_texts.get(file_record.relative_path)
        scan_text = chunk.text
        scan_start_line = chunk.start_line or entity.start_line or 1
        if full_text is not None:
            scan_text, scan_start_line = _expand_function_body(
                full_text,
                start_line=entity.start_line or scan_start_line,
                end_line=entity.end_line or scan_start_line,
            )
        contexts.append(
            _FunctionContext(
                entity=entity,
                chunk=chunk,
                file=file_record,
                scan_text=scan_text,
                scan_start_line=scan_start_line,
            )
        )
    return tuple(sorted(contexts, key=lambda item: item.entity.path))


def _index_functions_by_name(
    contexts: Sequence[_FunctionContext],
) -> Mapping[str, tuple[_FunctionContext, ...]]:
    indexed: dict[str, list[_FunctionContext]] = {}
    for context in contexts:
        indexed.setdefault(context.entity.name, []).append(context)
    return {key: tuple(value) for key, value in indexed.items()}


def _resolve_function_entity(
    callback_name: str | None,
    *,
    current_path: str,
    functions_by_name: Mapping[str, tuple[_FunctionContext, ...]],
) -> EntityRecord | None:
    if callback_name is None:
        return None
    candidates = functions_by_name.get(callback_name)
    if not candidates:
        return None
    same_file = [item for item in candidates if item.file.relative_path == current_path]
    if len(same_file) == 1:
        return same_file[0].entity
    if len(candidates) == 1:
        return candidates[0].entity
    return None


def _extract_call_sites(text: str, base_line: int) -> tuple[_CallSite, ...]:
    call_sites: list[_CallSite] = []
    for match in _RUNTIME_API_PATTERN.finditer(text):
        api_name = match.group(1)
        open_paren = text.find("(", match.start())
        if open_paren < 0:
            continue
        parsed = _parse_argument_list(text, open_paren)
        if parsed is None:
            continue
        args, end_offset = parsed
        start_line = base_line + text.count("\n", 0, match.start())
        end_line = start_line + text.count("\n", match.start(), end_offset)
        call_sites.append(
            _CallSite(
                api_name=api_name,
                args=args,
                start_offset=match.start(),
                end_offset=end_offset,
                start_line=start_line,
                end_line=end_line,
            )
        )
    return tuple(call_sites)


def _expand_function_body(full_text: str, *, start_line: int, end_line: int) -> tuple[str, int]:
    lines = full_text.splitlines()
    if not lines:
        return "", start_line
    start_index = max(start_line - 1, 0)
    open_seen = False
    depth = 0
    finish_index = min(max(end_line - 1, start_index), len(lines) - 1)
    for index in range(start_index, len(lines)):
        line = lines[index]
        for char in line:
            if char == "{":
                depth += 1
                open_seen = True
            elif char == "}" and open_seen:
                depth -= 1
                if depth == 0:
                    finish_index = index
                    return "\n".join(lines[start_index : finish_index + 1]), start_index + 1
        if open_seen:
            finish_index = index
    return "\n".join(lines[start_index : finish_index + 1]), start_index + 1


def _parse_argument_list(text: str, open_paren: int) -> tuple[tuple[str, ...], int] | None:
    depth = 0
    current: list[str] = []
    args: list[str] = []
    index = open_paren
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote is not None:
            current.append(char)
            if char == quote and text[index - 1] != "\\":
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            index += 1
            continue
        if char == "(":
            depth += 1
            if depth > 1:
                current.append(char)
            index += 1
            continue
        if char == ")":
            depth -= 1
            if depth == 0:
                arg = "".join(current).strip()
                if arg:
                    args.append(arg)
                return tuple(args), index + 1
            current.append(char)
            index += 1
            continue
        if char == "," and depth == 1:
            args.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    return None


def _argument_at(args: Sequence[str], index: int) -> str | None:
    if 0 <= index < len(args):
        value = args[index].strip()
        return value or None
    return None


def _ensure_runtime_entity(
    cache: dict[str, EntityRecord],
    *,
    snapshot_id: str,
    entity_type: str,
    file_record: FileRecord,
    anchor_path: str,
    name: str,
    metadata: Mapping[str, object],
) -> EntityRecord:
    entity_id = _stable_id("entity", "runtime", entity_type, anchor_path, name)
    existing = cache.get(entity_id)
    if existing is not None:
        return existing
    entity = EntityRecord(
        entity_id=entity_id,
        snapshot_id=snapshot_id,
        file_id=file_record.file_id,
        entity_type=entity_type,
        name=name,
        qualified_name=f"{anchor_path}::{entity_type}:{name}",
        path=f"{anchor_path}#runtime:{entity_type.lower()}:{name}",
        source_scope=file_record.source_scope,
        profile_id=ALL_SCOPE,
        metadata=dict(metadata),
    )
    cache[entity_id] = entity
    return entity


def _append_relation_with_evidence(
    *,
    relations: dict[str, RelationRecord],
    evidence: dict[str, EvidenceRecord],
    snapshot_id: str,
    relation_type: str,
    src_entity_id: str,
    dst_entity_id: str,
    file_record: FileRecord,
    function_chunk: ChunkRecord,
    source_scope: str,
    extractor: str,
    runtime_api: str,
    start_line: int | None,
    end_line: int | None,
    excerpt: str,
) -> None:
    relation_id = _stable_id(
        "relation",
        relation_type,
        src_entity_id,
        dst_entity_id,
        runtime_api,
        start_line,
        end_line,
    )
    evidence_id = _stable_id("evidence", "relation", relation_id)
    confidence = _CONFIDENCE_BY_RELATION_TYPE[relation_type]
    relations[relation_id] = RelationRecord(
        relation_id=relation_id,
        snapshot_id=snapshot_id,
        relation_type=relation_type,
        src_entity_id=src_entity_id,
        dst_entity_id=dst_entity_id,
        source_scope=source_scope,
        profile_id=ALL_SCOPE,
        metadata={
            "extractor": extractor,
            "schema_version": RUNTIME_PATTERN_SCHEMA_VERSION,
            "confidence": confidence,
            "evidence_id": evidence_id,
            "runtime_api": runtime_api,
            "start_line": start_line,
            "end_line": end_line,
        },
    )
    evidence[evidence_id] = EvidenceRecord(
        evidence_id=evidence_id,
        snapshot_id=snapshot_id,
        object_type="relation",
        object_id=relation_id,
        file_id=file_record.file_id,
        source_scope=file_record.source_scope,
        profile_id=ALL_SCOPE,
        chunk_id=function_chunk.chunk_id,
        excerpt=_summary_from_text(excerpt, limit=220),
        citation_label=f"{file_record.relative_path}:{start_line}",
        start_line=start_line,
        end_line=end_line,
        metadata={
            "path": file_record.relative_path,
            "title": relation_type,
            "runtime_api": runtime_api,
        },
    )


def _looks_like_isr(name: str) -> bool:
    return bool(_ISR_NAME_RE.search(name))


def _fault_matches(context: _FunctionContext) -> tuple[tuple[str, int, int, str], ...]:
    results: list[tuple[str, int, int, str]] = []
    seen: set[str] = set()
    if "fault" in context.entity.name.lower() or "panic" in context.entity.name.lower():
        normalized = _normalize_fault_name(context.entity.name)
        results.append(
            (
                normalized,
                context.chunk.start_line or context.entity.start_line or 1,
                context.chunk.start_line or context.entity.start_line or 1,
                _signature_excerpt(context.chunk.text),
            )
        )
        seen.add(normalized)
    base_line = context.chunk.start_line or context.entity.start_line or 1
    for match in _FAULT_TEXT_RE.finditer(context.chunk.text):
        normalized = _normalize_fault_name(match.group(1))
        if normalized in seen:
            continue
        start_line = base_line + context.chunk.text.count("\n", 0, match.start())
        end_line = start_line + context.chunk.text.count("\n", match.start(), match.end())
        results.append(
            (
                normalized,
                start_line,
                end_line,
                _line_excerpt(context.chunk.text, base_line, start_line, end_line),
            )
        )
        seen.add(normalized)
    return tuple(results)


def _normalize_fault_name(name: str) -> str:
    lowered = name.strip().lower().replace(" ", "_")
    lowered = lowered.replace("__", "_")
    lowered = lowered.removesuffix("_handler").removesuffix("_track")
    return lowered


def _infer_timer_name(text: str, call: _CallSite) -> str | None:
    line = _line_excerpt(text, call.start_line, call.start_line, call.end_line)
    assignment = line.split("=", maxsplit=1)
    if len(assignment) < 2:
        return None
    return _normalize_expression(assignment[0])


def _normalize_expression(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    cleaned = re.sub(r"^\([^()]+\)\s*", "", cleaned)
    cleaned = cleaned.lstrip("&*")
    match = _EXPRESSION_RE.search(cleaned)
    if match is None:
        return None
    return re.sub(r"\s+", "", match.group(0))


def _normalize_identifier(value: str | None) -> str | None:
    normalized = _normalize_expression(value)
    if normalized is None:
        return None
    match = _IDENTIFIER_RE.fullmatch(normalized)
    if match is not None:
        return match.group(0)
    tokens = _IDENTIFIER_RE.findall(normalized)
    return tokens[-1] if tokens else None


def _excerpt_from_chunk(text: str, base_line: int, call: _CallSite) -> str:
    return _line_excerpt(text, base_line, call.start_line, call.end_line)


def _signature_excerpt(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def _line_excerpt(text: str, base_line: int, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    start_index = max(start_line - base_line, 0)
    end_index = min(end_line - base_line + 1, len(lines))
    excerpt = "\n".join(lines[start_index:end_index]).strip()
    if excerpt:
        return excerpt
    return "\n".join(lines[start_index:end_index])


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
