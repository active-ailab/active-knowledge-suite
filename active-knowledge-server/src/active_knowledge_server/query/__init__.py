"""Query service, routing, retrieval, and evidence packaging."""

from active_knowledge_server.query.router import QueryRouter, normalize_query
from active_knowledge_server.query.retrievers import (
	FullTextMatchResult,
	FullTextRetriever,
	FullTextSearchRequest,
	FullTextSearchResult,
	SymbolCandidate,
	SymbolRetriever,
	SymbolSearchRequest,
	SymbolSearchResult,
	normalize_fts_lookup_text,
	normalize_lookup_text,
)
from active_knowledge_server.query.symbol_resolver import (
	SymbolResolution,
	SymbolResolver,
)

__all__ = [
	"QueryRouter",
	"FullTextMatchResult",
	"FullTextRetriever",
	"FullTextSearchRequest",
	"FullTextSearchResult",
	"SymbolCandidate",
	"SymbolResolution",
	"SymbolResolver",
	"SymbolRetriever",
	"SymbolSearchRequest",
	"SymbolSearchResult",
	"normalize_fts_lookup_text",
	"normalize_lookup_text",
	"normalize_query",
]
