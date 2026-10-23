# Copyright (c) 2024, Navari Limited and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

MODULE = "frappe_mpsa_payments.frappe_mpsa_payments.doctype.b2c_payment_disbursement.b2c_payment_disbursement"


class TestB2CPaymentDisbursement(FrappeTestCase):
    """Validators of B2C Payment Disbursement, called on an unsaved document (no database rows)."""

    def setUp(self):
        self.doc = frappe.new_doc("B2C Payment Disbursement")

    # ------------------------------------------------------------ mandatory fields
    def _fill_mandatory(self):
        self.doc.company = "Test Company"
        self.doc.posting_date = "2024-06-01"
        self.doc.party_type = "Supplier"
        self.doc.paid_from = "Test Paid From"
        self.doc.paid_to = "Test Paid To"

    def test_missing_mandatory_field_is_rejected(self):
        self._fill_mandatory()
        self.doc.company = None
        self.doc.append("references", {})
        with self.assertRaises(frappe.ValidationError) as context:
            self.doc.validate_mandatory_fields()
        self.assertIn("Company is mandatory", str(context.exception))

    def test_no_references_is_rejected(self):
        self._fill_mandatory()
        with self.assertRaises(frappe.ValidationError) as context:
            self.doc.validate_mandatory_fields()
        self.assertIn("At least one reference is required", str(context.exception))

    def test_mandatory_fields_and_reference_pass(self):
        self._fill_mandatory()
        self.doc.append("references", {})
        self.doc.validate_mandatory_fields()

    # ------------------------------------------------------------ mode of payment / party type
    def test_mode_of_payment_required(self):
        self.doc.mode_of_payment = None
        with self.assertRaises(frappe.ValidationError) as context:
            self.doc.validate_mode_of_payment()
        self.assertIn("Mode of Payment is required", str(context.exception))

    def test_party_type_employee_and_supplier_pass(self):
        for party_type in ("Employee", "Supplier"):
            self.doc.party_type = party_type
            self.doc.validate_party_type()

    def test_party_type_customer_is_rejected(self):
        self.doc.party_type = "Customer"
        with self.assertRaises(frappe.ValidationError) as context:
            self.doc.validate_party_type()
        self.assertIn("Party Type must be Employee or Supplier", str(context.exception))

    # ------------------------------------------------------------ amounts
    def _amounts(self, paid, allocations, currency="KES", base=None):
        self.doc.paid_amount = paid
        self.doc.base_paid_amount = paid if base is None else base
        self.doc.paid_from_account_currency = currency
        self.doc.references = []
        for amount in allocations:
            self.doc.append("references", {"allocated_amount": amount})

    def test_zero_or_negative_paid_amount_is_rejected(self):
        for paid in (0, -10):
            self._amounts(paid, [paid])
            with self.assertRaises(frappe.ValidationError) as context:
                self.doc.validate_amounts()
            self.assertIn("Paid Amount must be greater than zero", str(context.exception))

    def test_base_paid_amount_required_for_foreign_currency(self):
        self._amounts(100, [100], currency="USD", base=0)
        with self.assertRaises(frappe.ValidationError) as context:
            self.doc.validate_amounts()
        self.assertIn("Base Paid Amount (KES) is required for M-Pesa", str(context.exception))

    def test_allocations_must_add_up_to_paid_amount(self):
        self._amounts(100, [60, 30])
        with self.assertRaises(frappe.ValidationError) as context:
            self.doc.validate_amounts()
        self.assertIn("Total allocated amount 90.0 must equal Paid Amount 100", str(context.exception))

    def test_matching_allocations_pass(self):
        self._amounts(150, [50, 100])
        self.doc.validate_amounts()

    # ------------------------------------------------------------ reference doctypes
    def _references(self, *doctypes):
        self.doc.transaction_to_pay_against = "Purchase Invoice"
        self.doc.references = []
        for doctype in doctypes:
            self.doc.append("references", {"reference_doctype": doctype})

    def test_matching_reference_doctypes_pass(self):
        self._references("Purchase Invoice", "Purchase Invoice")
        self.doc.validate_reference_doctypes()

    def test_first_mismatching_reference_doctype_is_reported(self):
        self._references("Purchase Invoice", "Employee Advance", "Employee Advance")
        with self.assertRaises(frappe.ValidationError) as context:
            self.doc.validate_reference_doctypes()
        self.assertIn(
            "Reference doctype 'Employee Advance' does not match 'Purchase Invoice' for row 2",
            str(context.exception),
        )

    # ------------------------------------------------------------ set_missing_values
    def _missing_values(self, **values):
        self.doc.update(
            {"company": "Test Company", "posting_date": "2024-06-01", "paid_amount": 100,
             "paid_from_account_currency": "KES", **values}
        )

    def test_company_currency_filled_from_company(self):
        self._missing_values(company_currency=None, source_exchange_rate=1.0)
        with patch("frappe.get_cached_value", return_value="KES"):
            self.doc.set_missing_values()
        self.assertEqual(self.doc.company_currency, "KES")

    def test_existing_company_currency_kept(self):
        self._missing_values(company_currency="USD", source_exchange_rate=1.0)
        with patch("frappe.get_cached_value", return_value="EUR"):
            self.doc.set_missing_values()
        self.assertEqual(self.doc.company_currency, "USD")

    def test_source_exchange_rate_filled_when_missing(self):
        self._missing_values(company_currency="USD", source_exchange_rate=None)
        with patch("erpnext.setup.utils.get_exchange_rate", return_value=0.0077) as rate:
            self.doc.set_missing_values()
        rate.assert_called_once_with("KES", "USD", "2024-06-01")
        self.assertEqual(self.doc.source_exchange_rate, 0.0077)

    def test_existing_source_exchange_rate_kept(self):
        self._missing_values(company_currency="USD", source_exchange_rate=1.5)
        with patch("erpnext.setup.utils.get_exchange_rate", return_value=2.0):
            self.doc.set_missing_values()
        self.assertEqual(self.doc.source_exchange_rate, 1.5)

    def test_base_paid_amount_converted_to_kes(self):
        self._missing_values(company_currency="USD", source_exchange_rate=1.0, paid_from_account_currency="USD")
        with patch("erpnext.setup.utils.get_exchange_rate", return_value=0.0077):  # USD -> KES "rate" is per KES
            self.doc.set_missing_values()
        self.assertAlmostEqual(self.doc.base_paid_amount, 100 / 0.0077)

    # ------------------------------------------------------------ Mpesa Settings lookup
    def test_mpesa_settings_found_by_gateway_name(self):
        self.doc.mode_of_payment = "Mpesa-Payroll"
        with patch("frappe.get_doc", return_value="settings") as get_doc:
            self.assertEqual(self.doc._get_mpesa_settings(), "settings")
        get_doc.assert_called_once_with("Mpesa Settings", {"payment_gateway_name": "Payroll"}, as_dict=True)

    def test_missing_mpesa_settings_is_reported(self):
        self.doc.mode_of_payment = "Mpesa-Missing"
        with patch("frappe.get_doc", side_effect=frappe.DoesNotExistError):
            with self.assertRaises(frappe.ValidationError) as context:
                self.doc._get_mpesa_settings()
        self.assertIn("No Mpesa Settings found for gateway Mpesa-Missing", str(context.exception))
