# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.daraja import callbacks
from mpsa_suite.install import SUITE_ROLES

SETTINGS = "M-Pesa Suite Settings"


class FakeRequest(SimpleNamespace):
    pass


class TestMPesaSuiteSettings(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.settings = frappe.get_single(SETTINGS)
        self.settings.enforce_token_results = 0
        self.settings.enforce_token_c2b = 0
        self.settings.enforce_ip_allowlist = 0
        self.settings.save()
        callbacks.get_cache().delete_value("mpesa_suite:callback_rejected_recently")
        frappe.local.request_ip = "196.201.214.200"

    def tearDown(self):
        frappe.local.form_dict = frappe._dict()
        frappe.set_user("Administrator")
        # A rejected callback commits (callbacks._record_rejection), which also commits the
        # enforcement switched on above; switch it off for the suites that run after this one.
        frappe.db.set_single_value(
            SETTINGS, {"enforce_token_results": 0, "enforce_token_c2b": 0, "enforce_ip_allowlist": 0}
        )
        frappe.db.commit()

    def _request(self, token=None):
        args = {"t": token} if token else {}
        return patch.object(frappe.local, "request", FakeRequest(args=args, path="/api/method/x"), create=True)

    def test_roles_created(self):
        for role in SUITE_ROLES:
            self.assertTrue(frappe.db.exists("Role", role), role)

    def test_token_is_generated_and_appended(self):
        token = callbacks.callback_token()
        self.assertTrue(token and len(token) >= 32)
        url = callbacks.with_token("https://erp.example.com/api/method/a.b?provider=Mpesa")
        self.assertIn("provider=Mpesa", url)
        self.assertIn(f"t={token}", url)
        # idempotent: never adds a second token
        self.assertEqual(url.count("t="), 1)
        self.assertEqual(callbacks.with_token(url).count("t="), 1)

    def test_accepts_everything_when_not_enforced(self):
        with self._request():
            self.assertTrue(callbacks.accept(callbacks.KIND_RESULT))
            self.assertTrue(callbacks.accept(callbacks.KIND_C2B))

    def test_result_token_enforcement(self):
        self.settings.enforce_token_results = 1
        self.settings.save()
        with self._request():
            self.assertFalse(callbacks.accept(callbacks.KIND_RESULT))
        with self._request("wrong"):
            self.assertFalse(callbacks.accept(callbacks.KIND_RESULT))
        with self._request(callbacks.callback_token()):
            self.assertTrue(callbacks.accept(callbacks.KIND_RESULT))
        # C2B and Pull are governed separately
        with self._request():
            self.assertTrue(callbacks.accept(callbacks.KIND_C2B))
            self.assertTrue(callbacks.accept(callbacks.KIND_PULL))

    def test_ip_allowlist(self):
        self.settings.enforce_ip_allowlist = 1
        self.settings.save()
        with self._request():
            self.assertTrue(callbacks.accept(callbacks.KIND_C2B))
            frappe.local.request_ip = "8.8.8.8"
            self.assertFalse(callbacks.accept(callbacks.KIND_C2B))
            self.assertFalse(callbacks.accept(callbacks.KIND_PULL))

    def test_rejection_is_recorded(self):
        self.settings.enforce_token_c2b = 1
        self.settings.save()
        before = frappe.db.get_single_value(SETTINGS, "rejected_callbacks") or 0
        with self._request():
            self.assertFalse(callbacks.accept(callbacks.KIND_C2B))
        self.assertEqual(frappe.db.get_single_value(SETTINGS, "rejected_callbacks"), before + 1)

    def test_decorator_rejects_and_runs_as_integration_user(self):
        seen = {}

        @callbacks.safaricom_callback(callbacks.KIND_RESULT)
        def handler(Body=None):
            seen["user"] = frappe.session.user
            seen["body"] = Body
            return "ok"

        self.settings.enforce_token_results = 1
        self.settings.save()
        frappe.set_user("Guest")
        with self._request():
            self.assertEqual(handler(Body=1), callbacks.rejected_response())
        self.assertNotIn("user", seen)

        token = callbacks.callback_token()
        with self._request(token):
            # unknown kwargs (t, cmd, extra) are dropped for handlers without **kwargs
            self.assertEqual(handler(Body=2, t=token, cmd="x", extra=3), "ok")
        self.assertEqual(seen, {"user": callbacks.integration_user(), "body": 2})
        self.assertEqual(frappe.session.user, "Guest")  # caller restored

    def test_ip_allowlist_requires_entries(self):
        self.settings.enforce_ip_allowlist = 1
        self.settings.allowed_callback_ips = "not an ip"
        self.assertRaises(frappe.ValidationError, self.settings.save)
