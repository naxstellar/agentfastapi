"""A tiny stdio MCP server for tests (WP6B)."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("sample")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def explode() -> str:
    """Always fails."""
    raise ValueError("kaboom")


@server.tool()
def die() -> str:
    """Kills the server process mid-call."""
    import os

    os._exit(1)


if __name__ == "__main__":
    server.run("stdio")
