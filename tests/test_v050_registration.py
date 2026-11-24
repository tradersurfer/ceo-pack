"""v0.5.0 registration tests: register() against a fake ctx with the
REAL host method set.

Asserts the exact tool set (22) from plugin.yaml equals what registers,
the 30 commands, the hook, the delegation provider, the CLI command and
the skills. Fictional data only; the task DB is a temp file.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_COMMANDS = {
    "ceo", "q4", "orchestrate",
    "brief", "assign", "verify", "log", "decision", "route", "models",
    "weekly-pulse", "closeout", "q1", "q2", "q3", "monthly-closeout",
    "daily-checks", "eod-checks", "performance-checks", "status-check",
    "roster",
    "task-create", "task-status", "task-list", "task-approve", "task-assign",
    "task-complete",
    "run-create", "run-status",
    "ceo delegation",
}
EXPECTED_SKILLS = {"ceo-brief", "ceo-decision-log", "ceo-assign",
                   "q4-closeout", "orchestration", "model-selection",
                   "ceo-pack-usage"}


class RecordingCtx:
    """Fake PluginContext with the real method set register() uses."""

    def __init__(self):
        self.tools = {}
        self.hooks = []
        self.skills = []
        self.commands = {}
        self.providers = []
        self.cli_commands = {}
        self.config = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, toolset, schema, handler):
        self.tools[name] = {"toolset": toolset, "schema": schema,
                            "handler": handler}

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))

    def register_skill(self, name, path):
        self.skills.append((name, path))

    def register_command(self, name, fn, description="", args_hint=None):
        self.commands[name] = {"fn": fn, "description": description,
                               "args_hint": args_hint}

    def register_delegation_provider(self, provider):
        self.providers.append(provider)

    def register_cli_command(self, name, help, setup_fn, handler_fn,
                             description=""):
        self.cli_commands[name] = {"help": help, "setup_fn": setup_fn,
                                   "handler_fn": handler_fn,
                                   "description": description}


def load_plugin_package(name):
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    spec.loader.exec_module(pkg)
    return pkg


class TestRegistration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "reg_tasks.db")
        cls._old_env = os.environ.get("CEO_PACK_TASK_DB")
        os.environ["CEO_PACK_TASK_DB"] = cls.db
        cls.pkg = load_plugin_package("ceo_pack_v050_registration_test")
        cls.ctx = RecordingCtx()
        cls.pkg.register(cls.ctx)
        with open(_ROOT / "plugin.yaml") as fh:
            cls.manifest = yaml.safe_load(fh)

    @classmethod
    def tearDownClass(cls):
        if cls._old_env is None:
            os.environ.pop("CEO_PACK_TASK_DB", None)
        else:
            os.environ["CEO_PACK_TASK_DB"] = cls._old_env
        cls.tmp.cleanup()

    def test_tool_set_matches_plugin_yaml_exactly(self):
        declared = self.manifest["provides_tools"]
        registered = sorted(self.ctx.tools)
        self.assertEqual(registered, sorted(declared))
        self.assertEqual(len(registered), 21)
        self.assertNotIn("ceo_task_approve", registered)   # approval is owner-only
        self.assertEqual(self.manifest["version"], "0.6.0")

    def test_every_tool_has_schema_and_callable_handler(self):
        for name, entry in self.ctx.tools.items():
            with self.subTest(tool=name):
                self.assertEqual(entry["toolset"], "ceo-pack")
                self.assertIsInstance(entry["schema"], dict)
                self.assertEqual(entry["schema"].get("name"), name)
                self.assertTrue(callable(entry["handler"]))

    def test_command_set_is_exactly_30(self):
        self.assertEqual(set(self.ctx.commands), EXPECTED_COMMANDS)
        self.assertEqual(len(self.ctx.commands), 30)

    def test_hook_registered(self):
        # Stage 1 gate (warn/observe only): pre_tool_call joined
        # post_tool_call; the pinned list is the exact hook set.
        self.assertEqual([name for name, _ in self.ctx.hooks],
                         ["post_tool_call", "pre_tool_call"])

    def test_delegation_provider_registered(self):
        self.assertEqual(len(self.ctx.providers), 1)
        provider = self.ctx.providers[0]
        self.assertEqual(provider.name, "ceo_pack")
        self.assertEqual(type(provider).__name__, "CEODelegationProvider")
        self.assertTrue(provider.is_available())

    def test_cli_command_registered(self):
        self.assertIn("ceo delegation", self.ctx.cli_commands)
        entry = self.ctx.cli_commands["ceo delegation"]
        self.assertTrue(callable(entry["setup_fn"]))
        self.assertTrue(callable(entry["handler_fn"]))

    def test_skills_registered(self):
        self.assertEqual({name for name, _ in self.ctx.skills}, EXPECTED_SKILLS)
        for name, path in self.ctx.skills:
            self.assertTrue(Path(path).exists(), name)

    def test_task_db_initialized_by_register(self):
        # register() runs init_db(): the temp DB has the task tables.
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        for table in ("tasks", "task_history", "task_dependencies", "task_runs"):
            self.assertIn(table, tables)


if __name__ == "__main__":
    unittest.main()
