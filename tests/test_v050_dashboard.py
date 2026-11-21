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
- unauthorized actions rejected (write routes are exactly the four
  approved POST routes; every other route is GET)
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
             "completed", "verified", "failed", "rejected"}

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


class TestErrorsVisible(DashboardBase):
    def test_write_routes_are_exactly_the_approved_set(self):
        posts = sorted(p for m, p in _routes() if m == {"POST"})
        self.assertEqual(posts, [
            "/tasks",
            "/tasks/{task_id}/approve",
            "/tasks/{task_id}/assign",
            "/tasks/{task_id}/reject",
        ])
        for methods, path in _routes():
            if methods != {"POST"}:
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


class TestLastActivity(DashboardBase):
    """Per-seat last activity (Req 1's genuinely new bit): the org chart
    shows when each seat last did registry work, derived from the task
    rows + history — never invented, "unknown" when a seat has none."""

    def _expected_last_activity(self, seat):
        import sqlite3
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT MAX(at) FROM ("
            "  SELECT updated_at AS at FROM tasks WHERE seat = ?"
            "  UNION ALL "
            "  SELECT h.at FROM task_history h"
            "  JOIN tasks t ON t.id = h.task_id WHERE t.seat = ?)",
            (seat, seat)).fetchone()
        conn.close()
        return row[0]

    def _roster_node(self, seat):
        roster = _call(api.roster)
        node = next(s for s in roster["seats"] if s["seat"] == seat)
        return roster, node

    def test_seat_with_activity_shows_last_activity(self):
        tid = self.make_task(seat="CFO")
        self.walk(tid, "approved", "assigned")
        expected = self._expected_last_activity("CFO")
        self.assertIsNotNone(expected)
        roster, node = self._roster_node("CFO")
        self.assertEqual(node["last_activity"], expected)
        # The org tree carries the same value — one source, both views.
        child = next(c for c in roster["org"]["root"]["children"] if c["seat"] == "CFO")
        self.assertEqual(child["last_activity"], expected)

    def test_seat_without_activity_is_unknown_not_invented(self):
        roster = _call(api.roster)
        for node in roster["seats"]:
            self.assertIsNone(node["last_activity"], node["seat"])

    def test_last_activity_moves_when_task_moves(self):
        tid = self.make_task(seat="CFO")
        self.walk(tid, "approved")
        _roster, first = self._roster_node("CFO")
        self.walk(tid, "assigned", "running")
        _roster, second = self._roster_node("CFO")
        self.assertGreaterEqual(second["last_activity"], first["last_activity"])
        self.assertEqual(second["last_activity"], self._expected_last_activity("CFO"))

    def test_roster_survives_unavailable_registry(self):
        import sqlite3
        self.make_task(seat="CFO")
        orig = tasks_mod.get_seat_last_activity
        def _boom(db_path=None):
            raise sqlite3.OperationalError("registry gone")
        tasks_mod.get_seat_last_activity = _boom
        try:
            roster = _call(api.roster)
        finally:
            tasks_mod.get_seat_last_activity = orig
        for node in roster["seats"]:
            self.assertIsNone(node["last_activity"], node["seat"])


class TestDashboardWrites(DashboardBase):
    """The four POST routes: same backend, same checks as the chat tools,
    mapped to HTTP codes. Every assertion compares against a direct
    registry read, never against the response alone."""

    def _type1_task(self, seat="CFO"):
        run_id = self.make_run()
        return self.make_task(run_id, seat=seat, approval_required=True)

    def test_create_happy_path(self):
        run_id = self.make_run()
        out = _call(api.create_task_route, body={
            "run_id": run_id, "seat": "cfo", "title": "Reconcile books",
            "approval_required": True})
        self.assertEqual(out["seat"], "CFO")
        self.assertEqual(out["status"], "proposed")
        self.assertTrue(out["approval_required"])
        row = tasks_mod.get_task(out["task_id"])
        self.assertEqual(row["status"], "proposed")
        self.assertEqual(row["seat"], "CFO")

    def test_create_requires_fields(self):
        run_id = self.make_run()
        for body in ({"seat": "CFO", "title": "x"},
                     {"run_id": run_id, "title": "x"},
                     {"run_id": run_id, "seat": "CFO"}):
            code, _d = _err_of(lambda: _call(api.create_task_route, body=body))
            self.assertEqual(code, 400, body)
        code, _d = _err_of(lambda: _call(api.create_task_route, body=["not", "a", "dict"]))
        self.assertEqual(code, 400)

    def test_create_rejects_unknown_seat_run_and_due_date(self):
        run_id = self.make_run()
        code, _d = _err_of(lambda: _call(api.create_task_route, body={
            "run_id": run_id, "seat": "Wizard", "title": "x"}))
        self.assertEqual(code, 400)
        code, _d = _err_of(lambda: _call(api.create_task_route, body={
            "run_id": "no-such-run", "seat": "CFO", "title": "x"}))
        self.assertEqual(code, 400)
        code, _d = _err_of(lambda: _call(api.create_task_route, body={
            "run_id": run_id, "seat": "CFO", "title": "x", "due_date": "not-a-date"}))
        self.assertEqual(code, 400)

    def test_create_task_key_is_idempotent(self):
        run_id = self.make_run()
        body = {"run_id": run_id, "seat": "CFO", "title": "One",
                "task_key": "books-oct"}
        first = _call(api.create_task_route, body=body)
        second = _call(api.create_task_route, body=body)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(tasks_mod.get_tasks_by_run(run_id)), 1)

    def test_create_response_strips_secrets(self):
        run_id = self.make_run()
        out = _call(api.create_task_route, body={
            "run_id": run_id, "seat": "CFO", "title": "x",
            "metadata": {"secret": "shh"}, "description": "d"})
        for forbidden in ("metadata", "result", "error", "result_json", "error_json"):
            self.assertNotIn(forbidden, out)

    def test_approve_happy_path(self):
        tid = self._type1_task()
        out = _call(api.approve_task_route, task_id=tid)
        self.assertEqual(out["status"], "approved")
        self.assertEqual(out["approved_by"], "owner")
        row = tasks_mod.get_task(tid)
        self.assertEqual(row["status"], "approved")
        hist = tasks_mod.get_task_history(tid)
        approved = [h for h in hist if h["to_status"] == "approved"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["actor"], "owner")
        self.assertIn("dashboard", approved[0]["note"])

    def test_approve_rejects_non_type1_wrong_state_and_unknown(self):
        run_id = self.make_run()
        plain = self.make_task(run_id)
        code, _d = _err_of(lambda: _call(api.approve_task_route, task_id=plain))
        self.assertEqual(code, 409)
        tid = self._type1_task()
        self.walk(tid, "approved", "assigned")
        code, _d = _err_of(lambda: _call(api.approve_task_route, task_id=tid))
        self.assertEqual(code, 409)
        code, _d = _err_of(lambda: _call(api.approve_task_route, task_id="no-such-task"))
        self.assertEqual(code, 404)
        tid2 = self._type1_task()
        _call(api.approve_task_route, task_id=tid2)
        code, _d = _err_of(lambda: _call(api.approve_task_route, task_id=tid2))
        self.assertEqual(code, 409)

    def test_reject_happy_path_is_terminal(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        out = _call(api.reject_task_route, task_id=tid, body={})
        self.assertEqual(out["status"], "rejected")
        row = tasks_mod.get_task(tid)
        self.assertEqual(row["status"], "rejected")
        self.assertIsNotNone(row["completed_at"])
        # Terminal: no exit, from any route or the raw transition table.
        self.assertFalse(tasks_mod.update_task_status(tid, "approved", db_path=self.db))
        code, _d = _err_of(lambda: _call(api.approve_task_route, task_id=tid))
        self.assertEqual(code, 409)
        code, _d = _err_of(lambda: _call(api.assign_task_route, task_id=tid, body={"owner": "CFO"}))
        self.assertEqual(code, 409)
        hist = tasks_mod.get_task_history(tid)
        rejected = [h for h in hist if h["to_status"] == "rejected"]
        self.assertEqual(rejected[0]["actor"], "owner (dashboard)")

    def test_reject_wrong_state_and_unknown(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        self.walk(tid, "approved", "assigned")
        code, _d = _err_of(lambda: _call(api.reject_task_route, task_id=tid, body={}))
        self.assertEqual(code, 409)
        code, _d = _err_of(lambda: _call(api.reject_task_route, task_id="no-such-task", body={}))
        self.assertEqual(code, 404)

    def test_rejected_shows_in_counts(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        _call(api.reject_task_route, task_id=tid, body={})
        self.assertEqual(_call(api.summary)["tasks_by_status"], {"rejected": 1})
        self.assertIn("rejected", TestStatusNeverInvented.LEGAL)

    def test_assign_happy_path(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        out = _call(api.assign_task_route, task_id=tid, body={"owner": "CFO"})
        self.assertEqual(out["status"], "assigned")
        self.assertEqual(out["owner"], "CFO")
        row = tasks_mod.get_task(tid)
        self.assertEqual(row["owner"], "CFO")
        hist = tasks_mod.get_task_history(tid)
        assigned = [h for h in hist if h["to_status"] == "assigned"]
        self.assertEqual(assigned[-1]["actor"], "owner (dashboard)")
        # Approved tasks assign too.
        tid2 = self._type1_task()
        _call(api.approve_task_route, task_id=tid2)
        out2 = _call(api.assign_task_route, task_id=tid2, body={"owner": "CTO"})
        self.assertEqual(out2["status"], "assigned")

    def test_assign_rejects_wrong_state_unknown_and_bad_input(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        self.walk(tid, "approved", "assigned", "running")
        code, _d = _err_of(lambda: _call(api.assign_task_route, task_id=tid, body={"owner": "CFO"}))
        self.assertEqual(code, 409)
        code, _d = _err_of(lambda: _call(api.assign_task_route, task_id="no-such-task", body={"owner": "CFO"}))
        self.assertEqual(code, 404)
        tid2 = self.make_task(run_id)
        code, _d = _err_of(lambda: _call(api.assign_task_route, task_id=tid2, body={}))
        self.assertEqual(code, 400)
        code, _d = _err_of(lambda: _call(api.assign_task_route, task_id=tid2, body={"owner": 123}))
        self.assertEqual(code, 400)

    def test_locked_registry_returns_503_not_500(self):
        import sqlite3
        tid = self._type1_task()
        locker = sqlite3.connect(self.db)
        locker.execute("BEGIN EXCLUSIVE")
        orig_connect = sqlite3.connect

        def _fast_connect(*args, **kwargs):
            kwargs["timeout"] = 0.05
            return orig_connect(*args, **kwargs)

        sqlite3.connect = _fast_connect
        try:
            code, _d = _err_of(lambda: _call(api.approve_task_route, task_id=tid))
        finally:
            sqlite3.connect = orig_connect
            locker.execute("ROLLBACK")
            locker.close()
        self.assertEqual(code, 503)


class TestRedesignContract(DashboardBase):
    """Chat's Nov 20 visual redesign: the reskin reads exactly these
    fields. Pin the contract so a backend change that drops one breaks
    here, not silently in the UI. No new data was assumed — every field
    below already existed in v0.5.0."""

    def _seed_board(self):
        run_id = self.make_run()
        t1 = self.make_task(run_id, title="Draft launch email", approval_required=True)
        t2 = self.make_task(run_id, title="Reconcile books")
        self.walk(t2, "approved")
        t3 = self.make_task(run_id, title="Update pricing")
        self.walk(t3, "approved", "assigned")
        t4 = self.make_task(run_id, title="Market analysis")
        self.walk(t4, "approved", "assigned", "running")
        t5 = self.make_task(run_id, title="Review contracts")
        self.walk(t5, "approved", "assigned", "running", "completed")
        t6 = self.make_task(run_id, title="Prepare Q4 report")
        self.walk(t6, "approved", "assigned", "running", "completed", "verified")
        return [t1, t2, t3, t4, t5, t6]

    def test_summary_carries_all_six_kpi_fields(self):
        self._seed_board()
        s = _call(api.summary)
        for key in ("tasks_total", "running", "stuck", "pending_approvals",
                    "total_runs", "generated_at", "tasks_by_status"):
            self.assertIn(key, s, "KPI strip reads %s" % key)
        self.assertEqual(s["tasks_total"], 6)
        self.assertEqual(s["running"], 1)
        self.assertEqual(s["stuck"], 0)
        self.assertEqual(s["pending_approvals"], 1)
        self.assertEqual(s["total_runs"], 1)
        self.assertEqual(s["tasks_by_status"]["verified"], 1)

    def test_roster_seats_carry_display_last_activity_and_gaps(self):
        self._seed_board()
        roster = _call(api.roster)
        self.assertEqual(len(roster["seats"]), 8)
        for seat in roster["seats"]:
            for key in ("seat", "display_name", "initials", "last_activity",
                        "missing", "bound", "profile"):
                self.assertIn(key, seat, "seat card reads %s" % key)
            self.assertIsInstance(seat["missing"], list)
        cfo = next(s for s in roster["seats"] if s["seat"] == "CFO")
        self.assertIsNotNone(cfo["last_activity"])
        cro = next(s for s in roster["seats"] if s["seat"] == "CRO")
        self.assertIsNone(cro["last_activity"], "no activity invents none")

    def test_activity_history_rows_carry_everything_the_timeline_renders(self):
        self._seed_board()
        act = _call(api.activity)
        self.assertEqual(len(act["running"]), 1)
        self.assertEqual(act["running"][0]["status"], "running")
        self.assertEqual(act["stuck"], [])
        self.assertTrue(act["recent_history"])
        for ev in act["recent_history"]:
            for key in ("id", "from_status", "to_status", "actor", "at"):
                self.assertIn(key, ev, "timeline row reads %s" % key)
            self.assertTrue(ev["title"] or ev.get("task_id"))

    def test_approvals_carry_pending_history_and_note(self):
        self._seed_board()
        resp = _call(api.approvals)
        self.assertEqual(resp["pending_count"], 1)
        row = resp["pending"][0]
        for key in ("task_id", "title", "seat", "status",
                    "approval_required", "updated_at"):
            self.assertIn(key, row, "approval card reads %s" % key)
        self.assertTrue(row["approval_required"])
        tasks_mod.approve_task(row["task_id"], "Owner", db_path=self.db)
        resp = _call(api.approvals)
        self.assertEqual(resp["pending_count"], 0)
        self.assertEqual(resp["history"][0]["actor"], "Owner")
        self.assertIn("not stored", resp["decisions_note"])

    def test_board_chip_counts_derive_from_tasks(self):
        # The UI's chips are All / Proposed / Approved / In Progress /
        # Done; In Progress is assigned + running, Done is completed +
        # verified. The counts must come from the returned task list.
        self._seed_board()
        tasks = _call(api.list_tasks)["tasks"]
        by_status = {}
        for t in tasks:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        chips = {
            "all": sum(by_status.values()),
            "proposed": by_status.get("proposed", 0),
            "approved": by_status.get("approved", 0),
            "in-progress": by_status.get("assigned", 0) + by_status.get("running", 0),
            "done": by_status.get("completed", 0) + by_status.get("verified", 0),
        }
        self.assertEqual(chips, {
            "all": 6, "proposed": 1, "approved": 1,
            "in-progress": 2, "done": 2,
        })

    def test_no_business_health_or_trend_fields(self):
        # The redesign shows no productivity / efficiency / on-time /
        # compliance scores, no trend deltas, no financial figures. The
        # backend must not start emitting them either.
        self._seed_board()
        banned = {
            "productivity", "efficiency", "on_time", "on-time",
            "compliance", "trend", "delta", "revenue", "profit",
            "business_health", "health_score",
        }

        def _keys(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield k
                    yield from _keys(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from _keys(v)

        payloads = [
            _call(api.summary),
            _call(api.list_tasks),
            _call(api.activity),
            _call(api.approvals),
        ]
        for payload in payloads:
            found = banned & set(_keys(payload))
            self.assertEqual(found, set(), "fabricated metric keys: %s" % found)


if __name__ == "__main__":
    unittest.main()
