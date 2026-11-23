"""Type 1 gate, Stage 1: host-tool classification + watch rows.

The v0.6.0 Stage 1 gate ran infer_risk over the planning fields only
(task/goal/notes...). Host tools never carry those fields, so a host
send_email announcing a $500,000 wire classified as Type 2, while the
plugin's own planning tools (ceo_brief "Wire $50,000") read as Type 1.
These tests pin the execution-boundary behaviour:

- risk cues are read over EVERY argument string of a host tool call;
- the plugin's non-side-effect ceo_* tools are not execution;
- an unflagged call to a tool that can change things leaves an
  "observed" row (tool + hash + session, never the arguments), which is
  the watch data Adrian's answer #2 needs before the net is widened.

Warn mode only: nothing here blocks anything.
"""
import os
import sqlite3
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate as gate_mod  # noqa: E402
import tasks as tasks_mod  # noqa: E402
from test_v050_gate import GateBase, _db  # noqa: E402

RISKY_EMAIL = {"to": "press@nytimes.com", "subject": "Announcement",
               "body": "We are wiring $500,000 to the vendor today."}


class HostToolClassification(GateBase):

    def decide(self, tool, args, session_id="s-1"):
        return gate_mod.decide_pre_tool_call(tool, args, session_id=session_id, db_path=self.db)

    def test_risky_host_tool_body_is_type1(self):
        out = self.decide("mcp_gmail_send", RISKY_EMAIL)
        self.assertEqual(out["decision"], "would_block")
        self.assertEqual(len(self.audit("would_block")), 1)

    def test_nested_argument_strings_are_read(self):
        out = self.decide("mcp_crm_update", {"payload": {"steps": [{"note": "wire $9,000 to the vendor"}]}})
        self.assertEqual(out["decision"], "would_block")

    def test_terminal_with_plain_words_is_type1(self):
        out = self.decide("terminal", {"command": 'mail -s "Payment" vendor@acme.com <<< "Wiring $9,000 today"'})
        self.assertEqual(out["decision"], "would_block")

    def test_known_gap_code_shaped_commands_are_watched_not_flagged(self):
        # The cues are natural-language: identifiers like pay_invoice.py or
        # API paths like /v1/transfers do not match. That is a known gap,
        # recorded as an "observed" row for the owner to review — not a
        # silent pass and not a claim that the net caught it.
        for cmd in ("python pay_invoice.py --vendor acme --amount 9000",
                    "curl -X POST https://api.stripe.com/v1/transfers -d amount=5000000"):
            self.assertEqual(self.decide("terminal", {"command": cmd})["decision"], "type2")
        self.assertEqual(len(self.audit("observed")), 2)

    def test_plugin_planning_tool_is_not_execution(self):
        out = self.decide("ceo_brief", {"goal": "Wire $50,000 to the new vendor"})
        self.assertEqual(out["decision"], "type2")
        self.assertEqual(self.audit(), [])

    def test_plugin_side_effect_tool_is_still_type1(self):
        out = self.decide("ceo_assign", {"task": "anything"})
        self.assertEqual(out["decision"], "would_block")

    def test_live_ticket_still_allows_the_exact_host_call(self):
        task_id = self.make_task(tool_name="mcp_gmail_send", arguments=RISKY_EMAIL)
        self.approve_task(task_id)
        out = self.decide("mcp_gmail_send", RISKY_EMAIL)
        self.assertEqual(out["decision"], "would_allow")
        changed = dict(RISKY_EMAIL, body=RISKY_EMAIL["body"] + " Also cc the board.")
        self.assertEqual(self.decide("mcp_gmail_send", changed)["decision"], "would_block")

    def test_payload_scan_is_bounded(self):
        big = {"rows": ["harmless line %d" % i for i in range(20000)],
               "blob": "x" * 2_000_000}
        start = time.perf_counter()
        self.decide("write_file", big)
        self.assertLess(time.perf_counter() - start, 0.5)


class WatchRows(GateBase):

    def decide(self, tool, args, session_id="sess-42"):
        return gate_mod.decide_pre_tool_call(tool, args, session_id=session_id, db_path=self.db)

    def test_unflagged_effect_tool_is_observed(self):
        out = self.decide("terminal", {"command": "ls -la"})
        self.assertEqual(out["decision"], "type2")
        rows = self.audit("observed")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_name"], "terminal")
        self.assertEqual(rows[0]["reason"], "unflagged_effect_tool")
        self.assertEqual(rows[0]["session_id"], "sess-42")
        self.assertEqual(rows[0]["args_hash"], tasks_mod.canonical_args_hash({"command": "ls -la"}))

    def test_unknown_connector_tool_is_observed(self):
        self.decide("mcp_stripe_create_transfer", {"amount": 5000000, "destination": "acct_1"})
        rows = self.audit("observed")
        self.assertEqual([r["reason"] for r in rows], ["unflagged_unknown_tool"])

    def test_read_only_and_plugin_tools_leave_no_row(self):
        self.decide("read_file", {"path": "notes.md"})
        self.decide("web_search", {"query": "weather"})
        self.decide("ceo_task_list", {})
        self.assertEqual(self.audit(), [])

    def test_observed_rows_never_store_argument_text(self):
        secret = "sk_live_DO_NOT_STORE_7f3a"
        self.decide("terminal", {"command": "echo " + secret})
        with _db(self.db) as conn:
            dump = " ".join(str(v) for row in conn.execute("SELECT * FROM gate_audit") for v in row)
        self.assertNotIn(secret, dump)
        self.assertNotIn("echo", dump)

    def test_watch_never_blocks(self):
        for tool in ("terminal", "mcp_anything", "write_file"):
            self.assertIsNone(gate_mod._on_pre_tool_call(tool_name=tool, args={"x": "y"}, session_id="s"))


if __name__ == "__main__":
    unittest.main()
