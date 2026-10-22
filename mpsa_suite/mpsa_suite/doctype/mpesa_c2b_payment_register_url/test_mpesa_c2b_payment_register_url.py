# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
import requests

from mpsa_suite.compat import IntegrationTestCase

SETTINGS = "_Test C2B URL"
MODULE = "mpsa_suite.mpsa_suite.doctype.mpesa_c2b_payment_register_url.mpesa_c2b_payment_register_url"


def company() -> str:
    return frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")


def create_mpesa_settings():
    """Settings with a company, so saving it creates the Mode of Payment "Mpesa-<name>"."""
    if not frappe.db.exists("Mpesa Settings", SETTINGS):
        frappe.get_doc(
            {
                "doctype": "Mpesa Settings",
                "payment_gateway_name": SETTINGS,
                "api_type": "MPesa C2B (Customer to Business)",
                "paybill_type": "Pay Bill",
                "sandbox": 1,
                "consumer_key": "key",
                "consumer_secret": "secret",
                "online_passkey": "passkey",
                "business_shortcode": "174379",
                "till_number": "600984",
                "company": company(),
            }
        ).insert(ignore_permissions=True)


def response(json_body, status=200):
    r = Mock(status_code=status)
    r.json.return_value = json_body
    r.raise_for_status = Mock()
    return r


class TestMpesaC2BPaymentRegisterURL(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        create_mpesa_settings()
        if frappe.db.exists("Mpesa C2B Payment Register URL", SETTINGS):
            frappe.delete_doc("Mpesa C2B Payment Register URL", SETTINGS, force=True)

    def register(self, post):
        doc = frappe.get_doc(
            {
                "doctype": "Mpesa C2B Payment Register URL",
                "mpesa_settings": SETTINGS,
                "company": company(),
                "mode_of_payment": f"Mpesa-{SETTINGS}",
            }
        )
        with patch(f"{MODULE}.get_token", return_value="token"), patch("requests.post", post):
            doc.insert(ignore_permissions=True)
        return doc

    def test_success_registers_sandbox_till_with_token_urls(self):
        post = Mock(return_value=response({"ResponseCode": "0", "ResponseDescription": "Success"}))
        doc = self.register(post)
        self.assertEqual(doc.register_status, "Success")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["ShortCode"], "600984")  # sandbox registers the till / test shortcode
        self.assertIn("m_pesa_api.confirmation", payload["ConfirmationURL"])
        self.assertIn("m_pesa_api.validation", payload["ValidationURL"])

    def test_already_registered_counts_as_success(self):
        post = Mock(return_value=response({"errorCode": "500.003.1001", "errorMessage": "Already registered"}))
        self.assertEqual(self.register(post).register_status, "Success")

    def test_rejection_is_failed(self):
        post = Mock(return_value=response({"ResponseCode": "1", "ResponseDescription": "Failure"}))
        self.assertEqual(self.register(post).register_status, "Failed")

    def test_connection_error_is_failed(self):
        post = Mock(side_effect=requests.exceptions.ConnectionError("down"))
        self.assertEqual(self.register(post).register_status, "Failed")

    def test_saving_again_does_not_re_register(self):
        doc = self.register(Mock(return_value=response({"ResponseCode": "0"})))
        post = Mock()
        with patch("requests.post", post):
            doc.save(ignore_permissions=True)
        post.assert_not_called()
