"""Audit regression tests for the v0.4.0 seat-alias, display-text, and opt-in surface.

Each test pins a bug reproduced on main @ 1d45fe49. Fictional data only.
Run: python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestration as o  # noqa: E402

FAKE_SK = "sk-" + "abcdefghijklmnop1234567890"   # fake; assembled so no scanner flags this file


class _Clean(unittest.TestCase):
    def setUp(self):
        self._reset()

    def tearDown(self):
        self._reset()

    @staticmethod
    def _reset():
        o.set_seat_aliases({})
        o.SEAT_DISPLAY.clear()
        o.SEAT_BINDINGS.clear()
        o._OPTIN_STORE.clear()
        del o.OPTIN_AUDIT_LOG[:]
        o.OPTIN_AUDIT_DROPPED = 0


class AliasCannotShadowSeat(_Clean):
    """Bug: alias 'CFO' -> COO was accepted and silently rerouted CFO assignments to COO."""

    def test_alias_named_like_a_seat_is_refused(self):
        for name in ("CFO", "cfo", "CEO", "COO", "Chief of Staff", "Hermes", "COS", "CRO"):
            ok, err = o.set_seat_aliases({name: "COO"})
            self.assertFalse(ok, name)
            self.assertIn("reserved", err)

    def test_seat_routing_unchanged_after_refusal(self):
        o.set_seat_aliases({"CFO": "COO"})
        env = o.make_tool_call("ceo_assign", {"task": "Reconcile books", "seat": "CFO", "due_date": "2030-01-01"})
        self.assertEqual(env["seat"], "CFO")

    def test_normal_alias_still_works(self):
        self.assertEqual(o.set_seat_aliases({"Poseidon": "COO"}), (True, None))
        self.assertEqual(o.normalize_seat("Poseidon"), "COO")
        self.assertEqual(o.display_seat("COO"), "Poseidon (COO)")

    def test_alias_shape_limits(self):
        for bad in ("Bob (CFO)", "A" * 41, "", "x\ny", "<b>x</b>", FAKE_SK):
            ok, _ = o.set_seat_aliases({bad: "COO"})
            self.assertFalse(ok, repr(bad))

    def test_secret_alias_never_echoed(self):
        ok, err = o.set_seat_aliases({FAKE_SK: "COO"})
        self.assertFalse(ok)
        self.assertNotIn("abcdefghijklmnop", err)

    def test_refused_alias_leaves_previous_aliases_intact(self):
        o.set_seat_aliases({"Poseidon": "COO"})
        self.assertFalse(o.set_seat_aliases({"Zeus": "CEO", "CFO": "COO"})[0])
        self.assertEqual(o.normalize_seat("Poseidon"), "COO")


class DisplayTextGuarded(_Clean):
    """Bug: soul/avatar/display_name stored unscanned and unbounded, then echoed in the roster."""

    def test_secret_in_soul_refused_and_not_echoed(self):
        ok, err = o.set_seat_display("CFO", soul="Calm and exact. api_key: hunter2hunter2")
        self.assertFalse(ok)
        self.assertNotIn("hunter2", err)
        self.assertEqual(o.get_seat_display("CFO"), {})

    def test_length_limits(self):
        self.assertFalse(o.set_seat_display("CFO", soul="x" * 2001)[0])
        self.assertFalse(o.set_seat_display("CFO", avatar="x" * 501)[0])
        self.assertFalse(o.set_seat_display("CFO", display_name="x" * 41)[0])

    def test_normal_display_still_works(self):
        self.assertEqual(o.set_seat_display("CFO", display_name="Ledger", avatar="https://example.com/a.png",
                                            soul="Careful with numbers."), (True, None))
        self.assertEqual(o.get_seat_display("CFO")["display_name"], "Ledger")

    def test_roster_marks_text_untrusted(self):
        self.assertTrue(o.get_roster_view()["display_text_is_untrusted"])


class OptInHardened(_Clean):
    def test_credential_fields_refused(self):
        for f in ("api_key", "bank_password", "Access Token", "private_key", FAKE_SK):
            r = o.create_business_data_opt_in(["customer_list", f], "Quarterly review")
            self.assertFalse(r["valid"], f)
            self.assertNotIn("abcdefghijklmnop", str(r))

    def test_secret_purpose_refused_not_echoed(self):
        r = o.create_business_data_opt_in(["customer_list"], "check balance with " + FAKE_SK)
        self.assertFalse(r["valid"])
        self.assertNotIn("abcdefghijklmnop", str(r))

    def test_bounds(self):
        self.assertFalse(o.create_business_data_opt_in(["f%d" % i for i in range(51)], "p")["valid"])
        self.assertFalse(o.create_business_data_opt_in(["f"], "p" * 501)["valid"])
        self.assertFalse(o.create_business_data_opt_in(["f" * 101], "p")["valid"])

    def test_session_binding(self):
        r = o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id="chat-A")
        tok = r["opt_in"]["token"]
        self.assertTrue(o.check_business_data_opt_in(tok, "customer_list", session_id="chat-A"))
        self.assertFalse(o.check_business_data_opt_in(tok, "customer_list", session_id="chat-B"))
        self.assertFalse(o.check_business_data_opt_in(tok, "customer_list"))

    def test_unbound_opt_in_says_so(self):
        r = o.create_business_data_opt_in(["customer_list"], "Quarterly review")
        self.assertIn("Not bound", r["note"])
        self.assertTrue(o.check_business_data_opt_in(r["opt_in"]["token"], "customer_list"))

    def test_exact_field_only_and_revoke(self):
        r = o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id="s")
        tok = r["opt_in"]["token"]
        self.assertFalse(o.check_business_data_opt_in(tok, "payroll", session_id="s"))
        o.revoke_business_data_opt_in(tok)
        self.assertFalse(o.check_business_data_opt_in(tok, "customer_list", session_id="s"))

    def test_expired_is_denied(self):
        r = o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id="s")
        tok = r["opt_in"]["token"]
        o._OPTIN_STORE[tok]["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.assertFalse(o.check_business_data_opt_in(tok, "customer_list", session_id="s"))

    def test_every_check_is_audited_allowed_or_denied(self):
        r = o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id="s")
        tok = r["opt_in"]["token"]
        before = len(o.OPTIN_AUDIT_LOG)
        o.check_business_data_opt_in(tok, "customer_list", session_id="s")
        o.check_business_data_opt_in(tok, "payroll", session_id="s")
        o.check_business_data_opt_in("bogus-token", "x")
        checks = [e for e in o.OPTIN_AUDIT_LOG[before:] if e["event"] == "opt_in_check"]
        self.assertEqual([e["allowed"] for e in checks], [True, False, False])
        self.assertEqual(checks[2]["token"], "<unknown>")      # an unknown token is never echoed back

    def test_audit_log_is_bounded_and_drops_are_counted(self):
        for _ in range(o._OPTIN_AUDIT_MAX + 25):
            o.check_business_data_opt_in("bogus", "x")
        self.assertEqual(len(o.OPTIN_AUDIT_LOG), o._OPTIN_AUDIT_MAX)
        self.assertGreaterEqual(o.OPTIN_AUDIT_DROPPED, 25)


class ModelSlugsMatchNousCatalog(unittest.TestCase):
    """Bug: paid ids were bare 'claude-*' in a field named openrouter_id. Nous's catalog
    (hermes_cli/models_catalog_static.py) lists them as anthropic/claude-*-5.5."""

    def test_paid_openrouter_ids_are_vendor_prefixed(self):
        import models
        by_key = {m["key"]: m for m in models.MODELS}
        for key in ("paid_value", "paid_flagship"):
            self.assertTrue(by_key[key]["openrouter_id"].startswith("anthropic/"), key)

    def test_every_openrouter_id_is_vendor_slashed_or_router_alias(self):
        import models
        for spec in models.MODELS:
            rid = spec.get("openrouter_id")
            if rid:
                self.assertTrue("/" in rid, (spec["key"], rid))


if __name__ == "__main__":
    unittest.main()
