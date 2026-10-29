# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from .b2c_payment_disbursement_reference import is_valid_receiver_contact, sanitise_phone_number


class TestB2CPaymentDisbursementReference(FrappeTestCase):
    def _row(self, payment_type="Mpesa Disbursement", **values):
        row = frappe.get_doc(
            {
                "doctype": "B2C Payment Disbursement Reference",
                "idx": 1,
                "party": "_Test Supplier",
                "partyb": "0712345678",
                "allocated_amount": 100,
                **values,
            }
        )
        self.parent = frappe.new_doc("B2C Payment Disbursement")  # parent_doc must be a Document
        self.parent.payment_type = payment_type
        row.parent_doc = self.parent
        return row

    def test_valid_row_passes_and_number_is_normalised(self):
        row = self._row()
        row.validate()
        self.assertEqual(row.partyb, "254712345678")

    def test_party_is_mandatory(self):
        with self.assertRaises(frappe.ValidationError):
            self._row(party=None).validate()

    def test_amount_below_10_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            self._row(allocated_amount=5).validate()

    def test_amount_above_outstanding_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            self._row(allocated_amount=200, outstanding_amount=100).validate()

    def test_invalid_receiver_number_is_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            self._row(partyb="some_invalid_number").validate()

    def test_sanitise_phone_number(self):
        for raw in ("0712345678", "+254712345678", "254712345678", "0712345678 ", " 0712345678",
                    "0712345678 +", "0712345678  "):
            self.assertEqual(sanitise_phone_number(raw), "254712345678", raw)

    def test_sanitise_leaves_other_numbers_alone_apart_from_plus_and_spaces(self):
        for raw, expected in (
            ("+25471234567", "25471234567"),
            ("07123456789", "07123456789"),
            ("25471234567", "25471234567"),
            ("0712345678a", "0712345678a"),
        ):
            self.assertEqual(sanitise_phone_number(raw), expected, raw)

    def test_is_valid_receiver_contact(self):
        for contact in ("254712345678", "+254712345678", "0712345678", "0112345678", "+254112345678",
                        "254102345678", "0118123456", "0119123456", "0140123456", "254143123456", "0180123456",
                        "+254 182 123 456", "0111-444-514", "111444514"):
            self.assertTrue(is_valid_receiver_contact(contact), contact)
        for contact in ("07123456789", "25471234567", "+25471234567", "0712345678a", "01123456789",
                        "254122345678", "0152345678", "25416055453", ""):
            self.assertFalse(is_valid_receiver_contact(contact), contact)

    def test_a_014x_line_is_paid_and_stored_in_254_form(self):
        row = self._row(partyb="0142 000 280")
        row.validate()
        self.assertEqual(row.partyb, "254142000280")

    def test_a_refused_number_is_named_masked_with_the_forms_to_use(self):
        with self.assertRaises(frappe.ValidationError) as caught:
            self._row(partyb="25416055453").validate()
        message = str(caught.exception)
        self.assertNotIn("25416055453", message)
        self.assertIn("ending 453 (11 digits)", message)
        self.assertIn("01XX XXX XXX", message)
