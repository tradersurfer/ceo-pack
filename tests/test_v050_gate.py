"""Type 1 approval gate, Stage 1 (warn/observe only) — fixtures.

Runs against the REAL pre_tool_call hook function and a REAL SQLite
registry (temp file), never against mocks of the gate. Warn mode asserts
audit rows and decisions, never blocks: every hook call here must return
None, and no ticket is ever consumed.

Fictional data only. A distinctive marker string (ZULU-7-DELTA) stands
in for a sensitive argument value, to prove argument VALUES are never
stored or logged — only SHA-256 hashes.

Covers design doc §6, adapted to warn mode:
  token lifecycle, modified args, expiry/reuse, registry unavailable,
  uncertain classification, dashboard vs chat approve, delegation path,
  hook mechanics, regression (existing suite green — verified by running
  the full suite, not by a test here).
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gate as gate_mod  # noqa: E402
import orchestration as orch  # noqa: E402
import tasks as tasks_mod  # noqa: E402
import tools as tools_mod  # noqa: E402
import delegation_provider as dp  # noqa: E402



@contextmanager
def _db(path):
    """Test-side connection that commits AND closes (sqlite3's own context
    manager only commits; an open handle blocks temp cleanup on Windows)."""
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _load_api():
    spec = importlib.util.spec_from_file_location(
        "ceo_pack_dashboard_api_gate", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ceo_pack_dashboard_api_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load_api()

# The fixture action: a Type 1 call the plugin itself would make.
# ceo_decision_log is in APPROVAL_TOOLS and its arguments carry the
# sign-the-NDA risk cue in the scanned planning fields.
ACTION_TOOL = "ceo_decision_log"
ACTION_ARGS = {"decision": "sign the vendor NDA",
               "chosen": "sign it — marker ZULU-7-DELTA"}
ACTION_ARGS_CHANGED = {"decision": "sign the vendor NDA",
                       "chosen": "sign it — marker ZULU-7-ECHO"}


_SLASH_PKG = None


def _slash_approve():
    """The real /task-approve handler, loaded through the plugin package."""
    global _SLASH_PKG
    if _SLASH_PKG is None:
        from test_v050_registration import load_plugin_package
        _SLASH_PKG = load_plugin_package("ceo_pack_gate_slash_test")
    return _SLASH_PKG._task_approve_command


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "gate.db")
        self._old_env = os.environ.get("CEO_PACK_TASK_DB")
        os.environ["CEO_PACK_TASK_DB"] = self.db
        tasks_mod.init_db(self.db)
        api._DB_READY = False
        self.run_id = tasks_mod.create_task_run("Gate fixtures", "CEO", db_path=self.db)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("CEO_PACK_TASK_DB", None)
        else:
            os.environ["CEO_PACK_TASK_DB"] = self._old_env
        self.tmp.cleanup()

    def make_task(self, tool_name=None, arguments=None, approval_required=True):
        return tasks_mod.create_task(
            run_id=self.run_id, seat="CFO", title="Sign the vendor NDA",
            approval_required=approval_required,
            tool_name=tool_name, arguments=arguments, db_path=self.db)

    def approve_task(self, task_id, tool_name=None, arguments=None, minted_via="chat"):
        return tasks_mod.approve_task(
            task_id, "Owner", tool_name=tool_name, arguments=arguments,
            minted_via=minted_via, db_path=self.db)

    def audit(self, decision=None, tool_name=None):
        rows = tasks_mod.get_gate_decisions(limit=100, db_path=self.db)
        if decision is not None:
            rows = [r for r in rows if r["decision"] == decision]
        if tool_name is not None:
            rows = [r for r in rows if r.get("tool_name") == tool_name]
        return rows

    def db_bytes(self):
        """Raw bytes of the registry files (db + WAL + shm) — the
        hash-only check reads these, never the API."""
        out = b""
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.db + suffix)
            if path.exists():
                out += path.read_bytes()
        return out


class TestCanonicalHash(GateBase):
    def test_same_args_different_key_order_hash_equal(self):
        a = tasks_mod.canonical_args_hash({"a": 1, "b": 2})
        b = tasks_mod.canonical_args_hash({"b": 2, "a": 1})
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_one_changed_value_hash_differs(self):
        a = tasks_mod.canonical_args_hash(ACTION_ARGS)
        b = tasks_mod.canonical_args_hash(ACTION_ARGS_CHANGED)
        self.assertNotEqual(a, b)

    def test_non_dict_and_unserializable_hash_none(self):
        self.assertIsNone(tasks_mod.canonical_args_hash("not a dict"))
        self.assertIsNone(tasks_mod.canonical_args_hash({"f": object()}))
        self.assertIsNone(tasks_mod.canonical_args_hash(None))


class TestTokenLifecycle(GateBase):
    def test_approve_with_action_mints_token(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        self.assertTrue(self.approve_task(task_id))
        task = tasks_mod.get_task(task_id, db_path=self.db)
        self.assertEqual(task["status"], "approved")
        self.assertEqual(task["action_tool"], ACTION_TOOL)
        token = tasks_mod.get_live_token(
            task_id, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db)
        self.assertIsNotNone(token)
        self.assertEqual(token["status"], "live")
        self.assertEqual(token["minted_via"], "chat")
        self.assertIsNone(token["used_at"])
        minted = self.audit(decision="token_minted", tool_name=ACTION_TOOL)
        self.assertEqual(len(minted), 1)
        self.assertEqual(minted[0]["task_id"], task_id)
        self.assertEqual(minted[0]["mode"], "warn")

    def test_approve_without_action_mints_nothing(self):
        task_id = self.make_task()
        self.assertTrue(self.approve_task(task_id))
        self.assertEqual(tasks_mod.get_task(task_id, db_path=self.db)["status"], "approved")
        self.assertEqual(self.audit(decision="token_minted"), [])
        self.assertEqual(
            tasks_mod.token_status_for(ACTION_TOOL, ACTION_ARGS, db_path=self.db),
            "none")

    def test_approve_with_action_at_approve_time_mints(self):
        task_id = self.make_task()
        self.assertTrue(self.approve_task(task_id, tool_name=ACTION_TOOL, arguments=ACTION_ARGS))
        token = tasks_mod.get_live_token(
            task_id, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db)
        self.assertIsNotNone(token)
        task = tasks_mod.get_task(task_id, db_path=self.db)
        self.assertEqual(task["action_tool"], ACTION_TOOL)

    def test_mint_idempotent_per_task_and_action(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        first = tasks_mod.mint_approval_token(
            task_id, ACTION_TOOL, ACTION_ARGS, "Owner", "chat", db_path=self.db)
        second = tasks_mod.mint_approval_token(
            task_id, ACTION_TOOL, ACTION_ARGS, "Owner", "chat", db_path=self.db)
        self.assertEqual(first, second)
        rows = tasks_mod.list_tokens_for_tool(ACTION_TOOL, db_path=self.db)
        live = [r for r in rows if r["task_id"] == task_id and r["status"] == "live"]
        self.assertEqual(len(live), 1)

    def test_expired_token_is_not_live(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        token_id = tasks_mod.mint_approval_token(
            task_id, ACTION_TOOL, ACTION_ARGS, "Owner", "chat",
            db_path=self.db, ttl_minutes=-1)
        self.assertIsNotNone(token_id)
        self.assertIsNone(tasks_mod.get_live_token(
            task_id, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db))
        self.assertEqual(
            tasks_mod.token_status_for(ACTION_TOOL, ACTION_ARGS, db_path=self.db),
            "expired")

    def test_reject_revokes_live_token(self):
        # Reject is only legal from proposed, and approval moves a task
        # to approved — so the reachable revoke path is a ticket minted
        # directly on a proposed task, then rejected. (Approval-minted
        # tickets are covered by test_revoked_token_would_block_*
        # through the same revoke function.)
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        tasks_mod.mint_approval_token(
            task_id, ACTION_TOOL, ACTION_ARGS, "Owner", "chat", db_path=self.db)
        self.assertTrue(tasks_mod.update_task_status(
            task_id, "rejected", actor="Owner", db_path=self.db))
        self.assertIsNone(tasks_mod.get_live_token(
            task_id, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db))
        self.assertEqual(
            tasks_mod.token_status_for(ACTION_TOOL, ACTION_ARGS, db_path=self.db),
            "none")
        revoked = self.audit(decision="token_revoked", tool_name=ACTION_TOOL)
        self.assertEqual(len(revoked), 1)
        self.assertEqual(revoked[0]["reason"], "token_revoked")

    def test_argument_values_never_stored_only_hashes(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        self.assertTrue(self.approve_task(task_id))
        gate_mod.decide_pre_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        # The marker string appears in the arguments and nowhere else.
        # The registry files must contain the hash, never the value.
        self.assertNotIn(b"ZULU-7-DELTA", self.db_bytes())
        for row in self.audit():
            h = row.get("args_hash")
            self.assertTrue(h is None or (len(h) == 64 and all(c in "0123456789abcdef" for c in h)), row)
        task = tasks_mod.get_task(task_id, db_path=self.db)
        self.assertEqual(task["action_args_hash"],
                         tasks_mod.canonical_args_hash(ACTION_ARGS))


class TestHookWarnMode(GateBase):
    def test_type1_without_token_would_block_and_returns_none(self):
        result = gate_mod._on_pre_tool_call(ACTION_TOOL, ACTION_ARGS)
        self.assertIsNone(result)
        decision = gate_mod.decide_pre_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        self.assertEqual(decision["decision"], "would_block")
        self.assertEqual(decision["reason"], "no_token")
        self.assertEqual(decision["mode"], "warn")
        # One row per call: the hook call above wrote one, the direct
        # decide call wrote the second.
        rows = self.audit(decision="would_block", tool_name=ACTION_TOOL)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["reason"], "no_token")
        self.assertEqual(rows[0]["mode"], "warn")
        self.assertEqual(rows[0]["args_hash"],
                         tasks_mod.canonical_args_hash(ACTION_ARGS))

    def test_type1_with_live_token_would_allow_and_token_not_consumed(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        self.assertTrue(self.approve_task(task_id))
        token_id = tasks_mod.get_live_token(
            task_id, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db)["token_id"]
        self.assertIsNone(gate_mod._on_pre_tool_call(ACTION_TOOL, ACTION_ARGS))
        decision = gate_mod.decide_pre_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        self.assertEqual(decision["decision"], "would_allow")
        self.assertEqual(decision["reason"], "token_match")
        self.assertEqual(decision["token_id"], token_id)
        self.assertEqual(decision["task_id"], task_id)
        # Stage 1 never consumes: the ticket is still live afterwards.
        self.assertIsNotNone(tasks_mod.get_live_token(
            task_id, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db))
        # One row per call: the hook call above wrote one, the direct
        # decide call wrote the second.
        rows = self.audit(decision="would_allow", tool_name=ACTION_TOOL)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["token_id"], token_id)

    def test_modified_arguments_would_block(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        self.assertTrue(self.approve_task(task_id))
        decision = gate_mod.decide_pre_tool_call(
            ACTION_TOOL, ACTION_ARGS_CHANGED, db_path=self.db)
        self.assertEqual(decision["decision"], "would_block")
        self.assertEqual(decision["reason"], "modified_arguments")

    def test_spent_token_would_block_token_used(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        token_id = tasks_mod.mint_approval_token(
            task_id, ACTION_TOOL, ACTION_ARGS, "Owner", "chat", db_path=self.db)
        # Simulate Stage 2 consumption: the ticket was used once.
        with _db(self.db) as conn:
            conn.execute(
                "UPDATE approval_tokens SET used_at = ?, status = 'used' WHERE token_id = ?",
                ("2026-11-20T00:00:00+00:00", token_id))
        decision = gate_mod.decide_pre_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        self.assertEqual(decision["decision"], "would_block")
        self.assertEqual(decision["reason"], "token_used")
        self.assertEqual(
            tasks_mod.token_status_for(ACTION_TOOL, ACTION_ARGS, db_path=self.db),
            "used")

    def test_expired_token_would_block_token_expired(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        tasks_mod.mint_approval_token(
            task_id, ACTION_TOOL, ACTION_ARGS, "Owner", "chat",
            db_path=self.db, ttl_minutes=-1)
        decision = gate_mod.decide_pre_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        self.assertEqual(decision["decision"], "would_block")
        self.assertEqual(decision["reason"], "token_expired")

    def test_revoked_token_would_block_token_revoked(self):
        # Mint directly on a proposed task, then reject it (approved ->
        # rejected is not a legal transition, so the reachable revoke
        # path runs through a proposed task).
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        tasks_mod.mint_approval_token(
            task_id, ACTION_TOOL, ACTION_ARGS, "Owner", "chat", db_path=self.db)
        tasks_mod.update_task_status(task_id, "rejected", actor="Owner", db_path=self.db)
        decision = gate_mod.decide_pre_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        self.assertEqual(decision["decision"], "would_block")
        self.assertEqual(decision["reason"], "token_revoked")

    def test_cue_based_type1_without_token_would_block(self):
        # ceo_brief is not in APPROVAL/SIDE_EFFECT_TOOLS, but its wording
        # carries the external cue, so the narrow net still catches it.
        decision = gate_mod.decide_pre_tool_call(
            "ceo_brief", {"goal": "publish the launch announcement to our followers"},
            db_path=self.db)
        self.assertEqual(decision["decision"], "would_block")
        self.assertEqual(decision["reason"], "no_token")

    def test_type2_call_writes_no_audit_row(self):
        before = len(self.audit())
        decision = gate_mod.decide_pre_tool_call(
            "ceo_brief", {"goal": "plan Q4"}, db_path=self.db)
        self.assertEqual(decision["decision"], "type2")
        self.assertEqual(len(self.audit()), before)

    def test_registry_unavailable_would_block_and_hook_returns_none(self):
        missing = os.path.join(self.tmp.name, "missing.db")
        decision = gate_mod.decide_pre_tool_call(
            ACTION_TOOL, ACTION_ARGS, db_path=missing)
        self.assertEqual(decision["decision"], "would_block")
        self.assertEqual(decision["reason"], "registry_unavailable")
        # The hook itself never raises, even with no registry to read.
        old_env = os.environ.get("CEO_PACK_TASK_DB")
        os.environ["CEO_PACK_TASK_DB"] = missing
        try:
            self.assertIsNone(gate_mod._on_pre_tool_call(ACTION_TOOL, ACTION_ARGS))
        finally:
            if old_env is None:
                os.environ.pop("CEO_PACK_TASK_DB", None)
            else:
                os.environ["CEO_PACK_TASK_DB"] = old_env

    def test_hook_never_raises_on_internal_error(self):
        # A find_live_token failure is a REGISTRY error by design (handled
        # as would_block / registry_unavailable), so the internal-error
        # path is simulated by breaking the hash itself.
        with mock.patch.object(
                gate_mod._tasks, "canonical_args_hash",
                side_effect=RuntimeError("boom")):
            decision = gate_mod.decide_pre_tool_call(
                ACTION_TOOL, ACTION_ARGS, db_path=self.db)
            self.assertEqual(decision["decision"], "hook_error")
            self.assertEqual(decision["reason"], "hook_error")
            self.assertIsNone(gate_mod._on_pre_tool_call(ACTION_TOOL, ACTION_ARGS))
        rows = self.audit(decision="hook_error", tool_name=ACTION_TOOL)
        # One row per call: the direct decide call wrote one, the hook
        # call wrote the second.
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["reason"], "hook_error")

    def test_hook_returns_none_for_garbage_inputs(self):
        self.assertIsNone(gate_mod._on_pre_tool_call(None, "not a dict"))
        self.assertIsNone(gate_mod._on_pre_tool_call("", None))
        decision = gate_mod.decide_pre_tool_call(None, "not a dict", db_path=self.db)
        self.assertEqual(decision["decision"], "type2")


class TestEnvelopeAdvisory(GateBase):
    def test_make_tool_call_carries_token_status_live(self):
        task_id = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        self.assertTrue(self.approve_task(task_id))
        envelope = orch.make_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        self.assertTrue(envelope["valid"])
        self.assertEqual(envelope["token_status"], "live")

    def test_make_tool_call_carries_token_status_none_without_token(self):
        envelope = orch.make_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        self.assertTrue(envelope["valid"])
        self.assertEqual(envelope["token_status"], "none")

    def test_make_tool_call_token_status_degrades_to_none_on_missing_registry(self):
        missing = os.path.join(self.tmp.name, "missing.db")
        envelope = orch.make_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=missing)
        self.assertTrue(envelope["valid"])
        self.assertEqual(envelope["token_status"], "none")


class TestDashboardGate(GateBase):
    def _create_via_tool(self, tool_name=None, arguments=None):
        payload = {
            "run_id": self.run_id, "seat": "CFO",
            "title": "Sign the vendor NDA", "approval_required": True,
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
        if arguments is not None:
            payload["arguments"] = arguments
        result = json.loads(tools_mod.ceo_task_create(payload))
        self.assertEqual(result["status"], "success", result)
        return result["task_id"]

    def test_dashboard_approve_mints_and_reports(self):
        task_id = self._create_via_tool(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        response = api.approve_task_route(task_id=task_id, body=None)
        self.assertEqual(response["status"], "approved")
        self.assertTrue(response["token_minted"])
        self.assertIsNotNone(response["token_id"])
        self.assertEqual(response["action_tool"], ACTION_TOOL)

    def test_dashboard_approve_with_action_in_body_mints(self):
        task_id = self._create_via_tool()
        response = api.approve_task_route(
            task_id=task_id,
            body={"tool_name": ACTION_TOOL, "arguments": ACTION_ARGS})
        self.assertTrue(response["token_minted"])
        self.assertIsNotNone(response["token_id"])

    def test_dashboard_approve_without_action_mints_nothing(self):
        task_id = self._create_via_tool()
        response = api.approve_task_route(task_id=task_id, body=None)
        self.assertFalse(response["token_minted"])
        self.assertIsNone(response["token_id"])

    def test_slash_approve_reports_token_minted(self):
        # Owner approval in chat is the /task-approve slash command (typed
        # by the user, never model-routed); the model has no approve tool.
        task_id = self._create_via_tool(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        reply = _slash_approve()(task_id)
        self.assertIn("Approved", reply)
        self.assertIn("One-time ticket minted for " + ACTION_TOOL, reply)
        self.assertIsNotNone(tasks_mod.find_live_token(
            ACTION_TOOL, tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db))

    def test_slash_approve_without_action_reports_no_token(self):
        task_id = self._create_via_tool()
        reply = _slash_approve()(task_id)
        self.assertIn("Approved", reply)
        self.assertIn("no ticket was minted", reply)

    def test_model_cannot_mint_a_ticket(self):
        self.assertFalse(hasattr(tools_mod, "ceo_task_approve"))

    def test_gate_decisions_lists_audit_rows(self):
        task_id = self._create_via_tool(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        self.approve_task(task_id, minted_via="chat")
        gate_mod.decide_pre_tool_call(ACTION_TOOL, ACTION_ARGS, db_path=self.db)
        gate_mod.decide_pre_tool_call(ACTION_TOOL, ACTION_ARGS_CHANGED, db_path=self.db)
        response = api.gate_decisions(limit=50)
        self.assertGreaterEqual(response["count"], 3)
        decisions = {r["decision"] for r in response["decisions"]}
        self.assertIn("token_minted", decisions)
        self.assertIn("would_allow", decisions)
        self.assertIn("would_block", decisions)
        for row in response["decisions"]:
            self.assertEqual(row["mode"], "warn")
            self.assertNotIn("ZULU-7-DELTA", json.dumps(row))

    def test_dashboard_reject_revokes_token(self):
        # Reject is only legal from proposed, so the token is minted
        # directly on the proposed task (approval would move it to
        # approved, where reject is not a legal transition).
        task_id = self._create_via_tool(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        tasks_mod.mint_approval_token(
            task_id, ACTION_TOOL, ACTION_ARGS, "Owner", "dashboard", db_path=self.db)
        self.assertIsNotNone(tasks_mod.get_live_token(
            task_id, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db))
        api.reject_task_route(task_id=task_id, body={"note": "not now"})
        self.assertIsNone(tasks_mod.get_live_token(
            task_id, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db))
        self.assertEqual(len(self.audit(decision="token_revoked")), 1)


class TestMigration(GateBase):
    def _downgrade_to_v1(self):
        """Rebuild the temp registry as a v1 registry: drop the gate
        tables and the action columns, reset the version row."""
        with _db(self.db) as conn:
            conn.execute("UPDATE schema_version SET version = 1")
            conn.execute("DROP TABLE approval_tokens")
            conn.execute("DROP TABLE gate_audit")
            conn.execute("DROP TABLE tool_intents")
            conn.execute("ALTER TABLE tasks DROP COLUMN action_tool")
            conn.execute("ALTER TABLE tasks DROP COLUMN action_args_hash")

    def test_v1_registry_migrates_additively(self):
        # A task created before the migration keeps working after it.
        old_task = self.make_task()
        self._downgrade_to_v1()
        tasks_mod.init_db(self.db)
        with _db(self.db) as conn:
            version = conn.execute(
                "SELECT version FROM schema_version LIMIT 1").fetchone()[0]
        self.assertEqual(version, 2)
        self.assertTrue(self.approve_task(old_task))
        self.assertEqual(
            tasks_mod.get_task(old_task, db_path=self.db)["status"], "approved")
        # And the gate works on the migrated registry.
        new_task = self.make_task(tool_name=ACTION_TOOL, arguments=ACTION_ARGS)
        self.assertTrue(self.approve_task(new_task))
        self.assertIsNotNone(tasks_mod.get_live_token(
            new_task, ACTION_TOOL,
            tasks_mod.canonical_args_hash(ACTION_ARGS), db_path=self.db))

    def test_fresh_db_is_schema_v2(self):
        with _db(self.db) as conn:
            version = conn.execute(
                "SELECT version FROM schema_version LIMIT 1").fetchone()[0]
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        self.assertEqual(version, 2)
        self.assertIn("approval_tokens", tables)
        self.assertIn("gate_audit", tables)
        self.assertIn("tool_intents", tables)


class TestDelegationGap(GateBase):
    """Stage 1 leaves routing unchanged: route_task cannot know whether
    the target profile has ceo-pack installed (no cross-profile plugin
    list exists in the host APIs the provider can call), so there is no
    verifiable mechanism to refuse a profile-routed Type 1 task. The gap
    is documented in the design doc §10; this test pins the current
    behavior so a future refusal is a deliberate, tested change."""

    def test_profile_routed_type1_task_is_not_refused(self):
        provider = dp.CEODelegationProvider()
        # settings() reads config["backends"]["ceo_pack"] directly (no
        # "delegation" wrapper); seats is a list of seat configs.
        config = {
            "backends": {"ceo_pack": {
                "seats": [
                    {"seat": "CFO", "type": "profile_route", "profile": "builder"},
                ],
            }},
        }
        task = {"seat": "CFO", "goal": "Pay invoice",
                "approval_required": True, "action_tool": ACTION_TOOL,
                "arguments": ACTION_ARGS}
        route = provider.route_task(task, config)
        self.assertEqual(route, {"profile": "builder", "label": "CFO (profile: builder)"})


if __name__ == "__main__":
    unittest.main()


class TestMergeRegressions(GateBase):
    """v0.6.0 x main merge: the owner-only approval fix must survive the
    v0.6 assign rewrite (try_assign_task) and the single pre_tool_call hook
    must still feed the Stage 1 observer."""

    def test_try_assign_refuses_unapproved_type1(self):
        task_id = self.make_task()
        outcome, row = tasks_mod.try_assign_task(task_id, "CFO", db_path=self.db)
        self.assertEqual(outcome, "awaiting_approval")
        stored = tasks_mod.get_task(task_id, db_path=self.db)
        self.assertEqual(stored["status"], "proposed")
        self.assertIsNone(stored["owner"])          # nothing written

    def test_try_assign_allows_after_owner_approval(self):
        task_id = self.make_task()
        self.approve_task(task_id)
        outcome, _ = tasks_mod.try_assign_task(task_id, "CFO", db_path=self.db)
        self.assertEqual(outcome, "assigned")

    def test_dashboard_assign_refuses_unapproved_type1_with_409(self):
        task_id = self.make_task()
        try:
            api.assign_task_route(task_id=task_id, body={"owner": "CFO"})
            self.fail("expected 409")
        except Exception as exc:
            self.assertEqual(getattr(exc, "status_code", None), 409)
        self.assertIsNone(tasks_mod.get_task(task_id, db_path=self.db)["owner"])

    def test_tool_assign_refuses_unapproved_type1(self):
        task_id = self.make_task()
        out = json.loads(tools_mod.ceo_task_assign({"task_id": task_id, "owner": "CFO"}))
        self.assertEqual(out["status"], "error")
        self.assertIn("waiting for owner approval", json.dumps(out))

    def test_single_registered_hook_still_feeds_the_observer(self):
        from test_v050_registration import RecordingCtx, load_plugin_package
        pkg = load_plugin_package("ceo_pack_gate_hook_test")
        ctx = RecordingCtx()
        pkg.register(ctx)
        hooks = [cb for (event, cb) in ctx.hooks if event == "pre_tool_call"] \
            if isinstance(ctx.hooks, list) else [ctx.hooks["pre_tool_call"]]
        self.assertEqual(len(hooks), 1)
        self.assertIsNone(hooks[0](tool_name="ceo_assign", args={"task": "x"}))
        with _db(self.db) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM gate_audit WHERE tool_name = 'ceo_assign'").fetchone()[0]
        self.assertEqual(n, 1)
