"""Query service, routing, retrieval, and evidence packaging."""

from active_knowledge_server.query.router import QueryRouter, normalize_query

__all__ = ["QueryRouter", "normalize_query"]
