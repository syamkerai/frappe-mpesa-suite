"""Bench tests: a receipt whose register was cancelled, or then deleted, is never imported and
booked again (finding C17), through the real register hooks, Payment Entry and database.

Staff cancel a register (and so its Payment Entry) when the money was refunded in the M-PESA
portal or booked another way. Pull, which now re-reads 47 hours every hour, and Transaction
Status used to insert a new register for it, which auto-submitted and booked the money again.
"""

import json
from unittest.mock import patch

import frappe
from frappe.utils import flt

from frappe_mpsa_payments.compat import IntegrationTestCase
from frappe_mpsa_payments.frappe_mpsa_payments.api import m_pesa_api
from frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_response_handler import (
    pull_transaction_on_success,
)
from frappe_mpsa_payments.money_in import matching
from frappe_mpsa_payments.money_in import testing as t
from frappe_mpsa_payments.money_in.common import MATCH_LOG, REGISTER
from frappe_mpsa_payments.money_in.receipts import CANCELLED, DELETED, DUPLICATE, insert_register


def pull_response(transid: str, amount: float, billref: str) -> dict:
    return {
        "ResponseCode": "1000",
        "Response": [
            [
                {
                    "transactionId": transid,
                    "trxDate": "2026-10-18T10:00:00+03:00",
                    "msisdn": "254712345678",
                    "transactiontype": "c2b-paybill-debi",
                    "billreference": billref,
                    "amount": f"{amount:.2f}",
                }
            ]
        ],
    }


class TestFixPull(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test M-Pesa Customer")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    def booked_then_cancelled(self, amount: float = 1000):
        """A receipt booked against an invoice, then cancelled by staff with its Payment Entry."""
        si = t.sales_invoice(self.customer, amount)
        name = t.receipt(amount, billref=si.name)
        matching.match_register(name)  # books it if the auto-submit did not
        reg = frappe.get_doc(REGISTER, name)
        self.assertEqual(reg.docstatus, 1)
        pe = reg.payment_entry
        self.assertTrue(pe)
        reg.cancel()
        self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 2)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), amount)
        return si, reg

    def assert_not_booked_again(self, si, reg, amount: float = 1000):
        self.assertEqual(frappe.db.count(REGISTER, {"transid": reg.transid}), 1)
        self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": reg.transid, "docstatus": 1}))
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), amount)

    def test_pull_does_not_import_a_cancelled_receipt_again(self):
        si, reg = self.booked_then_cancelled()
        with patch.object(frappe.db, "commit"):  # keep the test's rows uncommitted
            pull_transaction_on_success(
                pull_response(reg.transid, 1000, si.name), t.SETTINGS, settings_name=t.SETTINGS
            )
        self.assert_not_booked_again(si, reg)

    def test_transaction_status_result_does_not_import_a_cancelled_receipt_again(self):
        si, reg = self.booked_then_cancelled()
        conversation_id = f"AG_{frappe.generate_hash(length=16)}"
        frappe.get_doc(  # the completed "Fetch transaction" query this result answers
            {
                "doctype": "Integration Request",
                "integration_request_service": "Mpesa Transaction Status",
                "reference_doctype": REGISTER,
                "status": "Completed",
                "data": json.dumps({"mpesa_settings": t.SETTINGS, "transaction_id": reg.transid}),
                "output": json.dumps({"ConversationID": conversation_id, "ResponseCode": "0"}),
            }
        ).insert(ignore_permissions=True)
        result = {
            "ResultCode": 0,
            "ConversationID": conversation_id,
            "ResultParameters": {
                "ResultParameter": [
                    {"Key": "DebitPartyName", "Value": "254708374149 - TEST PAYER"},
                    {"Key": "CreditPartyName", "Value": f"{t.SHORTCODE} - Test Co"},
                    {"Key": "TransactionStatus", "Value": "Completed"},
                    {"Key": "Amount", "Value": "1000.00"},
                    {"Key": "ReceiptNo", "Value": reg.transid},
                ]
            },
        }
        ir = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "integration_request_service": "Mpesa Transaction Status Result Callback",
                "reference_doctype": REGISTER,
                "status": "Queued",
                "data": json.dumps({"Result": result}),
            }
        ).insert(ignore_permissions=True)
        with patch.object(frappe.db, "commit"):
            m_pesa_api.process_mpesa_integration_request(ir.name)
        self.assertEqual(frappe.db.get_value("Integration Request", ir.name, "status"), "Failed")
        self.assert_not_booked_again(si, reg)

    def test_amended_register_is_the_live_owner(self):
        _si, reg = self.booked_then_cancelled()
        self.assertEqual(insert_register({"transid": reg.transid}), (CANCELLED, reg.name))

        amended = frappe.copy_doc(reg)  # what the form's Amend does: the receipt fields stay
        amended.name = f"{reg.name}-1"
        amended.amended_from = reg.name
        amended.docstatus = 0
        amended.payment_entry = None
        amended.db_insert()  # only its state matters here, not booking it again
        self.assertEqual(amended.transid, reg.transid)
        self.assertEqual(insert_register({"transid": reg.transid}), (DUPLICATE, amended.name))
        self.assertEqual(frappe.db.count(REGISTER, {"transid": reg.transid}), 2)

    def test_pull_does_not_import_a_deleted_register_again(self):
        si, reg = self.booked_then_cancelled()
        # a System Manager tidies up: the receipt's Match Log rows (they link to it), then the register
        for log in frappe.get_all(MATCH_LOG, filters={"register": reg.name}, pluck="name"):
            frappe.delete_doc(MATCH_LOG, log)
        frappe.delete_doc(REGISTER, reg.name)
        self.assertFalse(frappe.db.exists(REGISTER, reg.name))
        # the real Deleted Document JSON is what the tombstone check reads
        self.assertEqual(insert_register({"transid": reg.transid}), (DELETED, None))

        with patch.object(frappe.db, "commit"):
            pull_transaction_on_success(
                pull_response(reg.transid, 1000, si.name), t.SETTINGS, settings_name=t.SETTINGS
            )
        self.assertEqual(frappe.db.count(REGISTER, {"transid": reg.transid}), 0)
        self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": reg.transid, "docstatus": 1}))
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 1000)
