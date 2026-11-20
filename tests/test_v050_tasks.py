"""v0.5.0 task registry tests (tasks.py: SQLite lifecycle).

Fictional data only. Every test uses a fresh temp DB file; nothing
touches the real ~/.hermes task database.

Known gaps these tests pin down (see report): update_task_status
currently allows ANY transition, and create_task does not enforce
task_key idempotency. Those tests fail against the v0.5.0 tree on
purpose — they encode the lifecycle the handoff specifies.
"""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tasks  # noqa: E402

# Legal transitions per the lifecycle in plugin.yaml:
# proposed -> approved -> assigned -> running -> blocked/completed -> verified
# (failed is reachable from running/blocked; failed -> running is the retry path)
LEGAL = {
    ("approved", "assigned"),
    ("assigned", "running"),
    ("running", "blocked"),
    ("running", "completed"),
    ("running", "failed"),
    ("blocked", "running"),
    ("blocked", "failed"),
    ("completed", "verified"),
    ("failed", "running"),
}

# Everything else is illegal: backwards moves, stage skips, and any move
# out of a terminal state (completed except -> verified, verified, failed
# except -> running).
ILLEGAL = {
    ("proposed", "running"), ("proposed", "completed"), ("proposed", "verified"),
    ("proposed", "blocked"), ("proposed", "assigned"),
    ("approved", "proposed"), ("approved", "running"),
    ("approved", "completed"), ("approved", "verified"),
    ("assigned", "proposed"), ("assigned", "approved"),
    ("assigned", "completed"), ("assigned", "verified"),
    ("running", "proposed"), ("running", "approved"),
    ("running", "assigned"), ("running", "verified"),
    ("blocked", "proposed"), ("blocked", "approved"),
    ("blocked", "assigned"), ("blocked", "completed"), ("blocked", "verified"),
    ("completed", "proposed"), ("completed", "approved"),
    ("completed", "assigned"), ("completed", "running"),
    ("completed", "blocked"), ("completed", "failed"),
    ("verified", "proposed"), ("verified", "approved"),
    ("verified", "assigned"), ("verified", "running"),
    ("verified", "blocked"), ("verified", "completed"), ("verified", "failed"),
    ("failed", "proposed"), ("failed", "approved"),
    ("failed", "assigned"), ("failed", "completed"),
    ("failed", "verified"), ("failed", "blocked"),
}


class TaskRegistryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test_tasks.db")
        tasks.init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def make_run(self, goal="Grow newsletter"):
        return tasks.create_task_run(goal, "CEO", db_path=self.db)

    def make_task(self, run_id=None, seat="CFO", title="Reconcile books", **kw):
        run_id = run_id or self.make_run()
        return tasks.create_task(run_id, seat, title, db_path=self.db, **kw)

    def walk_to(self, state):
        """Create a task and drive it to `state` via the legal path."""
        tid = self.make_task()
        if state == "proposed":
            return tid
        self.assertTrue(tasks.approve_task(tid, "Owner", db_path=self.db))
        if state == "approved":
            return tid
        self.assertTrue(tasks.update_task_status(tid, "assigned", db_path=self.db))
        if state == "assigned":
            return tid
        self.assertTrue(tasks.update_task_status(tid, "running", db_path=self.db))
        if state == "running":
            return tid
        if state == "blocked":
            self.assertTrue(tasks.update_task_status(tid, "blocked", db_path=self.db))
            return tid
        if state == "completed":
            self.assertTrue(tasks.update_task_status(tid, "completed", db_path=self.db))
            return tid
        if state == "verified":
            self.assertTrue(tasks.update_task_status(tid, "completed", db_path=self.db))
            self.assertTrue(tasks.update_task_status(tid, "verified", db_path=self.db))
            return tid
        if state == "failed":
            self.assertTrue(tasks.update_task_status(tid, "failed", db_path=self.db))
            return tid
        raise AssertionError(f"unknown target state {state!r}")


class TestLifecycle(TaskRegistryBase):
    def test_full_happy_path(self):
        run_id = self.make_run("Winter Blend launch")
        tid = self.make_task(run_id, seat="CFO", title="Reconcile October books",
                             due_date="2030-01-15", approval_required=True,
                             risk_flags=["legal_or_financial"])
        t = tasks.get_task(tid, db_path=self.db)
        self.assertEqual(t["status"], "proposed")
        self.assertEqual(t["run_id"], run_id)
        self.assertEqual(t["risk_flags"], ["legal_or_financial"])
        self.assertTrue(t["approval_required"])

        self.assertTrue(tasks.approve_task(tid, "Owner", db_path=self.db))
        t = tasks.get_task(tid, db_path=self.db)
        self.assertEqual(t["status"], "approved")
        self.assertEqual(t["approved_by"], "Owner")
        self.assertIsNotNone(t["approved_at"])

        self.assertTrue(tasks.assign_task(tid, "CFO", "free_default", db_path=self.db))
        t = tasks.get_task(tid, db_path=self.db)
        self.assertEqual(t["owner"], "CFO")
        self.assertEqual(t["model_route"], "free_default")

        self.assertTrue(tasks.update_task_status(tid, "assigned", db_path=self.db))
        self.assertTrue(tasks.update_task_status(tid, "running", db_path=self.db))
        t = tasks.get_task(tid, db_path=self.db)
        self.assertIsNotNone(t["started_at"])

        self.assertTrue(tasks.update_task_status(
            tid, "completed", result={"message_id": "msg-1"}, db_path=self.db))
        t = tasks.get_task(tid, db_path=self.db)
        self.assertEqual(t["result"], {"message_id": "msg-1"})
        self.assertIsNotNone(t["completed_at"])

        self.assertTrue(tasks.update_task_status(tid, "verified", db_path=self.db))
        self.assertEqual(tasks.get_task(tid, db_path=self.db)["status"], "verified")

    def test_history_records_every_transition(self):
        tid = self.walk_to("verified")
        hist = tasks.get_task_history(tid, db_path=self.db)
        seen = [(h["from_status"], h["to_status"]) for h in hist]
        self.assertEqual(seen[0], (None, "proposed"))
        self.assertIn(("proposed", "approved"), seen)
        self.assertIn(("approved", "assigned"), seen)
        self.assertIn(("completed", "verified"), seen)
        self.assertTrue(all(h["at"] for h in hist))

    def test_legal_transitions_succeed(self):
        for frm, to in sorted(LEGAL):
            with self.subTest(frm=frm, to=to):
                tid = self.walk_to(frm)
                self.assertTrue(tasks.update_task_status(tid, to, db_path=self.db))
                self.assertEqual(tasks.get_task(tid, db_path=self.db)["status"], to)

    def test_illegal_transitions_refused(self):
        for frm, to in sorted(ILLEGAL):
            with self.subTest(frm=frm, to=to):
                tid = self.walk_to(frm)
                self.assertFalse(tasks.update_task_status(tid, to, db_path=self.db),
                                 f"{frm} -> {to} must be refused")
                self.assertEqual(tasks.get_task(tid, db_path=self.db)["status"], frm)

    def test_approve_only_from_proposed(self):
        tid = self.walk_to("assigned")
        self.assertFalse(tasks.approve_task(tid, "Owner", db_path=self.db))
        self.assertEqual(tasks.get_task(tid, db_path=self.db)["status"], "assigned")

    def test_same_status_transition_is_noop(self):
        tid = self.make_task()
        self.assertFalse(tasks.update_task_status(tid, "proposed", db_path=self.db))

    def test_retry_path_failed_to_running(self):
        tid = self.walk_to("failed")
        self.assertTrue(tasks.update_task_status(tid, "running", db_path=self.db))
        self.assertEqual(tasks.increment_retry(tid, db_path=self.db), 1)
        self.assertEqual(tasks.increment_retry(tid, db_path=self.db), 2)


class TestDependencies(TaskRegistryBase):
    def test_dependency_blocks_until_prerequisite_done(self):
        run_id = self.make_run()
        prereq = self.make_task(run_id, title="Close October books")
        dependent = self.make_task(run_id, title="File October taxes")
        self.assertTrue(tasks.add_task_dependency(dependent, prereq, db_path=self.db))
        self.assertFalse(tasks.are_dependencies_met(dependent, db_path=self.db))
        self.assertEqual(tasks.get_task_dependencies(dependent, db_path=self.db), [prereq])
        self.assertEqual(tasks.get_dependent_tasks(prereq, db_path=self.db), [dependent])

        tasks.update_task_status(prereq, "approved", db_path=self.db)
        self.assertFalse(tasks.are_dependencies_met(dependent, db_path=self.db))
        # Walk the legal path: approved -> assigned -> running -> completed
        # (approved -> completed directly is an illegal transition).
        tasks.update_task_status(prereq, "assigned", db_path=self.db)
        tasks.update_task_status(prereq, "running", db_path=self.db)
        tasks.update_task_status(prereq, "completed", db_path=self.db)
        self.assertTrue(tasks.are_dependencies_met(dependent, db_path=self.db))

    def test_duplicate_dependency_refused(self):
        run_id = self.make_run()
        a = self.make_task(run_id, title="A")
        b = self.make_task(run_id, title="B")
        self.assertTrue(tasks.add_task_dependency(a, b, db_path=self.db))
        self.assertFalse(tasks.add_task_dependency(a, b, db_path=self.db))

    def test_dependency_on_missing_task_refused(self):
        tid = self.make_task()
        self.assertFalse(tasks.add_task_dependency(tid, "no-such-task", db_path=self.db))
        self.assertFalse(tasks.add_task_dependency("no-such-task", tid, db_path=self.db))

    def test_no_dependencies_means_met(self):
        tid = self.make_task()
        self.assertTrue(tasks.are_dependencies_met(tid, db_path=self.db))


class TestIdempotency(TaskRegistryBase):
    def test_duplicate_create_with_same_task_key_is_idempotent(self):
        run_id = self.make_run()
        first = tasks.create_task(run_id, "CFO", "Reconcile books",
                                  task_key="month-end-2030-01", db_path=self.db)
        second = tasks.create_task(run_id, "CFO", "Reconcile books",
                                   task_key="month-end-2030-01", db_path=self.db)
        self.assertEqual(first, second)
        rows = tasks.get_tasks_by_run(run_id, db_path=self.db)
        self.assertEqual(len([r for r in rows if r.get("task_key") == "month-end-2030-01"]), 1)

    def test_different_task_keys_create_distinct_tasks(self):
        run_id = self.make_run()
        a = tasks.create_task(run_id, "CFO", "Books", task_key="k-1", db_path=self.db)
        b = tasks.create_task(run_id, "CFO", "Books", task_key="k-2", db_path=self.db)
        self.assertNotEqual(a, b)


class TestPersistence(TaskRegistryBase):
    def test_restart_survives_reopen_and_reinit(self):
        run_id = self.make_run("Q4 closeout")
        tid = self.make_task(run_id, title="Reconcile books")
        tasks.approve_task(tid, "Owner", db_path=self.db)
        # Simulate a restart: re-run init (idempotent) and read back.
        tasks.init_db(self.db)
        t = tasks.get_task(tid, db_path=self.db)
        self.assertEqual(t["status"], "approved")
        self.assertEqual(t["title"], "Reconcile books")
        self.assertEqual(len(tasks.get_task_history(tid, db_path=self.db)), 2)
        # Raw reopen with a plain connection sees the same rows.
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "approved")

    def test_pending_approvals_and_stuck_queries(self):
        run_id = self.make_run()
        t1 = self.make_task(run_id, title="Wire $5,000", approval_required=True)
        self.make_task(run_id, title="Draft newsletter")
        pending = tasks.get_pending_approvals(db_path=self.db)
        self.assertEqual([p["id"] for p in pending], [t1])
        tasks.approve_task(t1, "Owner", db_path=self.db)
        self.assertEqual(tasks.get_pending_approvals(db_path=self.db), [])

        stuck = self.walk_to("failed")
        # A failed task with retries left is not stuck yet; exhaust them.
        tasks.increment_retry(stuck, db_path=self.db)
        self.assertEqual([s["id"] for s in tasks.get_stuck_tasks(db_path=self.db)], [stuck])


class TestConcurrency(TaskRegistryBase):
    def test_concurrent_creates_all_land(self):
        run_id = self.make_run()
        errors = []
        barrier = threading.Barrier(8)

        def worker(n):
            try:
                barrier.wait(timeout=10)
                for i in range(25):
                    tasks.create_task(run_id, "CFO", f"Task {n}-{i}",
                                      task_key=f"w{n}-{i}", db_path=self.db)
            except Exception as exc:  # noqa: BLE001 - collected, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(tasks.get_tasks_by_run(run_id, db_path=self.db)), 200)

    def test_concurrent_status_updates_dont_lose_rows(self):
        run_id = self.make_run()
        ids = [self.make_task(run_id, title=f"T{i}") for i in range(40)]
        errors = []

        def worker(chunk):
            try:
                for tid in chunk:
                    tasks.update_task_status(tid, "approved", db_path=self.db)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        chunks = [ids[i::4] for i in range(4)]
        threads = [threading.Thread(target=worker, args=(c,)) for c in chunks]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(errors, [])
        states = {tasks.get_task(t, db_path=self.db)["status"] for t in ids}
        self.assertEqual(states, {"approved"})


class TestUniqueIds(TaskRegistryBase):
    def test_ids_are_unique_uuids_not_hash_of_goal(self):
        run_id = self.make_run()
        ids = [self.make_task(run_id, title="Same title every time") for _ in range(100)]
        self.assertEqual(len(set(ids)), 100)
        for tid in ids:
            parsed = uuid.UUID(tid)
            self.assertEqual(str(parsed), tid)
            self.assertEqual(parsed.version, 4)
        run_ids = [tasks.create_task_run("Same goal", "CEO", db_path=self.db) for _ in range(20)]
        self.assertEqual(len(set(run_ids)), 20)


class TestBadInput(TaskRegistryBase):
    def test_missing_rows_return_false_or_none(self):
        self.assertIsNone(tasks.get_task("no-such-task", db_path=self.db))
        self.assertIsNone(tasks.get_task(None, db_path=self.db))
        self.assertFalse(tasks.update_task_status("no-such-task", "approved", db_path=self.db))
        self.assertFalse(tasks.approve_task("no-such-task", "Owner", db_path=self.db))
        self.assertFalse(tasks.assign_task("no-such-task", "CFO", db_path=self.db))
        self.assertEqual(tasks.increment_retry("no-such-task", db_path=self.db), 0)
        self.assertIsNone(tasks.get_task_run("no-such-run", db_path=self.db))
        self.assertFalse(tasks.update_task_run_status("no-such-run", "running", db_path=self.db))
        self.assertFalse(tasks.set_task_run_plan("no-such-run", "{}", db_path=self.db))
        self.assertEqual(tasks.get_task_history("no-such-task", db_path=self.db), [])
        self.assertEqual(tasks.get_tasks_by_run("no-such-run", db_path=self.db), [])

    def test_invalid_status_raises_value_error(self):
        tid = self.make_task()
        with self.assertRaises(ValueError):
            tasks.update_task_status(tid, "exploded", db_path=self.db)
        self.assertEqual(tasks.get_task(tid, db_path=self.db)["status"], "proposed")

    def test_null_required_fields_raise_integrity_error(self):
        # Direct registry calls do not validate: the handlers guard this,
        # so a tool call never surfaces a raw sqlite error. Documented here.
        with self.assertRaises(sqlite3.IntegrityError):
            tasks.create_task(None, "CFO", "x", db_path=self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            tasks.create_task_run(None, "CEO", db_path=self.db)


class TestNoSecrets(TaskRegistryBase):
    def test_registry_stores_no_credential_like_content(self):
        run_id = self.make_run("Q4 closeout")
        self.make_task(run_id, title="Reconcile books",
                       description="Monthly close for Harbor Lane Coffee Co.",
                       metadata={"source": "fictional-fixture"})
        raw = Path(self.db).read_bytes()
        for needle in (b"sk-", b"api_key", b"password", b"AKIA", b"ghp_"):
            self.assertNotIn(needle, raw)
        t = tasks.get_task(tasks.get_tasks_by_run(run_id, db_path=self.db)[0]["id"],
                           db_path=self.db)
        self.assertNotIn("api_key", json.dumps(t))

    def test_result_and_error_payloads_round_trip(self):
        tid = self.walk_to("running")
        tasks.update_task_status(tid, "failed",
                                 error={"code": "E_TIMEOUT", "detail": "fictional timeout"},
                                 db_path=self.db)
        t = tasks.get_task(tid, db_path=self.db)
        self.assertEqual(t["error"], {"code": "E_TIMEOUT", "detail": "fictional timeout"})


class TestRuns(TaskRegistryBase):
    def test_run_lifecycle(self):
        run_id = tasks.create_task_run("Winter Blend launch", "CEO",
                                       model_key="paid_value", db_path=self.db)
        run = tasks.get_task_run(run_id, db_path=self.db)
        self.assertEqual(run["status"], "planned")
        self.assertEqual(run["goal"], "Winter Blend launch")
        self.assertEqual(run["model_key"], "paid_value")
        self.assertIsNone(run["completed_at"])

        self.assertTrue(tasks.set_task_run_plan(run_id, '{"steps": []}', db_path=self.db))
        self.assertTrue(tasks.update_task_run_status(run_id, "running", db_path=self.db))
        self.assertTrue(tasks.update_task_run_status(run_id, "completed", db_path=self.db))
        run = tasks.get_task_run(run_id, db_path=self.db)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["plan_json"], '{"steps": []}')
        self.assertIsNotNone(run["completed_at"])


if __name__ == "__main__":
    unittest.main()
