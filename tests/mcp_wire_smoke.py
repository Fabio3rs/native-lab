"""Exercise the real SDK stdio transport and MCP tool discovery."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {"run", "head", "tail", "expect", "write", "kill", "processes"}


async def smoke() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "native_lab_mcp.server"],
        cwd=ROOT,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream, read_timeout_seconds=5) as session:
            initialized = await session.initialize()
            assert initialized.server_info.name == "native-lab"
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == EXPECTED_TOOLS, (names, EXPECTED_TOOLS)


if __name__ == "__main__":
    asyncio.run(smoke())
