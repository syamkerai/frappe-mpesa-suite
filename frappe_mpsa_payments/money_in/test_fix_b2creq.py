"""B2C payout requests against the real database (v15 and v16).

Rows and entries are written with db_insert, so no company, accounts or Mpesa Settings are
needed. Nothing is ever sent: the send and the queue are patched. The concurrency tests
commit their rows (a second connection must see them) and delete them again afterwards.
"""

from unittest.mock import Mock, patch

import frappe
from frappe.utils import add_days, now_datetime, nowdate

from frappe_mpsa_payments.compat import IntegrationTestCase
from frappe_mpsa_payments.connectors.mpesa import b2c_connector
from frappe_mpsa_payments.frappe_mpsa_payments.doctype.b2c_disbursement_request import (
    b2c_disbursement_request as request_module,
)
from frappe_mpsa_payments.services import b2c_postprocess_service as postprocess

PARENT = "B2C Payment Disbursement"
REF = "B2C Payment Disbursement Reference"
REQ = "B2C Disbursement Request"
COMMITTED = (REQ, REF, PARENT, "Purchase Invoice", "Payment Entry", "Payment Entry Reference")


class TestFixB2CRequests(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.tag = frappe.generate_hash(length=8)

    def tearDown(self):
        frappe.db.rollback()

    # ------------------------------------------------------------ helpers
    def _disbursement(self, suffix="1"):
        name = f"_T-B2CPD-{self.tag}-{suffix}"
        frappe.get_doc(
            {"doctype": PARENT, "name": name, "docstatus": 1, "status": "Initiated",
             "payment_type": "Mpesa Disbursement", "party_type": "Supplier",
             "transaction_to_pay_against": "Purchase Invoice", "posting_date": nowdate()}
        ).db_insert()
        return name

    def _row(self, parent, i, reference, amount, status="Paid", receipt=None, doctype="Purchase Invoice",
             party="_T Supplier", request_status=None, creation=None):
        """A disbursement row and its B2C request, as the payout jobs and the Result leave them."""
        row, request = f"{parent}-R{i}", f"_T-B2C-{self.tag}-{parent[-1]}{i}"
        frappe.get_doc(
            {"doctype": REF, "name": row, "parent": parent, "parenttype": PARENT, "parentfield": "references",
             "idx": i, "docstatus": 1, "reference_doctype": doctype, "reference_name": reference,
             "party_type": "Supplier", "party": party, "partyb": "254712345678", "allocated_amount": amount,
             "payment_status": status, "b2c_disbursement_request": request, "reference_no": receipt}
        ).db_insert()
        frappe.get_doc(
            {"doctype": REQ, "name": request, "docstatus": 1, "status": request_status or status,
             "amount": amount, "reference_doctype": doctype, "reference_name": reference,
             "party_type": "Supplier", "party": party, "phone_number": "254712345678",
             "b2c_payment_disbursement": parent, "b2c_payment_disbursement_reference": row,
             "originator_conversation_id": f"OCID-{request}", "transaction_id": receipt,
             "result_code": "1" if status == "Failed" else None, "creation": creation}
        ).db_insert()
        return row, request

    def _payment_entry(self, receipt, reference, docstatus=1, disbursement=None, doctype="Purchase Invoice",
                       amount=1):
        pe = f"_T-PE-{self.tag}-{frappe.generate_hash(length=6)}"
        frappe.get_doc(
            {"doctype": "Payment Entry", "name": pe, "docstatus": docstatus, "payment_type": "Pay",
             "posting_date": nowdate(), "reference_no": receipt, "reference_date": nowdate()}
        ).db_insert()
        frappe.get_doc(
            {"doctype": "Payment Entry Reference", "name": f"{pe}-1", "parent": pe, "parenttype": "Payment Entry",
             "parentfield": "references", "idx": 1, "docstatus": docstatus, "reference_doctype": doctype,
             "reference_name": reference, "allocated_amount": amount, "b2c_payment_disbursement": disbursement}
        ).db_insert()
        return pe

    def _journal_entry(self, cheque_no, party, docstatus=1, disbursement=None, amount=1):
        je = f"_T-JV-{frappe.generate_hash(length=10)}"
        frappe.get_doc(
            {"doctype": "Journal Entry", "name": je, "docstatus": docstatus, "voucher_type": "Bank Entry",
             "posting_date": nowdate(), "cheque_no": cheque_no}
        ).db_insert()
        for i, row in enumerate(
            ({"credit_in_account_currency": amount},
             {"party_type": "Employee", "party": party, "debit_in_account_currency": amount,
              "b2c_payment_disbursement": disbursement}),
            1,
        ):
            frappe.get_doc(
                {"doctype": "Journal Entry Account", "name": f"{je}-{i}", "parent": je,
                 "parenttype": "Journal Entry", "parentfield": "accounts", "idx": i, "docstatus": docstatus,
                 "account": "_T Account", **row}
            ).db_insert()
        return je

    def _staff_submit(self, doctype, name, field, value):
        """Staff submit the draft, typing ``value`` as its Reference No (None: changed the voucher type)."""
        frappe.db.sql(f"UPDATE `tab{doctype}` SET docstatus = 1, `{field}` = %s WHERE name = %s", (value, name))

    def _booked(self, disbursement, row):
        return postprocess.booked_docstatus(disbursement, frappe.get_doc(REF, row))

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
        request or job would. (The test case's secondary_connection() leaves frappe.db on the
        new connection afterwards, so a raw connection is used instead.)"""
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

    # ------------------------------------------------------------ C11: booking key
    def test_booking_is_keyed_on_the_payouts_receipt(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        r1, _ = self._row(d1, 1, pinv, 250000, receipt="SKT1AAAAAA")
        r2, _ = self._row(d1, 2, pinv, 150000, receipt="SKT2BBBBBB")
        self._payment_entry("SKT1AAAAAA", pinv, disbursement=d1, amount=250000)  # row 1's entry

        self.assertEqual(self._booked(d1, r1), 1)
        self.assertIsNone(self._booked(d1, r2))  # same disbursement and invoice, but not its entry

        self._payment_entry("SKT2BBBBBB", pinv, docstatus=0, disbursement=d1, amount=150000)
        self.assertEqual(self._booked(d1, r2), 0)  # a draft: found, not booked

    def test_entry_without_the_disbursement_link_counts_only_for_the_same_document(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        r1, _ = self._row(d1, 1, pinv, 100, receipt="SKT1AAAAAA")

        self._payment_entry("SKT1AAAAAA", f"{pinv}-OTHER")  # receipt booked against another invoice
        self._payment_entry("SKT1AAAAAA", pinv, docstatus=2)  # cancelled
        self.assertIsNone(self._booked(d1, r1))

        self._payment_entry("SKT1AAAAAA", pinv)  # booked before the suite, or by hand
        self.assertEqual(self._booked(d1, r1), 1)

    def test_salary_journal_entry_keyed_on_cheque_no_and_employee(self):
        d1, slip, emp = self._disbursement(), f"_T-SS-{self.tag}", f"_T-EMP-{self.tag}"
        r1, _ = self._row(d1, 1, slip, 250000, receipt="SKT1AAAAAA", doctype="Salary Slip", party=emp)
        r2, _ = self._row(d1, 2, slip, 50000, receipt="SKT2BBBBBB", doctype="Salary Slip", party=emp)

        self._journal_entry("SKT1AAAAAA", emp, disbursement=d1, amount=250000)
        self._journal_entry("SKT2BBBBBB", f"{emp}-OTHER", amount=50000)
        self.assertEqual(self._booked(d1, r1), 1)
        self.assertIsNone(self._booked(d1, r2))

        # this flow's draft for row 2, made without the receipt as Cheque No, is found (so it
        # is not made again)
        self._journal_entry(None, emp, docstatus=0, disbursement=d1, amount=50000)
        self.assertEqual(self._booked(d1, r2), 0)
        self.assertEqual(self._booked(d1, r1), 1)

    def test_entry_staff_submitted_without_the_receipt_books_its_payout_once(self):
        d1, slip, emp = self._disbursement(), f"_T-SS-{self.tag}", f"_T-EMP-{self.tag}"
        r1, q1 = self._row(d1, 1, slip, 45000, receipt="SKT1AAAAAA", doctype="Salary Slip", party=emp)
        je = self._journal_entry(None, emp, docstatus=0, disbursement=d1, amount=45000)  # this flow's draft
        self.assertEqual(self._booked(d1, r1), 0)

        self._staff_submit("Journal Entry", je, "cheque_no", "OCT-PAYROLL")
        with patch.object(postprocess, "_book", Mock(side_effect=AssertionError("must not book again"))):
            self.assertTrue(postprocess.handle_successful_payment(d1, r1))
        self.assertEqual(frappe.db.get_value(REQ, q1, "accounting_booked"), 1)

        # a second payout of the same amount to the employee in this disbursement: one entry
        # cannot book both
        r2, _ = self._row(d1, 2, slip, 45000, receipt="SKT2BBBBBB", doctype="Salary Slip", party=emp)
        self.assertIsNone(self._booked(d1, r1))
        self.assertIsNone(self._booked(d1, r2))

    def test_payment_entry_submitted_with_another_reference_no_is_booked_once(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        r1, q1 = self._row(d1, 1, pinv, 100, receipt="SKT1AAAAAA")
        pe = self._payment_entry("SKT1AAAAAA", pinv, docstatus=0, disbursement=d1, amount=100)
        self._staff_submit("Payment Entry", pe, "reference_no", "BANK-REF-7")

        with patch.object(postprocess, "_book", Mock(side_effect=AssertionError("must not book again"))):
            self.assertTrue(postprocess.handle_successful_payment(d1, r1))
        self.assertEqual(frappe.db.get_value(REQ, q1, "accounting_booked"), 1)

    def test_split_rows_each_booked_and_marked(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        r1, q1 = self._row(d1, 1, pinv, 250000, receipt="SKT1AAAAAA")
        r2, q2 = self._row(d1, 2, pinv, 150000, receipt="SKT2BBBBBB")
        made = []

        def book(disbursement_name, ref):  # stands in for create_payment_entry_for_doc
            made.append(self._payment_entry(ref.reference_no, ref.reference_name, disbursement=disbursement_name,
                                            amount=ref.allocated_amount))

        with patch.object(postprocess, "_book", book):
            self.assertTrue(postprocess.handle_successful_payment(d1, r1))
            self.assertTrue(postprocess.handle_successful_payment(d1, r2))
            self.assertTrue(postprocess.handle_successful_payment(d1, r2))  # re-sent Result

        self.assertEqual(len(made), 2)
        self.assertEqual(
            sorted(frappe.get_all("Payment Entry", filters={"name": ["in", made]}, pluck="reference_no")),
            ["SKT1AAAAAA", "SKT2BBBBBB"],
        )
        self.assertEqual([frappe.db.get_value(REQ, q, "accounting_booked") for q in (q1, q2)], [1, 1])

    def test_paid_payout_without_receipt_not_booked(self):
        d1, pinv = self._disbursement(), f"_T-PINV-{self.tag}"
        self._row(d1, 2, pinv, 150000, receipt="SKT2BBBBBB")
        self._payment_entry("SKT2BBBBBB", pinv, disbursement=d1, amount=150000)  # the other row's entry
        r1, q1 = self._row(d1, 1, pinv, 250000, receipt=None)

        with patch.object(postprocess, "_book", Mock(side_effect=AssertionError("must not book"))):
            self.assertFalse(postprocess.handle_successful_payment(d1, r1))

        booked, error = frappe.db.get_value(REQ, q1, ["accounting_booked", "accounting_error"])
        self.assertEqual(booked, 0)
        self.assertIn("no M-Pesa receipt", error)

    # ------------------------------------------------------------ C13: retry guard
    def _retry(self, request):
        doc = frappe.get_doc(REQ, request)
        sent = Mock()
        with patch.object(request_module, "make_b2c_payment_request", sent):
            doc.retry_failed_payment()
        return sent

    def _assert_refused(self, request, message, doc=None):
        sent = Mock()
        with (
            patch.object(request_module, "make_b2c_payment_request", sent),
            self.assertRaisesRegex(frappe.ValidationError, message),
        ):
            (doc or frappe.get_doc(REQ, request)).retry_failed_payment()
        sent.assert_not_called()

    def _assert_unchanged(self, request):
        status, ocid = frappe.db.get_value(REQ, request, ["status", "originator_conversation_id"])
        self.assertEqual((status, ocid), ("Failed", f"OCID-{request}"))

    def test_retry_refused_once_the_invoice_was_paid(self):
        pinv = self._invoice(0)  # paid since by another disbursement or a bank Payment Entry
        _, request = self._row(self._disbursement(), 1, pinv, 50000, status="Failed")
        self._assert_refused(request, "owes 0.0; retrying would pay 50000.0")
        self._assert_unchanged(request)

    def test_retry_refused_while_another_disbursement_pays_the_invoice(self):
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2")
        self._row(d2, 1, pinv, 50000, status="Initiated")
        self._assert_refused(request, f"already has a payout in progress or awaiting accounting: {d2}")
        self._assert_unchanged(request)

    def test_retry_refused_while_another_disbursement_awaits_accounting(self):
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        self._row(self._disbursement("2"), 1, pinv, 50000, receipt="SKT9ZZZZZZ")  # Paid, not booked
        self._assert_refused(request, "awaiting accounting")
        self._assert_unchanged(request)

    def test_retry_refused_by_an_old_paid_payout_nothing_booked(self):
        # D2 paid the invoice longer ago than the accounting retry window, so open_payouts no
        # longer lists it; nothing booked it, so the invoice still shows the amount owed
        pinv = self._invoice(100000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2")
        long_ago = add_days(now_datetime(), -(postprocess.RETRY_WINDOW_DAYS + 10))
        _, old = self._row(d2, 1, pinv, 50000, receipt="SKT9ZZZZZZ", creation=long_ago)
        self.assertLess(frappe.db.get_value(REQ, old, "creation"), add_days(now_datetime(), -postprocess.RETRY_WINDOW_DAYS))

        self._assert_refused(request, f"awaiting accounting: {d2}")
        self._payment_entry("SKT9ZZZZZZ", pinv, docstatus=0)  # a draft does not reduce what it owes
        self._assert_refused(request, f"awaiting accounting: {d2}")
        self._assert_unchanged(request)

        self._payment_entry("SKT9ZZZZZZ", pinv)  # booked by hand (or before the suite) for its receipt
        self._retry(request).assert_called_once_with(request)

    def test_retry_sends_while_the_invoice_still_owes_it(self):
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement(), 1, pinv, 50000, status="Failed")

        sent = self._retry(request)

        sent.assert_called_once_with(request)
        status, ocid = frappe.db.get_value(REQ, request, ["status", "originator_conversation_id"])
        self.assertEqual(status, "Not Initiated")
        self.assertNotEqual(ocid, f"OCID-{request}")

    def test_retry_shares_what_is_owed_with_this_disbursements_other_rows(self):
        pinv = self._invoice(400000)
        d1 = self._disbursement()
        self._row(d1, 1, pinv, 250000, status="Initiated")  # in flight
        _, request = self._row(d1, 2, pinv, 150000, status="Failed")
        self._retry(request).assert_called_once_with(request)

    def test_retry_refused_when_this_disbursements_other_rows_take_what_is_owed(self):
        pinv = self._invoice(300000)  # partly paid by bank since
        d1 = self._disbursement()
        self._row(d1, 1, pinv, 250000, status="Initiated")
        _, request = self._row(d1, 2, pinv, 150000, status="Failed")
        self._assert_refused(request, "250000.0 is still being paid")
        self._assert_unchanged(request)

    def test_retry_failed_transactions_refuses_one_row_and_still_sends_the_other(self):
        paid, owing = self._invoice(0), self._invoice(50000)
        d1 = self._disbursement()
        _, refused = self._row(d1, 1, paid, 50000, status="Failed")
        _, retried = self._row(d1, 2, owing, 50000, status="Failed")

        sent = Mock()
        with (
            patch.object(request_module, "make_b2c_payment_request", sent),
            patch("frappe.log_error") as log_error,
        ):
            frappe.get_doc(PARENT, d1).retry_failed_payments()

        sent.assert_called_once_with(retried)
        log_error.assert_called_once()
        logged = log_error.call_args.kwargs["message"]
        self.assertIn(refused, logged)
        self.assertIn("owes 0.0", logged)
        self._assert_unchanged(refused)

    def test_retry_reads_the_status_a_concurrent_retry_committed(self):
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement(), 1, pinv, 50000, status="Failed")
        self._commit_for_second_connection()

        doc = frappe.get_doc(REQ, request)  # read while Failed: this transaction's snapshot
        self._commit_elsewhere(  # a second retry resets it and commits midway through its send
            (
                f"UPDATE `tab{REQ}` SET status = 'Not Initiated', originator_conversation_id = 'OCID-NEW' WHERE name = %s",
                (request,),
            ),
        )
        self.assertEqual(frappe.db.get_value(REQ, request, "status"), "Failed")  # what a plain read sees

        self._assert_refused(request, r"can be retried \(Not Initiated\)", doc=doc)

    def test_retry_reads_what_the_invoice_owes_after_a_booking_committed_since(self):
        pinv = self._invoice(50000)
        _, request = self._row(self._disbursement("1"), 1, pinv, 50000, status="Failed")
        d2 = self._disbursement("2")
        _, other = self._row(d2, 1, pinv, 50000, receipt="SKT9ZZZZZZ")  # awaiting accounting
        pe = self._payment_entry("SKT9ZZZZZZ", pinv, docstatus=0, disbursement=d2, amount=50000)
        self._commit_for_second_connection()

        doc = frappe.get_doc(REQ, request)  # this transaction's snapshot
        self._commit_elsewhere(  # D2's Payment Entry is booked: the invoice owes 0, its payout is booked
            ("UPDATE `tabPayment Entry` SET docstatus = 1 WHERE name = %s", (pe,)),
            ("UPDATE `tabPayment Entry Reference` SET docstatus = 1 WHERE parent = %s", (pe,)),
            ("UPDATE `tabPurchase Invoice` SET outstanding_amount = 0 WHERE name = %s", (pinv,)),
            (f"UPDATE `tab{REQ}` SET accounting_booked = 1 WHERE name = %s", (other,)),
        )
        self.assertEqual(frappe.db.get_value("Purchase Invoice", pinv, "outstanding_amount"), 50000)  # snapshot

        self._assert_refused(request, "owes 0.0; retrying would pay 50000.0", doc=doc)

    # ------------------------------------------------------------ C28: after commit
    def test_daraja_error_on_send_queues_the_row_update_after_commit(self):
        _, request = self._row(self._disbursement(), 1, f"_T-PINV-{self.tag}", 100, status="Initiated",
                               request_status="Not Initiated")
        queue = Mock(count=0)
        connector = object.__new__(b2c_connector.MpesaB2CConnector)
        with (
            patch("frappe.utils.background_jobs.get_queue", return_value=queue),
            patch.object(b2c_connector, "update_integration_request"),
        ):
            connector._handle_error(
                response=Mock(status_code=400),
                data={"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid PartyB"},
                document_name=request,
                integration_request=frappe._dict(name="_T-IR"),
            )
            self.assertEqual(frappe.db.get_value(REQ, request, "status"), "Failed")
            queue.enqueue_call.assert_not_called()  # nothing a worker could pick up yet

            frappe.db.after_commit.run()  # what the commit does

        queue.enqueue_call.assert_called_once()
        job = queue.enqueue_call.call_args.kwargs["kwargs"]
        self.assertEqual(job["method"], "frappe_mpsa_payments.services.b2c_response_service.update_b2c_reference_status")
        self.assertEqual(job["kwargs"]["b2c_request_name"], request)
