from __future__ import annotations

from pathlib import Path

from active_knowledge_server.config.loader import resolve_config


def write_yaml(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_resolve_config_priority_cli_env_local_baseline_defaults(tmp_path: Path) -> None:
    baseline = write_yaml(
        tmp_path / "baseline.yaml",
        """
runtime:
  workdir: baseline-kb
project:
  workspace_root: /baseline/workspace
  default_profile: baseline_profile
server:
  transport: stdio
""",
    )
    local = write_yaml(
        tmp_path / "local.yaml",
        """
runtime:
  workdir: local-kb
project:
  workspace_root: /local/workspace
  default_profile: local_profile
server:
  transport: streamable-http
""",
    )

    resolved = resolve_config(
        config_path=baseline,
        local_config_path=local,
        cli_overrides={"project": {"workspace_root": "/cli/workspace"}},
        env={
            "ACTIVE_KB_WORKDIR": "env-kb",
            "ACTIVE_KB_PROFILE": "env_profile",
        },
        cwd=tmp_path,
    )

    assert resolved.get("project.workspace_root") == "/cli/workspace"
    assert resolved.get("runtime.workdir") == "env-kb"
    assert resolved.get("project.default_profile") == "env_profile"
    assert resolved.get("server.transport") == "streamable-http"
    assert resolved.get("query.default_top_k") == 12


def test_default_local_config_path_follows_cli_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "custom-kb"
    write_yaml(
        workdir / "local" / "config" / "active-kb.local.yaml",
        """
project:
  default_profile: local_from_cli_workdir
""",
    )

    resolved = resolve_config(
        cli_overrides={"runtime": {"workdir": str(workdir)}},
        env={},
        cwd=tmp_path,
    )

    assert resolved.local_config_path == workdir / "local" / "config" / "active-kb.local.yaml"
    assert resolved.get("project.default_profile") == "local_from_cli_workdir"
