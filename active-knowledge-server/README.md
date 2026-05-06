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
