"""The M-Pesa Payment Schedule against the real database (ERPNext v15 and v16): prepared by an
Accounts User, paid by an M-Pesa Payer with Pay Selected, sent from the job with its marker,
then the Result, the booking and the line Paid.

Same rules as test_b2b_documents: a shortcode of its own (TESTB2BDOCS, 600777) and a supplier of
these tests with an approved Supplier M-Pesa Account; nothing reaches Safaricom (post() is
replaced and Results are handed to handle_result); every commit made by the code under test is
replaced for the whole test, so each test rolls back; the jobs the code queues are run in the
test. Unit coverage is in tests/unit/test_schedule.py.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import b2b, owed, schedule
from mpsa_suite.money_out.test_b2b_documents import _settings  # the shortcode TESTB2BDOCS, its accounts

SETTINGS = "TESTB2BDOCS"
B2B, PI = b2b.B2B_DOCTYPE, "Purchase Invoice"
SCHEDULE, LINE = schedule.SCHEDULE, schedule.LINE
SUPPLIER = "_T Schedule Supplier"
ACCOUNT = "_T-MSA-SCHEDULE"
BUYING = "_Test M-Pesa KES Buying"
PREPARER, PAYER = "_t_sched_accounts@example.com", "_t_sched_payer@example.com"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, payout_lines_from=None, allow_one_off_b2b=0,
                    entry_reminder_days=3, schedule_maker_checker=1, send_interval_seconds=0)


def _supplier() -> str:
    name = frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER}, "name")
    if not name:
        group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
        name = frappe.get_doc({"doctype": "Supplier", "supplier_name": SUPPLIER, "supplier_group": group}).insert().name
    if not frappe.db.exists("Supplier M-Pesa Account", ACCOUNT):
        doc = frappe.get_doc({
            "doctype": "Supplier M-Pesa Account", "supplier": name, "account_type": "Paybill", "shortcode": "888881",
            "account_number": "TSCH-1", "expected_name": SUPPLIER, "is_default": 1, "docstatus": 1,
            "proof": "/private/files/_t_schedule_proof.pdf", "approved_by": "Administrator",
            "approved_on": now_datetime(), "active_from": add_days(now_datetime(), -1), "creation": now_datetime(),
            "modified": now_datetime(), "owner": "Administrator"})
        doc.name = ACCOUNT
        doc.db_insert()
    return name


def _user(email: str, roles: tuple) -> None:
    if frappe.db.exists("User", email):
        return
    frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0,
                    "roles": [{"role": r} for r in roles]}).insert(ignore_permissions=True)


@contextmanager
def _roles_checked():
    """v15's frappe.only_for lets everyone through while flags.in_test is set (v16's does not), so
    a refusal by role is seen only with it off. Saved and put back by hand: patch.object would
    delete the key from frappe's _dict on exit and leave in_test off for the rest of the run."""
    flags = frappe.local.flags
    was = flags.in_test
    flags.in_test = False
    try:
        yield
    finally:
        flags.in_test = was


class TestPaymentSchedule(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        _settings()
        cls.supplier = _supplier()
        if not frappe.db.exists("Price List", BUYING):
            frappe.get_doc({"doctype": "Price List", "price_list_name": BUYING, "currency": "KES", "buying": 1,
                            "enabled": 1}).insert()
        frappe.db.set_value("Item", t.ITEM, "is_purchase_item", 1)
        _user(PREPARER, ("Accounts User",))
        _user(PAYER, ("M-Pesa Payer",))
        # what an interrupted run committed for the test supplier (it exists only for these tests)
        frappe.db.delete(B2B, {"supplier": cls.supplier})
        for (name,) in frappe.db.sql(f"SELECT DISTINCT parent FROM `tab{LINE}` WHERE party = %s", cls.supplier):
            frappe.db.delete(LINE, {"parent": name})
            frappe.db.delete(SCHEDULE, {"name": name})
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self.tag = frappe.generate_hash(length=8)
        self.clock = [1000.0]
        self._patches = [patch.object(frappe.db, "commit"),
                         patch.object(schedule, "_clock", side_effect=lambda: self.clock[0])]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.local.mpesa_suite_bank_accounts = None
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")  # a switch a test changed

    # ------------------------------------------------------------ helpers
    def _invoice(self, amount) -> str:
        pi = frappe.get_doc({
            "doctype": PI, "supplier": self.supplier, "company": t.COMPANY, "posting_date": nowdate(),
            "due_date": nowdate(), "currency": "KES", "conversion_rate": 1, "buying_price_list": BUYING,
            "plc_conversion_rate": 1, "bill_no": f"SCH-{self.tag}-{frappe.generate_hash(length=4)}",
            "items": [{"item_code": t.ITEM, "qty": 1, "rate": amount, "uom": "Nos", "conversion_factor": 1}]})
        pi.insert()
        pi.submit()
        return pi.name

    def _schedule(self, *lines, user=PREPARER):
        """A schedule saved by ``user``: lines are (amount, invoice or None, days from today)."""
        frappe.set_user(user)
        try:
            doc = frappe.get_doc({
                "doctype": SCHEDULE, "title": f"T {self.tag}", "company": t.COMPANY, "b2b_settings": SETTINGS,
                "lines": [{"planned_date": add_days(nowdate(), days), "party_type": "Supplier", "party": self.supplier,
                           "amount": amount, "reference_doctype": PI if pi else None, "reference_name": pi,
                           "remarks": f"T {self.tag}"} for amount, pi, days in lines]})
            doc.insert()
        finally:
            frappe.set_user("Administrator")
        return frappe.get_doc(SCHEDULE, doc.name)

    def _pay(self, lines, user=PAYER) -> dict:
        """Pay Selected as ``user``: the summary, 6 seconds, Confirm. The queued jobs are kept."""
        frappe.set_user(user)
        try:
            summary = schedule.prepare_payment([line.name for line in lines])
            self.clock[0] += 6
            with patch("frappe.enqueue") as enqueue:
                answer = schedule.confirm_payment(summary["token"]) if summary["token"] else {"sent": [], "refused": []}
        finally:
            frappe.set_user("Administrator")
        answer["summary"], answer["jobs"] = summary, [c for c in enqueue.call_args_list] if summary["token"] else []
        return answer

    def _send(self, answer, user=PAYER):
        """The send_batch job confirm queued, as its user, with Daraja's ack."""
        (job,) = [c for c in answer["jobs"] if c.args[0].endswith("schedule.send_batch")]
        frappe.set_user(user)
        try:
            with patch("mpsa_suite.money_out.b2b.post", side_effect=self._ack) as post, patch("frappe.enqueue"):
                schedule.send_batch(job.kwargs["requests"], job.kwargs["batch"])
        finally:
            frappe.set_user("Administrator")
        for name in job.kwargs["requests"]:
            schedule.sync_line(name)  # the job sync_line_later queued
        return post

    def _ack(self, settings, endpoint, payload, **kw):
        name = kw["name"]
        return {"ResponseCode": "0", "ResponseDescription": "Accept the service request successfully.",
                "ConversationID": f"AG_{name}", "OriginatorConversationID": f"OC_{name}"}, None

    def _result(self, name) -> str:
        """Safaricom's Paid Result, the booking job, and the line's update."""
        receipt = "T" + frappe.generate_hash(length=9).upper()
        result = {
            "ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
            "ConversationID": f"AG_{name}", "OriginatorConversationID": f"OC_{name}", "TransactionID": receipt,
            "ResultParameters": {"ResultParameter": [
                {"Key": "DebitAccountBalance", "Value": "Working Account|KES|50000.00|50000.00|0.00|0.00"},
                {"Key": "TransCompletedTime", "Value": now_datetime().strftime("%Y%m%d%H%M%S")},
                {"Key": "DebitPartyCharges", "Value": "Business Pay Bill Charge|KES|2.00"},
                {"Key": "ReceiverPartyPublicName", "Value": f"888881 - {SUPPLIER.upper()}"},
            ]},
        }
        with patch("frappe.enqueue"):
            b2b.handle_result(result, name)
            b2b.book_payment_entry(name)
        schedule.sync_line(name)
        return receipt

    def _line(self, name):
        return frappe.db.get_value(LINE, name, ["status", "request", "receipt", "entry_status", "last_problem",
                                                "approved_by", "prepared_by", "changed_by", "planned_date"],
                                   as_dict=True)

    # ------------------------------------------------------------ the flow
    def test_three_lines_prepared_paid_sent_and_booked(self):
        pi1, pi2 = self._invoice(10000), self._invoice(6000)
        doc = self._schedule((4000, pi1, 0), (6000, pi2, 0), (1000, None, 1))
        l1, l2, l3 = doc.lines
        self.assertEqual((l1.status, l1.route, l1.payee_account, l1.prepared_by), ("Planned", "B2B Paybill", ACCOUNT,
                                                                                  PREPARER))
        self.assertEqual(owed.left_to_pay(PI, pi1).left, 6000)  # the planned line counts

        answer = self._pay([l1, l2, l3])
        summary = answer["summary"]
        self.assertEqual((summary["count"], summary["total"]), (2, 10000))
        self.assertEqual(summary["not_included"][0]["line"], l3.name)
        self.assertIn("Not due until", summary["not_included"][0]["reason"])
        self.assertEqual(len(answer["sent"]), 2)
        for line in (l1, l2):
            state = self._line(line.name)
            self.assertEqual((state.status, state.approved_by), ("Sending", PAYER))
            self.assertEqual(frappe.db.get_value(B2B, state.request, ["docstatus", "status"]), (1, "Not Initiated"))

        post = self._send(answer)
        self.assertEqual(post.call_count, 2)
        for line in (l1, l2):
            state = self._line(line.name)
            self.assertEqual(state.status, "Sent")
            self.assertTrue(frappe.db.get_value(B2B, state.request, "send_started_on"))

        receipt = self._result(self._line(l1.name).request)
        state = self._line(l1.name)
        self.assertEqual((state.status, state.receipt, state.entry_status), ("Paid", receipt, "Entry Made"))
        self.assertEqual(flt(frappe.db.get_value(PI, pi1, "outstanding_amount")), 6000)
        schedule.update_totals(doc.name)
        totals = frappe.db.get_value(SCHEDULE, doc.name, ["status", "total_paid", "total_in_progress",
                                                          "total_planned"], as_dict=True)
        self.assertEqual((totals.status, flt(totals.total_paid), flt(totals.total_in_progress),
                          flt(totals.total_planned)), ("Partly Paid", 4000, 6000, 1000))

    # ------------------------------------------------------------ never before the planned day
    def test_a_line_not_yet_due_is_refused_on_every_route(self):
        pi = self._invoice(10000)
        doc = self._schedule((3000, pi, 0))
        line = doc.lines[0]
        frappe.set_user(PAYER)
        summary = schedule.prepare_payment([line.name])
        frappe.set_user("Administrator")
        tomorrow = add_days(nowdate(), 1)
        frappe.db.set_value(LINE, line.name, "planned_date", tomorrow, update_modified=False)
        self.clock[0] += 6
        frappe.set_user(PAYER)
        answer = schedule.confirm_payment(summary["token"])  # 1. Pay Selected
        frappe.set_user("Administrator")
        self.assertEqual(answer["sent"], [])
        self.assertIn("Not due until", answer["refused"][0]["reason"])

        values = {"doctype": B2B, "mpesa_settings": SETTINGS, "company": t.COMPANY, "amount": 3000, "remarks": "x",
                  "supplier": self.supplier, "supplier_mpesa_account": ACCOUNT, "purchase_invoice": pi,
                  "payment_schedule": doc.name, "schedule_line": line.name}
        with patch("mpsa_suite.money_out.b2b.post") as post:
            # 2. an API insert already submitted, carrying the line (flags cannot come through the API)
            self.assertRaisesRegex(frappe.ValidationError, "Pay Selected",
                                   frappe.get_doc({**values, "docstatus": 1}).insert)
            # 3. a console insert with the schedule's flags set by hand: the submit still refuses
            req = frappe.get_doc(values)
            req.flags.mpesa_from_schedule = True
            req.insert()
            frappe.db.set_value(LINE, line.name, {"request": req.name, "status": "Sending"}, update_modified=False)
            req.flags.mpesa_send_later = True
            self.assertRaisesRegex(frappe.ValidationError, "Not due until", req.submit)
            # 4. a console send of a submitted one, and 5. the job's send
            frappe.db.set_value(B2B, req.name, {"docstatus": 1, "status": "Not Initiated"}, update_modified=False)
            self.assertRaisesRegex(frappe.ValidationError, "Not due until", b2b.send_b2b, req.name)
            b2b.send_deferred(req.name)
        post.assert_not_called()
        self.assertTrue(frappe.db.get_value(B2B, req.name, "error_message").startswith("Not sent: Not due until"))
        self.assertEqual(frappe.db.get_value(B2B, req.name, "status"), "Failed")

    # ------------------------------------------------------------ maker-checker
    def test_who_prepared_a_line_cannot_pay_it_unless_switched_off(self):
        frappe.get_doc("User", PREPARER).add_roles("M-Pesa Payer")
        pi = self._invoice(10000)
        line = self._schedule((3000, pi, 0)).lines[0]
        answer = self._pay([line], user=PREPARER)
        self.assertEqual(answer["summary"]["not_included"][0]["reason"],
                         "You prepared or last changed this line: another person must pay it.")
        frappe.db.set_single_value("M-Pesa Suite Settings", "schedule_maker_checker", 0)
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
        answer = self._pay([line], user=PREPARER)
        self.assertEqual(len(answer["sent"]), 1)

    # ------------------------------------------------------------ the token
    def test_confirm_needs_five_seconds_and_works_once(self):
        pi = self._invoice(10000)
        line = self._schedule((3000, pi, 0)).lines[0]
        frappe.set_user(PAYER)
        try:
            token = schedule.prepare_payment([line.name])["token"]
            self.clock[0] += 4
            self.assertRaisesRegex(frappe.ValidationError, "5 seconds", schedule.confirm_payment, token)
            self.clock[0] += 2
            with patch("frappe.enqueue"):
                self.assertEqual(len(schedule.confirm_payment(token)["sent"]), 1)
            self.assertRaisesRegex(frappe.ValidationError, "used already", schedule.confirm_payment, token)
        finally:
            frappe.set_user("Administrator")

    def test_a_schedule_form_opened_before_a_payment_cannot_be_saved(self):
        pi = self._invoice(10000)
        doc = self._schedule((3000, pi, 0), (2000, pi, 2))
        stale = frappe.get_doc(SCHEDULE, doc.name)
        self._pay([doc.lines[0]])
        stale.lines[1].amount = 2500
        frappe.set_user(PREPARER)
        try:
            self.assertRaises(frappe.TimestampMismatchError, stale.save)
        finally:
            frappe.set_user("Administrator")
        fresh = frappe.get_doc(SCHEDULE, doc.name)
        fresh.lines[0].amount = 3500  # sent: it cannot change
        self.assertRaisesRegex(frappe.ValidationError, "was sent to M-Pesa", fresh.save)

    # ------------------------------------------------------------ a line only through its schedule
    def test_a_line_cannot_be_saved_or_deleted_on_its_own(self):
        """Write on the schedule is enough for Frappe to save or delete a row by itself (REST PUT
        and DELETE on the row, frappe.client.save), which would skip every schedule check."""
        from frappe.api import v1
        from frappe.client import save as client_save

        pi = self._invoice(10000)
        doc = self._schedule((3000, pi, 0))
        line = doc.lines[0]
        frappe.set_user(PREPARER)
        try:
            frappe.local.form_dict = frappe._dict(data=frappe.as_json({"prepared_by": "Administrator",
                                                                          "changed_by": None}))
            self.assertRaisesRegex(frappe.ValidationError, "on their schedule", v1.update_doc, LINE, line.name)
            self.assertRaisesRegex(frappe.ValidationError, "on their schedule", v1.delete_doc, LINE, line.name)
            row = frappe.db.get_value(LINE, line.name, "*", as_dict=True)
            self.assertRaisesRegex(frappe.ValidationError, "on their schedule", client_save,
                                   {**row, "doctype": LINE, "planned_date": add_days(nowdate(), -3)})
        finally:
            frappe.local.form_dict = frappe._dict()
            frappe.set_user("Administrator")
        state = self._line(line.name)
        self.assertEqual((state.prepared_by, state.changed_by, str(state.planned_date)), (PREPARER, PREPARER,
                                                                                         nowdate()))
        # on the schedule, as always
        frappe.set_user(PREPARER)
        try:
            fresh = frappe.get_doc(SCHEDULE, doc.name)
            fresh.lines[0].remarks = "changed on the schedule"
            fresh.save()
            fresh.lines = []
            fresh.save()
        finally:
            frappe.set_user("Administrator")
        self.assertFalse(frappe.db.exists(LINE, line.name))

    def test_a_line_of_another_schedule_cannot_be_moved_in(self):
        """Frappe updates rows by name only: a row carrying a sent line's name would move it here
        and reset its record of the payment."""
        pi = self._invoice(10000)
        first = self._schedule((3000, pi, 0))
        self._send(self._pay(first.lines))
        sent = frappe.db.get_value(LINE, first.lines[0].name, "*", as_dict=True)
        second = frappe.get_doc(SCHEDULE, self._schedule((1000, None, 1)).name)
        # with the derived fields too: Frappe checks the account's Dynamic Link before validate
        second.append("lines", {**{f: sent[f] for f in (*schedule.EDITABLE, *schedule.DERIVED)}, "name": sent.name})
        frappe.set_user(PREPARER)
        try:
            self.assertRaisesRegex(frappe.ValidationError, f"is a line of {first.name}: it cannot be moved",
                                   second.save)
        finally:
            frappe.set_user("Administrator")
        self.assertEqual(frappe.db.get_value(LINE, sent.name, ["parent", "status", "request"]),
                         (first.name, "Sent", sent.request))

    # ------------------------------------------------------------ a send that began (R1)
    def test_a_request_whose_send_began_is_cancelled_by_a_manager_only_and_stays_unknown(self):
        pi = self._invoice(10000)
        line = self._schedule((3000, pi, 0)).lines[0]
        name = self._pay([line])["sent"][0]["request"]
        # its worker died after committing the marker, or the call is still out
        frappe.db.set_value(B2B, name, "send_started_on", now_datetime(), update_modified=False)
        frappe.set_user(PAYER)  # M-Pesa Payer has cancel on the request
        try:
            with _roles_checked():
                self.assertRaises(frappe.PermissionError, frappe.get_doc(B2B, name).cancel)
        finally:
            frappe.set_user("Administrator")
        self.assertEqual(frappe.db.get_value(B2B, name, "docstatus"), 1)
        frappe.get_doc(B2B, name).cancel()  # Administrator, after checking the statement
        self.assertEqual(frappe.db.get_value(B2B, name, ["docstatus", "status", "error_message"]),
                         (2, "Unknown", schedule.INTERRUPTED))
        self.assertRaisesRegex(frappe.ValidationError, "is the record of that payment", frappe.delete_doc, B2B, name)
        schedule.sync_line(name)
        self.assertEqual(self._line(line.name).status, "Failed")
        # a cancel that went around before_cancel: only a manager plans the line again
        frappe.db.set_value(B2B, name, {"status": "Not Initiated", "error_message": None}, update_modified=False)
        schedule.sync_line(name)
        self.assertIn("M-Pesa may have paid it", self._line(line.name).last_problem)
        self.assertRaisesRegex(frappe.ValidationError, "when it stopped", frappe.delete_doc, B2B, name)
        frappe.set_user(PREPARER)
        try:
            self.assertRaisesRegex(frappe.ValidationError, "cancelled after its send began", schedule.plan_again,
                                   line.name)
        finally:
            frappe.set_user("Administrator")
        schedule.plan_again(line.name)
        self.assertEqual((self._line(line.name).status, self._line(line.name).request), ("Planned", None))

    # ------------------------------------------------------------ planned lines and direct payments
    def test_a_planned_line_stops_a_direct_payment_beyond_what_is_left(self):
        pi = self._invoice(10000)
        self._schedule((8000, pi, 3))
        values = {"doctype": B2B, "mpesa_settings": SETTINGS, "company": t.COMPANY, "remarks": "direct",
                  "purchase_invoice": pi}
        self.assertRaisesRegex(frappe.ValidationError, "planned on MPS-",
                               frappe.get_doc({**values, "amount": 3000}).insert)
        frappe.get_doc({**values, "amount": 2000}).insert()

    def test_double_planning_a_document_is_refused(self):
        pi = self._invoice(10000)
        self._schedule((6000, pi, 1))
        self.assertRaisesRegex(frappe.ValidationError, "left to pay", self._schedule, (5000, pi, 2))

    # ------------------------------------------------------------ moving dates and Plan Again
    def test_accounts_move_a_date_and_the_mover_may_not_pay_it(self):
        frappe.get_doc("User", PREPARER).add_roles("M-Pesa Payer")
        pi = self._invoice(10000)
        line = self._schedule((3000, pi, 2), user="Administrator").lines[0]
        frappe.set_user(PREPARER)
        try:
            self.assertEqual(schedule.move_date([line.name], nowdate()), {"moved": 1})
        finally:
            frappe.set_user("Administrator")
        state = self._line(line.name)
        self.assertEqual((str(state.planned_date), state.changed_by), (nowdate(), PREPARER))
        answer = self._pay([line], user=PREPARER)
        self.assertIn("another person must pay it", answer["summary"]["not_included"][0]["reason"])
        self.assertEqual(len(self._pay([line])["sent"]), 1)

    def test_a_failed_send_is_planned_again_and_paid(self):
        pi = self._invoice(10000)
        line = self._schedule((3000, pi, 0)).lines[0]
        answer = self._pay([line])
        frappe.db.set_value("Supplier", self.supplier, {"on_hold": 1, "hold_type": "Payments", "release_date": None})
        post = self._send(answer)
        post.assert_not_called()
        self.assertEqual(self._line(line.name).status, "Failed")
        frappe.db.set_value("Supplier", self.supplier, "on_hold", 0)
        frappe.set_user(PREPARER)
        try:
            schedule.plan_again(line.name)
        finally:
            frappe.set_user("Administrator")
        self.assertEqual((self._line(line.name).status, self._line(line.name).request), ("Planned", None))
        self.assertEqual(len(self._pay([line])["sent"]), 1)

    # ------------------------------------------------------------ the sweep
    def test_the_sweep_marks_an_interrupted_send_unknown_and_plans_an_unstarted_one_again(self):
        pi = self._invoice(10000)
        doc = self._schedule((3000, pi, 0), (2000, pi, 0))
        answer = self._pay(doc.lines)
        first, second = (s["request"] for s in answer["sent"])
        # the first one's worker died after committing its marker; the second never started
        frappe.db.set_value(B2B, first, "send_started_on", add_to_date(now_datetime(), minutes=-11),
                            update_modified=False)
        frappe.db.set_value(B2B, second, "creation", add_to_date(now_datetime(), minutes=-61), update_modified=False)
        with patch("mpsa_suite.money_out.supplier_accounts._alert") as alert, patch("frappe.enqueue"), \
                patch("mpsa_suite.money_out.b2b.post") as post:
            schedule.sweep()
            b2b.send_deferred(first)
        post.assert_not_called()
        self.assertEqual(frappe.db.get_value(B2B, first, ["status", "error_message"]),
                         ("Unknown", schedule.INTERRUPTED))
        self.assertEqual(self._line(doc.lines[0].name).status, "Unknown")
        self.assertTrue(alert.called)
        self.assertEqual(frappe.db.get_value(B2B, second, "docstatus"), 2)
        state = self._line(doc.lines[1].name)
        self.assertEqual((state.status, state.request), ("Planned", None))
        self.assertIn("was not sent within an hour", state.last_problem)
