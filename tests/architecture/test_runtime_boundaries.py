from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import unittest

from helperme.runtime import AgentRuntime, MemoryJournal


ROOT = Path(__file__).resolve().parents[2]
ADAPTERS_ROOT = ROOT / "adapters"
CORE_ROOT = ROOT / "core"
RUNTIME_ROOT = ROOT / "helperme" / "runtime"
ASSISTANT_ROOT = ROOT / "helperme" / "assistant"
CLI_ROOT = ROOT / "helperme" / "channels" / "cli"
HELPERME_ROOT = ROOT / "helperme"
TURN_FAILURE_MARKERS = (
    "TurnRuntime",
    "TurnHost",
    "TurnInvocation",
    "TurnStatus",
    "AgentApplication",
    "SessionTurnOutcome",
    "TodoList",
    "rewrite_todos",
    "max_goal_turns",
    "GoalLoop",
)


def _module_level_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _all_imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _all_imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _broad_exception_handlers(path: Path) -> tuple[int, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None or (
            isinstance(node.type, ast.Name)
            and node.type.id in {"Exception", "BaseException"}
        ):
            lines.append(node.lineno)
    return tuple(lines)


def _unsafe_broad_exception_handlers(path: Path) -> tuple[int, ...]:
    def local_descendants(node: ast.AST):
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ExceptHandler):
                continue
            yield from local_descendants(child)

    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            lines.append(node.lineno)
            continue
        if not (
            isinstance(node.type, ast.Name)
            and node.type.id in {"Exception", "BaseException"}
        ):
            continue
        descendants = tuple(
            child
            for statement in node.body
            for child in local_descendants(statement)
        )
        if any(
            isinstance(child, (ast.Return, ast.Continue, ast.Break, ast.Pass))
            for child in descendants
        ):
            lines.append(node.lineno)
            continue
        raises = any(isinstance(child, ast.Raise) for child in descendants)
        aggregates = any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "append"
            for child in descendants
        )
        if not raises and not aggregates:
            lines.append(node.lineno)
    return tuple(lines)


class RuntimeArchitecturePurityTest(unittest.TestCase):
    def test_removed_stream_api_is_not_reintroduced(self):
        self.assertIsNone(
            importlib.util.find_spec("helperme.assistant.streams")
        )
        self.assertFalse(hasattr(AgentRuntime, "create_stream"))
        self.assertFalse(hasattr(AgentRuntime, "stream_exists"))
        self.assertFalse(hasattr(MemoryJournal, "create_stream"))
        self.assertFalse(hasattr(MemoryJournal, "stream_exists"))

    def test_removed_core_source_tree_is_not_reintroduced(self):
        self.assertFalse(CORE_ROOT.exists())

    def test_console_chat_does_not_import_core_at_module_level(self):
        roots = _module_level_roots(ROOT / "console_chat.py")
        self.assertNotIn("core", roots)
        self.assertNotIn("console_core", roots)
        self.assertNotIn("plugins", roots)
        self.assertNotIn("host", roots)

    def test_runtime_does_not_import_product_or_technical_layers(self):
        offenders: list[str] = []
        for path in sorted(RUNTIME_ROOT.rglob("*.py")):
            modules = _all_imported_modules(path)
            leaked = sorted(
                (_all_imported_roots(path)
                & {"core", "tools", "plugins", "host", "adapters"})
                | {
                    module
                    for module in modules
                    if module == "helperme"
                    or (
                        module.startswith("helperme.")
                        and not (
                            module == "helperme.runtime"
                            or module.startswith("helperme.runtime.")
                        )
                    )
                }
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(RUNTIME_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_runtime_does_not_own_product_domain_vocabulary(self):
        markers = (
            "Criteria",
            "Judgment",
            "criteria.",
            "judgment.",
            "Toolset",
            "toolset",
            "MCP",
            "mcp",
        )
        offenders: list[str] = []
        for path in sorted(RUNTIME_ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            found = [marker for marker in markers if marker in source]
            if found:
                offenders.append(
                    f"{path.relative_to(RUNTIME_ROOT)}: {', '.join(found)}"
                )
        self.assertEqual(offenders, [])

    def test_assistant_runner_is_not_a_turn_wrapper(self):
        runner = (ASSISTANT_ROOT / "runner.py").read_text(encoding="utf-8")
        found = [marker for marker in TURN_FAILURE_MARKERS if marker in runner]
        self.assertEqual(found, [])
        self.assertIn("pending_authorization_ids", runner)
        self.assertIn("drive_until_idle", runner)

    def test_cli_uses_bootstrap_and_session_application_service(self):
        source = (CLI_ROOT / "console.py").read_text(encoding="utf-8")
        self.assertNotIn("class JournalBackedLlmDecisionMaker", source)
        self.assertNotIn("async def drive_until_idle", source)
        self.assertNotIn("AgentRuntime", source)
        self.assertNotIn("SqliteJournal", source)
        self.assertNotIn("CanonicalState", source)
        self.assertIn("bootstrap_assistant", source)
        self.assertIn("AssistantSessions", source)
        self.assertNotIn('"/stop"', source)

    def test_channel_bootstrap_does_not_enable_session_finalization(self):
        source = (HELPERME_ROOT / "bootstrap.py").read_text(encoding="utf-8")
        self.assertNotIn("JudgmentPolicy", source)
        self.assertNotIn("make_isolated_judge", source)

    def test_assistant_driver_and_cli_do_not_swallow_internal_errors(self):
        offenders = {
            str(path.relative_to(ROOT)): _broad_exception_handlers(path)
            for path in (
                ASSISTANT_ROOT / "runner.py",
                CLI_ROOT / "console.py",
            )
            if _broad_exception_handlers(path)
        }
        self.assertEqual(offenders, {})

    def test_broad_handlers_only_cleanup_rollback_or_aggregate(self):
        offenders = {
            str(path.relative_to(ROOT)): lines
            for path in sorted(HELPERME_ROOT.rglob("*.py"))
            if (lines := _unsafe_broad_exception_handlers(path))
        }
        self.assertEqual(offenders, {})

    def test_removed_adapter_source_package_is_not_reintroduced(self):
        self.assertEqual(
            sorted(path.name for path in ADAPTERS_ROOT.glob("*.py")),
            [],
        )

    def test_assistant_does_not_mention_removed_turn_runtime(self):
        offenders: list[str] = []
        for path in sorted(ASSISTANT_ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            found = [
                marker
                for marker in ("TurnRuntime", "TurnHost", "TodoList", "AgentApplication")
                if marker in source
            ]
            if found:
                offenders.append(
                    f"{path.relative_to(ASSISTANT_ROOT)}: {', '.join(found)}"
                )
        self.assertEqual(offenders, [])
