"""Generic MCP stdio client helper.

Launches an MCP server as a local subprocess, converts its tool schemas to Anthropic's
tool format, and parses CallToolResult back into plain Python data. Not Gmail-specific —
any future local MCP server (e.g. a second one for Procare, if that ever has one) can
reuse this.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def mcp_session(*, command: str, args: list[str]) -> AsyncIterator[ClientSession]:
    """Start an MCP server subprocess and yield an initialized session to it.

    The subprocess lives only for the duration of the `async with` block — there's no
    persistent server between digest runs.
    """
    server_params = StdioServerParameters(command=command, args=args)
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_anthropic_tools(session: ClientSession, *, exclude: set[str] = frozenset()) -> list[dict]:
    """List the server's tools, converted to Anthropic's tool schema format."""
    result = await session.list_tools()
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
        for t in result.tools
        if t.name not in exclude
    ]


def parse_tool_result(result: Any) -> Any:
    """Parse a CallToolResult into plain Python data.

    FastMCP's structured-output support is inconsistent in practice: a tool returning a
    list gets `structuredContent` wrapped as {"result": [...]}, while one returning a
    bare dict currently comes back with `structuredContent` unset — and a list return
    also arrives as *multiple* separate TextContent blocks (one per item), not one JSON
    array. This handles all of those shapes rather than assuming any single one.
    """
    if result.isError:
        error_text = " ".join(b.text for b in result.content if getattr(b, "type", None) == "text")
        return {"error": error_text or "tool call failed"}

    if result.structuredContent is not None:
        content = result.structuredContent
        if isinstance(content, dict) and set(content.keys()) == {"result"}:
            return content["result"]
        return content

    parsed = []
    for block in result.content:
        if getattr(block, "type", None) != "text":
            continue
        try:
            parsed.append(json.loads(block.text))
        except (json.JSONDecodeError, TypeError):
            parsed.append(block.text)

    return parsed[0] if len(parsed) == 1 else parsed
