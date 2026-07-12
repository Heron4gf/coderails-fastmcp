"""CodeRails FastMCP: a local MCP server exposing web_search, code_search and code_apply."""

from .app import create_server, main, mcp

__version__ = "0.1.0"
__all__ = ["create_server", "main", "mcp", "__version__"]
