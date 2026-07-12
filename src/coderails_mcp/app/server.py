"""CodeRails MCP server: web_search, code_search, code_apply over OpenRouter."""

from fastmcp import FastMCP

from .tools import register_tools


def create_server() -> FastMCP:
    """Build a FastMCP server with all CodeRails tools registered."""
    mcp = FastMCP("coderails")
    register_tools(mcp)
    return mcp


mcp = create_server()


def main() -> None:
    """Run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
