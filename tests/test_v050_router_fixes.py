"""v0.5.0 router fixes (mobile ladder v3, 2026-11-19).

Two gaps found live on the laptop run (33/35):
- NDA: _RISK_CUES had no outside_authority cue at all, and the sign pattern
  required the document word immediately after the article, so "sign the
  vendor NDA" inferred no risk.
- Fire: "fire"/"terminate" were in the people RISK cue but not in the
  Chief of Staff ROUTE terms, so "Fire the shift lead" routed to CRO on
  the word "lead" — and even adding "fire" alone ties 1-1 with CRO, which
  wins ties on _ROUTE_PRIORITY order, so the "shift lead" phrase is the
  tie-breaker.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestration  # noqa: E402
import tools  # noqa: E402


def route(task, domain=None):
    args = {"task": task}
    if domain is not None:
        args["domain"] = domain
    out = json.loads(tools.ceo_route(args))
    assert out["status"] == "success", out
    return out


class NdaOutsideAuthority(unittest.TestCase):
    """Signing a document is outside unilateral authority (Type 1)."""

    def test_sign_vendor_nda_infers_outside_authority(self):
        risk = orchestration.infer_risk(
            {"task": "Review and sign the vendor NDA for Harbor Lane"})
        self.assertIn("outside_authority", risk)
        self.assertIn("legal_or_financial", risk)

    def test_sign_vendor_nda_routes_clo_with_approval(self):
        out = route("Review and sign the vendor NDA for Harbor Lane")
        self.assertEqual(out["routed_to"], "CLO")
        self.assertTrue(out["approval_required"])

    def test_sign_contract_and_agreement(self):
        for text in ("Sign the contract", "Sign the new vendor agreement",
                     "Countersign the lease"):
            risk = orchestration.infer_risk({"task": text})
            self.assertIn("outside_authority", risk, text)
            self.assertIn("legal_or_financial", risk, text)

    def test_sign_non_document_does_not_fire(self):
        self.assertEqual(
            orchestration.infer_risk({"task": "Sign the quarterly report"}), [])


class FireRoutesToChiefOfStaff(unittest.TestCase):
    """People actions route to Chief of Staff, not to CRO on the word "lead"."""

    def test_fire_shift_lead(self):
        out = route("Fire the shift lead")
        self.assertEqual(out["routed_to"], "Chief of Staff")
        self.assertIn("fire", out["matched_terms"])
        self.assertIn("shift lead", out["matched_terms"])
        self.assertTrue(out["approval_required"])
        self.assertIn("people", out["inferred_risk"])

    def test_terminate_and_layoff_shift_lead(self):
        self.assertEqual(route("Terminate the shift lead")["routed_to"],
                         "Chief of Staff")
        self.assertEqual(route("Lay off the night shift")["routed_to"],
                         "Chief of Staff")

    def test_cro_and_clo_guards_unchanged(self):
        # "proposal" still routes CRO, "ops" still must not fire inside it.
        out = route("Review the proposal for new laptops")
        self.assertEqual(out["routed_to"], "CRO")
        self.assertEqual(out["matched_terms"], ["proposal"])
        # CRO keeps its core: sales language still wins.
        self.assertEqual(route("Close the sales deal")["routed_to"], "CRO")
        # Terminating a CONTRACT is legal, not a people action.
        self.assertEqual(route("Terminate the vendor contract")["routed_to"],
                         "CLO")
        # Dismissing a complaint is support (CRO), not a people action.
        self.assertEqual(route("Dismiss the customer complaint")["routed_to"],
                         "CRO")


if __name__ == "__main__":
    unittest.main()
