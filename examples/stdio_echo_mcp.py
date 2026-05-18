"""Tiny stdio MCP server with one `echo` tool. For testing wss-bridge.

Run directly to use as a stdio MCP, OR have wss-bridge spawn it.

    uv run python examples/stdio_echo_mcp.py    # stdio mode
    wss-bridge --cmd uv run python examples/stdio_echo_mcp.py  # bridge mode
"""

from fastmcp import FastMCP

mcp = FastMCP("stdio-echo")


@mcp.tool
def echo(msg: str) -> str:
    """Echo back the message."""
    return f"echoed: {msg}"


if __name__ == "__main__":
    mcp.run()  # defaults to stdio
