# active-knowledge-server

`active-knowledge-server` is the FastMCP-based server for the Active RAG knowledge base.

The detailed architecture and implementation plan is maintained in:

- [Active Knowledge Server 架构与方案设计](../doc/active_knowledge_server_architecture_design.md)

Planned responsibilities:

- initialize the local knowledge workdir
- index Active source code, build profiles, API docs, widget docs, and future product/project/design docs
- store metadata, full-text indexes, vector indexes, cache, and job state under the configured workdir
- expose stable MCP tools and resources for Skills and agents

Default source-distribution layout:

```text
active-knowledge/
  .active-kb/              # generated at runtime
  knowledge-sources/       # source documents remembered by RAG
  active-knowledge-server/
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run active-kb --version
```

The package uses a `src/` layout so tests exercise the installed package shape
rather than importing directly from the repository root.

## Current CLI Skeleton

Phase C1-02 provides the command and config contract used by later indexing and
MCP phases:

```bash
uv run active-kb init --workspace /path/to/active
uv run active-kb status --format json
uv run active-kb validate --strict
uv run active-kb serve --transport stdio
uv run active-kb index --incremental
```

Config precedence is fixed as:

```text
CLI > ACTIVE_KB_* environment > local config > baseline config > defaults
```

The merged config is validated by the Pydantic schema in
`active_knowledge_server.config.schema`, expands `${...}` references such as
`${runtime.workdir}`, and emits safe summaries with token-like scalar fields
redacted and absolute paths shortened when they live under the current directory
or the user's home directory.

`serve` and `index` currently return executable plans; FastMCP runtime wiring and
the indexing pipeline are introduced by later implementation phases.

`init` is idempotent. It creates the baseline/local directory skeleton, writes
`local/config/active-kb.local.yaml` when missing, preserves an existing local
config unless `--force` is used, warns when `baseline/manifest.json` is missing,
and warns if runtime files under `.active-kb/local/` are tracked by git.
