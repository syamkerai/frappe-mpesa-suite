import hashlib

import frappe
from frappe.utils import flt

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import bank_transaction, matching, reconcile
from mpsa_suite.money_in.common import REGISTER, bank_account_for, compact, parse_transtime
from mpsa_suite.money_in import testing as t


class TestMoneyIn(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test M-Pesa Customer")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ helpers
    def test_helpers(self):
        self.assertEqual(compact("sinv-26 00/123."), "SINV2600123")
        self.assertEqual(str(parse_transtime("20261018143005")), "2026-10-18 14:30:05")
        self.assertIsNone(parse_transtime("garbage"))

    # ------------------------------------------------------------ ingest
    def test_duplicate_confirmation_creates_one_register(self):
        # the real confirmation job path (commits and sleeps 1 s, so only used here)
        from mpsa_suite.mpsa_suite.api.m_pesa_api import delayed_insert_c2b

        transid = "TDUP" + frappe.generate_hash(length=6).upper()
        delayed_insert_c2b(t.confirmation_payload(100, transid=transid))
        delayed_insert_c2b(t.confirmation_payload(100, transid=transid))
        self.assertEqual(frappe.db.count(REGISTER, {"transid": transid, "docstatus": ["<", 2]}), 1)

    def test_statement_line_created_once_and_duplicates_blocked(self):
        name = t.receipt(250, billref="nobody")
        reg = frappe.get_doc(REGISTER, name)
        ba = bank_account_for(t.SETTINGS)
        line = bank_transaction.find_existing(ba, reg.transid)
        self.assertTrue(line, "after_insert should have created the statement line")
        self.assertEqual(bank_transaction.for_register(reg), line)

        dup = frappe.get_doc(
            {"doctype": "Bank Transaction", "date": frappe.utils.nowdate(), "bank_account": ba,
             "deposit": 250, "reference_number": reg.transid, "currency": "KES"}
        )
        self.assertRaises(frappe.ValidationError, dup.insert)

    # ------------------------------------------------------------ matching + booking
    def test_mistyped_invoice_number_is_repaired_and_booked(self):
        si = t.sales_invoice(self.customer, 1000)
        typo = compact(si.name).lower()  # dashes dropped, lower case
        name = t.receipt(1000, billref=typo)
        match = matching.match_register(name)
        self.assertEqual(match.method, "Invoice Number Repair")

        reg = frappe.get_doc(REGISTER, name)
        self.assertEqual(reg.docstatus, 1)
        pe = frappe.get_doc("Payment Entry", reg.payment_entry)
        self.assertEqual(pe.reference_no, reg.transid)  # receipt, not the register name
        self.assertEqual(pe.references[0].reference_name, si.name)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0)

    def test_overpayment_allocates_only_outstanding(self):
        si = t.sales_invoice(self.customer, 700)
        name = t.receipt(1000, billref=si.name)
        matching.match_register(name)
        pe = frappe.get_doc("Payment Entry", frappe.db.get_value(REGISTER, name, "payment_entry"))
        self.assertEqual(flt(pe.references[0].allocated_amount), 700)
        self.assertEqual(flt(pe.unallocated_amount), 300)

    def test_customer_name_without_spaces(self):
        cust = t.customer("Wanjiku Testpayer")
        name = t.receipt(500, billref="WANJIKUTESTPAYER")
        match = matching.match_register(name)
        self.assertEqual(match.method, "Customer Name")
        self.assertEqual(frappe.db.get_value(REGISTER, name, "customer"), cust)
        self.assertEqual(frappe.db.get_value(REGISTER, name, "docstatus"), 1)

    def test_ambiguous_customer_is_not_booked(self):
        t.customer("x", customer_name="Ambig Twin")
        t.customer("y", customer_name="Ambig Twin")
        name = t.receipt(300, billref="ambigtwin")
        self.assertIsNone(matching.match_register(name))
        self.assertEqual(frappe.db.get_value(REGISTER, name, "docstatus"), 0)
        self.assertTrue(frappe.db.exists("Mpesa Match Log", {"register": name, "result": "Ambiguous"}))

    def test_unmatched_is_logged_once(self):
        name = t.receipt(123, billref="zzz-unknown")
        matching.match_register(name)
        matching.match_register(name)
        self.assertEqual(frappe.db.count("Mpesa Match Log", {"register": name, "result": "Unmatched"}), 1)

    def test_payer_phone_matches_single_open_order(self):
        cust = t.customer("_Test Phone Payer")
        so = t.sales_order(cust, 777, submit=True)
        frappe.db.set_value("Sales Order", so.name, "contact_mobile", "0712000777")
        hashed = hashlib.sha256(b"254712000777").hexdigest()
        name = t.receipt(777, billref="deal 55", msisdn=hashed)
        match = matching.match_register(name)
        self.assertEqual((match.method, match.name), ("Payer Phone", so.name))
        self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "advance_paid")), 777)

    def test_duplicate_payment_entry_for_receipt_is_blocked(self):
        si = t.sales_invoice(self.customer, 400)
        name = t.receipt(400, billref=si.name)
        matching.match_register(name)
        pe = frappe.get_doc("Payment Entry", frappe.db.get_value(REGISTER, name, "payment_entry"))
        copy = frappe.copy_doc(pe)
        copy.references = []
        self.assertRaises(frappe.ValidationError, copy.insert)

    # ------------------------------------------------------------ prepaid orders
    def test_prepaid_order_released_when_fully_paid(self):
        if frappe.db.exists("Workflow", {"document_type": "Sales Order", "is_active": 1}):
            self.skipTest("site has an active Sales Order workflow")
        so = t.sales_order(self.customer, 1000)
        first = t.receipt(400, billref=so.name)
        matching.match_register(first)
        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0, "partial payment must wait")
        self.assertEqual(frappe.db.get_value(REGISTER, first, "docstatus"), 0)

        second = t.receipt(600, billref=so.name)
        matching.match_register(second)
        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 1)
        self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "advance_paid")), 1000)
        for reg in (first, second):
            self.assertEqual(frappe.db.get_value(REGISTER, reg, "docstatus"), 1)

    # ------------------------------------------------------------ reconciliation
    def test_statement_line_reconciled_with_payment_entry(self):
        si = t.sales_invoice(self.customer, 900)
        name = t.receipt(900, billref=si.name)
        matching.match_register(name)
        reg = frappe.get_doc(REGISTER, name)
        ba = bank_account_for(t.SETTINGS)
        bt_name = bank_transaction.find_existing(ba, reg.transid)
        bt = frappe.db.get_value(
            "Bank Transaction", bt_name,
            ["name", "reference_number", "transaction_id", "deposit", "unallocated_amount"], as_dict=True,
        )
        gl = frappe.db.get_value("Bank Account", ba, "account")
        self.assertEqual(reconcile.reconcile_line(bt, gl), reg.payment_entry)
        self.assertEqual(flt(frappe.db.get_value("Bank Transaction", bt_name, "unallocated_amount")), 0)
