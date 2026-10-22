"""Round 2 "b2c2" fixes to B2C payout booking and retry, against the real database (v15, v16).

Rows and entries are written with db_insert, so no company, accounts or Daraja credentials are
needed. Nothing is sent (the send is patched) and nothing is committed (commits are patched;
every test rolls back), except in the tests where a second connection commits a concurrent
cancel: their rows are committed first and deleted again afterwards.
"""

from unittest.mock import Mock, patch

import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate

from mpsa_suite.accounting import journal_service
from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request import (
    b2c_disbursement_request as request_module,
)
from mpsa_suite.mpsa_suite.patches import relink_b2c_reference_rows
from mpsa_suite.services import b2c_postprocess_service as postprocess
from mpsa_suite.services import b2c_response_service as response

PARENT = "B2C Payment Disbursement"
REF = "B2C Payment Disbursement Reference"
REQ = "B2C Disbursement Request"
LINKED = object()  # _row: the row links its request
COMMITTED = (PARENT, REF, REQ, "Payment Entry", "Payment Entry Reference", "Purchase Invoice")


class TestFix2B2C2(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.tag = frappe.generate_hash(length=8)

    def tearDown(self):
        frappe.db.rollback()

    # ------------------------------------------------------------ helpers
    def _receipt(self, i):
        return f"SK{i}{self.tag}".upper()

    def _disbursement(self, suffix="1", docstatus=1):
        name = f"_T-B2CPD-{self.tag}-{suffix}"
        frappe.get_doc(
            {"doctype": PARENT, "name": name, "docstatus": docstatus, "status": "Initiated",
             "payment_type": "Mpesa Disbursement", "party_type": "Supplier",
             "transaction_to_pay_against": "Purchase Invoice", "posting_date": nowdate()}
        ).db_insert()
        return name

    def _settings(self):
        name = f"_T-MS-{self.tag}"
        frappe.get_doc(
            {"doctype": "Mpesa Settings", "name": name, "payment_gateway_name": name,
             "submit_b2c_accounting_entries": 1}
        ).db_insert()
        return name

    def _row(self, parent, i, reference, amount, status="Paid", receipt=None, doctype="Purchase Invoice",
             party="_T Supplier", link=LINKED, row_status=None, request_docstatus=1, creation=None,
             settings=None, request=None):
        """A disbursement row and its B2C request, as the payout jobs and the Result leave them."""
        row, request = f"{parent}-R{i}", request or f"_T-B2C-{self.tag}-{parent[-1]}{i}"
        frappe.get_doc(
            {"doctype": REF, "name": row, "parent": parent, "parenttype": PARENT, "parentfield": "references",
             "idx": i, "docstatus": 1, "reference_doctype": doctype, "reference_name": reference,
             "party_type": "Supplier", "party": party, "partyb": "254712345678", "allocated_amount": amount,
             "payment_status": row_status or status, "reference_no": receipt if status == "Paid" else None,
             "b2c_disbursement_request": request if link is LINKED else link}
        ).db_insert()
        self._request(request, row, parent, reference, amount, status, receipt, doctype, party,
                      request_docstatus, creation, settings)
        return row, request

    def _copy_row(self, parent, i, reference, amount, link, receipt=None):
        """A row an amend copied from a paid row: it keeps the link, and is not sent again."""
        row = f"{parent}-R{i}"
        frappe.get_doc(
            {"doctype": REF, "name": row, "parent": parent, "parenttype": PARENT, "parentfield": "references",
             "idx": i, "docstatus": 1, "reference_doctype": "Purchase Invoice", "reference_name": reference,
             "party_type": "Supplier", "party": "_T Supplier", "partyb": "254712345678",
             "allocated_amount": amount, "payment_status": "Paid", "reference_no": receipt,
             "b2c_disbursement_request": link}
        ).db_insert()
        return row

    def _request(self, name, row, parent, reference, amount, status="Paid", receipt=None,
                 doctype="Purchase Invoice", party="_T Supplier", docstatus=1, creation=None, settings=None):
        frappe.get_doc(
            {"doctype": REQ, "name": name, "docstatus": docstatus, "status": status,
             "amount": amount, "reference_doctype": doctype, "reference_name": reference,
             "party_type": "Supplier", "party": party, "phone_number": "254712345678",
             "b2c_payment_disbursement": parent, "b2c_payment_disbursement_reference": row,
             "originator_conversation_id": f"OCID-{name}", "transaction_id": receipt,
             "transaction_completed_datetime": now_datetime() if receipt else None,
             "result_code": "1" if status == "Failed" else ("0" if status == "Paid" else None),
             "mpesa_settings": settings, "creation": creation}
        ).db_insert()
        return name

    def _payment_entry(self, receipt, reference, docstatus=1, disbursement=None, amount=1):
        pe = f"_T-PE-{self.tag}-{frappe.generate_hash(length=6)}"
        frappe.get_doc(
            {"doctype": "Payment Entry", "name": pe, "docstatus": docstatus, "payment_type": "Pay",
             "posting_date": nowdate(), "reference_no": receipt, "reference_date": nowdate()}
        ).db_insert()
        frappe.get_doc(
            {"doctype": "Payment Entry Reference", "name": f"{pe}-1", "parent": pe, "parenttype": "Payment Entry",
             "parentfield": "references", "idx": 1, "docstatus": docstatus, "reference_doctype": "Purchase Invoice",
             "reference_name": reference, "allocated_amount": amount, "b2c_payment_disbursement": disbursement}
        ).db_insert()
        return pe

    def _invoice(self, outstanding):
        name = f"_T-PINV-{self.tag}-{frappe.generate_hash(length=6)}"
        frappe.get_doc(
            {"doctype": "Purchase Invoice", "name": name, "docstatus": 1, "outstanding_amount": outstanding,
             "grand_total": outstanding, "posting_date": nowdate()}
        ).db_insert()
        return name

    def _commit_for_second_connection(self):
        """Commit this test's rows so a second connection sees them; deleted again afterwards."""
        frappe.db.commit()
        self.addCleanup(self._delete_committed)

    def _commit_elsewhere(self, *statements):
        """Run and commit ``(query, values)`` statements on a second connection, as a concurrent
        request does (a raw connection: frappe.db stays on this test's transaction)."""
        conn = frappe.db.create_connection()
        try:
            cursor = conn.cursor()
            for query, values in statements:
                cursor.execute(query, values)
            conn.commit()
        finally:
            conn.close()

    def _delete_committed(self):
        frappe.db.rollback()
        for doctype in COMMITTED:
            frappe.db.sql(f"DELETE FROM `tab{doctype}` WHERE name LIKE %s", (f"%{self.tag}%",))
        frappe.db.commit()

    def _booked(self, disbursement, row):
        return postprocess.booked_docstatus(disbursement, frappe.get_doc(REF, row))

    def _retry(self, request):
        sent = Mock()
        with patch.object(request_module, "make_b2c_payment_request", sent):
            frappe.get_doc(REQ, request).retry_failed_payment()
        return sent

    def _assert_refused(self, request, message):
        sent = Mock()
        with (
            patch.object(request_module, "make_b2c_payment_request", sent),
            self.assertRaisesRegex(frappe.ValidationError, message),
        ):
            frappe.get_doc(REQ, request).retry_failed_payment()
        sent.assert_not_called()
        status, ocid = frappe.db.get_value(REQ, request, ["status", "originator_conversation_id"])
        self.assertEqual((status, ocid), ("Failed", f"OCID-{request}"))

    # ------------------------------------------------------------ C10: the Result heals the link
    def test_result_restores_a_wiped_row_link_and_books_the_payout(self):
        d1, pinv, receipt = self._disbursement(), f"_T-PINV-{self.tag}", self._receipt(1)
        # the old full save of the disbursement wiped the link the payout job had set
        row, request = self._row(d1, 1, pinv, 100, receipt=receipt, link=None, row_status="Initiated",
                                 settings=self._settings())
        made = []

        def create_payment_entry_for_doc(disbursement, ref, submit=False):
            made.append(self._payment_entry(ref.reference_no, ref.reference_name, docstatus=1 if submit else 0,
                                            disbursement=disbursement.name, amount=ref.allocated_amount))

        with (
            patch.object(postprocess, "create_payment_entry_for_doc", create_payment_entry_for_doc),
            patch.object(frappe.db, "commit"),
        ):
            response.update_b2c_reference_status(request, enqueue_next=True)  # also what the sweep runs

        self.assertEqual(
            frappe.db.get_value(REF, row, ["b2c_disbursement_request", "payment_status", "reference_no"]),
            (request, "Paid", receipt),
        )
        self.assertEqual(len(made), 1)
        self.assertEqual(frappe.db.get_value(REQ, request, ["accounting_booked", "accounting_error"]), (1, None))

    def test_failed_result_restores_a_wiped_row_link(self):
        row, request = self._row(self._disbursement(), 1, f"_T-PINV-{self.tag}", 100, status="Failed", link=None,
                                 row_status="Initiated")
        with patch.object(frappe.db, "commit"):
            response.update_b2c_reference_status(request)
        self.assertEqual(frappe.db.get_value(REF, row, ["b2c_disbursement_request", "payment_status"]),
                         (request, "Failed"))

    def test_cancelled_requests_late_result_does_not_take_the_row_over(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        amended = f"_T-B2C-{self.tag}-AMENDED"
        row, old = self._row(d1, 1, pinv, 100, status="Failed", link=amended, request_docstatus=2)
        self._request(amended, row, d1, pinv, 100, status="Initiated")
        with patch.object(frappe.db, "commit"):
            response.update_b2c_reference_status(old)
        self.assertEqual(frappe.db.get_value(REF, row, "b2c_disbursement_request"), amended)

    def test_cancelled_requests_late_result_fills_an_empty_link(self):
        # Cancel All on D2 took the request with it; an old full save had wiped the row's link
        d2, pinv = self._disbursement("2", docstatus=2), f"_T-PINV-{self.tag}"
        row, request = self._row(d2, 1, pinv, 100, receipt=self._receipt(1), link=None, row_status="Initiated",
                                 request_docstatus=2)
        with patch.object(frappe.db, "commit"):
            response.update_b2c_reference_status(request)
        self.assertEqual(frappe.db.get_value(REF, row, ["b2c_disbursement_request", "payment_status"]),
                         (request, "Paid"))

    def test_cancelled_requests_late_result_leaves_a_row_a_submitted_request_names(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        row, old = self._row(d1, 1, pinv, 100, status="Failed", link=None, row_status="Initiated",
                             request_docstatus=2)
        self._request(f"_T-B2C-{self.tag}-AMENDED", row, d1, pinv, 100, status="Initiated")  # still out
        with patch.object(frappe.db, "commit"):
            response.update_b2c_reference_status(old)
        self.assertIsNone(frappe.db.get_value(REF, row, "b2c_disbursement_request"))

    # ------------------------------------------------------------ C10: the repair patch
    def test_patch_restores_wiped_links_from_the_submitted_requests(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        wiped, request = self._row(d1, 1, pinv, 100, status="Initiated", link=None)
        # two submitted requests name one row: the latest wins
        twice, _ = self._row(d1, 2, pinv, 100, status="Failed", link=None,
                                 creation=add_days(now_datetime(), -2))
        newer = self._request(f"_T-B2C-{self.tag}-NEWER", twice, d1, pinv, 100, status="Initiated")
        # a row whose requests were all cancelled (Cancel All) takes the latest of them
        only_cancelled, cancelled = self._row(d1, 3, pinv, 100, status="Paid", receipt=self._receipt(3), link=None,
                                              request_docstatus=2)
        linked, _ = self._row(d1, 4, pinv, 100, status="Initiated", link=f"_T-B2C-{self.tag}-OTHER")
        # a submitted request wins over a newer cancelled one
        both, submitted = self._row(d1, 5, pinv, 100, status="Initiated", link=None,
                                    creation=add_days(now_datetime(), -2))
        self._request(f"_T-B2C-{self.tag}-NEWER-CANCELLED", both, d1, pinv, 100, status="Failed", docstatus=2)
        modified = frappe.db.get_value(REF, wiped, "modified")

        for _ in range(2):  # a second run changes nothing
            relink_b2c_reference_rows.execute()
            links = {r: frappe.db.get_value(REF, r, "b2c_disbursement_request")
                     for r in (wiped, twice, only_cancelled, linked, both)}
            self.assertEqual(links, {wiped: request, twice: newer, only_cancelled: cancelled,
                                     linked: f"_T-B2C-{self.tag}-OTHER", both: submitted})
        self.assertEqual(frappe.db.get_value(REF, wiped, "modified"), modified)

    # ------------------------------------------------------------ C11: Salary Slip Journal Entry
    def test_salary_journal_entry_passes_erpnexts_reference_check(self):
        captured = []
        entry_class = type(frappe.new_doc("Journal Entry"))
        real_get_doc = frappe.get_doc

        def get_doc(*args, **kwargs):
            if args and args[0] == "Salary Slip":
                return frappe._dict(name=args[1])
            return real_get_doc(*args, **kwargs)

        disbursement = frappe._dict(name="_T-D", company="_T Company", paid_from="_T Mpesa", paid_to="_T Payable")
        ref = frappe._dict(reference_name="_T-SS", allocated_amount=45000, party_type="Employee", party="_T-EMP",
                           payroll_entry=None, reference_no=self._receipt(1),
                           reference_date=add_days(now_datetime(), -1))
        with (
            patch.object(entry_class, "insert", lambda self, *a, **kw: captured.append(self)),
            patch("frappe.get_doc", side_effect=get_doc),
        ):
            journal_service.create_journal_entry(disbursement, ref)

        entry = captured[0]
        self.assertEqual((entry.voucher_type, entry.cheque_no), ("Bank Entry", self._receipt(1)))
        self.assertEqual(getdate(entry.cheque_date), getdate(ref.reference_date))
        entry.validate_cheque_info()  # what JournalEntry.on_submit runs: passes

        entry.cheque_no = entry.cheque_date = None  # as journal_service made it before
        with self.assertRaises(frappe.ValidationError):
            entry.validate_cheque_info()

    # ------------------------------------------------------------ C11: spare Payment Entries
    def test_payment_entry_carrying_a_siblings_receipt_is_not_this_payouts(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        r1, _ = self._row(d1, 1, pinv, 3000, receipt=self._receipt(1))
        r2, _ = self._row(d1, 2, pinv, 3000, receipt=self._receipt(2))
        for _ in range(2):  # row 1's entry and a duplicate of it, both with row 1's receipt
            self._payment_entry(self._receipt(1), pinv, disbursement=d1, amount=3000)

        self.assertEqual(self._booked(d1, r1), 1)
        self.assertIsNone(self._booked(d1, r2))  # booked by its own entry only

        self._payment_entry("BANK-REF-7", pinv, disbursement=d1, amount=3000)  # staff typed their own reference
        self.assertEqual(self._booked(d1, r2), 1)

    # ------------------------------------------------------------ C13: retry guard
    def test_retry_refused_when_the_other_payouts_entry_was_cancelled(self):
        # D2 paid the invoice and its Payment Entry booked it; an accountant cancelled the entry,
        # so the invoice owes the amount again while the payout stays marked booked
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2")
        _, other = self._row(d2, 1, pinv, 50000, receipt=self._receipt(9))
        frappe.db.set_value(REQ, other, "accounting_booked", 1)
        self._payment_entry(self._receipt(9), pinv, docstatus=2, disbursement=d2, amount=50000)

        self._assert_refused(request, f"awaiting accounting: {d2}")

    def test_retry_refused_by_a_payout_cancelled_with_its_disbursement(self):
        # Cancel All on D2 after its payout was Paid, before anything booked it
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2", docstatus=2)
        self._row(d2, 1, pinv, 50000, receipt=self._receipt(9), request_docstatus=2)

        self._assert_refused(request, f"awaiting accounting: {d2}")

    def test_retry_sends_when_the_other_payout_is_still_booked(self):
        # PI owes 100,000; D2 paid 50,000 and its entry is live: the other 50,000 may be retried
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2")
        _, by_receipt = self._row(d2, 1, pinv, 25000, receipt=self._receipt(8))
        _, by_bank_ref = self._row(d2, 2, pinv, 25000, receipt=self._receipt(9))
        frappe.db.set_value(REQ, by_receipt, "accounting_booked", 1)
        frappe.db.set_value(REQ, by_bank_ref, "accounting_booked", 1)
        self._payment_entry(self._receipt(8), pinv, disbursement=d2, amount=25000)
        self._payment_entry("BANK-REF-7", pinv, disbursement=d2, amount=25000)  # staff's own reference

        self._retry(request).assert_called_once_with(request)

    def test_retry_refused_by_a_cancelled_payout_whose_row_lost_its_link(self):
        # Cancel All on D2 after its payout was Paid, before anything booked it; an old full save
        # had wiped the row's link, so only the request still names the invoice
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2", docstatus=2)
        self._row(d2, 1, pinv, 50000, receipt=self._receipt(9), link=None, request_docstatus=2)

        self._assert_refused(request, f"awaiting accounting: {d2}$")

    def test_retry_counts_a_row_without_its_link_that_open_payouts_lists_once(self):
        # PI owes 100,000 over two rows of D1: row 2 is Paid but lost its link (open_payouts
        # still lists it, by the row's own status), row 1 failed: its 50,000 may be retried
        pinv = self._invoice(100000)
        d1 = self._disbursement("1")
        _, request = self._row(d1, 1, pinv, 50000, status="Failed")
        self._row(d1, 2, pinv, 50000, receipt=self._receipt(9), link=None, row_status="Initiated")

        self._retry(request).assert_called_once_with(request)

    def _cancel_all_and_amend(self, booked, owed=100000):
        """D1 paid PI (100,000) in two rows: R1 Paid (booked by PE-1 when ``booked``), R2 Failed.
        Cancel All on D1 cancelled R1, R2 and PE-1, so PI owes 100,000 again. The amend D3 kept
        R1's paid row (its link copied, not sent again) and re-sent the other half, which failed."""
        pinv = self._invoice(owed)
        d1 = self._disbursement("1", docstatus=2)
        _, paid = self._row(d1, 1, pinv, 50000, receipt=self._receipt(1), request_docstatus=2)
        self._row(d1, 2, pinv, 50000, status="Failed", request_docstatus=2)
        if booked:
            frappe.db.set_value(REQ, paid, "accounting_booked", 1)
            self._payment_entry(self._receipt(1), pinv, docstatus=2, disbursement=d1, amount=50000)
        d3 = self._disbursement("3")
        self._copy_row(d3, 1, pinv, 50000, link=paid, receipt=self._receipt(1))
        _, retried = self._row(d3, 2, pinv, 50000, status="Failed")
        return retried

    def test_retry_after_cancel_all_and_amend_counts_the_kept_paid_row_once(self):
        retried = self._cancel_all_and_amend(booked=True)
        self._retry(retried).assert_called_once_with(retried)

    def test_retry_after_cancel_all_and_amend_of_an_unbooked_payout_counts_it_once(self):
        # open_payouts lists the kept row (its payout is not booked yet), so it counts there only
        retried = self._cancel_all_and_amend(booked=False)
        self._retry(retried).assert_called_once_with(retried)

    def test_retry_after_cancel_all_and_amend_still_counts_the_kept_paid_row(self):
        retried = self._cancel_all_and_amend(booked=True, owed=50000)  # 50,000 paid by bank since
        self._assert_refused(retried, "owes 50000.0; retrying would pay 50000.0, and 50000.0 is still being paid")

    # ------------------------------------------------------------ C13: cancels committed during the retry
    def test_retry_refused_when_a_payment_entry_cancel_commits_after_the_snapshot(self):
        # D2's payout paid the invoice and PE-1 booked it, so the invoice owes nothing
        pinv = self._invoice(0)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2")
        _, other = self._row(d2, 1, pinv, 50000, receipt=self._receipt(9))
        frappe.db.set_value(REQ, other, "accounting_booked", 1)
        pe = self._payment_entry(self._receipt(9), pinv, disbursement=d2, amount=50000)
        self._commit_for_second_connection()

        frappe.get_doc(REQ, request)  # the form's read: this transaction's snapshot
        # an accountant cancels PE-1 meanwhile, so the invoice owes 50,000 again
        self._commit_elsewhere(
            ("UPDATE `tabPayment Entry` SET docstatus = 2 WHERE name = %s", (pe,)),
            ("UPDATE `tabPayment Entry Reference` SET docstatus = 2 WHERE parent = %s", (pe,)),
            ("UPDATE `tabPurchase Invoice` SET outstanding_amount = 50000 WHERE name = %s", (pinv,)),
        )
        self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 1)  # the snapshot still

        self._assert_refused(request, f"awaiting accounting: {d2}$")

    def test_retry_refused_when_a_cancel_all_commits_after_the_snapshot(self):
        # D2's payout is Paid and not booked yet, so the invoice still shows it owed
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2")
        _, other = self._row(d2, 1, pinv, 50000, receipt=self._receipt(9))
        self._commit_for_second_connection()

        frappe.get_doc(REQ, request)  # the form's read: this transaction's snapshot
        self._commit_elsewhere(  # Cancel All on D2 meanwhile, which cancels its request too
            ("UPDATE `tabB2C Disbursement Request` SET docstatus = 2 WHERE name = %s", (other,)),
            ("UPDATE `tabB2C Payment Disbursement Reference` SET docstatus = 2 WHERE parent = %s", (d2,)),
            ("UPDATE `tabB2C Payment Disbursement` SET docstatus = 2 WHERE name = %s", (d2,)),
        )
        self.assertEqual(frappe.db.get_value(PARENT, d2, "docstatus"), 1)  # the snapshot still

        self._assert_refused(request, f"awaiting accounting: {d2}$")

    def test_retry_refused_by_what_the_invoice_owes_after_a_booking_committed_since(self):
        # D2's draft Payment Entry is submitted after this transaction's snapshot: the entry
        # carries the payout's receipt, so the payout is booked and the invoice owes nothing
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2")
        _, other = self._row(d2, 1, pinv, 50000, receipt=self._receipt(9))
        pe = self._payment_entry(self._receipt(9), pinv, docstatus=0, disbursement=d2, amount=50000)
        self._commit_for_second_connection()

        frappe.get_doc(REQ, request)  # the form's read: this transaction's snapshot
        self._commit_elsewhere(
            ("UPDATE `tabPayment Entry` SET docstatus = 1 WHERE name = %s", (pe,)),
            ("UPDATE `tabPayment Entry Reference` SET docstatus = 1 WHERE parent = %s", (pe,)),
            ("UPDATE `tabPurchase Invoice` SET outstanding_amount = 0 WHERE name = %s", (pinv,)),
            (f"UPDATE `tab{REQ}` SET accounting_booked = 1 WHERE name = %s", (other,)),
        )
        self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 0)  # the snapshot still

        self._assert_refused(request, "owes 0.0; retrying would pay 50000.0")

    def test_retry_without_erpnext_queries_no_payment_entry_table(self):
        todo = f"_T-TODO-{self.tag}"
        _, request = self._row(self._disbursement("1"), 1, todo, 50000, status="Failed", doctype="ToDo")
        sql = Mock(wraps=frappe.db.sql)
        with (
            patch.object(request_module, "_erpnext_installed", return_value=False),  # a Frappe-only site
            patch.object(frappe.db, "sql", sql),
        ):
            self._retry(request).assert_called_once_with(request)

            # nothing can book a payout there, so another disbursement's old Paid payout counts
            paid_long_ago = f"_T-TODO-{self.tag}-2"
            _, refused = self._row(self._disbursement("2"), 1, paid_long_ago, 50000, status="Failed",
                                   doctype="ToDo")
            d3 = self._disbursement("3")
            long_ago = add_days(now_datetime(), -(postprocess.RETRY_WINDOW_DAYS + 10))
            self._row(d3, 1, paid_long_ago, 50000, receipt=self._receipt(9), doctype="ToDo", creation=long_ago)
            self._assert_refused(refused, f"awaiting accounting: {d3}$")

        queries = [str(c.args[0]) for c in sql.call_args_list if c.args]
        self.assertTrue(any("paid" in q.lower() and "B2C Disbursement Request" in q for q in queries))
        self.assertFalse([q for q in queries if "tabPayment Entry" in q])

    # ------------------------------------------------------------ C12: reference index
    def test_request_reference_name_is_indexed(self):
        self.assertEqual(frappe.get_meta(REQ).get_field("reference_name").search_index, 1)
        indexed = frappe.db.sql(f"SHOW INDEX FROM `tab{REQ}` WHERE Column_name = 'reference_name'", as_dict=True)
        self.assertTrue(indexed)
