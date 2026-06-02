from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import ConfigDict, resolve_config
from active_knowledge_server.indexing.jobs import (
    SQLiteJobStore,
    record_task_applied_checkpoint,
)
from active_knowledge_server.indexing.tasks import IndexTask
from active_knowledge_server.mcp import create_fastmcp_app
from active_knowledge_server.mcp.schemas import OPS_TOOL_NAMES
from active_knowledge_server.storage import ProfileRecord, SourceRecord, StorageWriteRequest
from active_knowledge_server.storage.sqlite_store import (
    SQLiteStorageAdapter,
    configured_sqlite_paths,
    migrate_sqlite_store,
)


def _resolved_config(
    tmp_path: Path,
    *,
    expose_ops_tools: bool = False,
    deployment_mode: str = "local_single_user",
) -> object:
    workspace = tmp_path / "workspace"
    source_docs = tmp_path / "knowledge-sources"
    workspace.mkdir()
    source_docs.mkdir()

    overrides: ConfigDict = {
        "deployment_mode": deployment_mode,
        "runtime": {
            "workdir": ".active-kb",
            "source_docs_root": "knowledge-sources",
        },
        "project": {
            "workspace_root": "workspace",
            "id": "active-test",
            "display_name": "Active Test",
        },
        "server": {
            "transport": "stdio",
            "expose_ops_tools": expose_ops_tools,
            "http": {
                "host": "127.0.0.1",
                "port": 8765,
                "mcp_path": "/mcp",
            },
        },
    }
    if deployment_mode == "remote_shared":
        overrides["server"] = {
            "transport": "streamable-http",
            "expose_ops_tools": expose_ops_tools,
            "http": {
                "host": "0.0.0.0",
                "port": 8765,
                "mcp_path": "/mcp",
                "require_auth": True,
                "auth_provider": "token",
                "token": {"env": "ACTIVE_KB_AUTH_TOKEN"},
                "allowed_origins": ["https://chatgpt.com"],
            },
        }
        overrides["security"] = {"audit": {"enabled": True}}
    return resolve_config(cli_overrides=overrides, cwd=tmp_path)


def _prepare_store(resolved: object, tmp_path: Path) -> SQLiteStorageAdapter:
    model = resolved.model
    paths = configured_sqlite_paths(model, cwd=tmp_path)
    migrate_sqlite_store(paths["baseline_metadata"], target="baseline_metadata")
    migrate_sqlite_store(paths["overlay_metadata"], target="overlay_metadata")
    migrate_sqlite_store(paths["jobs"], target="jobs")
    return SQLiteStorageAdapter.from_config(model, cwd=tmp_path)


def test_ops_tools_are_hidden_by_default(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path)

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}
    info = handlers["server_info"]()

    assert not set(OPS_TOOL_NAMES).intersection(runtime.inventory.tool_names)
    assert info.expose_ops_tools is False
    assert info.ops_tools == ()


def test_ops_tools_register_only_when_explicitly_enabled_locally(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path, expose_ops_tools=True)

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}
    info = handlers["server_info"]()

    assert runtime.inventory.tool_names[-len(OPS_TOOL_NAMES) :] == OPS_TOOL_NAMES
    assert info.expose_ops_tools is True
    assert info.ops_tools == OPS_TOOL_NAMES


def test_remote_shared_never_registers_ops_tools_even_if_requested(tmp_path: Path) -> None:
    resolved = _resolved_config(
        tmp_path,
        expose_ops_tools=True,
        deployment_mode="remote_shared",
    )

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}
    info = handlers["server_info"]()

    assert not set(OPS_TOOL_NAMES).intersection(runtime.inventory.tool_names)
    assert info.expose_ops_tools is False
    assert info.ops_tools == ()


def test_ops_validate_setup_reports_strict_blocking_when_local_state_is_missing(
    tmp_path: Path,
) -> None:
    resolved = _resolved_config(tmp_path, expose_ops_tools=True)

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    (tmp_path / "knowledge-sources").rmdir()
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}
    result = handlers["ops_validate_setup"](strict=True)

    assert result.status == "blocked"
    assert any(warning.code == "validation.source_docs_root" for warning in result.warnings)


def test_ops_tools_support_start_conflict_cancel_and_inventory_views(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path, expose_ops_tools=True)
    adapter = _prepare_store(resolved, tmp_path)
    writer = adapter.writer(StorageWriteRequest(target="overlay"))
    writer.upsert_profile(
        ProfileRecord(
            profile_record_id="profile:watch",
            snapshot_id="snapshot:1",
            profile_id="watch",
            defconfig_hash="def:1",
            dotconfig_hash="dot:1",
            defconfig_path="configs/watch_defconfig",
            dotconfig_path="build/.config",
            app="watch",
            board="mhs003",
            metadata={"profile_manifest_hash": "profile-manifest"},
        )
    )
    writer.upsert_source(
        SourceRecord(
            source_id="source:workspace",
            source_type="workspace",
            display_name="Workspace",
            root_path=str(tmp_path / "workspace"),
            revision="rev-1",
            metadata={"category": "code"},
        )
    )

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}

    first = handlers["ops_start_index"](
        mode="full",
        source="docs",
        profile_id="watch",
        snapshot_id="snapshot:1",
    )
    conflict = handlers["ops_start_index"]()
    job_id = str(first.payload["job"]["job_id"])
    profiles = handlers["ops_list_profiles"]()
    sources = handlers["ops_list_sources"]()
    status = handlers["ops_index_status"](limit=5)
    cancel = handlers["ops_cancel_index"](job_id)
    retry = handlers["ops_start_index"]()

    assert first.status == "accepted"
    assert first.payload["job"]["status"] == "pending"
    assert first.payload["job"]["snapshot_id"] == "snapshot:1"
    assert first.payload["job"]["profile_id"] == "watch"
    assert first.payload["job"]["metadata"]["schema_version"] == "index_job_contract.v1"
    assert first.payload["job"]["metadata"]["requested_target"] == "overlay"
    assert first.payload["job"]["metadata"]["resume_policy"]["mode"] == "auto"
    assert conflict.status == "conflict"
    assert profiles.status == "ok"
    assert profiles.items[0]["profile_id"] == "watch"
    assert sources.status == "ok"
    assert sources.items[0]["source_id"] == "source:workspace"
    assert status.status == "ok"
    assert status.items[0]["job_id"] == job_id
    assert status.items[0]["task_stats"]["tasks_applied"] == 0
    assert status.payload["job_status_counts"]["pending"] == 1
    assert cancel.status == "ok"
    assert cancel.payload["job"]["status"] == "failed"
    assert cancel.payload["job"]["metadata"]["cancelled"] is True
    assert retry.status == "accepted"
    assert retry.payload["job"]["job_id"] != job_id


def test_ops_index_status_aggregates_task_checkpoints(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path, expose_ops_tools=True)
    _prepare_store(resolved, tmp_path)

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}
    start = handlers["ops_start_index"]()
    job_id = str(start.payload["job"]["job_id"])

    paths = configured_sqlite_paths(resolved.model, cwd=tmp_path)
    store = SQLiteJobStore(paths["jobs"])
    record_task_applied_checkpoint(
        store,
        job_id,
        IndexTask(
            task_key="doc:apply:guide.md",
            phase="doc_apply",
            source_kind="docs",
            operation="apply",
            relative_path="guide.md",
            input_hash="hash:guide",
            schema_version="doc_indexer.v1",
        ),
    )

    status = handlers["ops_index_status"](limit=1)

    assert status.status == "ok"
    assert status.items[0]["task_stats"]["tasks_applied"] == 1
    assert status.items[0]["task_stats"]["applied_by_phase"] == {"doc_apply": 1}
    assert status.payload["task_status_counts"]["applied"] == 1


def test_ops_resume_index_retries_failed_job(tmp_path: Path) -> None:
    resolved = _resolved_config(tmp_path, expose_ops_tools=True)
    _prepare_store(resolved, tmp_path)

    runtime = create_fastmcp_app(resolved, cwd=tmp_path)
    handlers = {tool.name: tool.handler for tool in runtime.inventory.tools}
    start = handlers["ops_start_index"](resume="disabled")
    job_id = str(start.payload["job"]["job_id"])

    paths = configured_sqlite_paths(resolved.model, cwd=tmp_path)
    store = SQLiteJobStore(paths["jobs"])
    store.transition_job(job_id, "failed", error_summary="interrupted")

    resumed = handlers["ops_resume_index"](job_id)

    assert resumed.status == "accepted"
    assert resumed.payload["job"]["job_id"] == job_id
    assert resumed.payload["job"]["status"] == "pending"
    assert resumed.payload["job"]["metadata"]["resume_count"] == 1
    assert resumed.payload["job"]["metadata"]["resume_policy"]["mode"] == "job_id"
