from __future__ import annotations

import json
from pathlib import Path

import pytest

from active_knowledge_server.config.defaults import default_config
from active_knowledge_server.config.loader import ConfigError, load_yaml_config, resolve_config
from active_knowledge_server.config.schema import (
    safe_config_dump,
    shorten_path,
    validate_config_dict,
)


def test_example_configs_validate() -> None:
    repo_root = Path(__file__).resolve().parents[3]

    for example in ("local-single-user.yaml", "remote-shared.yaml"):
        data = load_yaml_config(repo_root / "examples" / example)
        config = validate_config_dict(data, source=example)

        assert config.config_schema_version == "0.1"
        assert config.project.workspace_root
        assert config.storage.overlay.path


def test_variable_expansion_uses_merged_values(tmp_path: Path) -> None:
    config_path = tmp_path / "active-kb.yaml"
    config_path.write_text(
        """
runtime:
  workdir: /var/tmp/active-kb-test
storage:
  overlay:
    path: ${runtime.local_dir}/db/custom-overlay.db
""",
        encoding="utf-8",
    )

    resolved = resolve_config(config_path=config_path, env={}, cwd=tmp_path)

    assert resolved.model.runtime.local_dir == "/var/tmp/active-kb-test/local"
    assert (
        resolved.model.storage.baseline.manifest == "/var/tmp/active-kb-test/baseline/manifest.json"
    )
    assert (
        resolved.model.storage.overlay.path == "/var/tmp/active-kb-test/local/db/custom-overlay.db"
    )


def test_unknown_variable_reports_actionable_error(tmp_path: Path) -> None:
    config_path = tmp_path / "active-kb.yaml"
    config_path.write_text(
        """
runtime:
  workdir: ${runtime.missing}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"unknown config variable \$\{runtime\.missing\}"):
        resolve_config(config_path=config_path, env={}, cwd=tmp_path)


def test_missing_required_field_error_names_dotted_path() -> None:
    bad = default_config()
    project = bad["project"]
    assert isinstance(project, dict)
    del project["workspace_root"]

    with pytest.raises(ValueError, match="project.workspace_root"):
        validate_config_dict(bad, source="unit test config")


def test_safe_config_dump_redacts_secret_scalars() -> None:
    data = default_config()
    server = data["server"]
    assert isinstance(server, dict)
    http = server["http"]
    assert isinstance(http, dict)
    http["token"] = {
        "env": "ACTIVE_KB_AUTH_TOKEN",
        "header": "Authorization",
        "scheme": "Bearer",
        "value": "super-secret-token",
    }

    config = validate_config_dict(data, source="secret fixture")
    dumped = safe_config_dump(config)

    assert json.dumps(dumped, sort_keys=True).find("super-secret-token") == -1
    assert dumped["server"]["http"]["token"]["env"] == "ACTIVE_KB_AUTH_TOKEN"
    assert dumped["server"]["http"]["token"]["value"] == "***REDACTED***"


def test_shorten_path_keeps_summary_readable(tmp_path: Path) -> None:
    nested = tmp_path / "project" / ".active-kb"

    assert shorten_path(nested, tmp_path) == "./project/.active-kb"
    assert shorten_path("relative/path", tmp_path) == "relative/path"
