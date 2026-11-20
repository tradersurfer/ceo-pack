"""v0.5.0 slash-command prompts: analyze-first + ELI5 + session mode.

The 26 one-tool commands tell the agent to read the user's words and
work out the tool's inputs before calling it — never a bare pointer like
"CEO Pack: use {tool}. Input was: {raw}". Every one of those prompts
answers in plain words a business owner can follow.

/ceo is different: the host prints a command's return value as the
visible reply, so /ceo returns SHORT state messages ("CEO ON", "CEO
OFF") and points the agent at the ceo-pack-usage skill, which carries
the flow and the ELI5 rules. A long instruction prompt here showed up
as a wall of text (Adrian, 2026-11-20).
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


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


# Commands whose prompt names the tool they call (delegation is a real
# handler with its own output shape, not a tool pointer).
EXPECTED_TOOL = {
    "orchestrate": "ceo_orchestrate",
    "brief": "ceo_brief",
    "assign": "ceo_assign",
    "verify": "ceo_verify",
    "log": "ceo_decision_log",
    "decision": "ceo_decision_log",
    "route": "ceo_route",
    "models": "ceo_choose_model",
    "weekly-pulse": "ceo_orchestrate",
    "closeout": "q4_closeout_checklist",
    "q1": "ceo_closeout_checklist",
    "q2": "ceo_closeout_checklist",
    "q3": "ceo_closeout_checklist",
    "monthly-closeout": "ceo_closeout_checklist",
    "daily-checks": "ceo_daily_check",
    "eod-checks": "ceo_daily_check",
    "performance-checks": "ceo_performance_check",
    "status-check": "ceo_status_check",
    "roster": "ceo_roster",
    "task-create": "ceo_task_create",
    "task-status": "ceo_task_status",
    "task-list": "ceo_task_list",
    "task-approve": "ceo_task_approve",
    "task-assign": "ceo_task_assign",
    "run-create": "ceo_run_create",
    "run-status": "ceo_run_status",
}


class TestCommandPrompts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "cmd_tasks.db")
        cls._old_env = os.environ.get("CEO_PACK_TASK_DB")
        os.environ["CEO_PACK_TASK_DB"] = cls.db
        cls.pkg = load_plugin_package("ceo_pack_v050_commands_test")
        cls.ctx = RecordingCtx()
        cls.pkg.register(cls.ctx)

    @classmethod
    def tearDownClass(cls):
        if cls._old_env is None:
            os.environ.pop("CEO_PACK_TASK_DB", None)
        else:
            os.environ["CEO_PACK_TASK_DB"] = cls._old_env
        cls.tmp.cleanup()

    def test_every_tool_command_is_analyze_first_and_eli5(self):
        for name, tool in EXPECTED_TOOL.items():
            with self.subTest(command=name):
                out = self.ctx.commands[name]["fn"]("fix the books")
                self.assertIn(tool, out)
                # Analyze-first: the prompt must tell the agent to read the
                # user's words and work out the inputs — never a bare pointer.
                self.assertIn("read the user's words", out)
                self.assertNotIn(f"CEO Pack: use {tool}.", out)
                # ELI5: plain words a business owner can follow.
                self.assertIn("plain, simple words", out)

    def test_ceo_with_input_analyzes_first_and_turns_mode_on(self):
        out = self.ctx.commands["ceo"]["fn"]("how do I set up my business with you?")
        self.assertIn("CEO ON", out)
        self.assertIn("say it back in one plain sentence", out)
        self.assertIn("ceo-pack-usage", out)
        # Short state reply, not the old wall of prompt text: no tool
        # names, no ELI5 block, no "never route before the brief" lecture.
        self.assertNotIn("ceo_brief", out)
        self.assertNotIn("Never route before the brief", out)
        self.assertLess(len(out), 300)

    def test_ceo_on_is_a_short_confirmation(self):
        out = self.ctx.commands["ceo"]["fn"]("on")
        self.assertIn("CEO ON", out)
        self.assertIn("'/ceo off'", out)
        self.assertIn("ceo-pack-usage", out)
        self.assertNotIn("ceo_brief", out)
        self.assertNotIn("plain, simple words", out)
        self.assertLess(len(out), 200)

    def test_ceo_empty_turns_mode_on_and_asks_for_a_goal(self):
        out = self.ctx.commands["ceo"]["fn"]("")
        self.assertIn("CEO ON", out)
        self.assertIn("What business goal", out)
        self.assertIn("ceo-pack-usage", out)
        self.assertNotIn("ceo_brief", out)
        self.assertLess(len(out), 200)

    def test_ceo_off_turns_mode_off(self):
        out = self.ctx.commands["ceo"]["fn"]("off")
        self.assertIn("CEO OFF", out)
        self.assertIn("'/ceo on'", out)
        self.assertNotIn("ceo_brief", out)
        self.assertLess(len(out), 200)

    def test_q4_derives_the_year_and_is_eli5(self):
        out = self.ctx.commands["q4"]["fn"]("close out the year")
        self.assertIn("q4_closeout_checklist", out)
        self.assertIn("this year if they did not say", out)
        self.assertIn("plain, simple words", out)

    def test_usage_skill_is_registered(self):
        names = {name for name, _ in self.ctx.skills}
        self.assertIn("ceo-pack-usage", names)
