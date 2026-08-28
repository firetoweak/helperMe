from __future__ import annotations

import ast
from pathlib import Path
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "helperme" / "runtime"
REMOVED_HOST_ROOT = Path(__file__).resolve().parents[2] / "host"
ASSISTANT_ROOT = Path(__file__).resolve().parents[2] / "helperme" / "assistant"
LLM_ROOT = Path(__file__).resolve().parents[2] / "helperme" / "llm"
MCP_ROOT = Path(__file__).resolve().parents[2] / "helperme" / "mcp"
PLUGINS_ROOT = Path(__file__).resolve().parents[2] / "plugins"
SKILLS_ROOT = Path(__file__).resolve().parents[2] / "helperme" / "skills"
TOOLS_ROOT = Path(__file__).resolve().parents[2] / "helperme" / "tools"
SANDBOX_ROOT = Path(__file__).resolve().parents[2] / "helperme" / "sandbox"
CHANNELS_ROOT = Path(__file__).resolve().parents[2] / "helperme" / "channels"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "helperme" / "config.py"
BOOTSTRAP_PATH = Path(__file__).resolve().parents[2] / "helperme" / "bootstrap.py"


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _imports_any(modules: set[str], prefixes: set[str]) -> set[str]:
    return {
        module
        for module in modules
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)
    }


class LayerImportBoundaryTest(unittest.TestCase):
    def test_skills_do_not_import_runtime_assistant_or_removed_plugin_layer(self):
        offenders: list[str] = []
        for path in sorted(SKILLS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                (_imported_roots(path)
                & {"adapters", "agent_runtime", "core", "host", "plugins"})
                | _imports_any(
                    modules,
                    {"helperme.assistant", "helperme.runtime"},
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(SKILLS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_removed_plugin_source_tree_is_not_reintroduced(self):
        self.assertFalse(PLUGINS_ROOT.exists())

    def test_mcp_does_not_import_runtime_assistant_or_removed_plugin_layer(self):
        offenders: list[str] = []
        for path in sorted(MCP_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                (_imported_roots(path)
                & {"adapters", "agent_runtime", "core", "host", "plugins"})
                | _imports_any(
                    modules,
                    {"helperme.assistant", "helperme.runtime"},
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(MCP_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_mcp_uses_namespaced_package_without_shadowing_sdk(self):
        self.assertTrue(MCP_ROOT.is_dir())
        self.assertFalse((MCP_ROOT.parents[1] / "mcp").exists())

    def test_llm_does_not_import_product_or_execution_layers(self):
        offenders: list[str] = []
        for path in sorted(LLM_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                (_imported_roots(path)
                & {"adapters", "agent_runtime", "core", "host", "plugins", "tools"})
                | _imports_any(
                    modules,
                    {
                        "helperme.assistant",
                        "helperme.channels",
                        "helperme.mcp",
                        "helperme.runtime",
                        "helperme.skills",
                        "helperme.tools",
                    },
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(LLM_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_removed_host_source_tree_is_not_reintroduced(self):
        self.assertEqual(sorted(REMOVED_HOST_ROOT.rglob("*.py")), [])

    def test_assistant_does_not_import_removed_layers(self):
        offenders: list[str] = []
        for path in sorted(ASSISTANT_ROOT.rglob("*.py")):
            leaked = sorted(_imported_roots(path) & {"adapters", "core"})
            if leaked:
                offenders.append(
                    f"{path.relative_to(ASSISTANT_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_assistant_uses_only_the_llm_api_port(self):
        offenders: list[str] = []
        for path in sorted(ASSISTANT_ROOT.rglob("*.py")):
            unexpected = sorted(
                module
                for module in _imported_modules(path)
                if module.startswith("helperme.llm.")
                and module != "helperme.llm.api"
            )
            if unexpected:
                offenders.append(
                    f"{path.relative_to(ASSISTANT_ROOT)}: {', '.join(unexpected)}"
                )
        self.assertEqual(offenders, [])

    def test_bootstrap_not_config_owns_the_concrete_llm_client(self):
        self.assertNotIn(
            "helperme.llm.client",
            _imported_modules(CONFIG_PATH),
        )
        self.assertIn(
            "helperme.llm.client",
            _imported_modules(BOOTSTRAP_PATH),
        )

    def test_channels_do_not_import_runtime_or_infrastructure_layers(self):
        offenders: list[str] = []
        forbidden = {
            "helperme.llm",
            "helperme.runtime",
            "helperme.sandbox",
            "helperme.tools",
        }
        for path in sorted(CHANNELS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                (_imported_roots(path)
                & {"adapters", "agent_runtime", "core", "host", "plugins", "tools"})
                | _imports_any(modules, forbidden)
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(CHANNELS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_assistant_uses_only_its_explicit_tool_ports(self):
        allowed = {
            "builtin_tools.py": {
                "helperme.tools.builtin",
                "helperme.tools.executor",
                "helperme.tools.registry",
            },
            "skills.py": {"helperme.tools.spec"},
            "tool_results.py": {"helperme.tools.control"},
            "control.py": {
                "helperme.tools.control",
                "helperme.tools.spec",
            },
            "management.py": {"helperme.tools.spec"},
        }
        offenders: list[str] = []
        for path in sorted(ASSISTANT_ROOT.rglob("*.py")):
            relative = str(path.relative_to(ASSISTANT_ROOT)).replace("\\", "/")
            actual = {
                module
                for module in _imported_modules(path)
                if module == "helperme.tools" or module.startswith("helperme.tools.")
            }
            unexpected = sorted(actual - allowed.get(relative, set()))
            if unexpected:
                offenders.append(f"{relative}: {', '.join(unexpected)}")
        self.assertEqual(offenders, [])

    def test_tools_do_not_import_runtime_or_product_layers(self):
        offenders: list[str] = []
        forbidden = {
            "helperme.assistant",
            "helperme.channels",
            "helperme.llm",
            "helperme.mcp",
            "helperme.runtime",
            "helperme.skills",
        }
        for path in sorted(TOOLS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                (_imported_roots(path)
                & {"adapters", "agent_runtime", "core", "host", "plugins", "tools"})
                | _imports_any(modules, forbidden)
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(TOOLS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_tools_do_not_import_sandbox_implementations(self):
        offenders: list[str] = []
        for path in sorted(TOOLS_ROOT.rglob("*.py")):
            leaked = sorted(
                _imports_any(
                    _imported_modules(path),
                    {"helperme.sandbox.local"},
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(TOOLS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_runtime_does_not_import_product_layers(self):
        offenders: list[str] = []
        for path in sorted(RUNTIME_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                (_imported_roots(path) & {"core", "tools", "plugins", "host", "adapters"})
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

    def test_sandbox_does_not_import_product_or_runtime_layers(self):
        offenders: list[str] = []
        forbidden = {
            "helperme.assistant",
            "helperme.channels",
            "helperme.llm",
            "helperme.mcp",
            "helperme.runtime",
            "helperme.skills",
            "helperme.tools",
        }
        for path in SANDBOX_ROOT.rglob("*.py"):
            modules = _imported_modules(path)
            leaked = sorted(
                (_imported_roots(path)
                & {"core", "agent_runtime", "adapters", "host", "plugins", "tools"})
                | _imports_any(modules, forbidden)
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(SANDBOX_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])
