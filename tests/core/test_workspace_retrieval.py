import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.context import ContextManager, ContextRequest, ContextState
from core.environment import (
    EnvironmentBinding,
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    RuntimeAttachment,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.tool_registry import ToolRegistry
from core.tools_runtime.tools_executor import ToolsExecutor, encode_tool_result
from tools.file_read import (
    MAX_GREP_HIT_CHARS,
    MAX_GREP_SUBMATCHES,
    MAX_READ_CHARS,
    MAX_READ_FILE_SIZE_BYTES,
    create_file_read_specs,
)


class WorkspaceRetrievalTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        view = WorkspaceViewSnapshot((
            RootBinding("project", WorkspaceScope.TASK, self.root),
        ))
        binding = EnvironmentBinding(
            environment_id="local-test",
            workspace_view=view,
            permission_binding=PermissionBinding((
                ("project", FilesystemPermission.READ_WRITE),
            )),
            cwd=self.root,
            shell_name="powershell",
            shell_path="powershell.exe",
            runtime_attachment=RuntimeAttachment("local-test", object()),
        )
        registry = ToolRegistry()
        for spec in create_file_read_specs(binding):
            registry.register(spec)
        self.executor = ToolsExecutor(registry)

    def tearDown(self):
        self.directory.cleanup()

    def execute(self, name: str, arguments: dict):
        return self.executor.execute(name, json.dumps(arguments))

    async def test_read_file_returns_a_bounded_resumable_line_window(self):
        (self.root / "sample.txt").write_text(
            "first\nsecond\nthird\n",
            encoding="utf-8",
        )

        result = await self.execute("read_file", {
            "path": "sample.txt",
            "offset": 2,
            "limit": 1,
        })

        self.assertEqual(result["code"], "FILE_READ")
        self.assertEqual(result["data"]["content"], "second\n")
        self.assertEqual(result["data"]["start_line"], 2)
        self.assertEqual(result["data"]["end_line"], 2)
        self.assertTrue(result["data"]["truncated"])
        self.assertEqual(result["data"]["truncated_by"], "lines")
        self.assertEqual(result["data"]["next_offset"], 3)
        self.assertNotIn("total_lines", result["data"])

    async def test_read_file_reports_a_long_line_without_claiming_resumable_success(self):
        (self.root / "long.txt").write_text(
            f"{'x' * 8_100}\nafter\n",
            encoding="utf-8",
        )

        result = await self.execute("read_file", {
            "path": "long.txt",
        })

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "LINE_TOO_LONG")
        self.assertEqual(len(result["data"]["preview"]), MAX_READ_CHARS)
        self.assertNotIn("truncated", result["data"])
        self.assertNotIn("next_offset", result["data"])

    async def test_read_file_accepts_an_empty_file_at_the_first_line(self):
        (self.root / "empty.txt").write_text("", encoding="utf-8")

        result = await self.execute("read_file", {
            "path": "empty.txt",
        })

        self.assertEqual(result["code"], "FILE_READ")
        self.assertEqual(result["data"]["content"], "")
        self.assertFalse(result["data"]["truncated"])
        self.assertIsNone(result["data"]["next_offset"])

    async def test_read_file_rejects_a_file_over_the_domain_limit(self):
        path = self.root / "huge.txt"
        with path.open("wb") as handle:
            handle.seek(MAX_READ_FILE_SIZE_BYTES)
            handle.write(b"x")

        result = await self.execute("read_file", {
            "path": "huge.txt",
        })

        self.assertEqual(result["code"], "FILE_TOO_LARGE")

    @unittest.skipIf(shutil.which("rg") is None, "需要 rg")
    async def test_grep_paginates_by_matching_line(self):
        (self.root / "a.txt").write_text(
            "needle needle\nnone\nneedle again\n",
            encoding="utf-8",
        )
        (self.root / "b.txt").write_text("needle last\n", encoding="utf-8")

        first = await self.execute("grep", {
            "query": "needle",
            "max_results": 2,
        })
        second = await self.execute("grep", {
            "query": "needle",
            "offset": 2,
            "max_results": 2,
        })

        self.assertEqual(len(first["data"]["hits"]), 2)
        self.assertIn(2, [len(hit["submatches"]) for hit in first["data"]["hits"]])
        self.assertTrue(first["data"]["truncated"])
        self.assertEqual(first["data"]["next_offset"], 2)
        self.assertNotIn("total_hits", first["data"])
        self.assertEqual(len(second["data"]["hits"]), 1)
        self.assertFalse(second["data"]["truncated"])
        self.assertIsNone(second["data"]["next_offset"])

    async def test_grep_rejects_an_empty_query(self):
        result = await self.execute("grep", {
            "query": "  ",
        })

        self.assertEqual(result["code"], "EMPTY_QUERY")

    @unittest.skipIf(shutil.which("rg") is None, "需要 rg")
    async def test_grep_bounds_each_matching_line_preview(self):
        (self.root / "long.txt").write_text(
            "needle" + "x" * (MAX_GREP_HIT_CHARS + 100),
            encoding="utf-8",
        )

        result = await self.execute("grep", {
            "query": "needle",
        })

        hit = result["data"]["hits"][0]
        self.assertEqual(len(hit["content"]), MAX_GREP_HIT_CHARS)
        self.assertTrue(hit["content_truncated"])

    @unittest.skipIf(shutil.which("rg") is None, "需要 rg")
    async def test_grep_page_char_budget_remains_resumable(self):
        for index in range(5):
            (self.root / f"{index}.txt").write_text(
                "needle" + "x" * 1_894,
                encoding="utf-8",
            )

        first = await self.execute("grep", {
            "query": "needle",
            "max_results": 100,
        })
        second = await self.execute("grep", {
            "query": "needle",
            "offset": first["data"]["next_offset"],
            "max_results": 100,
        })

        self.assertEqual(len(first["data"]["hits"]), 4)
        self.assertTrue(first["data"]["truncated"])
        self.assertEqual(first["data"]["next_offset"], 4)
        self.assertEqual(len(second["data"]["hits"]), 1)
        self.assertFalse(second["data"]["truncated"])

    @unittest.skipIf(shutil.which("rg") is None, "需要 rg")
    async def test_grep_bounds_submatches_per_hit(self):
        (self.root / "many-matches.txt").write_text(
            "x" * (MAX_GREP_SUBMATCHES + 10),
            encoding="utf-8",
        )

        result = await self.execute("grep", {
            "query": "x",
        })

        hit = result["data"]["hits"][0]
        self.assertEqual(len(hit["submatches"]), MAX_GREP_SUBMATCHES)
        self.assertTrue(hit["submatches_truncated"])

    async def test_grep_times_out_and_kills_the_process(self):
        killed = asyncio.Event()

        class BlockingStdout:
            async def readline(self):
                await asyncio.Event().wait()

        class EmptyStderr:
            async def read(self, _size):
                return b""

        class BlockingProcess:
            def __init__(self):
                self.stdout = BlockingStdout()
                self.stderr = EmptyStderr()
                self.returncode = None

            def kill(self):
                killed.set()
                self.returncode = -9

            def terminate(self):
                killed.set()
                self.returncode = -15

            async def wait(self):
                await killed.wait()
                return self.returncode

        with patch(
            "tools.file_read.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=BlockingProcess()),
        ), patch(
            "tools.file_read.GREP_TIMEOUT_SECONDS",
            0.01,
        ):
            result = await self.execute("grep", {
                "query": "needle",
            })

        self.assertTrue(killed.is_set())
        self.assertEqual(result["code"], "RG_TIMEOUT")

    async def test_glob_has_stable_offset_pagination_without_total_count(self):
        (self.root / "a.py").write_text("", encoding="utf-8")
        (self.root / "folder").mkdir()
        (self.root / "folder" / "b.py").write_text("", encoding="utf-8")
        (self.root / "z.txt").write_text("", encoding="utf-8")

        first = await self.execute("glob", {
            "pattern": "*.py",
            "kind": "file",
            "max_results": 1,
        })
        second = await self.execute("glob", {
            "pattern": "*.py",
            "kind": "file",
            "offset": 1,
            "max_results": 1,
        })

        self.assertEqual(
            [(item["path"], item["kind"]) for item in first["data"]["matches"]],
            [("a.py", "file")],
        )
        self.assertEqual(
            first["data"]["matches"][0]["location"]["environment_id"],
            "local-test",
        )
        self.assertTrue(first["data"]["truncated"])
        self.assertEqual(first["data"]["next_offset"], 1)
        self.assertNotIn("total", first["data"])
        self.assertEqual(
            [(item["path"], item["kind"]) for item in second["data"]["matches"]],
            [("folder/b.py", "file")],
        )
        self.assertFalse(second["data"]["truncated"])
        self.assertIsNone(second["data"]["next_offset"])

    async def test_glob_path_pattern_is_anchored_at_the_search_root(self):
        (self.root / "tools").mkdir()
        (self.root / "tools" / "a.py").write_text("", encoding="utf-8")
        (self.root / "nested" / "tools").mkdir(parents=True)
        (self.root / "nested" / "tools" / "b.py").write_text("", encoding="utf-8")

        result = await self.execute("glob", {
            "pattern": "tools/*.py",
            "kind": "file",
        })

        self.assertEqual(
            [item["path"] for item in result["data"]["matches"]],
            ["tools/a.py"],
        )

    async def test_glob_reports_partial_results_for_inaccessible_directory(self):
        blocked = self.root / "blocked"
        blocked.mkdir()
        (self.root / "visible.py").write_text("", encoding="utf-8")
        original_scandir = os.scandir

        def scandir(path):
            if Path(path) == blocked:
                raise PermissionError(13, "access denied", str(path))
            return original_scandir(path)

        with patch("tools.file_read.os.scandir", side_effect=scandir):
            result = await self.execute("glob", {
                "path": ".",
                "pattern": "*.py",
                "kind": "file",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "GLOB_PARTIAL")
        self.assertFalse(result["data"]["complete"])
        self.assertEqual(
            [item["path"] for item in result["data"]["matches"]],
            ["visible.py"],
        )
        self.assertEqual(
            result["data"]["inaccessible_paths"],
            ["blocked"],
        )
        self.assertFalse(result["data"]["inaccessible_paths_truncated"])
        self.assertIn("结果不完整", result["hint"])

    async def test_workspace_content_enters_model_context_only_after_read_tool_result(self):
        secret = "workspace-secret-not-implicitly-injected"
        (self.root / "secret.txt").write_text(secret, encoding="utf-8")
        conversation = Conversation()
        conversation.set_system_prompt("system")
        conversation.add_user("请处理项目")
        manager = ContextManager()

        before = manager.build(ContextRequest(
            conversation_records=conversation.records,
            runtime_instructions=[],
            context_state=ContextState(),
        ))
        self.assertNotIn(secret, json.dumps(before.messages, ensure_ascii=False))

        tool_call_id = "call-read-secret"
        conversation.add_assistant(LLMResponse(
            calls=(ToolCall(tool_call_id, "read_file", "{}"),),
        ))
        read_result = await self.execute("read_file", {
            "path": "secret.txt",
        })
        conversation.add_tools_result([{
            "tool_call_id": tool_call_id,
            "content": encode_tool_result(read_result),
        }])

        after = manager.build(ContextRequest(
            conversation_records=conversation.records,
            runtime_instructions=[],
            context_state=ContextState(),
        ))
        self.assertIn(secret, json.dumps(after.messages, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
