from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from active_knowledge_server.config.loader import resolve_config
from active_knowledge_server.indexing.embeddings import EmbeddingInput, prepare_embedding_inputs
from active_knowledge_server.security.secret_scan import (
    REDACTED_SECRET_MARKER,
    SecretScanner,
)


def test_secret_scan_detects_credentials_private_keys_and_redacts_excerpt() -> None:
    scanner = SecretScanner()
    text = dedent(
        """\
        password = hunter2
        -----BEGIN PRIVATE KEY-----
        MIIEvQIBADANBgkqhkiG9w0BAQEFAASC...
        -----END PRIVATE KEY-----
        """
    )

    result = scanner.scan_text(text, source_path=Path("configs/secret.env"))
    redacted = scanner.sanitize_excerpt(text)
    payload = json.dumps(result.to_report_entry().to_dict(), ensure_ascii=True, sort_keys=True)

    assert result.skip_embedding is True
    assert {match.secret_kind for match in result.matches} == {"credential", "private_key"}
    assert "hunter2" not in redacted
    assert "MIIEvQIBADAN" not in redacted
    assert REDACTED_SECRET_MARKER in redacted
    assert "hunter2" not in payload
    assert "MIIEvQIBADAN" not in payload


def test_secret_scan_detects_tokens_and_certificates() -> None:
    scanner = SecretScanner()
    text = dedent(
        """\
        Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456
        -----BEGIN CERTIFICATE-----
        MIIC+DCCAeCgAwIBAgIJAKDk5examplecertificate
        -----END CERTIFICATE-----
        """
    )

    result = scanner.scan_text(text, source_path="auth/secrets.txt")
    redacted = scanner.sanitize_excerpt(text)

    assert result.skip_embedding is True
    assert {match.secret_kind for match in result.matches} == {"token", "certificate"}
    assert "abcdefghijklmnopqrstuvwxyz123456" not in redacted
    assert "MIIC+DCCAeCgAwIBAgI" not in redacted
    assert "[REDACTED CERTIFICATE BLOCK]" in redacted


def test_secret_scan_supports_configured_deny_patterns(tmp_path: Path) -> None:
    config = resolve_config(
        cli_overrides={"security": {"secret_scan": {"deny_patterns": [r"ZEPP_[A-Z0-9]{8}"]}}},
        env={},
        cwd=tmp_path,
    ).model
    scanner = SecretScanner.from_config(config)

    result = scanner.scan_text("#define BUILD_SEED ZEPP_DEADBEEF", source_path="build_seed.h")
    redacted = scanner.sanitize_excerpt("#define BUILD_SEED ZEPP_DEADBEEF")

    assert result.skip_embedding is True
    assert [match.detector_id for match in result.matches] == ["custom.1"]
    assert "ZEPP_DEADBEEF" not in redacted


def test_prepare_embedding_inputs_skips_secret_bearing_items() -> None:
    scanner = SecretScanner()
    prepared = prepare_embedding_inputs(
        (
            EmbeddingInput(
                object_id="chunk-safe",
                object_type="chunk",
                source_path="src/main.c",
                content="int main(void) { return 0; }",
            ),
            EmbeddingInput(
                object_id="chunk-secret",
                object_type="chunk",
                source_path="config/.env",
                content="TOKEN=super-secret-value",
            ),
        ),
        secret_scanner=scanner,
    )
    payload = json.dumps(prepared.to_dict(), ensure_ascii=True, sort_keys=True)

    assert [item.object_id for item in prepared.accepted_inputs] == ["chunk-safe"]
    assert [report.source_path for report in prepared.skipped_reports] == ["config/.env"]
    assert "super-secret-value" not in payload


def test_prepare_embedding_inputs_keeps_all_items_when_scan_disabled() -> None:
    prepared = prepare_embedding_inputs(
        (
            EmbeddingInput(
                object_id="chunk-secret",
                object_type="chunk",
                source_path="config/.env",
                content="TOKEN=super-secret-value",
            ),
        ),
        secret_scanner=SecretScanner(enabled=False),
    )

    assert [item.object_id for item in prepared.accepted_inputs] == ["chunk-secret"]
    assert prepared.skipped_reports == ()