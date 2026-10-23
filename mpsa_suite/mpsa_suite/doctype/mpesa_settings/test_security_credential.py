# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe

from mpsa_suite.compat import IntegrationTestCase

NAME = "SUITE-CRED-TEST"


class TestSecurityCredential(IntegrationTestCase):
    """SecurityCredential must follow the initiator password and the environment.

    Uses the Safaricom certificates bundled in utils/public_certs (no network).
    """

    def setUp(self):
        frappe.set_user("Administrator")
        if frappe.db.exists("Mpesa Settings", NAME):
            frappe.delete_doc("Mpesa Settings", NAME, force=True)
        self.doc = frappe.get_doc(
            {
                "doctype": "Mpesa Settings",
                "payment_gateway_name": NAME,
                "api_type": "MPesa B2C (Business to Customer)",
                "paybill_type": "Pay Bill",
                "sandbox": 1,
                "consumer_key": "key",
                "consumer_secret": "secret",
                "online_passkey": "passkey",
                "business_shortcode": "600000",
                "till_number": "600000",
                "initiator_name": "testapi",
                "initiator_password": "Safaricom999!*!",
            }
        ).insert()

    def test_generated_on_insert(self):
        self.assertTrue(self.doc.security_credential)
        self.assertGreater(len(self.doc.security_credential), 300)  # base64 of a 2048-bit RSA block

    def test_unchanged_when_nothing_relevant_changes(self):
        before = self.doc.security_credential
        self.doc.reload()
        self.doc.transaction_limit = 1000
        self.doc.save()
        self.assertEqual(self.doc.security_credential, before)

    def test_regenerated_when_password_retyped(self):
        before = self.doc.security_credential
        self.doc.reload()
        self.doc.initiator_password = "AnotherPass1!"
        self.doc.save()
        self.assertNotEqual(self.doc.security_credential, before)

    def test_regenerated_when_environment_flips(self):
        before = self.doc.security_credential
        self.doc.reload()
        self.doc.sandbox = 0
        self.doc.save()
        self.assertNotEqual(self.doc.security_credential, before)
