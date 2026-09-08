"""Async process registry used by NativeLab's protocol adapters.

This module deliberately has no MCP imports.  A CLI, tests, or a future Tasks
extension can use the same process and output semantics.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeAlias


StreamName: TypeAlias = Literal["stdout", "stderr"]
StreamSelection: TypeAlias = Literal["stdout", "stderr", "both"]
FromPosition: TypeAlias = Literal["now", "start"]

READ_SIZE = 64 * 1024
MAX_PATTERN_CHARS = 4096
MAX_WRITE_CHARS = 1024 * 1024
MAX_ARGV_CHARS = 1024 * 1024
MAX_BUFFER_CHARS = 64 * 1024 * 1024
MAX_BUFFER_EVENTS = 4096


class NativeLabError(RuntimeError):
    """An anticipated error safe to return to a tool caller."""


@dataclass(frozen=True, slots=True)
class OutputEvent:
    start_cursor: int
    cursor: int
    stream: StreamName
    text: str

    def as_dict(self) -> dict[str, object]:
        return {
            "start_cursor": self.start_cursor,
            "cursor": self.cursor,
            "stream": self.stream,
            "text": self.text,
        }


@dataclass(slots=True)
class ManagedProcess:
    process_id: str
    argv: tuple[str, ...]
    process: asyncio.subprocess.Process
    started_at: str
    max_buffer_chars: int
    events: deque[OutputEvent] = field(default_factory=deque)
    buffered_chars: int = 0
    cursor: int = 0
    history_truncated: bool = False
    exit_code: int | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def state(self) -> str:
        return "exited" if self.exit_code is not None else "running"

    async def append(self, stream: StreamName, text: str) -> None:
        if not text:
            return
        async with self.condition:
            start_cursor = self.cursor + 1
            self.cursor += len(text)
            self.events.append(OutputEvent(start_cursor, self.cursor, stream, text))
            self.buffered_chars += len(text)
            self._trim()
            self.condition.notify_all()

    def _trim(self) -> None:
        while len(self.events) > MAX_BUFFER_EVENTS:
            removed = self.events.popleft()
            self.buffered_chars -= len(removed.text)
            self.history_truncated = True
        while self.events and self.buffered_chars > self.max_buffer_chars:
            overflow = self.buffered_chars - self.max_buffer_chars
            first = self.events[0]
            if len(first.text) <= overflow:
                self.events.popleft()
                self.buffered_chars -= len(first.text)
            else:
                self.events[0] = OutputEvent(
                    first.start_cursor + overflow,
                    first.cursor,
                    first.stream,
                    first.text[overflow:],
                )
                self.buffered_chars -= overflow
            self.history_truncated = True

    async def mark_exited(self, exit_code: int) -> None:
        async with self.condition:
            self.exit_code = exit_code
            self.condition.notify_all()


class ProcessRegistry:
    """Own subprocesses and bounded output buffers for one MCP server lifetime."""

    def __init__(
        self,
        *,
        workspace: str | os.PathLike[str],
        command_prefix: tuple[str, ...] | list[str],
        max_buffer_chars: int = 1024 * 1024,
        max_processes: int = 64,
    ) -> None:
        if max_buffer_chars < 1024 or max_buffer_chars > MAX_BUFFER_CHARS:
            raise ValueError(f"max_buffer_chars must be between 1024 and {MAX_BUFFER_CHARS}")
        if max_processes < 1 or max_processes > 4096:
            raise ValueError("max_processes must be between 1 and 4096")
        self.workspace = str(Path(workspace).resolve())
        self.command_prefix = tuple(command_prefix)
        self.max_buffer_chars = max_buffer_chars
        self.max_processes = max_processes
        self._processes: dict[str, ManagedProcess] = {}
        self._closed = False

    async def run(self, argv: list[str]) -> dict[str, object]:
        self._ensure_open()
        self._validate_argv(argv)
        while len(self._processes) >= self.max_processes:
            completed_id = next(
                (
                    process_id
                    for process_id, managed in self._processes.items()
                    if managed.exit_code is not None
                ),
                None,
            )
            if completed_id is not None:
                del self._processes[completed_id]
                continue
            raise NativeLabError(
                f"concurrent process limit reached ({self.max_processes})"
            )
        command = (*self.command_prefix, *argv)
        try:
            child = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.workspace,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            raise NativeLabError(f"could not start process: {error}") from error

        process_id = f"p_{uuid.uuid4().hex[:12]}"
        managed = ManagedProcess(
            process_id=process_id,
            argv=tuple(argv),
            process=child,
            started_at=datetime.now(timezone.utc).isoformat(),
            max_buffer_chars=self.max_buffer_chars,
        )
        self._processes[process_id] = managed
        assert child.stdout is not None and child.stderr is not None
        stdout_task = asyncio.create_task(self._drain(managed, "stdout", child.stdout))
        stderr_task = asyncio.create_task(self._drain(managed, "stderr", child.stderr))
        managed.tasks.extend((stdout_task, stderr_task))
        waiter = asyncio.create_task(self._wait_for_exit(managed, stdout_task, stderr_task))
        managed.tasks.append(waiter)
        await asyncio.sleep(0)
        return self._summary(managed)

    async def head(
        self, process_id: str, stream: StreamSelection = "both", limit_chars: int = 12000
    ) -> dict[str, object]:
        managed = self._get(process_id)
        self._validate_stream(stream)
        self._validate_limit(limit_chars)
        async with managed.condition:
            events = self._select_head(list(managed.events), stream, limit_chars)
            return self._output_result(managed, events, stream, limit_chars, direction="head")

    async def tail(
        self, process_id: str, stream: StreamSelection = "both", limit_chars: int = 12000
    ) -> dict[str, object]:
        managed = self._get(process_id)
        self._validate_stream(stream)
        self._validate_limit(limit_chars)
        async with managed.condition:
            events = self._select_tail(list(managed.events), stream, limit_chars)
            return self._output_result(managed, events, stream, limit_chars, direction="tail")

    async def expect(
        self,
        process_id: str,
        pattern: str,
        *,
        stream: StreamSelection = "both",
        from_position: FromPosition = "now",
        after_cursor: int | None = None,
        timeout_seconds: float = 30.0,
        context_lines: int = 3,
    ) -> dict[str, object]:
        managed = self._get(process_id)
        self._validate_stream(stream)
        if not pattern:
            raise NativeLabError("pattern must not be empty")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise NativeLabError(f"pattern exceeds {MAX_PATTERN_CHARS} characters")
        if from_position not in ("now", "start"):
            raise NativeLabError("from_position must be 'now' or 'start'")
        if after_cursor is not None and after_cursor < 0:
            raise NativeLabError("after_cursor must be non-negative")
        if timeout_seconds < 0 or timeout_seconds > 3600:
            raise NativeLabError("timeout_seconds must be between 0 and 3600")
        if context_lines < 0 or context_lines > 20:
            raise NativeLabError("context_lines must be between 0 and 20")

        async with managed.condition:
            if after_cursor is not None:
                minimum_cursor = after_cursor + 1
            elif from_position == "start":
                minimum_cursor = 0
            else:
                minimum_cursor = managed.cursor + 1

            async def wait_for_result() -> dict[str, object]:
                while True:
                    match = self._find_literal(
                        list(managed.events), pattern, stream, minimum_cursor, context_lines
                    )
                    if match is not None:
                        return {
                            "matched": True,
                            "process_id": managed.process_id,
                            **match,
                            "state": managed.state,
                            "exit_code": managed.exit_code,
                        }
                    if managed.exit_code is not None:
                        return {
                            "matched": False,
                            "process_id": managed.process_id,
                            "reason": "process_exited",
                            "cursor": managed.cursor,
                            "state": managed.state,
                            "exit_code": managed.exit_code,
                        }
                    await managed.condition.wait()

            try:
                async with asyncio.timeout(timeout_seconds):
                    return await wait_for_result()
            except TimeoutError:
                return {
                    "matched": False,
                    "process_id": managed.process_id,
                    "reason": "timeout",
                    "cursor": managed.cursor,
                    "state": managed.state,
                    "exit_code": managed.exit_code,
                }

    async def write(self, process_id: str, data: str = "", eof: bool = False) -> dict[str, object]:
        managed = self._get(process_id)
        if len(data) > MAX_WRITE_CHARS:
            raise NativeLabError(f"data exceeds {MAX_WRITE_CHARS} characters")
        stdin = managed.process.stdin
        if stdin is None or stdin.is_closing():
            raise NativeLabError(f"stdin is closed for process {process_id}")
        try:
            if data:
                stdin.write(data.encode("utf-8"))
                await stdin.drain()
            if eof:
                stdin.close()
                await stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise NativeLabError(f"stdin is closed for process {process_id}") from error
        return {
            "process_id": process_id,
            "written_chars": len(data),
            "eof": eof,
            "state": managed.state,
        }

    async def kill(self, process_id: str, force: bool = False) -> dict[str, object]:
        managed = self._get(process_id)
        if managed.exit_code is not None:
            return {**self._summary(managed), "signaled": False}
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(managed.process.pid, sig)
        except ProcessLookupError:
            pass
        return {**self._summary(managed), "signaled": True, "signal": sig.name}

    async def processes(self) -> dict[str, object]:
        return {"processes": [self._summary(item) for item in self._processes.values()]}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        running = [item for item in self._processes.values() if item.exit_code is None]
        for item in running:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(item.process.pid, signal.SIGTERM)
        if running:
            try:
                async with asyncio.timeout(2.0):
                    await asyncio.gather(*(item.process.wait() for item in running))
            except TimeoutError:
                for item in running:
                    if item.process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(item.process.pid, signal.SIGKILL)
        tasks = [task for item in self._processes.values() for task in item.tasks]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _drain(
        self,
        managed: ManagedProcess,
        stream: StreamName,
        reader: asyncio.StreamReader,
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await reader.read(READ_SIZE):
            await managed.append(stream, decoder.decode(chunk))
        await managed.append(stream, decoder.decode(b"", final=True))

    async def _wait_for_exit(
        self,
        managed: ManagedProcess,
        stdout_task: asyncio.Task[None],
        stderr_task: asyncio.Task[None],
    ) -> None:
        exit_code = await managed.process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        await managed.mark_exited(exit_code)

    def _get(self, process_id: str) -> ManagedProcess:
        try:
            return self._processes[process_id]
        except KeyError as error:
            raise NativeLabError(f"unknown process_id: {process_id}") from error

    def _ensure_open(self) -> None:
        if self._closed:
            raise NativeLabError("process registry is closed")

    @staticmethod
    def _validate_argv(argv: list[str]) -> None:
        if not argv:
            raise NativeLabError("argv must contain at least one element")
        if len(argv) > 4096:
            raise NativeLabError("argv contains too many elements")
        if sum(len(value) for value in argv if isinstance(value, str)) > MAX_ARGV_CHARS:
            raise NativeLabError(f"argv exceeds {MAX_ARGV_CHARS} characters")
        for index, value in enumerate(argv):
            if not isinstance(value, str):
                raise NativeLabError("every argv element must be a string")
            if index == 0 and not value:
                raise NativeLabError("argv[0] must not be empty")
            if "\x00" in value:
                raise NativeLabError("argv elements must not contain NUL")

    @staticmethod
    def _validate_stream(stream: str) -> None:
        if stream not in ("stdout", "stderr", "both"):
            raise NativeLabError("stream must be 'stdout', 'stderr', or 'both'")

    @staticmethod
    def _validate_limit(limit_chars: int) -> None:
        if limit_chars < 1 or limit_chars > 1_000_000:
            raise NativeLabError("limit_chars must be between 1 and 1000000")

    @staticmethod
    def _matches_stream(event: OutputEvent, stream: StreamSelection) -> bool:
        return stream == "both" or event.stream == stream

    @classmethod
    def _select_head(
        cls, events: list[OutputEvent], stream: StreamSelection, limit: int
    ) -> list[OutputEvent]:
        result: list[OutputEvent] = []
        remaining = limit
        for event in events:
            if not cls._matches_stream(event, stream):
                continue
            text = event.text[:remaining]
            if text:
                result.append(
                    OutputEvent(event.start_cursor, event.start_cursor + len(text) - 1, event.stream, text)
                )
                remaining -= len(text)
            if remaining == 0:
                break
        return result

    @classmethod
    def _select_tail(
        cls, events: list[OutputEvent], stream: StreamSelection, limit: int
    ) -> list[OutputEvent]:
        result: list[OutputEvent] = []
        remaining = limit
        for event in reversed(events):
            if not cls._matches_stream(event, stream):
                continue
            text = event.text[-remaining:]
            if text:
                result.append(
                    OutputEvent(event.cursor - len(text) + 1, event.cursor, event.stream, text)
                )
                remaining -= len(text)
            if remaining == 0:
                break
        result.reverse()
        return result

    @classmethod
    def _output_result(
        cls,
        managed: ManagedProcess,
        events: list[OutputEvent],
        stream: StreamSelection,
        limit: int,
        *,
        direction: str,
    ) -> dict[str, object]:
        eligible_chars = sum(
            len(event.text) for event in managed.events if cls._matches_stream(event, stream)
        )
        return {
            "process_id": managed.process_id,
            "direction": direction,
            "stream": stream,
            "events": [event.as_dict() for event in events],
            "cursor": managed.cursor,
            "history_truncated": managed.history_truncated,
            "result_truncated": eligible_chars > limit,
            "state": managed.state,
            "exit_code": managed.exit_code,
        }

    @staticmethod
    def _find_literal(
        events: list[OutputEvent],
        pattern: str,
        stream: StreamSelection,
        minimum_cursor: int,
        context_lines: int,
    ) -> dict[str, object] | None:
        candidates: list[dict[str, object]] = []
        streams: tuple[StreamName, ...] = (
            ("stdout", "stderr") if stream == "both" else (stream,)
        )
        for selected_stream in streams:
            selected: list[OutputEvent] = []
            for event in events:
                if event.stream != selected_stream or event.cursor < minimum_cursor:
                    continue
                if event.start_cursor < minimum_cursor:
                    offset = minimum_cursor - event.start_cursor
                    selected.append(
                        OutputEvent(minimum_cursor, event.cursor, event.stream, event.text[offset:])
                    )
                else:
                    selected.append(event)
            if not selected:
                continue
            text = "".join(event.text for event in selected)
            index = text.find(pattern)
            if index < 0:
                continue
            end_index = index + len(pattern)
            seen = 0
            match_cursor = selected[-1].cursor
            for event in selected:
                event_end = seen + len(event.text)
                if event_end >= end_index:
                    offset_in_event = end_index - seen
                    match_cursor = event.start_cursor + offset_in_event - 1
                    break
                seen = event_end
            line_start = text.rfind("\n", 0, index) + 1
            line_end = text.find("\n", end_index)
            if line_end < 0:
                line_end = len(text)
            before = text[:line_start].splitlines()[-context_lines:] if context_lines else []
            after = text[line_end + 1 :].splitlines()[:context_lines] if context_lines else []
            candidates.append(
                {
                    "stream": selected_stream,
                    "cursor": match_cursor,
                    "match": text[line_start:line_end],
                    "before": before,
                    "after": after,
                }
            )
        if not candidates:
            return None
        return min(candidates, key=lambda item: int(item["cursor"]))

    @staticmethod
    def _summary(managed: ManagedProcess) -> dict[str, object]:
        return {
            "process_id": managed.process_id,
            "argv": list(managed.argv),
            "pid": managed.process.pid,
            "state": managed.state,
            "exit_code": managed.exit_code,
            "cursor": managed.cursor,
            "started_at": managed.started_at,
        }
