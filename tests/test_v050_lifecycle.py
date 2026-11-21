"""Task-lifecycle hardening tests (Chat #4 + #5, Nov 21, 2026).

- ceo_task_complete: running -> completed only; completed is NOT verified,
  and no verify path exists (follow-up, not built here).
- ceo_task_assign duplicate protection: same owner re-assign is an
  idempotent no-op, a different owner is a conflict (no overwrite),
  terminal/running tasks refuse assignment.
- due_date audit: the create path already accepted + validated due_date
  (schemas, tool, dashboard route — verified, not re-built). What this
  file pins is the NEW derived `overdue` flag in the API payload and the
  frontends reading it instead of computing it locally.
- cost_mode / model_choice precedence (Chat #5): model selects, cost
  constrains, conflicts are rejected with the permitted keys named, never
  silently substituted.

Fictional data only; every test uses a fresh temp DB file.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import orchestration as orch  # noqa: E402
import tasks as tasks_mod  # noqa: E402
import tools as tools_mod  # noqa: E402


def _load_api():
    spec = importlib.util.spec_from_file_location(
        "ceo_pack_dashboard_api_lifecycle", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ceo_pack_dashboard_api_lifecycle"] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load_api()


def _call(handler, **kwargs):
    return handler(**kwargs)


def _err_of(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- the test asserts on the error
        return getattr(exc, "status_code", None), getattr(exc, "detail", None)
    return None, None


def _tool(fn, args):
    return json.loads(fn(args))


class LifecycleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "life.db")
        self._old_env = os.environ.get("CEO_PACK_TASK_DB")
        os.environ["CEO_PACK_TASK_DB"] = self.db
        tasks_mod.init_db(self.db)
        api._DB_READY = False
        self._saved_install = dict(orch.INSTALL)
        orch.reset_install_config()

    def tearDown(self):
        orch.INSTALL.clear()
        orch.INSTALL.update(self._saved_install)
        if self._old_env is None:
            os.environ.pop("CEO_PACK_TASK_DB", None)
        else:
            os.environ["CEO_PACK_TASK_DB"] = self._old_env
        self.tmp.cleanup()

    def make_run(self, goal="Grow Harbor Lane subscriptions"):
        return json.loads(tools_mod.ceo_run_create({"goal": goal}))["run_id"]

    def make_task(self, run_id=None, seat="CFO", title="Reconcile books", **kw):
        args = {"run_id": run_id or self.make_run(), "seat": seat, "title": title}
        args.update(kw)
        return json.loads(tools_mod.ceo_task_create(args))["task_id"]

    def walk(self, tid, *statuses):
        for status in statuses:
            self.assertTrue(
                tasks_mod.update_task_status(tid, status, db_path=self.db),
                "legal walk failed at %s" % status)

    def running_task(self, **kw):
        tid = self.make_task(**kw)
        self.walk(tid, "approved", "assigned", "running")
        return tid


class TestCompleteTool(LifecycleBase):
    def test_complete_running_task_is_completed_not_verified(self):
        tid = self.running_task()
        out = _tool(tools_mod.ceo_task_complete, {"task_id": tid})
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["task_status"], "completed")
        self.assertIn("not verified", out["note"])
        row = tasks_mod.get_task(tid, db_path=self.db)
        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["completed_at"])
        # Completed is not verified, and nothing here set verified.
        self.assertNotEqual(row["status"], "verified")

    def test_complete_from_proposed_assigned_blocked_failed_refused(self):
        cases = {
            "proposed": self.make_task(),
            "assigned": None,
            "blocked": None,
            "failed": None,
        }
        tid_assigned = self.make_task()
        self.walk(tid_assigned, "approved", "assigned")
        cases["assigned"] = tid_assigned
        tid_blocked = self.running_task()
        self.walk(tid_blocked, "blocked")
        cases["blocked"] = tid_blocked
        tid_failed = self.running_task()
        self.walk(tid_failed, "failed")
        cases["failed"] = tid_failed
        for status, tid in cases.items():
            out = _tool(tools_mod.ceo_task_complete, {"task_id": tid})
            self.assertEqual(out["status"], "error", status)
            self.assertIn("'%s'" % status, out["error"], status)
            self.assertIn("not 'running'", out["error"], status)
            self.assertEqual(tasks_mod.get_task(tid, db_path=self.db)["status"], status)

    def test_complete_terminal_states_refused_not_silent(self):
        tid_completed = self.running_task()
        _tool(tools_mod.ceo_task_complete, {"task_id": tid_completed})
        tid_verified = self.running_task()
        _tool(tools_mod.ceo_task_complete, {"task_id": tid_verified})
        self.walk(tid_verified, "verified")
        tid_rejected = self.make_task()
        tasks_mod.update_task_status(tid_rejected, "rejected", db_path=self.db)
        for tid, status in ((tid_completed, "completed"),
                            (tid_verified, "verified"),
                            (tid_rejected, "rejected")):
            out = _tool(tools_mod.ceo_task_complete, {"task_id": tid})
            self.assertEqual(out["status"], "error", status)
            self.assertIn("already '%s'" % status, out["error"], status)
            self.assertEqual(tasks_mod.get_task(tid, db_path=self.db)["status"], status)

    def test_complete_unknown_task_errors(self):
        out = _tool(tools_mod.ceo_task_complete, {"task_id": "no-such-task"})
        self.assertEqual(out["status"], "error")
        self.assertIn("not found", out["error"])

    def test_complete_route_happy_path_and_409s(self):
        tid = self.running_task()
        out = _call(api.complete_task_route, task_id=tid)
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["task_id"], tid)
        # Completing again is a 409, not a silent no-op.
        code, detail = _err_of(lambda: _call(api.complete_task_route, task_id=tid))
        self.assertEqual(code, 409)
        self.assertIn("already 'completed'", detail)
        # A proposed task cannot complete.
        tid2 = self.make_task()
        code, detail = _err_of(lambda: _call(api.complete_task_route, task_id=tid2))
        self.assertEqual(code, 409)
        self.assertIn("'proposed'", detail)
        self.assertIn("not 'running'", detail)
        code, _d = _err_of(lambda: _call(api.complete_task_route, task_id="no-such-task"))
        self.assertEqual(code, 404)


class TestAssignDuplicates(LifecycleBase):
    def test_reassign_same_owner_is_idempotent(self):
        tid = self.make_task()
        out1 = _tool(tools_mod.ceo_task_assign, {"task_id": tid, "owner": "CFO"})
        self.assertEqual(out1["status"], "success")
        hist_before = tasks_mod.get_task_history(tid, db_path=self.db)
        assigned_before = [h for h in hist_before if h["to_status"] == "assigned"]
        out2 = _tool(tools_mod.ceo_task_assign, {"task_id": tid, "owner": "CFO"})
        self.assertEqual(out2["status"], "success")
        self.assertEqual(out2["task_status"], "assigned")
        self.assertIn("Already assigned", out2["note"])
        self.assertIn("nothing changed", out2["note"])
        row = tasks_mod.get_task(tid, db_path=self.db)
        self.assertEqual(row["owner"], "CFO")
        self.assertEqual(row["status"], "assigned")
        # No duplicate work: still one task, no extra history row.
        self.assertEqual(len(tasks_mod.get_tasks_by_run(row["run_id"], db_path=self.db)), 1)
        hist_after = tasks_mod.get_task_history(tid, db_path=self.db)
        self.assertEqual(
            len([h for h in hist_after if h["to_status"] == "assigned"]),
            len(assigned_before))

    def test_reassign_same_owner_idempotent_on_dashboard(self):
        tid = self.make_task()
        _call(api.assign_task_route, task_id=tid, body={"owner": "CFO"})
        out = _call(api.assign_task_route, task_id=tid, body={"owner": "CFO"})
        self.assertEqual(out["status"], "assigned")
        self.assertEqual(out["owner"], "CFO")

    def test_reassign_different_owner_is_conflict_no_overwrite(self):
        tid = self.make_task()
        _tool(tools_mod.ceo_task_assign, {"task_id": tid, "owner": "CFO"})
        out = _tool(tools_mod.ceo_task_assign, {"task_id": tid, "owner": "CTO"})
        self.assertEqual(out["status"], "error")
        self.assertIn("already assigned to 'CFO'", out["error"])
        self.assertIn("refused", out["error"])
        row = tasks_mod.get_task(tid, db_path=self.db)
        self.assertEqual(row["owner"], "CFO")
        self.assertEqual(row["status"], "assigned")

    def test_reassign_different_owner_conflicts_on_dashboard(self):
        tid = self.make_task()
        _call(api.assign_task_route, task_id=tid, body={"owner": "CFO"})
        code, detail = _err_of(
            lambda: _call(api.assign_task_route, task_id=tid, body={"owner": "CTO"}))
        self.assertEqual(code, 409)
        self.assertIn("already assigned to 'CFO'", detail)
        row = tasks_mod.get_task(tid, db_path=self.db)
        self.assertEqual(row["owner"], "CFO")

    def test_assign_terminal_and_running_tasks_refused(self):
        tid_completed = self.running_task()
        _tool(tools_mod.ceo_task_complete, {"task_id": tid_completed})
        out = _tool(tools_mod.ceo_task_assign, {"task_id": tid_completed, "owner": "CFO"})
        self.assertEqual(out["status"], "error")
        self.assertIn("'completed'", out["error"])
        code, _d = _err_of(
            lambda: _call(api.assign_task_route, task_id=tid_completed, body={"owner": "CFO"}))
        self.assertEqual(code, 409)
        tid_running = self.running_task()
        out = _tool(tools_mod.ceo_task_assign, {"task_id": tid_running, "owner": "CFO"})
        self.assertEqual(out["status"], "error")
        self.assertIn("'running'", out["error"])


class TestOverdueDerived(LifecycleBase):
    def test_overdue_flag_is_derived_at_read_time(self):
        run_id = self.make_run()
        past = self.make_task(run_id, due_date="2020-01-01")
        future = self.make_task(run_id, due_date="2099-01-01")
        nodue = self.make_task(run_id)
        done_past = self.make_task(run_id, due_date="2020-01-01")
        self.walk(done_past, "approved", "assigned", "running", "completed")
        listed = {t["task_id"]: t for t in _call(api.list_tasks)["tasks"]}
        self.assertTrue(listed[past]["overdue"])
        self.assertFalse(listed[future]["overdue"])
        self.assertFalse(listed[nodue]["overdue"])
        # Terminal with a past due date is not overdue — overdue is a
        # live-work concept, and the flag is never stored on the row.
        self.assertFalse(listed[done_past]["overdue"])
        detail = _call(api.task_detail, task_id=past)
        self.assertTrue(detail["overdue"])
        row = tasks_mod.get_task(past, db_path=self.db)
        self.assertNotIn("overdue", row)

    def test_due_date_validation_is_the_same_both_paths(self):
        # Tool path and dashboard path share one rule + one wording.
        out = _tool(tools_mod.ceo_task_create,
                    {"run_id": self.make_run(), "seat": "CFO", "title": "x",
                     "due_date": "15-11-2026"})
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["error"], "due_date must be ISO format YYYY-MM-DD.")
        code, detail = _err_of(lambda: _call(api.create_task_route, body={
            "run_id": self.make_run(), "seat": "CFO", "title": "x",
            "due_date": "15-11-2026"}))
        self.assertEqual(code, 400)
        self.assertEqual(detail, "due_date must be ISO format YYYY-MM-DD.")
        # A well-formed but impossible date fails the same way both paths.
        out = _tool(tools_mod.ceo_task_create,
                    {"run_id": self.make_run(), "seat": "CFO", "title": "x",
                     "due_date": "2026-02-30"})
        self.assertEqual(out["error"], "due_date is not a real calendar date.")
        code, detail = _err_of(lambda: _call(api.create_task_route, body={
            "run_id": self.make_run(), "seat": "CFO", "title": "x",
            "due_date": "2026-02-30"}))
        self.assertEqual(code, 400)
        self.assertEqual(detail, "due_date is not a real calendar date.")

    def test_frontends_read_the_flag_instead_of_computing_it(self):
        web = (ROOT / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn("t.overdue", web)
        self.assertNotIn("isOverdue", web)
        desk = (ROOT / "desktop" / "plugin.js").read_text()
        self.assertIn("task.overdue", desk)
        self.assertNotIn("isOverdue", desk)


class TestCostModelPrecedence(LifecycleBase):
    def test_free_mode_rejects_paid_model_with_the_permitted_keys(self):
        orch.INSTALL["cost_mode"] = "free"
        plan = orch.plan_tool_calls("Grow newsletter", model_key="paid_flagship")
        self.assertFalse(plan["valid"])
        self.assertEqual(plan["status"], "error")
        self.assertIn("paid_flagship", plan["error"])
        self.assertIn("free", plan["error"])
        for key in ("free_default", "free_worker", "free_worker_2", "free_random"):
            self.assertIn(key, plan["error"])
        self.assertNotIn("model", plan)

    def test_free_mode_accepts_free_models(self):
        orch.INSTALL["cost_mode"] = "free"
        for key in ("free_default", "free_worker", "free_worker_2", "free_random"):
            plan = orch.plan_tool_calls("Grow newsletter", model_key=key)
            self.assertTrue(plan.get("valid", True), key)
            self.assertEqual(plan["model"]["key"], key)

    def test_paid_modes_accept_their_tier_and_cheaper(self):
        orch.INSTALL["cost_mode"] = "paid_value"
        self.assertEqual(
            orch.plan_tool_calls("x", model_key="paid_value")["model"]["key"], "paid_value")
        self.assertEqual(
            orch.plan_tool_calls("x", model_key="free_default")["model"]["key"], "free_default")
        # But paid_value does not buy the flagship.
        plan = orch.plan_tool_calls("x", model_key="paid_flagship")
        self.assertFalse(plan["valid"])
        orch.INSTALL["cost_mode"] = "paid_flagship"
        self.assertEqual(
            orch.plan_tool_calls("x", model_key="paid_flagship")["model"]["key"],
            "paid_flagship")

    def test_no_model_uses_the_cost_mode_default(self):
        orch.INSTALL["cost_mode"] = "free"
        self.assertEqual(
            orch.plan_tool_calls("x", model_key=None)["model"]["key"], "free_default")
        orch.INSTALL["cost_mode"] = "paid_value"
        self.assertEqual(
            orch.plan_tool_calls("x", model_key=None)["model"]["key"], "paid_value")
        orch.INSTALL["cost_mode"] = "paid_flagship"
        self.assertEqual(
            orch.plan_tool_calls("x", model_key=None)["model"]["key"], "paid_flagship")

    def test_unknown_model_key_is_rejected_not_substituted(self):
        orch.INSTALL["cost_mode"] = "free"
        plan = orch.plan_tool_calls("x", model_key="nonsense")
        self.assertFalse(plan["valid"])
        self.assertIn("nonsense", plan["error"])
        self.assertIn("No model was substituted", plan["error"])
        self.assertNotIn("model", plan)

    def test_unknown_cost_mode_is_rejected(self):
        picked = orch._pick_model("free_default", cost_mode="platinum")
        self.assertFalse(picked["valid"])
        self.assertIn("platinum", picked["error"])

    def test_install_config_refuses_incompatible_model_keeps_previous(self):
        orch.INSTALL["cost_mode"] = "free"
        orch.INSTALL["model_choice"] = "free_default"
        applied, notes = orch.apply_install_config({"model_choice": "paid_flagship"})
        self.assertNotIn("model_choice", applied)
        self.assertEqual(orch.INSTALL["model_choice"], "free_default")
        self.assertTrue(any("paid_flagship" in n and "free" in n for n in notes))
        self.assertTrue(any("free_default" in n for n in notes))

    def test_install_config_accepts_compatible_pair(self):
        applied, notes = orch.apply_install_config(
            {"cost_mode": "paid_flagship", "model_choice": "paid_flagship"})
        self.assertIn("cost_mode", applied)
        self.assertIn("model_choice", applied)
        self.assertEqual(orch.INSTALL["cost_mode"], "paid_flagship")
        self.assertEqual(orch.INSTALL["model_choice"], "paid_flagship")

    def test_install_config_refuses_lowering_cost_below_current_model(self):
        orch.apply_install_config(
            {"cost_mode": "paid_flagship", "model_choice": "paid_flagship"})
        applied, notes = orch.apply_install_config({"cost_mode": "free"})
        self.assertNotIn("cost_mode", applied)
        self.assertEqual(orch.INSTALL["cost_mode"], "paid_flagship")
        self.assertTrue(any("paid_flagship" in n for n in notes))

    def test_install_config_both_supplied_clash_refuses_the_model(self):
        applied, notes = orch.apply_install_config(
            {"cost_mode": "free", "model_choice": "paid_value"})
        self.assertIn("cost_mode", applied)
        self.assertNotIn("model_choice", applied)
        self.assertEqual(orch.INSTALL["cost_mode"], "free")
        self.assertEqual(orch.INSTALL["model_choice"], "free_default")


class TestCompleteButton(LifecycleBase):
    def test_web_task_detail_has_mark_complete(self):
        js = (ROOT / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn("Mark complete", js)
        self.assertIn("/complete", js)
        self.assertIn("onComplete", js)
        self.assertIn("completeBusy", js)
        self.assertIn('status === "running"', js)
        self.assertIn("Completed, not verified", js)

    def test_desktop_task_card_has_mark_complete(self):
        js = (ROOT / "desktop" / "plugin.js").read_text()
        self.assertIn("Mark complete", js)
        self.assertIn("writes.complete", js)
        self.assertIn("canComplete", js)
        self.assertIn("task.status === 'running'", js)


if __name__ == "__main__":
    unittest.main()
