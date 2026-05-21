"""FastMCP facade package."""

from active_knowledge_server.mcp.app import ActiveKnowledgeFastMCPApp, create_fastmcp_app
from active_knowledge_server.mcp.schemas import (
	BOOTSTRAP_RESOURCE_URIS,
	BOOTSTRAP_TOOL_NAMES,
	MCPComponentInventory,
)

__all__ = [
	"ActiveKnowledgeFastMCPApp",
	"BOOTSTRAP_RESOURCE_URIS",
	"BOOTSTRAP_TOOL_NAMES",
	"MCPComponentInventory",
	"create_fastmcp_app",
]
