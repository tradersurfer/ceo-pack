"""Owner-only Type 1 approval (Teknium review, PR #158334).

The model must have no way to move a Type 1 task proposed -> approved:
no approval tool, no free-text `approved_by`, no assign bypass, and no way
to create a risky task with the gate switched off.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tasks  # noqa: E402
import tools  # noqa: E402
import schemas  # noqa: E402
from test_v050_registration import RecordingCtx, load_plugin_package  # noqa: E402


def call(fn, **args):
    return json.loads(fn(args))


class ApprovalGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "gate.db")
        cls._old = os.environ.get("CEO_PACK_TASK_DB")
        os.environ["CEO_PACK_TASK_DB"] = cls.db
        tasks.init_db(cls.db)
        cls.pkg = load_plugin_package("ceo_pack_approval_gate_test")
        cls.ctx = RecordingCtx()
        cls.pkg.register(cls.ctx)

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("CEO_PACK_TASK_DB", None)
        else:
            os.environ["CEO_PACK_TASK_DB"] = cls._old
        cls.tmp.cleanup()

    def _task(self, **kw):
        run = call(tools.ceo_run_create, goal="gate test")["run_id"]
        out = call(tools.ceo_task_create, run_id=run, seat="CFO", **kw)
        self.assertEqual(out["status"], "success", out)
        return out["task_id"]

    # --- the model has no approval surface -------------------------------
    def test_no_approval_tool_is_registered_or_defined(self):
        self.assertFalse([n for n in self.ctx.tools if "approve" in n])
        self.assertFalse(hasattr(tools, "ceo_task_approve"))
        self.assertFalse(hasattr(schemas, "CEO_TASK_APPROVE"))
        text = (_ROOT / "plugin.yaml").read_text()
        self.assertNotIn("ceo_task_approve", text)
        self.assertNotIn("bot-forge", text)

    def test_no_tool_schema_takes_an_approver_name(self):
        for name, entry in self.ctx.tools.items():
            props = (entry["schema"].get("parameters") or {}).get("properties", {})
            self.assertNotIn("approved_by", props, name)

    def test_pre_tool_call_blocks_a_stale_approval_call(self):
        hooks = dict(self.ctx.hooks)
        out = hooks["pre_tool_call"]("ceo_task_approve", {"task_id": "x", "approved_by": "Owner"})
        self.assertEqual(out["action"], "block")
        self.assertIsNone(hooks["pre_tool_call"]("ceo_task_status", {"task_id": "x"}))

    # --- no side door ---------------------------------------------------
    def test_assign_cannot_skip_approval_on_a_type1_task(self):
        tid = self._task(title="Wire $50,000 to a new vendor", approval_required=True)
        out = call(tools.ceo_task_assign, task_id=tid, owner="CFO")
        self.assertEqual(out["status"], "error")
        row = tasks.get_task(tid)
        self.assertEqual(row["status"], "proposed")
        self.assertFalse(row.get("owner"))

    def test_storage_layer_also_refuses_proposed_to_assigned_for_type1(self):
        tid = self._task(title="Delete the customer database", approval_required=True)
        self.assertFalse(tasks.move_to_assigned(tid))
        self.assertEqual(tasks.get_task(tid)["status"], "proposed")

    def test_model_cannot_waive_the_gate_on_risky_wording(self):
        tid = self._task(title="Sign the contract and send the payment",
                         approval_required=False)
        self.assertTrue(tasks.get_task(tid)["approval_required"])

    def test_explicit_risk_flag_forces_the_gate(self):
        tid = self._task(title="Routine note", approval_required=False,
                         risk_flags=["legal_or_financial"])
        self.assertTrue(tasks.get_task(tid)["approval_required"])

    def test_plain_task_still_flows_without_approval(self):
        tid = self._task(title="Tidy the weekly notes")
        self.assertFalse(tasks.get_task(tid)["approval_required"])
        self.assertEqual(call(tools.ceo_task_assign, task_id=tid, owner="CFO")["status"], "success")

    # --- the owner paths still work -------------------------------------
    def test_owner_slash_command_approves_then_assign_works(self):
        tid = self._task(title="Pay the $9,000 invoice", approval_required=True)
        reply = self.ctx.commands["task-approve"]["fn"](tid) if isinstance(
            self.ctx.commands["task-approve"], dict) else self.ctx.commands["task-approve"](tid)
        self.assertIn("Approved", reply)
        row = tasks.get_task(tid)
        self.assertEqual((row["status"], row["approved_by"]), ("approved", "owner"))
        self.assertEqual(call(tools.ceo_task_assign, task_id=tid, owner="CFO")["status"], "success")

    def test_slash_command_is_a_real_handler_not_a_model_prompt(self):
        entry = self.ctx.commands["task-approve"]
        fn = entry["fn"] if isinstance(entry, dict) else entry
        self.assertNotIn("call ceo_", fn("").lower())
        self.assertIn("Usage", fn(""))

    def test_slash_command_guards(self):
        entry = self.ctx.commands["task-approve"]
        fn = entry["fn"] if isinstance(entry, dict) else entry
        self.assertIn("No task", fn("no-such-id"))
        plain = self._task(title="Tidy notes")
        self.assertIn("does not need approval", fn(plain))
        tid = self._task(title="Pay the $9,000 invoice", approval_required=True)
        fn(tid)
        self.assertIn("not 'proposed'", fn(tid))


class DbPath(unittest.TestCase):
    def test_default_path_follows_hermes_home(self):
        import importlib
        old = {k: os.environ.pop(k, None) for k in ("CEO_PACK_TASK_DB",)}
        try:
            with tempfile.TemporaryDirectory() as h:
                os.environ["HERMES_HOME"] = h
                importlib.reload(tasks)
                self.assertTrue(str(tasks._resolve_db_path()).startswith(h))
        finally:
            os.environ.pop("HERMES_HOME", None)
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
            importlib.reload(tasks)


if __name__ == "__main__":
    unittest.main()
