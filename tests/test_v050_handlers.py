"""v0.5.0 task-tool handler tests (tools.ceo_task_* / ceo_run_*).

These are the 7 new tools; only ceo_run_create was exercised in the
live Hermes run, so this file is the only coverage the other six get.
Fictional data only. Every test uses a fresh temp DB via the
CEO_PACK_TASK_DB env override and calls tasks.init_db() first, the way
register() does in Hermes.

Known gaps these tests pin down (see report):
- 5 of 7 handlers put the domain lifecycle status ("proposed",
  "planned", "assigned", "approved") into the envelope `status` field
  instead of "success", so verify_result marks them unverified and the
  post_tool_call hook escalates every call.
- ceo_task_assign on a merely-proposed task reports "assigned" while
  the DB still says "proposed".
- ceo_task_list with a non-integer limit raises TypeError instead of
  returning an error envelope.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestration as o  # noqa: E402
import tasks  # noqa: E402
import tools  # noqa: E402

FAKE_SK = "sk-" + "abcdefghijklmnop1234567890"  # fake, assembled so no scanner flags this file

HANDLERS = ("ceo_run_create", "ceo_task_create", "ceo_task_status",
            "ceo_task_list", "ceo_task_approve", "ceo_task_assign",
            "ceo_run_status")


def call(fn, **args):
    out = json.loads(fn(args))
    assert isinstance(out, dict)
    return out


class HandlerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "handler_tasks.db")
        self._old_env = os.environ.get("CEO_PACK_TASK_DB")
        os.environ["CEO_PACK_TASK_DB"] = self.db
        tasks.init_db(self.db)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("CEO_PACK_TASK_DB", None)
        else:
            os.environ["CEO_PACK_TASK_DB"] = self._old_env
        self.tmp.cleanup()

    def make_run(self, goal="Grow newsletter"):
        return call(tools.ceo_run_create, goal=goal)["run_id"]

    def make_task(self, run_id=None, seat="CFO", title="Reconcile books", **kw):
        args = {"run_id": run_id or self.make_run(), "seat": seat, "title": title}
        args.update(kw)
        return call(tools.ceo_task_create, **args)["task_id"]


class TestHappyPaths(HandlerBase):
    def test_full_flow_through_handlers(self):
        run_id = self.make_run("Winter Blend launch")
        tid = self.make_task(run_id, seat="CFO", title="Reconcile October books",
                             due_date="2030-01-15", approval_required=True,
                             risk_flags=["legal_or_financial"])
        status = call(tools.ceo_task_status, task_id=tid)
        self.assertEqual(status["title"], "Reconcile October books")
        self.assertEqual(status["task_status"], "proposed")
        self.assertTrue(status["approval_required"])
        self.assertEqual(status["history"][0]["to_status"], "proposed")

        approved = call(tools.ceo_task_approve, task_id=tid, approved_by="Owner")
        self.assertEqual(approved["task_status"], "approved")
        assigned = call(tools.ceo_task_assign, task_id=tid, owner="CFO",
                        model_route="free_default")
        self.assertEqual(assigned["owner"], "CFO")
        self.assertEqual(tasks.get_task(tid, db_path=self.db)["status"], "assigned")
        self.assertEqual(tasks.get_task(tid, db_path=self.db)["owner"], "CFO")

        listed = call(tools.ceo_task_list, run_id=run_id)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["tasks"][0]["task_id"], tid)
        run = call(tools.ceo_run_status, run_id=run_id)
        self.assertEqual(run["task_count"], 1)
        self.assertEqual(run["status_counts"], {"assigned": 1})

    def test_list_filters(self):
        run_id = self.make_run()
        self.make_task(run_id, seat="CFO", title="Books")
        self.make_task(run_id, seat="CTO", title="Deploy")
        by_seat = call(tools.ceo_task_list, seat="CFO")
        self.assertEqual(by_seat["count"], 1)
        self.assertEqual(by_seat["tasks"][0]["seat"], "CFO")
        by_run_status = call(tools.ceo_task_list, run_id=run_id, status="proposed")
        self.assertEqual(by_run_status["count"], 2)
        self.assertEqual(call(tools.ceo_task_list)["status"], "error")

    def test_past_due_date_still_creates(self):
        tid = self.make_task(due_date="2020-01-15")
        self.assertEqual(tasks.get_task(tid, db_path=self.db)["due_date"], "2020-01-15")


class TestVerifyEnvelope(HandlerBase):
    """Every successful handler result must verify cleanly (handoff req).

    The plugin's own contract (tools.py docstring): handlers return an
    explicit status field the verify hook can read. _ok() sets
    status="success" — unless the handler already put a domain status
    ("proposed", "planned", "assigned", "approved") into that key.
    """

    def test_all_seven_success_results_verify(self):
        run_id = self.make_run()
        tid = self.make_task(run_id, approval_required=True)
        cases = {
            "ceo_run_create": call(tools.ceo_run_create, goal="x"),
            "ceo_task_create": call(tools.ceo_task_create, run_id=run_id,
                                     seat="CFO", title="Books"),
            "ceo_task_status": call(tools.ceo_task_status, task_id=tid),
            "ceo_task_list": call(tools.ceo_task_list, run_id=run_id),
            "ceo_task_approve": call(tools.ceo_task_approve, task_id=tid,
                                      approved_by="Owner"),
            "ceo_task_assign": call(tools.ceo_task_assign, task_id=tid, owner="CFO"),
            "ceo_run_status": call(tools.ceo_run_status, run_id=run_id),
        }
        bad = {}
        for name, result in cases.items():
            if result.get("status") != "success":
                bad[name] = f"status={result.get('status')!r}"
                continue
            v = o.verify_result(name, result)
            if not v["verified"]:
                bad[name] = f"verify failed: {v}"
        self.assertEqual(bad, {}, "handlers whose results do not verify cleanly")


class TestGuards(HandlerBase):
    def test_approve_requires_type1_and_proposed(self):
        tid = self.make_task()  # not approval_required
        err = call(tools.ceo_task_approve, task_id=tid, approved_by="Owner")
        self.assertEqual(err["status"], "error")
        self.assertIn("does not require approval", err["error"])

        tid2 = self.make_task(approval_required=True)
        call(tools.ceo_task_approve, task_id=tid2, approved_by="Owner")
        err2 = call(tools.ceo_task_approve, task_id=tid2, approved_by="Owner")
        self.assertEqual(err2["status"], "error")
        self.assertIn("not 'proposed'", err2["error"])

    def test_assign_refuses_terminal_states(self):
        tid = self.make_task()
        tasks.update_task_status(tid, "approved", db_path=self.db)
        tasks.update_task_status(tid, "assigned", db_path=self.db)
        tasks.update_task_status(tid, "running", db_path=self.db)
        tasks.update_task_status(tid, "completed", db_path=self.db)
        err = call(tools.ceo_task_assign, task_id=tid, owner="CFO")
        self.assertEqual(err["status"], "error")
        self.assertIn("completed", err["error"])

    def test_assign_from_proposed_reports_db_status_truthfully(self):
        # The response's task_status must match the state the registry
        # actually holds (the envelope status is "success").
        tid = self.make_task()
        out = call(tools.ceo_task_assign, task_id=tid, owner="CFO")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["task_status"], "assigned")
        self.assertEqual(tasks.get_task(tid, db_path=self.db)["status"],
                         out["task_status"])

    def test_missing_rows_are_errors(self):
        for fn, args in (
            (tools.ceo_task_status, {"task_id": "no-such-task"}),
            (tools.ceo_task_approve, {"task_id": "no-such-task", "approved_by": "Owner"}),
            (tools.ceo_task_assign, {"task_id": "no-such-task", "owner": "CFO"}),
            (tools.ceo_run_status, {"run_id": "no-such-run"}),
        ):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(call(fn, **args)["status"], "error")

    def test_unknown_seat_and_bad_dates(self):
        run_id = self.make_run()
        self.assertEqual(
            call(tools.ceo_task_create, run_id=run_id, seat="Intern",
                 title="x")["status"], "error")
        self.assertEqual(
            call(tools.ceo_task_create, run_id=run_id, seat="CFO", title="x",
                 due_date="11/15/2026")["status"], "error")
        self.assertEqual(
            call(tools.ceo_task_create, run_id=run_id, seat="CFO", title="x",
                 due_date="2026-02-31")["status"], "error")
        self.assertEqual(
            call(tools.ceo_run_create, goal="x", head_seat="Intern")["status"], "error")

    def test_handlers_fail_without_init_db(self):
        # register() calls init_db(); a handler on a fresh, uninitialized
        # DB raises instead of returning an error envelope. Documented
        # coupling: handlers assume register() already ran.
        fresh = str(Path(self.tmp.name) / "uninit.db")
        os.environ["CEO_PACK_TASK_DB"] = fresh
        with self.assertRaises(sqlite3.OperationalError):
            tools.ceo_task_create({"run_id": "r", "seat": "CFO", "title": "x"})


class TestSecrets(HandlerBase):
    def test_secret_keys_refused_on_every_handler(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        cases = [
            (tools.ceo_run_create, {"goal": "x", "api_key": FAKE_SK}),
            (tools.ceo_task_create, {"run_id": run_id, "seat": "CFO", "title": "x",
                                     "api_key": FAKE_SK}),
            (tools.ceo_task_status, {"task_id": tid, "password": FAKE_SK}),
            (tools.ceo_task_list, {"run_id": run_id, "api_key": FAKE_SK}),
            (tools.ceo_task_approve, {"task_id": tid, "approved_by": "Owner",
                                      "api_key": FAKE_SK}),
            (tools.ceo_task_assign, {"task_id": tid, "owner": "CFO",
                                     "api_key": FAKE_SK}),
            (tools.ceo_run_status, {"run_id": run_id, "api_key": FAKE_SK}),
        ]
        for fn, args in cases:
            with self.subTest(fn=fn.__name__):
                out = call(fn, **args)
                self.assertEqual(out["status"], "error")
                self.assertNotIn(FAKE_SK, json.dumps(out))

    def test_secret_value_in_description_refused(self):
        out = call(tools.ceo_task_create, run_id=self.make_run(), seat="CFO",
                   title="x", description=f"use {FAKE_SK} for the fix")
        self.assertEqual(out["status"], "error")
        self.assertNotIn(FAKE_SK, json.dumps(out))

    def test_refused_secret_never_reaches_the_db(self):
        call(tools.ceo_task_create, run_id=self.make_run(), seat="CFO",
             title="x", description=f"use {FAKE_SK} for the fix")
        self.assertNotIn(b"sk-", Path(self.db).read_bytes())


class TestFuzz(HandlerBase):
    """Weird args must yield error envelopes, never an exception."""

    WEIRD = [
        ("none-args", None),
        ("string-args", "not json"),
        ("bad-json-string", "{not json"),
        ("int-args", 123),
        ("list-args", ["x"]),
        ("empty-dict", {}),
        ("none-title", {"run_id": "r", "seat": "CFO", "title": None}),
        ("int-title", {"run_id": "r", "seat": "CFO", "title": 123}),
        ("list-seat", {"run_id": "r", "seat": ["CFO"], "title": "x"}),
        ("nested-args", {"run_id": {"nested": {"deep": [1, 2, {"x": None}]}},
                         "seat": "CFO", "title": "x"}),
        ("huge-title", {"run_id": "r", "seat": "CFO", "title": "x" * 100_000}),
        ("huge-metadata", {"run_id": "r", "seat": "CFO", "title": "x",
                           "metadata": {"blob": "y" * 100_000}}),
        ("bool-fields", {"run_id": "r", "seat": "CFO", "title": "x",
                         "approval_required": "yes", "risk_flags": "external"}),
        ("int-due-date", {"run_id": "r", "seat": "CFO", "title": "x",
                          "due_date": 20260101}),
        ("empty-task-id", {"task_id": ""}),
        ("int-task-id", {"task_id": 0}),
        ("list-task-id", {"task_id": ["abc"]}),
    ]

    def _fuzz(self, fn, base):
        failures = []
        for label, weird in self.WEIRD:
            args = dict(base)
            if isinstance(weird, dict):
                args.update(weird)
                call_args = args
            else:
                call_args = weird
            try:
                out = fn(call_args)
                parsed = json.loads(out)
                if not isinstance(parsed, dict) or "status" not in parsed:
                    failures.append((label, f"bad envelope: {out[:120]!r}"))
            except Exception as exc:  # noqa: BLE001 - fuzz must not raise
                failures.append((label, f"{type(exc).__name__}: {exc}"))
        return failures

    def test_fuzz_all_seven_handlers(self):
        run_id = self.make_run()
        tid = self.make_task(run_id)
        bases = {
            "ceo_run_create": {"goal": "x"},
            "ceo_task_create": {"run_id": run_id, "seat": "CFO", "title": "x"},
            "ceo_task_status": {"task_id": tid},
            "ceo_task_list": {"run_id": run_id},
            "ceo_task_approve": {"task_id": tid, "approved_by": "Owner"},
            "ceo_task_assign": {"task_id": tid, "owner": "CFO"},
            "ceo_run_status": {"run_id": run_id},
        }
        fns = {
            "ceo_run_create": tools.ceo_run_create,
            "ceo_task_create": tools.ceo_task_create,
            "ceo_task_status": tools.ceo_task_status,
            "ceo_task_list": tools.ceo_task_list,
            "ceo_task_approve": tools.ceo_task_approve,
            "ceo_task_assign": tools.ceo_task_assign,
            "ceo_run_status": tools.ceo_run_status,
        }
        all_failures = {}
        for name, fn in fns.items():
            bad = self._fuzz(fn, bases[name])
            if bad:
                all_failures[name] = bad
        self.assertEqual(all_failures, {}, "fuzz cases that raised or broke the envelope")

    def test_list_limit_must_be_an_integer(self):
        run_id = self.make_run()
        self.make_task(run_id)
        for bad_limit in ("abc", 2.5, [10]):
            with self.subTest(limit=bad_limit):
                try:
                    out = call(tools.ceo_task_list, run_id=run_id, limit=bad_limit)
                except TypeError:
                    self.fail(f"limit={bad_limit!r} raised TypeError instead of an error envelope")
                self.assertEqual(out["status"], "error")


if __name__ == "__main__":
    unittest.main()
