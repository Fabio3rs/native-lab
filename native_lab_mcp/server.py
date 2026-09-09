"""MCP adapter for the NativeLab process registry."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from .core import NativeLabError, ProcessRegistry


@dataclass(slots=True)
class AppState:
    registry: ProcessRegistry


def _native_lab_binary() -> str:
    configured = os.environ.get("NATIVE_LAB_BIN")
    if configured:
        return str(Path(configured).expanduser().resolve())
    return str(Path(__file__).resolve().parent.parent / "native-lab")


@asynccontextmanager
async def lifespan(_: MCPServer[AppState]) -> AsyncIterator[AppState]:
    registry = ProcessRegistry(
        workspace=os.getcwd(),
        command_prefix=(_native_lab_binary(), "run", "--"),
        max_buffer_chars=int(os.environ.get("NATIVE_LAB_MCP_BUFFER_CHARS", 1024 * 1024)),
        max_processes=int(os.environ.get("NATIVE_LAB_MCP_MAX_PROCESSES", 64)),
    )
    try:
        yield AppState(registry=registry)
    finally:
        await registry.close()


mcp = MCPServer(
    "native-lab",
    description="Run and observe development workloads inside a persistent NativeLab sandbox.",
    instructions=(
        "Use run with structured argv. Use wait for process completion and expect for meaningful "
        "output instead of repeatedly polling tail. Commands from this server share the NativeLab "
        "session and private localhost for the workspace in which the server was started."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


def _registry(ctx: Context[AppState]) -> ProcessRegistry:
    return ctx.request_context.lifespan_context.registry


async def _call(operation: Any) -> dict[str, object]:
    try:
        return await operation
    except NativeLabError as error:
        raise ToolError(str(error)) from error


@mcp.tool()
async def run(argv: list[str], ctx: Context[AppState]) -> dict[str, object]:
    """Start structured argv inside NativeLab and return a process handle immediately.

    No shell is added. To use shell syntax, explicitly pass an argv such as
    ["sh", "-c", "command one && command two"].
    """
    return await _call(_registry(ctx).run(argv))


@mcp.tool()
async def head(
    process_id: str,
    ctx: Context[AppState],
    stream: Literal["stdout", "stderr", "both"] = "both",
    limit_chars: int = 12000,
) -> dict[str, object]:
    """Read the oldest output still retained in a process's bounded ring buffer."""
    return await _call(_registry(ctx).head(process_id, stream, limit_chars))


@mcp.tool()
async def tail(
    process_id: str,
    ctx: Context[AppState],
    stream: Literal["stdout", "stderr", "both"] = "both",
    limit_chars: int = 12000,
) -> dict[str, object]:
    """Read the newest output retained for a process without waiting for more."""
    return await _call(_registry(ctx).tail(process_id, stream, limit_chars))


@mcp.tool()
async def expect(
    process_id: str,
    pattern: str,
    ctx: Context[AppState],
    stream: Literal["stdout", "stderr", "both"] = "both",
    from_position: Literal["now", "start"] = "now",
    after_cursor: int | None = None,
    timeout_seconds: float = 30.0,
    context_lines: int = 3,
) -> dict[str, object]:
    """Wait until literal output appears, the process exits, or the timeout expires.

    from_position="now" ignores existing output; "start" searches retained history.
    after_cursor overrides from_position and precisely resumes after an opaque cursor
    returned by another NativeLab tool. Matching is literal and crosses read chunks.
    """
    return await _call(
        _registry(ctx).expect(
            process_id,
            pattern,
            stream=stream,
            from_position=from_position,
            after_cursor=after_cursor,
            timeout_seconds=timeout_seconds,
            context_lines=context_lines,
        )
    )


@mcp.tool()
async def wait(
    process_id: str,
    timeout_seconds: float,
    ctx: Context[AppState],
    tail_lines: int = 0,
    stream: Literal["stdout", "stderr", "both"] = "both",
) -> dict[str, object]:
    """Wait for a process to exit or for the timeout to expire.

    A timeout leaves the process running. Set tail_lines from 1 through 100 to
    include a final output snapshot, optionally filtered by stream.
    """
    return await _call(
        _registry(ctx).wait(
            process_id,
            timeout_seconds,
            tail_lines=tail_lines,
            stream=stream,
        )
    )


@mcp.tool()
async def write(
    process_id: str,
    ctx: Context[AppState],
    data: str = "",
    eof: bool = False,
) -> dict[str, object]:
    """Write UTF-8 text to a process's stdin and optionally close stdin."""
    return await _call(_registry(ctx).write(process_id, data, eof))


@mcp.tool()
async def kill(
    process_id: str,
    ctx: Context[AppState],
    force: bool = False,
) -> dict[str, object]:
    """Send SIGTERM to the process group, or SIGKILL when force is true."""
    return await _call(_registry(ctx).kill(process_id, force))


@mcp.tool()
async def processes(ctx: Context[AppState]) -> dict[str, object]:
    """List every process handle owned by this MCP server instance."""
    return await _registry(ctx).processes()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
