"""Secret scanning boundary."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from active_knowledge_server.config.schema import ActiveKnowledgeConfig

SECRET_SCAN_SCHEMA_VERSION: Final = "secret_scan.v1"
REDACTED_SECRET_MARKER: Final = "***REDACTED_SECRET***"

SecretKind = Literal[
	"access_key",
	"certificate",
	"credential",
	"custom",
	"private_key",
	"token",
]


@dataclass(frozen=True)
class SecretScanMatch:
	"""One redacted secret finding without preserving the original sensitive value."""

	detector_id: str
	secret_kind: SecretKind
	line_number: int
	end_line: int
	reason: str

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable secret finding."""

		return {
			"detector_id": self.detector_id,
			"secret_kind": self.secret_kind,
			"line_number": self.line_number,
			"end_line": self.end_line,
			"reason": self.reason,
		}


@dataclass(frozen=True)
class SecretScanReportEntry:
	"""Machine-readable index report entry for one secret-bearing file or chunk."""

	source_path: str
	finding_count: int
	reasons: tuple[str, ...]
	secret_kinds: tuple[SecretKind, ...]
	line_numbers: tuple[int, ...]
	skip_embedding: bool = True

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable report entry without raw secret content."""

		return {
			"source_path": self.source_path,
			"finding_count": self.finding_count,
			"reasons": list(self.reasons),
			"secret_kinds": list(self.secret_kinds),
			"line_numbers": list(self.line_numbers),
			"skip_embedding": self.skip_embedding,
		}


@dataclass(frozen=True)
class SecretScanResult:
	"""One secret-scan decision for a file, chunk, or evidence excerpt."""

	schema_version: str
	source_path: str
	matches: tuple[SecretScanMatch, ...]
	skip_embedding: bool

	def to_dict(self) -> dict[str, object]:
		"""Return a JSON-serializable secret-scan result."""

		return {
			"schema_version": self.schema_version,
			"source_path": self.source_path,
			"finding_count": len(self.matches),
			"matches": [match.to_dict() for match in self.matches],
			"skip_embedding": self.skip_embedding,
		}

	def to_report_entry(self) -> SecretScanReportEntry:
		"""Return the safe index-report view for this scan result."""

		reasons = tuple(dict.fromkeys(match.reason for match in self.matches))
		secret_kinds = tuple(dict.fromkeys(match.secret_kind for match in self.matches))
		line_numbers = tuple(dict.fromkeys(match.line_number for match in self.matches))
		return SecretScanReportEntry(
			source_path=self.source_path,
			finding_count=len(self.matches),
			reasons=reasons,
			secret_kinds=secret_kinds,
			line_numbers=line_numbers,
			skip_embedding=self.skip_embedding,
		)


@dataclass(frozen=True)
class _DetectedSecret:
	start: int
	end: int
	replacement: str
	priority: int
	public_match: SecretScanMatch


@dataclass(frozen=True)
class _SecretRule:
	detector_id: str
	secret_kind: SecretKind
	reason: str
	pattern: re.Pattern[str]
	replacement: str | Callable[[re.Match[str]], str]
	priority: int
	predicate: Callable[[re.Match[str]], bool] | None = None

	def detect(self, text: str, line_starts: Sequence[int]) -> tuple[_DetectedSecret, ...]:
		matches: list[_DetectedSecret] = []
		for match in self.pattern.finditer(text):
			if self.predicate is not None and not self.predicate(match):
				continue
			replacement = self.replacement(match) if callable(self.replacement) else self.replacement
			start_line = _offset_to_line(line_starts, match.start())
			end_line = _offset_to_line(line_starts, max(match.end() - 1, match.start()))
			matches.append(
				_DetectedSecret(
					start=match.start(),
					end=match.end(),
					replacement=replacement,
					priority=self.priority,
					public_match=SecretScanMatch(
						detector_id=self.detector_id,
						secret_kind=self.secret_kind,
						line_number=start_line,
						end_line=end_line,
						reason=self.reason,
					),
				)
			)
		return tuple(matches)


_CREDENTIAL_ASSIGNMENT_RE: Final = re.compile(
	r"(?im)(?P<prefix>\b(?:api[_-]?key|password|passwd|pwd|secret|token|access_token|refresh_token|client_secret)\b\s*[:=]\s*)(?P<quote>['\"]?)(?P<value>[^'\"\s,;]{6,})(?P=quote)"
)
_BEARER_TOKEN_RE: Final = re.compile(
	r"(?i)\bbearer\s+(?P<value>[A-Za-z0-9._~+/\-=]{12,})"
)
_JWT_RE: Final = re.compile(
	r"\b(?P<value>eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
_ACCESS_KEY_RE: Final = re.compile(r"\b(?:A3T|AKIA|ASIA)[0-9A-Z]{16}\b")
_PRIVATE_KEY_RE: Final = re.compile(
	r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
	re.DOTALL,
)
_CERTIFICATE_RE: Final = re.compile(
	r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
	re.DOTALL,
)
_PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset(
	{
		"",
		"${token}",
		"${value}",
		"${secret}",
		"<password>",
		"<secret>",
		"<token>",
		"changeme",
		"example",
		"false",
		"none",
		"null",
		"password",
		"secret",
		"token",
		"true",
		"your_token_here",
		"your-secret",
	}
)


def _credential_value_is_sensitive(match: re.Match[str]) -> bool:
	value = match.group("value").strip().strip("'\"")
	lowered = value.lower()
	if lowered in _PLACEHOLDER_VALUES:
		return False
	if value.startswith(("${", "{{", "env(", "os.getenv(", "vault://")):
		return False
	return True


def _replace_credential_assignment(match: re.Match[str]) -> str:
	quote = match.group("quote")
	return f"{match.group('prefix')}{quote}{REDACTED_SECRET_MARKER}{quote}"


def _replace_bearer_token(match: re.Match[str]) -> str:
	token = match.group(0)
	prefix = token.split()[0]
	return f"{prefix} {REDACTED_SECRET_MARKER}"


_DEFAULT_RULES: Final[tuple[_SecretRule, ...]] = (
	_SecretRule(
		detector_id="private_key.pem",
		secret_kind="private_key",
		reason="Detected a PEM private key block.",
		pattern=_PRIVATE_KEY_RE,
		replacement="[REDACTED PRIVATE KEY BLOCK]",
		priority=100,
	),
	_SecretRule(
		detector_id="certificate.pem",
		secret_kind="certificate",
		reason="Detected a PEM certificate block.",
		pattern=_CERTIFICATE_RE,
		replacement="[REDACTED CERTIFICATE BLOCK]",
		priority=95,
	),
	_SecretRule(
		detector_id="credential.assignment",
		secret_kind="credential",
		reason="Detected a hard-coded credential assignment.",
		pattern=_CREDENTIAL_ASSIGNMENT_RE,
		replacement=_replace_credential_assignment,
		priority=90,
		predicate=_credential_value_is_sensitive,
	),
	_SecretRule(
		detector_id="token.bearer",
		secret_kind="token",
		reason="Detected a bearer token literal.",
		pattern=_BEARER_TOKEN_RE,
		replacement=_replace_bearer_token,
		priority=80,
	),
	_SecretRule(
		detector_id="token.jwt",
		secret_kind="token",
		reason="Detected a JWT-like token literal.",
		pattern=_JWT_RE,
		replacement=REDACTED_SECRET_MARKER,
		priority=75,
	),
	_SecretRule(
		detector_id="access_key.aws",
		secret_kind="access_key",
		reason="Detected an AWS-style access key literal.",
		pattern=_ACCESS_KEY_RE,
		replacement=REDACTED_SECRET_MARKER,
		priority=70,
	),
)


class SecretScanner:
	"""Detect likely secrets before indexing and redact excerpts without leaking raw values."""

	def __init__(self, *, enabled: bool = True, deny_patterns: Sequence[str] = ()) -> None:
		self.enabled = enabled
		self._rules = _DEFAULT_RULES + _compile_custom_rules(deny_patterns)

	@classmethod
	def from_config(cls, config: ActiveKnowledgeConfig) -> SecretScanner:
		"""Build a secret scanner from validated runtime config."""

		return cls(
			enabled=config.security.secret_scan.enabled,
			deny_patterns=config.security.secret_scan.deny_patterns,
		)

	def scan_file(self, path: str | Path, *, encoding: str = "utf-8") -> SecretScanResult:
		"""Scan one file and return a secret-scan decision."""

		source_path = Path(path)
		text = source_path.read_text(encoding=encoding, errors="replace")
		return self.scan_text(text, source_path=source_path)

	def scan_text(self, text: str, *, source_path: str | Path = "<memory>") -> SecretScanResult:
		"""Scan one text payload and decide whether embeddings should be skipped."""

		if not self.enabled:
			return SecretScanResult(
				schema_version=SECRET_SCAN_SCHEMA_VERSION,
				source_path=_normalize_source_path(source_path),
				matches=(),
				skip_embedding=False,
			)

		line_starts = _build_line_starts(text)
		matches = tuple(secret.public_match for secret in self._detect_secrets(text, line_starts))
		return SecretScanResult(
			schema_version=SECRET_SCAN_SCHEMA_VERSION,
			source_path=_normalize_source_path(source_path),
			matches=matches,
			skip_embedding=bool(matches),
		)

	def sanitize_excerpt(self, excerpt: str) -> str:
		"""Return an excerpt with any detected secret values redacted."""

		if not self.enabled or not excerpt:
			return excerpt

		line_starts = _build_line_starts(excerpt)
		secrets = self._detect_secrets(excerpt, line_starts)
		if not secrets:
			return excerpt
		return _apply_redactions(excerpt, secrets)

	def _detect_secrets(self, text: str, line_starts: Sequence[int]) -> tuple[_DetectedSecret, ...]:
		detected: list[_DetectedSecret] = []
		for rule in self._rules:
			detected.extend(rule.detect(text, line_starts))
		return _dedupe_overlapping(detected)


def _compile_custom_rules(patterns: Sequence[str]) -> tuple[_SecretRule, ...]:
	rules: list[_SecretRule] = []
	for index, pattern_text in enumerate(patterns, start=1):
		compiled = re.compile(pattern_text, re.MULTILINE)
		rules.append(
			_SecretRule(
				detector_id=f"custom.{index}",
				secret_kind="custom",
				reason=f"Matched configured deny pattern #{index}.",
				pattern=compiled,
				replacement=REDACTED_SECRET_MARKER,
				priority=85,
			)
		)
	return tuple(rules)


def _normalize_source_path(path: str | Path) -> str:
	return path.as_posix() if isinstance(path, Path) else str(path)


def _build_line_starts(text: str) -> list[int]:
	line_starts = [0]
	for index, character in enumerate(text):
		if character == "\n":
			line_starts.append(index + 1)
	return line_starts


def _offset_to_line(line_starts: Sequence[int], offset: int) -> int:
	return bisect_right(line_starts, offset)


def _apply_redactions(text: str, secrets: Sequence[_DetectedSecret]) -> str:
	parts: list[str] = []
	cursor = 0
	for secret in secrets:
		parts.append(text[cursor : secret.start])
		parts.append(secret.replacement)
		cursor = secret.end
	parts.append(text[cursor:])
	return "".join(parts)


def _dedupe_overlapping(matches: Sequence[_DetectedSecret]) -> tuple[_DetectedSecret, ...]:
	ordered = sorted(
		matches,
		key=lambda item: (item.start, -item.priority, -(item.end - item.start), item.public_match.detector_id),
	)
	accepted: list[_DetectedSecret] = []
	for item in ordered:
		if accepted and item.start < accepted[-1].end:
			continue
		accepted.append(item)
	return tuple(accepted)
