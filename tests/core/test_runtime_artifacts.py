import json
import tempfile
import unittest
from pathlib import Path

from core.runtime_artifacts import (
    ArtifactNotFoundError,
    FileArtifactStore,
    ToolResultExternalizer,
    ToolResultLimit,
)
from core.context import ContextManager
from core.composition import create_agent_application
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tool_registry import EmptyInput, ToolRegistry, ToolSpec
from core.tools_runtime.run_runtime import RunRuntime
from core.tools_runtime.tools_executor import ToolsExecutor, encode_tool_result
from tools.artifact_read import create_read_artifact_spec
from tools.workspace import WORKSPACE
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
)


class RuntimeArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = FileArtifactStore(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_small_result_passes_through_without_artifact(self):
        result = {
            "ok": True,
            "code": "OK",
            "data": {"content": "small"},
            "error": None,
            "hint": None,
        }
        externalizer = ToolResultExternalizer(
            self.store,
            ToolResultLimit(max_chars=1000, preview_chars=100),
        )

        self.assertIs(externalizer.process(result), result)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_large_result_is_saved_and_replaced_by_opaque_reference(self):
        result = {
            "ok": True,
            "code": "BIG_RESULT",
            "data": {"content": "x" * 1000},
            "error": None,
            "hint": None,
        }
        original = encode_tool_result(result)
        externalizer = ToolResultExternalizer(
            self.store,
            ToolResultLimit(max_chars=500, preview_chars=80),
        )

        projected = externalizer.process(result)
        artifact_id = projected["data"]["artifact_id"]

        self.assertTrue(projected["data"]["externalized"])
        self.assertNotIn(str(self.root), json.dumps(projected))
        self.assertLessEqual(len(encode_tool_result(projected)), 500)
        chunk = self.store.read(artifact_id, 0, len(original))
        self.assertEqual(chunk.content, original)

    def test_store_reads_by_character_offset(self):
        ref = self.store.save("abcdefghij")

        first = self.store.read(ref.artifact_id, 0, 4)
        second = self.store.read(ref.artifact_id, first.next_offset, 6)

        self.assertEqual(first.content + second.content, "abcdefghij")
        self.assertEqual(first.next_offset, 4)
        self.assertIsNone(second.next_offset)

    def test_store_rejects_unknown_artifact(self):
        with self.assertRaises(ArtifactNotFoundError):
            self.store.read("art_00000000000000000000000000000000", 0, 1)

    def test_externalizer_does_not_hide_store_failure(self):
        class FailingStore:
            def save(self, content):
                raise OSError("disk failure")

        externalizer = ToolResultExternalizer(
            FailingStore(),
            ToolResultLimit(max_chars=500, preview_chars=80),
        )
        result = {
            "ok": True,
            "code": "BIG_RESULT",
            "data": {"content": "x" * 1000},
            "error": None,
            "hint": None,
        }

        with self.assertRaisesRegex(OSError, "disk failure"):
            externalizer.process(result)

    def test_runtime_root_must_be_outside_user_workspace(self):
        with self.assertRaisesRegex(ValueError, "workspace"):
            create_agent_application(
                "test-model",
                model_context_limit=1000,
                runtime_root=WORKSPACE / ".runtime",
            )

    def test_read_artifact_tool_has_no_path_input_and_enforces_limit(self):
        ref = self.store.save("x" * 4000)
        registry = ToolRegistry()
        registry.register(create_read_artifact_spec(self.store))
        executor = ToolsExecutor(registry)

        result = executor.execute(
            "read_artifact",
            json.dumps(
                {
                    "artifact_id": ref.artifact_id,
                    "offset": 0,
                    "limit": 3000,
                }
            ),
        )
        invalid = executor.execute(
            "read_artifact",
            json.dumps(
                {
                    "artifact_id": ref.artifact_id,
                    "offset": 0,
                    "limit": 3001,
                }
            ),
        )

        self.assertEqual(len(result["data"]["content"]), 3000)
        self.assertEqual(result["data"]["next_offset"], 3000)
        self.assertEqual(invalid["code"], "VALIDATION_ERROR")

    def test_run_runtime_only_puts_artifact_reference_in_conversation(self):
        class LLMClient:
            def __init__(self):
                self.responses = [
                    LLMResponse(
                        type="tool_calls",
                        calls=[ToolCall("call-1", "big_tool", "{}")],
                    ),
                    LLMResponse(type="text", content="done"),
                ]

            def chat(self, messages, model, tools=None):
                return call_result(self.responses.pop(0))

        def big_tool(_):
            return {
                "ok": True,
                "code": "BIG_RESULT",
                "content": "x" * 1000,
            }

        registry = ToolRegistry()
        registry.register(
            ToolSpec("big_tool", "returns a big result", EmptyInput, big_tool)
        )
        registry.register(create_read_artifact_spec(self.store))
        limit = ToolResultLimit(max_chars=500, preview_chars=80)
        conversation = Conversation()

        result = RunRuntime(
            model_calls=model_call_service(LLMClient()),
            model="test-model",
            runtime_mode=PlainMode(),
            context_preparation=context_preparation_service(
                ContextManager(limit.max_chars)
            ),
            tools_executor=ToolsExecutor(registry),
            tool_result_externalizer=ToolResultExternalizer(
                self.store,
                limit,
            ),
        ).run(conversation, "run big tool")

        tool_message = next(
            message
            for message in conversation.protocol_messages()
            if message["role"] == "tool"
        )
        tool_result = json.loads(tool_message["content"])
        artifact_id = tool_result["data"]["artifact_id"]

        self.assertEqual(result.status, "completed")
        self.assertLessEqual(len(tool_message["content"]), limit.max_chars)
        self.assertEqual(
            json.loads(
                self.store.read(artifact_id, 0, 10_000).content
            )["data"]["content"],
            "x" * 1000,
        )


if __name__ == "__main__":
    unittest.main()
