import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pydantic import ValidationError

from core.environment import (
    EnvironmentBinding,
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    RuntimeAttachment,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from core.runtime_artifacts import ArtifactStore
from core.tool_registry import BUILTIN_TOOL_REGISTRY
from core.todos.rewrite_todos import rewrite_todos_tool_schema
from tools import create_environment_tool_specs
from tools.artifact_read import create_read_artifact_spec
from tools.file_manage import WriteFileInput
from tools.file_read import GlobInput, GrepInput, ReadFileInput
from tools.powershell_runner import PowerShellCommandRunner


REQUIRED_SECTIONS = (
    "用途：",
    "何时使用：",
    "关键限制：",
    "失败/截断后：",
)


class ProductionToolDescriptionContractTest(unittest.TestCase):
    def test_all_production_tools_answer_the_four_contract_questions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = WorkspaceViewSnapshot((
                RootBinding("project", WorkspaceScope.TASK, root),
            ))
            binding = EnvironmentBinding(
                "local-test",
                view,
                PermissionBinding((
                    ("project", FilesystemPermission.READ_WRITE),
                )),
                root,
                "powershell",
                "powershell.exe",
                RuntimeAttachment(
                    "local-test",
                    PowerShellCommandRunner(),
                ),
            )
            specs = [
                *create_environment_tool_specs(binding),
                create_read_artifact_spec(Mock(spec=ArtifactStore)),
                BUILTIN_TOOL_REGISTRY.get("get_today_date"),
            ]

        descriptions = {
            spec.name: spec.description
            for spec in specs
        }
        descriptions["rewrite_todos"] = rewrite_todos_tool_schema()[
            "function"
        ]["description"]

        for tool_name, description in descriptions.items():
            with self.subTest(tool=tool_name):
                for section in REQUIRED_SECTIONS:
                    self.assertIn(section, description)


class WorkspaceToolInputSchemaContractTest(unittest.TestCase):
    def test_write_file_requires_content_but_accepts_explicit_empty_content(self):
        schema = WriteFileInput.model_json_schema()

        self.assertIn("content", schema["required"])
        with self.assertRaises(ValidationError):
            WriteFileInput.model_validate({"path": "empty.txt"})
        validated = WriteFileInput.model_validate({
            "path": "empty.txt",
            "content": "",
        })
        self.assertEqual(validated.content, "")

    def test_numeric_limits_are_exposed_in_json_schema(self):
        glob_properties = GlobInput.model_json_schema()["properties"]
        read_properties = ReadFileInput.model_json_schema()["properties"]
        grep_properties = GrepInput.model_json_schema()["properties"]

        max_depth_integer_schema = next(
            variant
            for variant in glob_properties["max_depth"]["anyOf"]
            if variant.get("type") == "integer"
        )
        self.assertEqual(max_depth_integer_schema["minimum"], 1)
        self.assertEqual(glob_properties["max_results"]["minimum"], 1)
        self.assertEqual(glob_properties["max_results"]["maximum"], 100)
        self.assertEqual(glob_properties["offset"]["minimum"], 0)
        self.assertEqual(read_properties["offset"]["minimum"], 1)
        self.assertEqual(read_properties["limit"]["minimum"], 1)
        self.assertEqual(read_properties["limit"]["maximum"], 2000)
        self.assertEqual(grep_properties["offset"]["minimum"], 0)
        self.assertEqual(grep_properties["max_results"]["minimum"], 1)
        self.assertEqual(grep_properties["max_results"]["maximum"], 100)

    def test_invalid_numeric_limits_fail_at_input_boundary(self):
        invalid_inputs = (
            (GlobInput, {"pattern": "*.py", "max_depth": 0}),
            (GlobInput, {"pattern": "*.py", "offset": -1}),
            (GlobInput, {"pattern": "*.py", "max_results": 0}),
            (ReadFileInput, {"path": "a.py", "offset": 0}),
            (ReadFileInput, {"path": "a.py", "limit": 2001}),
            (GrepInput, {"query": "x", "offset": -1}),
            (GrepInput, {"query": "x", "max_results": 101}),
        )

        for input_model, payload in invalid_inputs:
            with self.subTest(input_model=input_model.__name__, payload=payload):
                with self.assertRaises(ValidationError):
                    input_model.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
