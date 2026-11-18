"""v0.5.0 dashboard tests: the tab's backend reads the same registry the
tools write. Every assertion below compares an API response against a
seeded registry read — never against a hardcoded expectation of what the
dashboard "should" show.

Covers Chat's dashboard acceptance table:
- organization matches the actual roster
- agent activity matches actual execution records
- tasks match the persistent registry
- task status never invents transitions
- approvals show actual pending decisions
- unauthorized actions rejected (read-only API: no write routes)
- refresh doesn't lose state
- empty states show no fabricated activity
- failed API requests are visible (status + detail, not silent)
- no credentials or private business data exposed
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_api():
    spec = importlib.util.spec_from_file_location(
        "ceo_pack_dashboard_api", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ceo_pack_dashboard_api"] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load_api()

sys.path.insert(0, str(ROOT))
import orchestration as orch
import tasks as tasks_mod
import tools as tools_mod


def _call(handler, **kwargs):
    """Call a route handler directly (no fastapi needed). Returns the
    payload, or raises the handler's HTTPException for error cases."""
    return handler(**kwargs)


def _routes():
    """Normalize the router's registered routes to (methods, path) pairs,
    for both the real FastAPI router and the no-fastapi stub."""
    out = []
    for route in api.router.routes:
        if isinstance(route, tuple):
            method, path, _fn = route
            out.append(({method}, path))
        else:
            out.append((set(getattr(route, "methods", set())), route.path))
    return out


def _err_of(fn):
    """Run fn, return the HTTPException it raises (status_code, detail)."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- the test asserts on the error
        return getattr(exc, "status_code", None), getattr(exc, "detail", None)
    return None, None


class DashboardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "dash.db")
        self._old_env = os.environ.get("CEO_PACK_TASK_DB")
        os.environ["CEO_PACK_TASK_DB"] = self.db
        tasks_mod.init_db(self.db)
        api._DB_READY = False
        # Save + isolate orchestration module state (aliases, display,
        # bindings, head seat, install config) so roster tests cannot leak
        # into other test files sharing this process.
        self._saved_orch = (
            dict(orch.SEAT_ALIASES), dict(orch.SEAT_DISPLAY),
            dict(orch.SEAT_BINDINGS), orch.HEAD_SEAT, dict(orch.INSTALL),
        )

    def tearDown(self):
        (aliases, displays, bindings, head, install) = self._saved_orch
        orch.SEAT_ALIASES.clear()
        orch.SEAT_ALIASES.update(aliases)
        orch.SEAT_DISPLAY.clear()
        orch.SEAT_DISPLAY.update(displays)
        orch.SEAT_BINDINGS.clear()
        orch.SEAT_BINDINGS.update(bindings)
        orch.set_head_seat(head)
        orch.INSTALL.clear()
        orch.INSTALL.update(install)
        if self._old_env is None:
            os.environ.pop("CEO_PACK_TASK_DB", None)
        else:
            os.environ["CEO_PACK_TASK_DB"] = self._old_env
        self.tmp.cleanup()

    def make_run(self, goal="Grow Harbor Lane subscriptions"):
        return json.loads(tools_mod.ceo_run_create(
            {"goal": goal}))["run_id"]

    def make_task(self, run_id=None, seat="CFO", title="Reconcile books", **kw):
        args = {"run_id": run_id or self.make_run(), "seat": seat, "title": title}
        args.update(kw)
        return json.loads(tools_mod.ceo_task_create(args))["task_id"]

    def walk(self, tid, *statuses):
        for status in statuses:
            self.assertTrue(
                tasks_mod.update_task_status(tid, status, db_path=self.db),
                "legal walk failed at %s" % status)


class TestManifest(DashboardBase):
    def test_manifest_valid(self):
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
        for key in ("name", "label", "description", "icon", "version", "tab", "entry", "css", "api"):
            self.assertIn(key, manifest, key)
        self.assertEqual(manifest["name"], "ceo-pack")
        self.assertEqual(manifest["tab"]["path"], "/ceo-pack")
        self.assertEqual(manifest["tab"]["position"], "after:kanban")
        self.assertEqual(manifest["version"], "0.5.0")
        self.assertTrue((ROOT / "dashboard" / manifest["entry"]).exists())
        self.assertTrue((ROOT / "dashboard" / manifest["css"]).exists())
        self.assertTrue((ROOT / "dashboard" / manifest["api"]).exists())

    def test_entry_registers_plugin(self):
        js = (ROOT / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn('register("ceo-pack"', js)
        self.assertIn("__HERMES_PLUGIN_SDK__", js)
        self.assertIn("/api/plugins/ceo-pack", js)


class TestEmptyStates(DashboardBase):
    def test_fresh_registry_shows_empty_not_activity(self):
        summary = _call(api.summary)
        self.assertTrue(summary["empty"])
        self.assertEqual(summary["tasks_total"], 0)
        self.assertEqual(summary["tasks_by_status"], {})
        self.assertEqual(summary["pending_approvals"], 0)
        self.assertEqual(summary["running"], 0)
        self.assertEqual(summary["stuck"], 0)
        self.assertEqual(_call(api.list_tasks)["tasks"], [])
        self.assertEqual(_call(api.activity)["recent_history"], [])
        self.assertEqual(_call(api.approvals)["pending"], [])
        self.assertEqual(_call(api.approvals)["pending_count"], 0)
        self.assertEqual(_call(api.list_runs)["runs"], [])

    def test_roster_without_snapshot_is_default_not_invented(self):
        roster = _call(api.roster)
        self.assertFalse(roster["snapshot_loaded"])
        self.assertEqual(len(roster["seats"]), 8)
        self.assertTrue(all(s["bound"] is False for s in roster["seats"]))
        self.assertTrue(all(s["profile"] is None for s in roster["seats"]))
        self.assertEqual(roster["departments"], [])


class TestTasksMatchRegistry(DashboardBase):
    def test_tasks_endpoint_matches_registry(self):
        run_id = self.make_run()
        tid = self.make_task(run_id, title="Reconcile October books")
        self.make_task(run_id, seat="CTO", title="Review API security")
        resp = _call(api.list_tasks)
        self.assertEqual(resp["count"], 2)
        by_id = {t["task_id"]: t for t in resp["tasks"]}
        self.assertEqual(by_id[tid]["title"], "Reconcile October books")
        self.assertEqual(by_id[tid]["status"], "proposed")
        # Same rows the registry itself returns.
        self.assertEqual(
            {t["id"] for t in tasks_mod.list_tasks(db_path=self.db)},
            set(by_id))

    def test_summary_counts_match_registry(self):
        run_id = self.make_run()
        t1 = self.make_task(run_id)
        t2 = self.make_task(run_id, seat="CTO")
        self.walk(t1, "approved", "assigned", "running")
        self.walk(t2, "approved", "assigned", "running", "completed", "verified")
        summary = _call(api.summary)
        self.assertEqual(summary["tasks_by_status"], {"running": 1, "verified": 1})
        self.assertEqual(summary["tasks_total"], 2)
        self.assertFalse(summary["empty"])

    def test_refresh_does_not_lose_state(self):
        run_id = self.make_run()
        self.make_task(run_id)
        first = _call(api.list_tasks)
        second = _call(api.list_tasks)
        first.pop("generated_at", None)
        second.pop("generated_at", None)
        self.assertEqual(first, second)
        first_sum = _call(api.summary)
        second_sum = _call(api.summary)
        first_sum.pop("generated_at", None)
        second_sum.pop("generated_at", None)
        self.assertEqual(first_sum, second_sum)


class TestStatusNeverInvented(DashboardBase):
    LEGAL = {"proposed", "approved", "assigned", "running", "blocked",
             "completed", "verified", "failed"}

    def test_status_is_exactly_the_registry_status(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        # Assigned is not "in progress"; completed is not "verified".
        self.walk(tid, "approved", "assigned")
        task = _call(api.task_detail, task_id=tid)
        self.assertEqual(task["status"], "assigned")
        self.assertNotEqual(task["status"], "running")
        self.walk(tid, "running", "completed")
        task = _call(api.task_detail, task_id=tid)
        self.assertEqual(task["status"], "completed")
        self.assertNotEqual(task["status"], "verified")
        self.walk(tid, "verified")
        self.assertEqual(_call(api.task_detail, task_id=tid)["status"], "verified")

    def test_every_returned_status_is_a_legal_status(self):
        run_id = self.make_run()
        for seat in ("CFO", "CTO", "CMO"):
            self.make_task(run_id, seat=seat)
        for t in _call(api.list_tasks)["tasks"]:
            self.assertIn(t["status"], self.LEGAL)
        for status, count in _call(api.summary)["tasks_by_status"].items():
            self.assertIn(status, self.LEGAL)


class TestActivityMatchesExecution(DashboardBase):
    def test_activity_reflects_real_transitions(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        self.walk(tid, "approved", "assigned", "running")
        act = _call(api.activity)
        self.assertEqual([t["task_id"] for t in act["running"]], [tid])
        self.assertEqual(act["stuck"], [])
        trail = [(e["from_status"], e["to_status"])
                 for e in act["recent_history"] if e["task_id"] == tid]
        # History is newest first; the walk order is reversed in the feed.
        self.assertEqual(list(reversed(trail)), [
            (None, "proposed"), ("proposed", "approved"),
            ("approved", "assigned"), ("assigned", "running")])

    def test_stuck_tasks_come_from_registry(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        self.walk(tid, "approved", "assigned", "running", "blocked")
        act = _call(api.activity)
        # Blocked with retries left is not stuck yet — retries are not exhausted.
        self.assertEqual(act["stuck"], [])
        tasks_mod.increment_retry(tid, db_path=self.db)
        act = _call(api.activity)
        self.assertEqual([t["task_id"] for t in act["stuck"]], [tid])
        self.assertEqual(act["running"], [])

    def test_task_detail_history_matches_registry(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        self.walk(tid, "approved", "assigned")
        detail = _call(api.task_detail, task_id=tid)
        self.assertEqual(
            [(h["from_status"], h["to_status"]) for h in detail["history"]],
            [(None, "proposed"), ("proposed", "approved"), ("approved", "assigned")])
        self.assertEqual(detail["dependencies"], [])
        self.assertTrue(detail["dependencies_met"])


class TestExecutionVisible(DashboardBase):
    """Phase 3: the task detail (and list) surface the validated
    delegated-execution record; cost is honest — present when the host
    supplied it, "unknown" when it didn't. Metadata stays stripped."""

    def _full_entry(self):
        return {
            "route": {"backend": "ceo_pack",
                      "label": "CFO (model: openrouter/anthropic/claude-sonnet-5.5)",
                      "provider": "openrouter",
                      "model": "anthropic/claude-sonnet-5.5",
                      "reasoning_effort": "high", "service_tier": "priority"},
            "tokens": {"input": 1200, "output": 340},
            "cost_usd": 0.0042, "cost_status": "ok",
            "api_calls": 3, "duration_seconds": 12.5,
            "exit_reason": "completed",
        }

    def test_task_detail_includes_execution(self):
        tid = self.make_task()
        self.assertTrue(tasks_mod.record_execution(
            tid, self._full_entry(), db_path=self.db))
        detail = _call(api.task_detail, task_id=tid)
        self.assertEqual(detail["execution"]["route"]["model"],
                         "anthropic/claude-sonnet-5.5")
        self.assertEqual(detail["execution"]["route"]["reasoning_effort"], "high")
        self.assertEqual(detail["execution"]["tokens"],
                         {"input": 1200, "output": 340})
        self.assertEqual(detail["execution"]["cost_usd"], 0.0042)
        self.assertEqual(detail["execution"]["cost_status"], "ok")
        self.assertNotIn("metadata", detail)

    def test_task_detail_execution_null_when_none(self):
        tid = self.make_task()
        detail = _call(api.task_detail, task_id=tid)
        self.assertIsNone(detail["execution"])

    def test_unknown_cost_honest_in_detail(self):
        tid = self.make_task()
        entry = {"route": {"backend": "ceo_pack", "provider": "openrouter",
                           "model": "anthropic/claude-sonnet-5.5"},
                 "tokens": {"input": 10, "output": 5}}
        self.assertTrue(tasks_mod.record_execution(tid, entry, db_path=self.db))
        detail = _call(api.task_detail, task_id=tid)
        self.assertNotIn("cost_usd", detail["execution"])
        self.assertEqual(detail["execution"]["cost_status"], "unknown")

    def test_tasks_list_includes_execution(self):
        tid = self.make_task()
        self.assertTrue(tasks_mod.record_execution(
            tid, {"route": {"provider": "openrouter"}}, db_path=self.db))
        listed = _call(api.list_tasks)
        row = [t for t in listed["tasks"] if t["task_id"] == tid][0]
        self.assertEqual(row["execution"]["route"]["provider"], "openrouter")
        self.assertEqual(row["execution"]["cost_status"], "unknown")


class TestApprovals(DashboardBase):
    def test_pending_approvals_are_real(self):
        run_id = self.make_run()
        tid = self.make_task(run_id, approval_required=True)
        self.make_task(run_id, approval_required=False)
        resp = _call(api.approvals)
        self.assertEqual(resp["pending_count"], 1)
        self.assertEqual([t["task_id"] for t in resp["pending"]], [tid])
        # Approving removes it from pending and records it in history.
        tasks_mod.approve_task(tid, "Owner", db_path=self.db)
        resp = _call(api.approvals)
        self.assertEqual(resp["pending_count"], 0)
        self.assertEqual([h["task_id"] for h in resp["history"]], [tid])
        self.assertEqual(resp["history"][0]["actor"], "Owner")

    def test_decisions_note_is_honest(self):
        resp = _call(api.approvals)
        self.assertIn("not stored", resp["decisions_note"])
        self.assertIn("ceo_decision_log", resp["decisions_note"])


class TestRosterMatchesReality(DashboardBase):
    def _seed_snapshot(self):
        orch.set_seat_aliases({"Sam": "CFO"})
        orch.set_head_seat("CFO")
        orch.set_seat_display("CFO", display_name="Sam", avatar="https://example.invalid/avatar.png")
        orch.set_seat_binding("CFO", profile="cfo-profile", provided=["read numbers and summaries"])
        orch.write_roster_snapshot(tasks_mod.roster_snapshot_path(self.db))

    def test_roster_matches_applied_config(self):
        self._seed_snapshot()
        roster = _call(api.roster)
        self.assertTrue(roster["snapshot_loaded"])
        cfo = next(s for s in roster["seats"] if s["seat"] == "CFO")
        self.assertEqual(cfo["display_name"], "Sam")
        self.assertEqual(cfo["avatar_url"], "https://example.invalid/avatar.png")
        self.assertEqual(cfo["profile"], "cfo-profile")
        self.assertTrue(cfo["bound"])
        self.assertEqual(roster["head_seat"], "CFO")

    def test_org_tree_root_is_head_seat(self):
        self._seed_snapshot()
        roster = _call(api.roster)
        self.assertEqual(roster["org"]["root"]["seat"], "CFO")
        self.assertEqual(len(roster["org"]["root"]["children"]), 7)
        self.assertEqual(roster["org"]["departments"], [])
        # Every seat appears exactly once in the tree.
        seen = [roster["org"]["root"]["seat"]]
        seen += [c["seat"] for c in roster["org"]["root"]["children"]]
        self.assertEqual(sorted(seen), sorted(s["seat"] for s in roster["seats"]))

    def test_non_https_avatar_is_never_a_url(self):
        orch.set_seat_display("CFO", avatar="/local/path/avatar.png")
        orch.write_roster_snapshot(tasks_mod.roster_snapshot_path(self.db))
        roster = _call(api.roster)
        cfo = next(s for s in roster["seats"] if s["seat"] == "CFO")
        self.assertIsNone(cfo["avatar_url"])
        self.assertTrue(cfo["initials"])

    def test_snapshot_round_trip(self):
        orch.set_seat_aliases({"Sam": "CFO"})
        orch.set_seat_display("CTO", display_name="Ada")
        path = tasks_mod.roster_snapshot_path(self.db)
        self.assertTrue(orch.write_roster_snapshot(path))
        # Wipe in-memory state, then re-apply from the file.
        orch.SEAT_ALIASES.clear()
        orch.SEAT_DISPLAY.clear()
        orch.set_head_seat("CEO")
        applied, notes = orch.apply_roster_snapshot(orch.read_roster_snapshot(path))
        self.assertEqual(orch.get_seat_aliases(), {"SAM": "CFO"})
        self.assertEqual(orch.get_seat_display("CTO").get("display_name"), "Ada")
        self.assertEqual(notes, [])

    def test_corrupt_snapshot_is_ignored(self):
        path = tasks_mod.roster_snapshot_path(self.db)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(orch.read_roster_snapshot(path))
        roster = _call(api.roster)
        self.assertFalse(roster["snapshot_loaded"])
        self.assertEqual(len(roster["seats"]), 8)


class TestNoSecrets(DashboardBase):
    def test_results_and_metadata_never_leave_the_api(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        tasks_mod.update_task_status(
            tid, "approved", db_path=self.db)
        tasks_mod.update_task_status(
            tid, "assigned", db_path=self.db)
        tasks_mod.update_task_status(
            tid, "running", db_path=self.db)
        tasks_mod.update_task_status(
            tid, "failed", db_path=self.db,
            error={"detail": "sk-FAKEFAKEFAKE1234567890"})
        tasks_mod.update_task_status(
            tid, "running", db_path=self.db,
            result={"api_key": "sk-FAKEFAKEFAKE1234567890"})
        blob = json.dumps(_call(api.list_tasks))
        blob += json.dumps(_call(api.activity))
        blob += json.dumps(_call(api.task_detail, task_id=tid))
        self.assertNotIn("sk-FAKEFAKEFAKE1234567890", blob)
        self.assertNotIn("api_key", blob)
        task = _call(api.task_detail, task_id=tid)
        for banned in ("result", "result_json", "error", "error_json", "metadata"):
            self.assertNotIn(banned, task)


class TestErrorsVisibleAndReadOnly(DashboardBase):
    def test_no_write_routes_exist(self):
        for methods, path in _routes():
            self.assertEqual(methods, {"GET"}, path)

    def test_unknown_task_and_run_are_404_not_silent(self):
        code, detail = _err_of(lambda: _call(api.task_detail, task_id="no-such-task"))
        self.assertEqual(code, 404)
        self.assertIn("not found", detail)
        code, detail = _err_of(lambda: _call(api.run_detail, run_id="no-such-run"))
        self.assertEqual(code, 404)
        self.assertIn("not found", detail)

    def test_bad_input_is_400_with_detail(self):
        code, detail = _err_of(lambda: _call(api.list_tasks, limit=999))
        self.assertEqual(code, 400)
        self.assertIn("limit", detail)
        code, detail = _err_of(lambda: _call(api.list_tasks, limit="abc"))
        self.assertEqual(code, 400)
        code, detail = _err_of(lambda: _call(api.list_runs, limit=-1))
        self.assertEqual(code, 400)

    def test_routes_cover_the_contract(self):
        paths = {path for _methods, path in _routes()}
        for expected in ("/", "/summary", "/roster", "/tasks", "/tasks/{task_id}",
                         "/runs", "/runs/{run_id}", "/activity", "/approvals"):
            self.assertIn(expected, paths, expected)


class TestRuns(DashboardBase):
    def test_runs_match_registry(self):
        run_id = self.make_run("Winter Blend launch")
        self.make_task(run_id)
        self.make_task(run_id, seat="CTO")
        runs = _call(api.list_runs)
        self.assertEqual(runs["count"], 1)
        self.assertEqual(runs["runs"][0]["goal"], "Winter Blend launch")
        self.assertEqual(runs["runs"][0]["task_count"], 2)
        detail = _call(api.run_detail, run_id=run_id)
        self.assertEqual(detail["status_counts"], {"proposed": 2})
        self.assertEqual(len(detail["tasks"]), 2)


if __name__ == "__main__":
    unittest.main()
