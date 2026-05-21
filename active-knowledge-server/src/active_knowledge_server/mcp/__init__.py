"""FastMCP facade package."""

from active_knowledge_server.mcp.app import ActiveKnowledgeFastMCPApp, create_fastmcp_app
from active_knowledge_server.mcp.schemas import (
	ALL_TOOL_NAMES,
	BOOTSTRAP_RESOURCE_URIS,
	BOOTSTRAP_TOOL_NAMES,
	MCPComponentInventory,
	QUERY_TOOL_NAMES,
)

__all__ = [
	"ActiveKnowledgeFastMCPApp",
	"ALL_TOOL_NAMES",
	"BOOTSTRAP_RESOURCE_URIS",
	"BOOTSTRAP_TOOL_NAMES",
	"MCPComponentInventory",
	"QUERY_TOOL_NAMES",
	"create_fastmcp_app",
]
