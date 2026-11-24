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
            r = o.create_business_data_opt_in(["customer_list", f], "Quarterly review", session_id="s")
            self.assertFalse(r["valid"], f)
            self.assertNotIn("abcdefghijklmnop", str(r))

    def test_secret_purpose_refused_not_echoed(self):
        r = o.create_business_data_opt_in(["customer_list"], "check balance with " + FAKE_SK, session_id="s")
        self.assertFalse(r["valid"])
        self.assertNotIn("abcdefghijklmnop", str(r))

    def test_bounds(self):
        self.assertFalse(o.create_business_data_opt_in(["f%d" % i for i in range(51)], "p", session_id="s")["valid"])
        self.assertFalse(o.create_business_data_opt_in(["f"], "p" * 501, session_id="s")["valid"])
        self.assertFalse(o.create_business_data_opt_in(["f" * 101], "p", session_id="s")["valid"])

    def test_session_binding(self):
        r = o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id="chat-A")
        tok = r["opt_in"]["token"]
        self.assertTrue(o.check_business_data_opt_in(tok, "customer_list", session_id="chat-A"))
        self.assertFalse(o.check_business_data_opt_in(tok, "customer_list", session_id="chat-B"))
        self.assertFalse(o.check_business_data_opt_in(tok, "customer_list"))

    def test_session_is_mandatory(self):
        for sid in (None, "", "   ", 5):
            r = o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id=sid)
            self.assertFalse(r["valid"], repr(sid))
            self.assertIn("session_id is required", r["error"])
        self.assertEqual(o._OPTIN_STORE, {})

    def test_token_is_unguessable_and_unique(self):
        toks = {o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id="s")["opt_in"]["token"]
                for _ in range(50)}
        self.assertEqual(len(toks), 50)                       # same inputs, 50 different tokens
        self.assertTrue(all(len(t) >= 30 for t in toks))

    def test_audit_never_stores_raw_token_or_session(self):
        r = o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id="chat-secret-id")
        tok = r["opt_in"]["token"]
        o.check_business_data_opt_in(tok, "customer_list", session_id="chat-secret-id")
        o.revoke_business_data_opt_in(tok)
        dump = str(o.OPTIN_AUDIT_LOG)
        self.assertNotIn(tok, dump)
        self.assertNotIn("chat-secret-id", dump)

    def test_every_audit_record_is_timestamped(self):
        o.create_business_data_opt_in(["customer_list"], "Quarterly review", session_id="s")
        o.check_business_data_opt_in("bogus", "x", session_id="s")
        self.assertTrue(o.OPTIN_AUDIT_LOG and all("at" in e for e in o.OPTIN_AUDIT_LOG))

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
        self.assertEqual(checks[2]["token_fp"], "<unknown>")      # an unknown token is never echoed back

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


class LadderFindings(unittest.TestCase):
    """Regressions from the first Hermes test-ladder run (L1-1, L2-2, L2-6, L2-7, L4-1, L5-4)."""

    @staticmethod
    def run_tool(fn, **args):
        import json
        import tools
        return json.loads(getattr(tools, fn)(args))

    def test_L1_1_brief_is_not_blanket_gated(self):
        out = self.run_tool("ceo_brief", goal="Grow online subscriptions by 20% this quarter")
        self.assertFalse(out["approval_required"])
        self.assertNotIn("approval gate", out["decision_needed"])

    def test_L1_1_brief_still_gates_risky_goal(self):
        self.assertTrue(self.run_tool("ceo_brief", goal="Pay all vendors today")["approval_required"])

    def test_L4_1_launch_reads_as_external(self):
        plan = self.run_tool("ceo_orchestrate", goal='Launch the "Winter Blend" subscription box')
        self.assertIn("external", plan["inferred_risk"])
        self.assertTrue(plan["steps"][3]["approval_required"])

    def test_launch_cue_does_not_gate_unrelated_work(self):
        self.assertFalse(self.run_tool("ceo_orchestrate", goal="Summarize last week's metrics")["steps"][3]["approval_required"])

    def test_L2_7_unknown_field_is_reported_not_silently_dropped(self):
        out = self.run_tool("ceo_assign", task="Draft notes", seat="CFO", due_date="2030-01-01", risk={"external": True})
        self.assertEqual(out["ignored_fields"], ["risk"])
        self.assertIn("ceo_tool_call", out["warning"])

    def test_known_fields_produce_no_warning(self):
        out = self.run_tool("ceo_assign", task="Draft notes", seat="CFO", due_date="2030-01-01")
        self.assertNotIn("ignored_fields", out)
        self.assertNotIn("warning", out)

    def test_L2_2_price_email_to_subscribers_not_routed_to_chief_of_staff(self):
        out = self.run_tool("ceo_route", task="Email all 400 subscribers that prices go up in January.")
        self.assertIn(out["routed_to"], ("CRO", "CMO"))
        self.assertTrue(out["approval_required"])
        self.assertEqual(out["confidence"], "medium")      # three seats matched

    def test_L2_6_single_generic_word_is_not_high_confidence(self):
        out = self.run_tool("ceo_route", task="Review the proposal for new laptops.")
        self.assertEqual(out["confidence"], "medium")

    def test_two_term_single_seat_match_stays_high(self):
        out = self.run_tool("ceo_route", task="Reconcile October books and flag duplicate vendor charges.")
        self.assertEqual((out["routed_to"], out["confidence"]), ("CFO", "high"))

    def test_L5_4_data_reading_gets_honesty_note(self):
        for fn, key in (("ceo_brief", "goal"), ("ceo_route", "task"), ("ceo_assign", "task"), ("ceo_orchestrate", "goal")):
            args = {key: "Read our customer list and summarize churn risk"}
            if fn == "ceo_assign":
                args.update(seat="CRO", due_date="2030-01-01")
            out = self.run_tool(fn, **args)
            self.assertIn("does not read business data", out["data_access_note"], fn)

    def test_L5_4_exact_ladder_prompt_with_underscore_field_name(self):
        out = self.run_tool("ceo_route", task="Read our customer_list and summarize churn risk.")
        self.assertIn("data_access_note", out)

    def test_ordinary_task_has_no_data_note(self):
        self.assertNotIn("data_access_note", self.run_tool("ceo_route", task="Draft the weekly status update"))

    def test_chro_not_claimed_as_a_seat(self):
        self.assertNotIn("CHRO", Path(__file__).resolve().parent.parent.joinpath("docs/ARCHITECTURE.md").read_text())


class InstallConfigApplied(unittest.TestCase):
    """Bug: _apply_install_config guessed ctx.config/settings/plugin_config, which the real Hermes
    PluginContext does not have (it exposes ctx.get_config(key, default)), so nothing was applied.
    Seven declared settings were also never read by any code."""

    class RealShapeCtx:
        def __init__(self, cfg):
            self._cfg = cfg

        def get_config(self, key, default=None):
            return self._cfg.get(key, default)

    @staticmethod
    def _load_pkg():
        import importlib.util
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("ceo_pack_cfg_test", root / "__init__.py",
                                                      submodule_search_locations=[str(root)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["ceo_pack_cfg_test"] = pkg
        spec.loader.exec_module(pkg)
        return pkg

    def setUp(self):
        self._reset()

    def tearDown(self):
        self._reset()

    @staticmethod
    def _reset():
        o.reset_install_config()
        o.set_seat_aliases({})
        o.set_head_seat("CEO")
        o.SEAT_BINDINGS.clear()

    def test_real_shaped_ctx_applies_aliases_and_head_seat(self):
        pkg = self._load_pkg()
        pkg._apply_install_config(self.RealShapeCtx({"alias_coo": "Poseidon", "head_seat": "CFO"}))
        po = sys.modules["ceo_pack_cfg_test.orchestration"]    # the package's own module instance
        self.assertEqual(po.normalize_seat("Poseidon"), "COO")
        self.assertEqual(po.HEAD_SEAT, "CFO")

    def test_legacy_dict_ctx_still_honoured(self):
        pkg = self._load_pkg()

        class Legacy:
            config = {"alias_cmo": "Muse"}
        pkg._apply_install_config(Legacy())
        po = sys.modules["ceo_pack_cfg_test.orchestration"]
        self.assertEqual(po.normalize_seat("Muse"), "CMO")

    def test_ctx_without_any_config_is_harmless(self):
        self._load_pkg()._apply_install_config(object())
        self.assertEqual(sys.modules["ceo_pack_cfg_test.orchestration"].INSTALL["cost_mode"], "free")

    def test_all_declared_settings_are_applied(self):
        applied, notes = o.apply_install_config({
            "business_name": "Harbor Lane Coffee Co.", "owner_name": "Sam Rivera", "cost_mode": "paid_value",
            "model_provider": "openrouter", "model_choice": "free_worker", "head_seat": "COO", "head_profile": "ops-agent"})
        self.assertEqual(notes, [])
        for k in ("business_name", "owner_name", "cost_mode", "model_provider", "model_choice", "head_seat", "head_profile"):
            self.assertIn(k, applied)
        self.assertEqual(o.SEAT_BINDINGS["COO"]["profile"], "ops-agent")

    def test_every_declared_yaml_key_is_known_to_the_loader(self):
        import re
        yaml_text = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text()
        block = yaml_text.split("config_schema:")[1]
        declared = set(re.findall(r"^  ([a-z_]+): \{type", block, re.M))
        self.assertTrue(declared)
        self.assertEqual(declared - set(o.CONFIG_KEYS), set(), "declared in plugin.yaml but not applied by code")

    def test_invalid_values_refused_and_previous_kept(self):
        o.apply_install_config({"cost_mode": "paid_value"})
        applied, notes = o.apply_install_config({"cost_mode": "platinum", "model_choice": "nope",
                                                 "model_provider": "Bad Provider!", "business_name": FAKE_SK})
        self.assertEqual(applied, [])
        self.assertEqual(len(notes), 4)
        self.assertEqual(o.INSTALL["cost_mode"], "paid_value")
        self.assertNotIn("abcdefghijklmnop", str(notes))

    def test_approval_required_false_is_reported_not_obeyed(self):
        _, notes = o.apply_install_config({"approval_required": False})
        self.assertTrue(any("NOT applied" in n for n in notes))
        env = o.make_tool_call("ceo_assign", {"task": "Pay the vendor invoice", "seat": "CFO", "due_date": "2030-01-01"})
        self.assertTrue(env["approval_required"])

    def test_settings_change_tool_defaults(self):
        import json
        import tools
        o.apply_install_config({"business_name": "Harbor Lane Coffee Co.", "owner_name": "Sam Rivera",
                                "cost_mode": "paid_value", "model_provider": "openrouter", "model_choice": "free_worker"})
        brief = json.loads(tools.ceo_brief({"goal": "Grow subscriptions"}))
        self.assertEqual((brief["business_name"], brief["owner"]), ("Harbor Lane Coffee Co.", "Sam Rivera"))
        choice = json.loads(tools.ceo_choose_model({}))
        self.assertEqual(choice["provider_requested"], "openrouter")
        self.assertEqual(choice["recommended"]["key"], "paid_value")
        plan = json.loads(tools.ceo_orchestrate({"goal": "Grow subscriptions"}))
        self.assertEqual(plan["model"]["key"], "free_worker")
        self.assertEqual(json.loads(tools.ceo_choose_model({"budget": "free"}))["recommended"]["key"], "free_default")


class AliasLookalikes(_Clean):
    """Chat review: punctuation/space variants must not slip past the reserved-name check."""

    def test_lookalike_variants_refused(self):
        for name in ("C.F.O.", "c f o", "C-F-O", "Chief  of   Staff", "chief-of-staff", "H.E.R.M.E.S", "c.o.s"):
            ok, err = o.set_seat_aliases({name: "COO"})
            self.assertFalse(ok, name)
            self.assertIn("reserved", err)

    def test_alias_count_capped(self):
        many = {"Name%d" % i: "COO" for i in range(17)}
        self.assertFalse(o.set_seat_aliases(many)[0])


if __name__ == "__main__":
    unittest.main()
