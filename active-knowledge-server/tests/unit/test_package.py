from __future__ import annotations

import subprocess
import sys

import active_knowledge_server
from active_knowledge_server.cli import main


def test_package_exposes_version() -> None:
    assert active_knowledge_server.__version__ == "0.1.0"


def test_cli_main_prints_version(capsys) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "active-knowledge-server 0.1.0" in captured.out


def test_module_entrypoint_prints_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "active_knowledge_server.cli", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "active-knowledge-server 0.1.0" in result.stdout
