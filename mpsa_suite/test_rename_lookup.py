"""Old /api/method/frappe_mpsa_payments.* paths reach a whitelisted mpsa_suite function (R4, on a bench).

tests/unit/test_rename_safeguards.py checks the lookup table against release C from source; this
test imports every target the way Frappe's request handler does and checks Frappe's own
whitelist and guest registries.
"""

import json
from pathlib import Path

import frappe

from mpsa_suite.compat import IntegrationTestCase

# in the app's repository (bench get-app clones it whole); the guest check is skipped without it
SNAPSHOT = Path(__file__).resolve().parents[1] / "tests" / "unit" / "rename_whitelist_at_c.json"


class TestRenameLookup(IntegrationTestCase):
    def test_every_old_path_reaches_a_whitelisted_function(self):
        overrides = frappe.get_hooks("override_whitelisted_methods") or {}
        old_paths = sorted(p for p in overrides if p.startswith("frappe_mpsa_payments."))
        self.assertGreaterEqual(len(old_paths), 70)
        guest = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["whitelisted"] if SNAPSHOT.is_file() else {}
        for old in old_paths:
            target = overrides[old][-1]  # Frappe uses the last entry
            self.assertTrue(target.startswith("mpsa_suite."), target)
            fn = frappe.get_attr(target)
            self.assertIn(fn, frappe.whitelisted, f"{target} is not whitelisted")
            if old in guest:
                self.assertEqual(fn in frappe.guest_methods, guest[old], f"{target}: allow_guest differs")
