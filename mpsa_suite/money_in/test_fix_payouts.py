"""B2C Payment Disbursement payouts against the real database (v15 and v16).

Rows are written with db_insert, as the payout jobs leave them, so no ERPNext documents,
company or Mpesa Settings are needed and nothing is ever sent.
"""

import json
from unittest.mock import patch

import frappe
from frappe.utils import flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import (
    b2c_payment_disbursement as disbursement,
)

PARENT = "B2C Payment Disbursement"
REF = "B2C Payment Disbursement Reference"
REQ = "B2C Disbursement Request"


class TestFixPayouts(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.tag = frappe.generate_hash(length=8)

    def tearDown(self):
        frappe.db.rollback()

    # ------------------------------------------------------------ helpers
    def _batch(self, allocations=(100, 100, 100)):
        name = f"_T-B2CPD-{self.tag}"
        frappe.get_doc(
            {
                "doctype": PARENT,
                "name": name,
                "docstatus": 1,
                "status": "Initiated",
                "payment_type": "Mpesa Disbursement",
                "party_type": "Supplier",
                "transaction_to_pay_against": "Purchase Invoice",
                "posting_date": nowdate(),
                "retry_count": 2,
            }
        ).db_insert()
        for i, amount in enumerate(allocations, 1):
            frappe.get_doc(
                {
                    "doctype": REF,
                    "name": f"{name}-R{i}",
                    "parent": name,
                    "parenttype": PARENT,
                    "parentfield": "references",
                    "idx": i,
                    "docstatus": 1,
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": f"_T-PINV-{self.tag}-{i}",
                    "party_type": "Supplier",
                    "party": "_T Supplier",
                    "partyb": "254712345678",
                    "allocated_amount": amount,
                    "payment_status": "Initiated",
                }
            ).db_insert()
        return name

    def _jobs_ran(self, name, statuses):
        """What the create_b2c_request jobs and the Results write: straight to the rows."""
        for i, status in enumerate(statuses, 1):
            update = {"b2c_disbursement_request": f"B2C-{self.tag}-{i}"}
            if status == "Paid":
                update.update(payment_status="Paid", reference_no=f"SKT{i}", reference_date=now_datetime())
            elif status == "Failed":
                update["payment_status"] = "Failed"
            frappe.db.set_value(REF, f"{name}-R{i}", update)

    def _stale_form_copy(self, name):
        doc = frappe.get_doc(PARENT, name)
        # the test rows link to nothing real; without this the old full save would not run at all
        doc.flags.ignore_links = doc.flags.ignore_mandatory = True
        return doc

    def _rows(self, name):
        return frappe.get_all(
            REF,
            filters={"parent": name},
            fields=["name", "b2c_disbursement_request", "payment_status", "reference_no"],
            order_by="idx asc",
        )

    # ------------------------------------------------------------ C10: stale copy
    def test_status_check_from_stale_form_copy_keeps_request_links(self):
        name = self._batch()
        stale = self._stale_form_copy(name)  # loaded right after submit
        self._jobs_ran(name, ["Paid", "Initiated", "Failed"])

        message = stale.update_disbursement_status()

        rows = self._rows(name)
        self.assertEqual([r.b2c_disbursement_request for r in rows], [f"B2C-{self.tag}-{i}" for i in (1, 2, 3)])
        self.assertEqual([r.payment_status for r in rows], ["Paid", "Initiated", "Failed"])
        self.assertEqual(rows[0].reference_no, "SKT1")
        status, retry_count = frappe.db.get_value(PARENT, name, ["status", "retry_count"])
        self.assertEqual(status, "Partially Paid")
        self.assertEqual(retry_count, 3)  # 2 in the database, whatever the copy held
        self.assertIn("1 Paid, 1 Failed, 3 Total", message)

    def test_status_check_counts_rows_from_the_database(self):
        name = self._batch()
        stale = self._stale_form_copy(name)
        self._jobs_ran(name, ["Paid", "Paid", "Paid"])
        stale.update_disbursement_status()
        self.assertEqual(frappe.db.get_value(PARENT, name, "status"), "Paid")

    def test_status_check_runs_change_events_and_version(self):
        name = self._batch()
        stale = self._stale_form_copy(name)
        self._jobs_ran(name, ["Paid", "Paid", "Paid"])
        seen = []

        def on_change(doc):
            seen.append((doc.get_doc_before_save().status, doc.status, doc.references[0].b2c_disbursement_request))

        locked = []
        with (
            patch.object(disbursement.B2CPaymentDisbursement, "on_change", on_change, create=True),
            # db_set's own before-image is a get_doc(for_update=True), which locks every row
            patch.object(disbursement.B2CPaymentDisbursement, "load_doc_before_save", lambda *a, **kw: locked.append(1)),
        ):
            stale.update_disbursement_status()

        # on_change (Value Change notifications, webhooks) sees the old status and the database's rows
        self.assertEqual(seen, [("Initiated", "Paid", f"B2C-{self.tag}-1")])
        self.assertEqual(locked, [])
        versions = frappe.get_all("Version", filters={"ref_doctype": PARENT, "docname": name}, pluck="data")
        self.assertEqual(len(versions), 1)
        changed = {field: (old, new) for field, old, new in json.loads(versions[0])["changed"]}
        self.assertEqual(changed["status"], ("Initiated", "Paid"))
        self.assertEqual([str(v) for v in changed["retry_count"]], ["2", "3"])
        self.assertFalse(json.loads(versions[0]).get("row_changed"))  # no row was written

    def test_save_after_submit_keeps_rows_the_jobs_wrote(self):
        name = self._batch()
        stale = self._stale_form_copy(name)
        self._jobs_ran(name, ["Paid", "Initiated", "Initiated"])

        stale.save(ignore_permissions=True)  # form Update / API save of the old copy

        rows = self._rows(name)
        self.assertEqual([r.b2c_disbursement_request for r in rows], [f"B2C-{self.tag}-{i}" for i in (1, 2, 3)])
        self.assertEqual(rows[0].payment_status, "Paid")
        self.assertEqual(rows[0].reference_no, "SKT1")

    # ------------------------------------------------------------ C12: Salary Slip paid
    def _request(self, slip, amount, status="Paid", docstatus=1):
        name = f"_T-B2C-{frappe.generate_hash(length=10)}"
        frappe.get_doc(
            {
                "doctype": REQ,
                "name": name,
                "docstatus": docstatus,
                "status": status,
                "amount": amount,
                "reference_doctype": "Salary Slip",
                "reference_name": slip,
            }
        ).db_insert()
        return name

    def _journal(self, debit, employee, payroll_entry, voucher_type="Bank Entry", docstatus=1, b2c=None):
        je = f"_T-JV-{frappe.generate_hash(length=10)}"
        frappe.get_doc(
            {"doctype": "Journal Entry", "name": je, "docstatus": docstatus, "voucher_type": voucher_type,
             "posting_date": nowdate()}
        ).db_insert()
        rows = [
            {"party_type": "Employee", "party": employee, "reference_type": "Payroll Entry",
             "reference_name": payroll_entry, "debit_in_account_currency": debit, "b2c_payment_disbursement": b2c},
            {"credit_in_account_currency": debit},  # the bank side
        ]
        for i, row in enumerate(rows, 1):
            frappe.get_doc(
                {"doctype": "Journal Entry Account", "name": f"{je}-{i}", "parent": je, "parenttype": "Journal Entry",
                 "parentfield": "accounts", "idx": i, "docstatus": docstatus, "account": "_T Account", **row}
            ).db_insert()

    def test_salary_slip_paid_counts_paid_b2c_and_payroll_banking(self):
        self.assertTrue(frappe.get_meta("Journal Entry Account").has_field("b2c_payment_disbursement"))
        slip, emp, pe = f"_T-SS-{self.tag}", f"_T-EMP-{self.tag}", f"_T-PE-{self.tag}"
        self._request(slip, 20000)  # counts, booked or not
        self._request(slip, 5000, status="Failed")
        self._request(slip, 3000, status="Initiated")  # in flight: open_payouts refuses those
        self._request(slip, 7000, docstatus=2)  # cancelled with its disbursement: the money is still gone
        self._request(f"{slip}-OTHER", 9000)
        self._journal(10000, emp, pe)  # payroll bank entry: counts
        self._journal(1000, emp, pe, voucher_type="Cash Entry")  # counts
        self._journal(20000, emp, pe, b2c="_T-B2CPD-x")  # the B2C flow's own entry: already counted
        self._journal(4000, emp, pe, docstatus=0)
        self._journal(6000, emp, pe, voucher_type="Journal Entry")  # payroll accrual
        self._journal(8000, f"{emp}-2", pe)
        self._journal(2000, emp, f"{pe}-2")

        self.assertEqual(disbursement.salary_slip_paid(slip, emp, pe), 38000)
        # fix check S11: the slip's advice lists the payroll banking its paid total counts
        self.assertEqual(sorted((e.voucher_type, flt(e.amount)) for e in disbursement.slip_bank_payments(emp, pe)),
                         [("Bank Entry", 10000), ("Cash Entry", 1000)])
        self.assertEqual(disbursement.salary_slip_paid(slip), 27000)  # no Payroll Entry: B2C only
        self.assertEqual(disbursement.salary_slip_paid(f"_T-SS-{self.tag}-NONE"), 0)

    def test_salary_slip_paid_locked_read_through_the_rows(self):
        slip = f"_T-SS-{self.tag}"
        paid = self._request(slip, 20000)
        initiated = self._request(slip, 3000, status="Initiated")
        # D1's row and its amended copy link the same request; another row an unfinished one
        for i, request in enumerate((paid, paid, initiated), 1):
            frappe.get_doc(
                {"doctype": REF, "name": f"_T-REF-{self.tag}-{i}", "parent": f"_T-B2CPD-{self.tag}-{i}",
                 "parenttype": PARENT, "parentfield": "references", "idx": 1, "docstatus": 1,
                 "reference_doctype": "Salary Slip", "reference_name": slip, "party_type": "Employee",
                 "party": "_T-EMP", "allocated_amount": 1, "b2c_disbursement_request": request}
            ).db_insert()

        self.assertEqual(disbursement.salary_slip_paid(slip, for_update=True), 20000)  # counted once

    # ------------------------------------------------------------ C14: Daraja B2C limits
    def test_row_outside_b2c_limits_refused_before_submit(self):
        for amount, message in ((12345.5, "whole shillings"), (250001, "250,000")):
            row = frappe.get_doc(
                {"doctype": REF, "idx": 2, "party": "_T Supplier", "partyb": "0712345678",
                 "allocated_amount": amount, "outstanding_amount": 300000}
            )
            with self.assertRaisesRegex(frappe.ValidationError, f"Row #2: .*{message}"):
                row.validate()
        frappe.get_doc(
            {"doctype": REF, "idx": 2, "party": "_T Supplier", "partyb": "0712345678",
             "allocated_amount": 250000, "outstanding_amount": 300000}
        ).validate()

    def test_send_job_marks_row_outside_limits_failed(self):
        name = self._batch(allocations=(12345.5,))
        doc = frappe.get_doc(PARENT, name)
        ref = doc.references[0]

        # returns instead of raising: the job commits the row as Failed
        disbursement.create_b2c_request(ref, frappe._dict(name=f"_T-NO-SETTINGS-{self.tag}"), doc)

        self.assertEqual(frappe.db.get_value(REF, ref.name, "payment_status"), "Failed")
        self.assertFalse(frappe.db.exists(REQ, {"b2c_payment_disbursement_reference": ref.name}))
        self.assertEqual(disbursement.open_payouts("Purchase Invoice", ref.reference_name), [])
