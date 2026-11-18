"""Ladder v2 L2-7 regression: nested-risk bypass in ceo_tool_call.

Live run (2026-11-17, Nemotron 3 Ultra): ceo_tool_call with
risk={"extrenal": true} returned valid:true — the typo passed.
Root cause (verified against the v0.5.0 tree): the top-level `risk`
field IS validated, but the same flag nested inside `arguments` was
silently ignored by the gate. Fix (orchestration.make_tool_call):
refuse any `risk` key inside arguments, pointing at the top-level field.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestration as o  # noqa: E402
import tools  # noqa: E402


class NestedRiskRefused(unittest.TestCase):
    """A risk flag inside arguments must never pass the gate silently."""

    def test_nested_typo_flag_refused(self):
        # The exact L2-7 case from the live run.
        r = o.make_tool_call("ceo_brief", {"goal": "x", "risk": {"extrenal": True}})
        self.assertFalse(r["valid"])
        self.assertIn("top-level", r["error"])

    def test_nested_known_flag_also_refused(self):
        # Even a correctly-spelled flag is refused nested: the gate would
        # silently ignore it, so passing it would be a lie.
        r = o.make_tool_call("ceo_brief", {"goal": "x", "risk": {"external": True}})
        self.assertFalse(r["valid"])
        self.assertIn("top-level", r["error"])

    def test_top_level_typo_still_refused(self):
        r = o.make_tool_call("ceo_brief", {"goal": "x"}, risk={"extrenal": True})
        self.assertFalse(r["valid"])
        self.assertIn("Unknown risk flags", r["error"])

    def test_top_level_known_flag_still_works(self):
        r = o.make_tool_call("ceo_brief", {"goal": "x"}, risk={"external": True})
        self.assertTrue(r["valid"])
        self.assertTrue(r["approval_required"])

    def test_nested_risk_in_json_string_arguments_refused(self):
        # Models often send arguments as a JSON string; the parsed form
        # must be checked the same way.
        r = o.make_tool_call("ceo_brief", '{"goal": "x", "risk": {"external": true}}')
        self.assertFalse(r["valid"])
        self.assertIn("top-level", r["error"])

    def test_handler_end_to_end(self):
        out = json.loads(tools.ceo_tool_call({
            "tool_name": "ceo_brief",
            "arguments": {"goal": "x", "risk": {"extrenal": True}},
        }))
        self.assertFalse(out["valid"])
        self.assertIn("top-level", out["error"])

    def test_no_risk_key_still_valid(self):
        r = o.make_tool_call("ceo_brief", {"goal": "x"})
        self.assertTrue(r["valid"])


if __name__ == "__main__":
    unittest.main()
