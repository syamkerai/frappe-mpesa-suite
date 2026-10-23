"""Receipt register safety on a real database.

Reversed receipts are never booked (any path); receipt fields (and the currency) cannot be
changed over the API, while numeric values Daraja may send still auto-submit; a prepaid order
refused by the credit limit stays a draft, is logged even after an earlier error and retried
with backoff, without flooding the Error Log; the Bank Transaction Reference Number setter that
broke Customize Form is removed while its index stays.
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, flt, get_datetime, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import booking, matching, sales_order
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_in.common import REGISTER, REVERSAL, compact, log_match

MATCH_LOG = "Mpesa Match Log"


def _reversal(register: str, status: str = "Reversed", docstatus: int = 1) -> str:
    reg = frappe.db.get_value(REGISTER, register, ["transid", "transamount"], as_dict=True)
    doc = frappe.get_doc(
        {
            "doctype": REVERSAL,
            "c2b_register": register,
            "transaction_id": reg.transid,
            "amount": reg.transamount,
            "original_amount": reg.transamount,
            "mpesa_settings": t.SETTINGS,
            "remarks": "test",
            "status": status,
            "docstatus": docstatus,
        }
    )
    doc.name = "MRV-T-" + frappe.generate_hash(length=8)
    doc.db_insert()  # no validate, nothing sent: only the reversal's state matters here
    return doc.name


def _no_workflow(test):
    if frappe.db.exists("Workflow", {"document_type": "Sales Order", "is_active": 1}):
        test.skipTest("site has an active Sales Order workflow")


def _credit_limited_customer() -> str:
    cust = t.customer("_Test Credit Limited " + frappe.generate_hash(length=6))
    doc = frappe.get_doc("Customer", cust)
    doc.append("credit_limits", {"company": t.COMPANY, "credit_limit": 100})
    doc.save()
    # no credit controller role: ERPNext refuses the order for every approver
    frappe.db.set_single_value("Accounts Settings", "credit_controller", None)
    return cust


def _error_logs(so_name: str) -> int:
    # Error Logs outlive the test's rollback while order names are reused: compare counts
    return frappe.db.count("Error Log", {"reference_doctype": "Sales Order", "reference_name": so_name})


def _order_errors(so_name: str) -> list:
    return frappe.get_all(MATCH_LOG, filters={"reference_name": so_name, "result": "Error"},
                          fields=["name", "details", "creation", "modified"], order_by="creation asc")


class TestFixRegister(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test M-Pesa Customer")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ C16 reversed receipts
    def test_reversed_draft_cannot_be_submitted_by_hand(self):
        name = t.receipt(5000, billref="nobody-" + frappe.generate_hash(length=6))
        _reversal(name)
        reg = frappe.get_doc(REGISTER, name)
        reg.customer = self.customer
        reg.submit_payment = 1
        self.assertRaisesRegex(frappe.ValidationError, "cannot be booked", reg.submit)
        self.assertEqual(frappe.db.get_value(REGISTER, name, "docstatus"), 0)
        self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": reg.transid}))

    def test_reversed_draft_is_not_booked_by_the_matcher(self):
        si = t.sales_invoice(self.customer, 2000)
        name = t.receipt(2000, billref=compact(si.name).lower())  # only the matcher resolves the typo
        self.assertEqual(frappe.db.get_value(REGISTER, name, "docstatus"), 0)
        _reversal(name, status="Unknown")  # may have gone back: fail closed

        self.assertIsNone(booking.book_register(name, "Reference"))
        matching.match_register(name)
        reg = frappe.db.get_value(REGISTER, name, ["docstatus", "payment_entry", "matched_reference_name"], as_dict=True)
        self.assertEqual((reg.docstatus, reg.payment_entry, reg.matched_reference_name), (0, None, None))
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 2000)

        attempts = frappe.db.get_value(REGISTER, name, "match_attempts")
        matching.run()
        self.assertEqual(frappe.db.get_value(REGISTER, name, "match_attempts"), attempts, "never picked up again")

    def test_failed_reversal_leaves_the_receipt_bookable(self):
        si = t.sales_invoice(self.customer, 1500)
        name = t.receipt(1500, billref=compact(si.name).lower())
        self.assertEqual(frappe.db.get_value(REGISTER, name, "docstatus"), 0)
        _reversal(name, status="Failed")
        _reversal(name, status="Unknown", docstatus=2)  # cancelled
        matching.match_register(name)
        self.assertEqual(frappe.db.get_value(REGISTER, name, "docstatus"), 1)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0)

    def test_reversed_receipt_not_booked_when_its_order_is_submitted(self):
        so = t.sales_order(self.customer, 800, submit=True)
        name = t.receipt(800, billref="order " + frappe.generate_hash(length=6))
        frappe.db.set_value(REGISTER, name, {"customer": self.customer, "matched_reference_doctype": "Sales Order",
                                             "matched_reference_name": so.name})
        _reversal(name)
        matching.book_for_submitted_orders()
        sales_order.process_pending_orders()
        self.assertEqual(frappe.db.get_value(REGISTER, name, "docstatus"), 0)
        self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "advance_paid")), 0)

    def test_reversal_without_a_register_does_not_empty_the_queues(self):
        # "name not in (NULL, ...)" matches nothing on v16: the held list must skip empty links
        doc = frappe.get_doc({"doctype": REVERSAL, "transaction_id": "NOREG" + frappe.generate_hash(length=5).upper(),
                              "amount": 10, "original_amount": 10, "mpesa_settings": t.SETTINGS, "remarks": "test",
                              "status": "Reversed", "docstatus": 1})
        doc.name = "MRV-T-" + frappe.generate_hash(length=8)
        doc.db_insert()
        so = t.sales_order(self.customer, 700, submit=True)
        order_receipt = t.receipt(700, billref="order " + frappe.generate_hash(length=6))
        frappe.db.set_value(REGISTER, order_receipt, {"customer": self.customer, "matched_reference_doctype": "Sales Order",
                                                      "matched_reference_name": so.name})
        si = t.sales_invoice(self.customer, 900)
        typo_receipt = t.receipt(900, billref=compact(si.name).lower())

        sales_order.process_pending_orders()
        matching.run()

        self.assertEqual(frappe.db.get_value(REGISTER, order_receipt, "docstatus"), 1)
        self.assertEqual(frappe.db.get_value(REGISTER, typo_receipt, "docstatus"), 1)

    def test_refunded_deposit_does_not_release_a_prepaid_order(self):
        _no_workflow(self)
        so = t.sales_order(self.customer, 1000)
        deposit = t.receipt(500, billref=so.name)
        matching.match_register(deposit)  # partial: waits, matched to the order
        _reversal(deposit)
        again = t.receipt(500, billref=so.name)
        matching.match_register(again)
        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0, "only 500 is really paid")
        sales_order.process_pending_orders()
        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0)
        self.assertEqual(frappe.db.get_value(REGISTER, deposit, "docstatus"), 0)

    # ------------------------------------------------------------ C07 receipt fields
    def test_receipt_amount_and_number_cannot_be_changed(self):
        name = t.receipt(100, billref="nobody-" + frappe.generate_hash(length=6))
        transid = frappe.db.get_value(REGISTER, name, "transid")
        for field, value in (("transamount", 95000), ("transid", "FAKE" + transid), ("businessshortcode", "999999"),
                             ("currency", "USD")):  # booked as USD into a USD receivable: 100 becomes ~13,000 KES
            reg = frappe.get_doc(REGISTER, name)
            reg.set(field, value)
            self.assertRaises(frappe.CannotChangeConstantError, reg.save)

        # the REST path: a modified copy of the document submitted with frappe.client.submit
        from frappe.client import submit

        data = frappe.get_doc(REGISTER, name).as_dict()
        data.update(transamount=95000, customer=self.customer, submit_payment=1)
        self.assertRaises(frappe.CannotChangeConstantError, submit, data)
        self.assertEqual(frappe.db.get_value(REGISTER, name, ["docstatus", "transamount"]), (0, 100))
        self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": transid}))

    def test_numeric_receipt_values_still_auto_submit(self):
        # Daraja documents strings, but a number (OrgAccountBalance 500.0) used to be stored as
        # '500' and the auto-submit then refused the receipt as changed
        cust = t.customer("_Test Numeric Receipt " + frappe.generate_hash(length=6))  # no invoices to reconcile
        before = frappe.db.get_value("Mpesa Settings", t.SETTINGS, "auto_reconcile_c2b")
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, "auto_reconcile_c2b", 1)
        try:
            doc = frappe.get_doc({"doctype": REGISTER, **t.confirmation_payload(400, billref=cust),
                                  "orgaccountbalance": 500.0})
            doc.insert(ignore_permissions=True)
        finally:
            frappe.db.set_value("Mpesa Settings", t.SETTINGS, "auto_reconcile_c2b", before)
        errors = frappe.get_all("Error Log", filters={"method": ["like", "C2B Auto-submit Error%"]},
                                fields=["method"], order_by="creation desc", limit=1)
        self.assertEqual(frappe.db.get_value(REGISTER, doc.name, "docstatus"), 1, errors)
        self.assertEqual(frappe.db.get_value(REGISTER, doc.name, "orgaccountbalance"), "500.0")

    def test_staff_can_still_assign_the_customer_and_book(self):
        name = t.receipt(300, billref="nobody-" + frappe.generate_hash(length=6))
        reg = frappe.get_doc(REGISTER, name)
        reg.customer = self.customer
        reg.save()
        reg.submit_payment = 1
        reg.submit()
        pe = frappe.db.get_value("Payment Entry", reg.payment_entry, ["paid_amount", "reference_no"], as_dict=True)
        self.assertEqual((flt(pe.paid_amount), pe.reference_no), (300, reg.transid))

    # ------------------------------------------------------------ C09 credit limit
    def test_order_refused_by_credit_limit_stays_draft_and_logs_once(self):
        _no_workflow(self)
        cust = _credit_limited_customer()
        so = t.sales_order(cust, 1000)
        error_logs = _error_logs(so.name)
        name = t.receipt(1000, billref=so.name)
        matching.match_register(name)  # must not raise

        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0)
        reg = frappe.db.get_value(REGISTER, name, ["docstatus", "matched_reference_name"], as_dict=True)
        self.assertEqual((reg.docstatus, reg.matched_reference_name), (0, so.name), "match kept for the retry")
        [log] = _order_errors(so.name)
        self.assertIn("credit limit", log.details.lower())
        self.assertIn("Credit Controller", log.details)

        # within the backoff the scheduler does not run the release again
        sales_order.process_pending_orders()
        sales_order.process_pending_orders()
        self.assertEqual(_order_errors(so.name), [log])
        self.assertEqual(_error_logs(so.name), error_logs)

        # past it, the order is tried again: same refusal, so the row is only marked as tried
        aged = add_to_date(now_datetime(), minutes=-10)
        frappe.db.set_value(MATCH_LOG, log.name, {"creation": aged, "modified": aged}, update_modified=False)
        sales_order.process_pending_orders()
        [again] = _order_errors(so.name)
        self.assertEqual(again.name, log.name)
        self.assertGreater(get_datetime(again.modified), aged)
        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0)

        # limit raised on the customer: the next run releases the order at once and books the receipt
        doc = frappe.get_doc("Customer", cust)
        doc.credit_limits[0].credit_limit = 1000000
        doc.save()
        sales_order.process_pending_orders()
        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 1)
        self.assertEqual(frappe.db.get_value(REGISTER, name, "docstatus"), 1)

    def test_refusal_is_logged_after_an_earlier_order_error(self):
        _no_workflow(self)
        so = t.sales_order(_credit_limited_customer(), 1000)
        # an earlier problem with this order, fixed since; the refusal must not be swallowed by it
        log_match("Error", "Sales Order", reference_doctype="Sales Order", reference_name=so.name,
                  details="Order is fully paid but no 'Approve Paid Orders As' user is set in M-Pesa Suite Settings.")
        matching.match_register(t.receipt(1000, billref=so.name))
        logs = _order_errors(so.name)
        self.assertEqual(len(logs), 2)
        self.assertIn("credit limit", logs[-1].details.lower())
        self.assertIn("Credit Controller", logs[-1].details)

    def test_failing_order_writes_one_error_log_not_one_per_run(self):
        so = t.sales_order(self.customer, 650, submit=True)
        name = t.receipt(650, billref="order " + frappe.generate_hash(length=6))
        frappe.db.set_value(REGISTER, name, {"customer": self.customer, "matched_reference_doctype": "Sales Order",
                                             "matched_reference_name": so.name})
        error_logs = _error_logs(so.name)
        run = frappe.generate_hash(length=6)
        with patch("mpsa_suite.money_in.booking.book_register",
                   side_effect=frappe.ValidationError(f"boom {run}")):
            sales_order.process_pending_orders()
            sales_order.process_pending_orders()
        self.assertEqual(_error_logs(so.name), error_logs + 1)
        self.assertEqual(len(_order_errors(so.name)), 1)
        # a different failure for the same order is still recorded
        with patch("mpsa_suite.money_in.booking.book_register",
                   side_effect=frappe.ValidationError(f"bang {run}")):
            sales_order.process_pending_orders()
        self.assertEqual(_error_logs(so.name), error_logs + 2)
        self.assertEqual(len(_order_errors(so.name)), 2)

    # ------------------------------------------------------------ C08 Bank Transaction setter
    def test_text_field_search_index_setter_removed_index_kept(self):
        from frappe.core.doctype.doctype.doctype import validate_fields_for_doctype
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter

        from mpsa_suite.mpsa_suite.patches import drop_text_search_index_setters

        setter = {"doc_type": "Bank Transaction", "field_name": "reference_number", "property": "search_index"}
        if not frappe.db.exists("Property Setter", setter):
            make_property_setter("Bank Transaction", "reference_number", "search_index", 1, "Check",
                                 validate_fields_for_doctype=False)
        frappe.clear_cache(doctype="Bank Transaction")
        self.assertRaises(frappe.ValidationError, validate_fields_for_doctype, "Bank Transaction")
        data_setter = dict(setter, field_name="transaction_id")
        had_data_setter = bool(frappe.db.exists("Property Setter", data_setter))

        drop_text_search_index_setters.execute()

        self.assertFalse(frappe.db.exists("Property Setter", setter))
        validate_fields_for_doctype("Bank Transaction")  # Custom Field / Customize Form saves work again
        indexed = frappe.db.sql(
            "SHOW INDEX FROM `tabBank Transaction` WHERE Column_name = 'reference_number'", as_dict=True
        )
        self.assertTrue(indexed, "the duplicate check's locking read stays index-backed")
        # an indexable (Data) field keeps its setter
        self.assertEqual(bool(frappe.db.exists("Property Setter", data_setter)), had_data_setter)

    def test_migrate_leaves_no_search_index_setter_on_text_fields(self):
        from frappe.core.doctype.doctype.doctype import validate_fields_for_doctype
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter

        from mpsa_suite import install
        from mpsa_suite.mpsa_suite.patches import drop_text_search_index_setters

        for doctype, fieldname in install.LOOKUP_INDEXES:
            # add_index commits before building a missing index; that would commit test data
            if frappe.db.exists("DocType", doctype) and not frappe.db.has_index(f"tab{doctype}", fieldname):
                self.skipTest(f"{doctype}.{fieldname} has no index on this site")

        setter = {"doc_type": "Bank Transaction", "field_name": "reference_number", "property": "search_index"}
        if not frappe.db.exists("Property Setter", setter):  # as an earlier release left it
            make_property_setter("Bank Transaction", "reference_number", "search_index", 1, "Check",
                                 validate_fields_for_doctype=False)

        drop_text_search_index_setters.execute()  # post_model_sync patch
        install.ensure_lookup_indexes()  # then after_migrate, as bench migrate runs them
        frappe.clear_cache(doctype="Bank Transaction")

        self.assertFalse(frappe.db.exists("Property Setter", setter))
        validate_fields_for_doctype("Bank Transaction")
        self.assertTrue(frappe.db.has_index("tabBank Transaction", "reference_number"), "index kept")
        self.assertTrue(frappe.db.exists("Property Setter", dict(setter, field_name="transaction_id")))
