from __future__ import annotations

import asyncio
import sys
import unittest

from native_lab_mcp.core import NativeLabError, ProcessRegistry


class ProcessRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = ProcessRegistry(workspace=".", command_prefix=(), max_buffer_chars=1024)

    async def asyncTearDown(self) -> None:
        await self.registry.close()

    async def test_expect_matches_across_pipe_reads(self) -> None:
        script = (
            "import os,time; "
            "os.write(1,b'server rea'); time.sleep(.08); "
            "os.write(1,b'dy on port 5173\\n'); time.sleep(.08)"
        )
        started = await self.registry.run([sys.executable, "-c", script])
        result = await self.registry.expect(
            str(started["process_id"]), "server ready", timeout_seconds=2
        )
        self.assertTrue(result["matched"])
        self.assertEqual(result["stream"], "stdout")
        self.assertEqual(result["match"], "server ready on port 5173")

    async def test_expect_from_now_ignores_old_output(self) -> None:
        script = "import os,time; os.write(1,b'ready\\n'); time.sleep(.3)"
        started = await self.registry.run([sys.executable, "-c", script])
        process_id = str(started["process_id"])
        historical = await self.registry.expect(
            process_id, "ready", from_position="start", timeout_seconds=1
        )
        self.assertTrue(historical["matched"])
        future = await self.registry.expect(process_id, "ready", timeout_seconds=0.05)
        self.assertFalse(future["matched"])
        self.assertEqual(future["reason"], "timeout")

    async def test_stderr_and_cursor_resume(self) -> None:
        script = (
            "import os,time; os.write(2,b'first\\n'); time.sleep(.1); "
            "os.write(2,b'second\\n'); time.sleep(.1)"
        )
        started = await self.registry.run([sys.executable, "-c", script])
        process_id = str(started["process_id"])
        first = await self.registry.expect(
            process_id, "first", stream="stderr", from_position="start", timeout_seconds=1
        )
        second = await self.registry.expect(
            process_id,
            "second",
            stream="stderr",
            after_cursor=int(first["cursor"]),
            timeout_seconds=1,
        )
        self.assertTrue(second["matched"])
        self.assertGreater(int(second["cursor"]), int(first["cursor"]))

    async def test_cursor_can_resume_inside_one_read_event(self) -> None:
        script = "import os,time; os.write(1,b'abcdefghij\\n'); time.sleep(.2)"
        started = await self.registry.run([sys.executable, "-c", script])
        process_id = str(started["process_id"])
        first = await self.registry.expect(
            process_id, "abc", from_position="start", timeout_seconds=1
        )
        second = await self.registry.expect(
            process_id, "def", after_cursor=int(first["cursor"]), timeout_seconds=1
        )
        self.assertEqual(first["cursor"], 3)
        self.assertEqual(second["cursor"], 6)

    async def test_write_and_eof(self) -> None:
        script = "import sys; data=sys.stdin.read(); print('got:'+data, flush=True)"
        started = await self.registry.run([sys.executable, "-c", script])
        process_id = str(started["process_id"])
        written = await self.registry.write(process_id, "hello", eof=True)
        self.assertEqual(written["written_chars"], 5)
        result = await self.registry.expect(
            process_id, "got:hello", from_position="start", timeout_seconds=1
        )
        self.assertTrue(result["matched"])

    async def test_tail_limits_output_and_preserves_latest_text(self) -> None:
        script = "import os; os.write(1, b'a'*700+b'b'*700)"
        started = await self.registry.run([sys.executable, "-c", script])
        process_id = str(started["process_id"])
        await self.registry.expect(process_id, "never", from_position="start", timeout_seconds=1)
        result = await self.registry.tail(process_id, "stdout", 100)
        text = "".join(str(event["text"]) for event in result["events"])
        self.assertEqual(text, "b" * 100)
        self.assertTrue(result["history_truncated"])
        self.assertTrue(result["result_truncated"])

    async def test_wait_returns_exit_and_requested_tail_lines(self) -> None:
        script = (
            "import os,time; os.write(1,b'first\\npar'); time.sleep(.05); "
            "os.write(1,b'tial\\nlast')"
        )
        started = await self.registry.run([sys.executable, "-c", script])
        result = await self.registry.wait(
            str(started["process_id"]), 1, tail_lines=2, stream="stdout"
        )
        text = "".join(str(event["text"]) for event in result["events"])
        self.assertEqual(result["reason"], "process_exited")
        self.assertEqual(result["state"], "exited")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(text, "partial\nlast")
        self.assertTrue(result["result_truncated"])

    async def test_wait_timeout_keeps_process_running_and_includes_tail(self) -> None:
        script = "import os,time; os.write(2,b'ready\\n'); time.sleep(30)"
        started = await self.registry.run([sys.executable, "-c", script])
        process_id = str(started["process_id"])
        await self.registry.expect(
            process_id, "ready", stream="stderr", from_position="start", timeout_seconds=1
        )
        result = await self.registry.wait(
            process_id, 0.01, tail_lines=1, stream="stderr"
        )
        text = "".join(str(event["text"]) for event in result["events"])
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["state"], "running")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(text, "ready\n")

        await self.registry.kill(process_id)
        exited = await self.registry.wait(process_id, 1)
        self.assertEqual(exited["reason"], "process_exited")

    async def test_wait_defaults_to_no_output_and_filters_streams(self) -> None:
        script = "import os; os.write(1,b'out\\n'); os.write(2,b'err\\n')"
        started = await self.registry.run([sys.executable, "-c", script])
        process_id = str(started["process_id"])
        no_output = await self.registry.wait(process_id, 1)
        self.assertEqual(no_output["events"], [])

        stderr = await self.registry.wait(process_id, 0, tail_lines=1, stream="stderr")
        text = "".join(str(event["text"]) for event in stderr["events"])
        self.assertEqual(text, "err\n")

        both = await self.registry.wait(process_id, 0, tail_lines=100)
        streams = {event["stream"] for event in both["events"]}
        self.assertEqual(streams, {"stdout", "stderr"})

    async def test_wait_accepts_one_hundred_tail_lines(self) -> None:
        script = "import sys; sys.stdout.write(''.join(f'{n}\\n' for n in range(101)))"
        started = await self.registry.run([sys.executable, "-c", script])
        result = await self.registry.wait(
            str(started["process_id"]), 1, tail_lines=100, stream="stdout"
        )
        text = "".join(str(event["text"]) for event in result["events"])
        self.assertEqual(len(text.splitlines()), 100)
        self.assertEqual(text.splitlines()[0], "1")
        self.assertEqual(text.splitlines()[-1], "100")
        self.assertTrue(result["result_truncated"])

    async def test_wait_validates_timeout_tail_lines_and_process_id(self) -> None:
        started = await self.registry.run([sys.executable, "-c", "pass"])
        process_id = str(started["process_id"])
        with self.assertRaises(NativeLabError):
            await self.registry.wait(process_id, -1)
        with self.assertRaises(NativeLabError):
            await self.registry.wait(process_id, 3601)
        with self.assertRaises(NativeLabError):
            await self.registry.wait(process_id, 0, tail_lines=-1)
        with self.assertRaises(NativeLabError):
            await self.registry.wait(process_id, 0, tail_lines=101)
        with self.assertRaises(NativeLabError):
            await self.registry.wait("missing", 0)

    async def test_kill_terminates_process(self) -> None:
        started = await self.registry.run([sys.executable, "-c", "import time; time.sleep(30)"])
        process_id = str(started["process_id"])
        killed = await self.registry.kill(process_id)
        self.assertTrue(killed["signaled"])
        result = await self.registry.expect(
            process_id, "never", from_position="start", timeout_seconds=2
        )
        self.assertEqual(result["reason"], "process_exited")

    async def test_rejects_empty_argv_and_unknown_process(self) -> None:
        with self.assertRaises(NativeLabError):
            await self.registry.run([])
        with self.assertRaises(NativeLabError):
            await self.registry.run([""])
        with self.assertRaises(NativeLabError):
            await self.registry.tail("missing")

    async def test_preserves_empty_non_program_argv(self) -> None:
        script = "import sys; print(repr(sys.argv[1]), flush=True)"
        started = await self.registry.run([sys.executable, "-c", script, ""])
        result = await self.registry.expect(
            str(started["process_id"]), "''", from_position="start", timeout_seconds=1
        )
        self.assertTrue(result["matched"])

    async def test_process_count_is_bounded(self) -> None:
        registry = ProcessRegistry(workspace=".", command_prefix=(), max_processes=1)
        try:
            await registry.run([sys.executable, "-c", "import time; time.sleep(30)"])
            with self.assertRaises(NativeLabError):
                await registry.run([sys.executable, "-c", "pass"])
        finally:
            await registry.close()

    async def test_oldest_completed_handle_is_evicted_at_limit(self) -> None:
        registry = ProcessRegistry(workspace=".", command_prefix=(), max_processes=1)
        try:
            first = await registry.run([sys.executable, "-c", "pass"])
            first_id = str(first["process_id"])
            await registry.expect(first_id, "never", from_position="start", timeout_seconds=1)
            second = await registry.run([sys.executable, "-c", "pass"])
            self.assertNotEqual(first_id, second["process_id"])
            with self.assertRaises(NativeLabError):
                await registry.tail(first_id)
        finally:
            await registry.close()


class ServerSchemaTests(unittest.TestCase):
    def test_expected_tools_are_registered(self) -> None:
        from native_lab_mcp.server import mcp

        names = {tool.name for tool in mcp._tool_manager.list_tools()}
        self.assertEqual(
            names, {"run", "head", "tail", "expect", "wait", "write", "kill", "processes"}
        )

    def test_expect_schema_uses_unambiguous_from_position(self) -> None:
        from native_lab_mcp.server import mcp

        tool = mcp._tool_manager.get_tool("expect")
        assert tool is not None
        properties = tool.parameters["properties"]
        self.assertIn("from_position", properties)
        self.assertEqual(properties["from_position"]["default"], "now")

    def test_wait_schema_requires_timeout_and_exposes_tail_options(self) -> None:
        from native_lab_mcp.server import mcp

        tool = mcp._tool_manager.get_tool("wait")
        assert tool is not None
        properties = tool.parameters["properties"]
        self.assertEqual(set(tool.parameters["required"]), {"process_id", "timeout_seconds"})
        self.assertEqual(properties["tail_lines"]["default"], 0)
        self.assertEqual(properties["stream"]["default"], "both")


if __name__ == "__main__":
    unittest.main()
