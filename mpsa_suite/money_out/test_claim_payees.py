"""Merchants and casual payees paid straight from an Expense Claim (money_out.claim_payees) against
the real database (ERPNext and HRMS, v15 and v16).

A claimant with an expense line of type _T Payee Fuel (Paid to Merchants or Casual Payees by M-Pesa
ticked) pays a till, a paybill, a bank account (through a bank paybill of its own) and casual
payees' phones from the claim. Named users only: the claim's expense approver approves and submits
it, an HR User (a preparer) presses Pay merchants and payees, and a named M-Pesa Payer sends each
payout with Send via M-Pesa (send_confirm.send_for_test). Nothing reaches Safaricom: B2B post()
and the B2C connector are replaced, and Results are handed over the way Safaricom posts them. Each
paid payout is booked by money_out.employee_documents as a Payment Entry to the claimant against
the claim (the claim's payable account, its project), so HRMS's reimbursed total follows. Same
rules as test_b2b_documents: the shortcode TESTB2BDOCS (B2B and B2C), commits made by the code
under test replaced, each test rolled back. Unit coverage: tests/unit/test_claim_payees.py.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import b2b, claim_payees, employee_documents, owed, send_confirm
from mpsa_suite.money_out.test_b2b_documents import _account, _settings

SETTINGS = "TESTB2BDOCS"
CLAIM, LINE, TYPE = "Expense Claim", "M-Pesa Claim Payee", "Expense Claim Type"
B2B, DISB, ROW, REQ, PE = (b2b.B2B_DOCTYPE, "B2C Payment Disbursement", "B2C Payment Disbursement Reference",
                           "B2C Disbursement Request", "Payment Entry")
FUEL, TRAVEL = "_T Payee Fuel", "_T Payee Travel"
# M-Pesa Bank Paybill codes are letters, digits and dashes (money_out.bank_paybills.validate_row), and
# its paybill is on no other row: not a shipped bank's, nor the one test_payee_accounts moves KCB to
BANK, BANK_PAYBILL_NUMBER = "T-PAYEE-BANK", "599811"
APPROVER, PREPARER, PAYER = ("_t_cp_approver@example.com", "_t_cp_preparer@example.com",
                             "_t_cp_payer@example.com")
CLAIMANT_PHONE, COLLEAGUE_PHONE = "0722000771", "0722000772"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, payout_lines_from=None, allow_one_off_b2b=0,
                    entry_reminder_days=3, schedule_maker_checker=1)
PAYEE_SETTINGS = dict(allow_claim_payees=1, claim_payee_max_amount=0, claim_payee_daily_cap=0,
                      suggest_supplier_after=3)


def _user(email: str, *roles) -> str:
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0].strip("_"),
                        "send_welcome_email": 0, "user_type": "System User"}).insert(ignore_permissions=True)
    user = frappe.get_doc("User", email)
    missing = [r for r in roles if r not in {row.role for row in user.roles}]
    if missing:
        user.flags.ignore_permissions = True
        user.add_roles(*missing)
    return email


@contextmanager
def _as(user):
    frappe.set_user(user)
    try:
        yield
    finally:
        frappe.set_user("Administrator")


def _employee(first_name: str, phone: str) -> str:
    name = frappe.db.get_value("Employee", {"first_name": first_name, "company": t.COMPANY}, "name")
    if not name:
        if not frappe.db.exists("Gender", "Female"):
            frappe.get_doc({"doctype": "Gender", "gender": "Female"}).insert()
        name = frappe.get_doc({"doctype": "Employee", "first_name": first_name, "gender": "Female",
                               "date_of_birth": "1990-01-01", "date_of_joining": "2020-01-01", "company": t.COMPANY,
                               "status": "Active", "cell_number": phone, "expense_approver": APPROVER}).insert().name
    frappe.db.set_value("Employee", name, {"cell_number": phone, "expense_approver": APPROVER})
    return name


def _claim_type(name: str, allow: int, expense_account: str) -> str:
    if not frappe.db.exists(TYPE, name):
        frappe.get_doc({"doctype": TYPE, "expense_type": name,
                        "accounts": [{"company": t.COMPANY, "default_account": expense_account}]}).insert()
    frappe.db.set_value(TYPE, name, "allow_mpesa_payees", allow)
    return name


def _payable() -> str:
    name = _account("_T Payee Claims Payable", "Liability", "Balance Sheet")
    frappe.db.set_value("Account", name, "account_type", "Payable")
    return name


def _bank_paybill() -> str:
    """A bank paybill of the tests' own (the shipped list is not relied on)."""
    if not frappe.db.exists("M-Pesa Bank Paybill", BANK):
        doc = frappe.get_doc({"doctype": "M-Pesa Bank Paybill", "code": BANK, "institution_name": "_T Payee Bank",
                              "institution_type": "Bank", "paybill": BANK_PAYBILL_NUMBER,
                              "receiver_name": "_T Bank Paybill AC", "enabled": 1})
        doc.flags.mpsa_seed = True
        doc.insert(ignore_permissions=True)
    return BANK


class TestClaimPayees(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        _settings()
        _user(APPROVER, "Expense Approver")
        _user(PREPARER, "HR User")
        _user(PAYER, "M-Pesa Payer")
        cls.manager = t.manager()  # cancels a request that may have paid, having checked the statement
        cls.claimant = _employee("_T Payee Claimant", CLAIMANT_PHONE)
        cls.colleague = _employee("_T Payee Colleague", COLLEAGUE_PHONE)
        expense = _account("_T Payee Fuel Expense", "Expense", "Profit and Loss")
        _claim_type(FUEL, 1, expense)
        _claim_type(TRAVEL, 0, expense)
        cls.payable = _payable()
        cls.cost_center = frappe.db.get_value("Company", t.COMPANY, "cost_center")
        _bank_paybill()
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        suite = frappe.get_single("M-Pesa Suite Settings")
        suite.update(PAYEE_SETTINGS)
        suite.flags.ignore_permissions = True
        suite.save()
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        t.forget_roles(self.manager)  # a test gives the manager a role that reads claims
        frappe.local.mpesa_suite_bank_accounts = None
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")

    # ------------------------------------------------------------ helpers
    def _claim(self, payees, amount=10000, expense_type=FUEL, submit=True, **values):
        doc = frappe.get_doc({
            "doctype": CLAIM, "employee": self.claimant, "company": t.COMPANY, "posting_date": nowdate(),
            "expense_approver": APPROVER, "approval_status": "Approved", "payable_account": self.payable,
            "cost_center": self.cost_center,  # the desk fills the rows' cost center; HRMS requires it
            "expenses": [{"expense_type": expense_type, "expense_date": nowdate(), "amount": amount,
                          "sanctioned_amount": amount, "cost_center": self.cost_center}],
            "mpesa_payees": payees, **values})
        doc.insert()
        if submit:
            with _as(APPROVER):
                doc.submit()
        return frappe.get_doc(CLAIM, doc.name)

    def _lines(self, claim) -> dict:
        return {r.name: r for r in frappe.get_all(LINE, filters={"parent": claim}, fields=["*"], order_by="idx")}

    def _pay(self, claim, method="pay_payees") -> dict:
        with _as(PREPARER):
            return getattr(claim_payees, method)(claim, b2b_settings=SETTINGS, b2c_settings=SETTINGS)

    def _ack(self, doc=None, *a, **kw):
        receipt = frappe.generate_hash(length=10)
        return {"ResponseCode": "0", "ResponseDescription": "Accept the service request successfully.",
                "ConversationID": f"AG_{receipt}", "OriginatorConversationID": f"OC_{receipt}"}, None

    def _send_b2b(self, name):
        with patch("mpsa_suite.money_out.b2b.post", side_effect=self._ack), _as(PAYER):
            send_confirm.send_for_test(frappe.get_doc(B2B, name))
        return frappe.get_doc(B2B, name)

    def _b2b_result(self, name, receiver, paid=True):
        row = frappe.db.get_value(B2B, name, ["conversation_id", "originator_conversation_id"], as_dict=True)
        receipt = "T" + frappe.generate_hash(length=9).upper()
        parameters = [{"Key": "DebitAccountBalance", "Value": "Working Account|KES|50000.00|50000.00|0.00|0.00"},
                      {"Key": "TransCompletedTime", "Value": now_datetime().strftime("%Y%m%d%H%M%S")},
                      {"Key": "DebitPartyCharges", "Value": "Business Pay Bill Charge|KES|2.00"}]
        if receiver:
            parameters.append({"Key": "ReceiverPartyPublicName", "Value": receiver})
        result = {"ResultType": 0, "ResultCode": 0 if paid else 2001,
                  "ResultDesc": "The service request is processed successfully." if paid else "Invalid initiator",
                  "ConversationID": row.conversation_id, "OriginatorConversationID": row.originator_conversation_id,
                  "TransactionID": receipt, "ResultParameters": {"ResultParameter": parameters}}
        with patch("frappe.enqueue"):
            b2b.handle_result(result, name)
        if paid:
            b2b.book_payment_entry(name)
        return receipt

    def _send_b2c(self, name, results, record: bool = True) -> list:
        """Send via M-Pesa on the batch as the payer, then its payouts made and sent (the connector
        replaced) and each Result written as the B2C callback writes it: ``results`` by phone.
        ``record`` False: the Results are on the requests, but not yet on the rows or booked."""
        from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import b2c_payment_disbursement as disb
        from mpsa_suite.services import b2c_response_service

        with patch("frappe.enqueue") as enqueue, _as(PAYER):
            send_confirm.send_for_test(frappe.get_doc(DISB, name))
        jobs = [c for c in enqueue.call_args_list if c.args and c.args[0].endswith("create_b2c_request")]

        def ack(doc, callback_url):
            frappe.db.set_value(REQ, doc.name, {"status": "Initiated", "conversation_id": f"AG_{doc.name}"})
            return {"ResponseCode": "0"}

        connector = type("Connector", (), {"send_b2c_request": staticmethod(ack)})()
        requests = []
        with _as(PAYER), patch("mpsa_suite.services.b2c_request_service.get_b2c_connector", return_value=connector), \
                patch("frappe.enqueue"):
            for job in jobs:
                disb.create_b2c_request(job.kwargs["ref"], job.kwargs["settings"], job.kwargs["b2c_disbursement"])
                requests.append(frappe.db.get_value(ROW, job.kwargs["ref"].name, "b2c_disbursement_request"))
        for request in requests:
            phone = frappe.db.get_value(REQ, request, "phone_number")
            outcome, receiver = results[phone]
            values = {"status": outcome, "result_code": "0" if outcome == "Paid" else "2040",
                      "result_desc": "ok" if outcome == "Paid" else "The recipient is not registered."}
            if outcome == "Paid":
                values.update(transaction_id="T" + frappe.generate_hash(length=9).upper(),
                              transaction_completed_datetime=now_datetime(), receiver_public_name=receiver,
                              utility_acct_avlbl_funds=400000)
            frappe.db.set_value(REQ, request, values)
            if record:
                with patch("frappe.enqueue"):
                    b2c_response_service.update_b2c_reference_status(request, enqueue_next=True)
        return requests

    def _entries(self, claim) -> list:
        """Submitted Payment Entries against the claim: (party type, party, allocated)."""
        return sorted(
            (pe.party_type, pe.party, flt(ref.allocated_amount))
            for ref in frappe.get_all("Payment Entry Reference", filters={"reference_doctype": CLAIM,
                                                                          "reference_name": claim, "docstatus": 1},
                                      fields=["parent", "allocated_amount"])
            for pe in [frappe.db.get_value(PE, ref.parent, ["party_type", "party"], as_dict=True)]
        )

    # ------------------------------------------------------------ merchants by B2B
    def test_a_till_paybill_and_bank_account_are_paid_and_the_claim_is_reimbursed(self):
        claim = self._claim([
            {"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell Kilimani", "shortcode": "835811", "amount": 2000},
            {"payee_type": "Paybill", "payee_name": "_T Nairobi County", "shortcode": "647647",
             "account_number": "park-77", "amount": 1500},
            {"payee_type": "Bank Account (via Bank Paybill)", "payee_name": "_T Mama Mboga Ltd", "bank_paybill": BANK,
             "account_number": "0123 456 789", "amount": 3000},
        ])
        lines = list(self._lines(claim.name).values())
        self.assertEqual([(line.shortcode, line.account_number) for line in lines],
                         [("835811", None), ("647647", "park-77"), (BANK_PAYBILL_NUMBER, "0123456789")])
        made = self._pay(claim.name)
        self.assertEqual((len(made["b2b"]), made["b2c"], made["total"]), (3, None, 6500))
        for name in made["b2b"]:
            request = self._send_b2b(name)
            self.assertEqual((request.docstatus, request.status, request.employee, request.expense_claim),
                             (1, "Initiated", self.claimant, claim.name))
        names = dict(zip(made["b2b"], ("835811 - _T SHELL KILIMANI", "647647 - _T NAIROBI COUNTY",
                                       f"{BANK_PAYBILL_NUMBER} - _T Bank Paybill AC")))
        for name, receiver in names.items():
            self._b2b_result(name, receiver)
        for line in self._lines(claim.name).values():
            # R4.1: a bank account is never verified by name (M-Pesa shows the bank's, not the holder's)
            expected = (0, "Not Verified") if line.payee_type == "Bank Account (via Bank Paybill)" else (1, "Matches")
            self.assertEqual((line.status, line.name_matches, line.name_verdict), ("Paid", *expected), line.name_note)
        # booked as a reimbursement of the claim, whoever received the money
        self.assertEqual(self._entries(claim.name), [("Employee", self.claimant, 1500), ("Employee", self.claimant, 2000),
                                                     ("Employee", self.claimant, 3000)])
        self.assertEqual(flt(frappe.db.get_value(CLAIM, claim.name, "total_amount_reimbursed")), 6500)

    def test_a_tills_name_is_checked_when_safaricom_gives_it_later_and_a_mismatch_is_flagged(self):
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell Kilimani",
                              "shortcode": "835811", "amount": 2000}])
        name = self._pay(claim.name)["b2b"][0]
        self._send_b2b(name)
        self._b2b_result(name, receiver=None)  # a Buy Goods Result names no one
        line = next(iter(self._lines(claim.name).values()))
        self.assertTrue(line.name_note.startswith("Name not checked (till)"))
        frappe.db.set_value(B2B, name, "receiver_public_name", "835811 - SOMEONE ELSE LTD")
        claim_payees.on_payout_result(B2B, name)  # what money_out.b2b_status does with the answer
        line = next(iter(self._lines(claim.name).values()))
        self.assertEqual(line.name_matches, 0)
        self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": CLAIM, "reference_name": claim.name,
                                                     "content": ["like", "%SOMEONE ELSE%"]}))

    def test_the_preparer_cannot_send_and_the_payer_sends_only_the_lines_destination(self):
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                              "amount": 2000}])
        name = self._pay(claim.name)["b2b"][0]
        _user(PREPARER, "M-Pesa Payer")  # even a preparer who may pay: maker-checker
        try:
            with _as(PREPARER):
                self.assertFalse(send_confirm.prepare_send(B2B, name)["token"])
        finally:
            frappe.get_doc("User", PREPARER).remove_roles("M-Pesa Payer")
        frappe.db.set_value(B2B, name, "receiver_shortcode", "835899")  # changed behind the form's back
        with patch("mpsa_suite.money_out.b2b.post", side_effect=self._ack) as post, _as(PAYER):
            self.assertRaisesRegex(frappe.ValidationError, "is not payee row #1's", send_confirm.send_for_test,
                                   frappe.get_doc(B2B, name))
        post.assert_not_called()

    def test_a_line_is_never_paid_twice_and_a_failed_one_is_retried(self):
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                              "amount": 2000}])
        first = self._pay(claim.name)["b2b"][0]
        self.assertRaisesRegex(frappe.ValidationError, "Nothing to pay", self._pay, claim.name)
        self._send_b2b(first)
        self._b2b_result(first, None, paid=False)
        line = next(iter(self._lines(claim.name).values()))
        self.assertEqual(line.status, "Failed")
        second = self._pay(claim.name, "retry_payees")["b2b"][0]
        self.assertNotEqual(first, second)
        # a copy made while the retry is still a draft: whichever is sent first takes the line
        copy = frappe.copy_doc(frappe.get_doc(B2B, second))
        copy.insert(ignore_permissions=True)
        self._send_b2b(second)
        with patch("mpsa_suite.money_out.b2b.post", side_effect=self._ack) as post, _as(PAYER):
            self.assertRaisesRegex(frappe.ValidationError, "never paid twice", send_confirm.send_for_test,
                                   frappe.get_doc(B2B, copy.name))
        post.assert_not_called()
        # and no new draft copy is saved while the line is on its way. In a bench test frappe.copy_doc keeps
        # docstatus (in_test), and a copy inserted as submitted would be a submit: make it the draft Duplicate makes.
        again = frappe.copy_doc(frappe.get_doc(B2B, second), ignore_no_copy=False)
        again.docstatus = 0
        again.status = "Not Initiated"
        self.assertRaisesRegex(frappe.ValidationError, "never paid twice", again.insert, ignore_permissions=True)

    def test_a_project_claim_books_its_project(self):
        project = frappe.db.get_value("Project", {"project_name": "_T Payee Project"}, "name") or frappe.get_doc(
            {"doctype": "Project", "project_name": "_T Payee Project", "company": t.COMPANY}).insert().name
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                              "amount": 2000}], project=project)
        name = self._pay(claim.name)["b2b"][0]
        self._send_b2b(name)
        self._b2b_result(name, "835811 - _T SHELL")
        entry = frappe.db.get_value("Payment Entry Reference", {"reference_name": claim.name, "docstatus": 1}, "parent")
        self.assertEqual(frappe.db.get_value(PE, entry, "project"), project)

    # ------------------------------------------------------------ casual payees by B2C
    def test_a_casual_batch_is_one_b2c_payout_per_phone_and_a_failed_phone_is_retried(self):
        claim = self._claim([
            {"payee_type": "Phone (B2C)", "payee_name": "_T Jane Wanjiku", "phone": "0711000331", "amount": 1500},
            {"payee_type": "Phone (B2C)", "payee_name": "_T John Otieno", "phone": "0711000332", "amount": 1200},
            {"payee_type": "Phone (B2C)", "payee_name": "_T Mary Akinyi", "phone": "0711000333", "amount": 800},
        ], amount=5000)
        made = self._pay(claim.name)
        self.assertEqual((made["b2b"], made["lines"], made["total"]), ([], 3, 3500))
        rows = frappe.get_all(ROW, filters={"parent": made["b2c"]}, fields=["claim_payee", "partyb", "party"],
                              order_by="idx")
        self.assertEqual([(r.partyb, r.party) for r in rows], [("254711000331", self.claimant),
                                                               ("254711000332", self.claimant),
                                                               ("254711000333", self.claimant)])
        self._send_b2c(made["b2c"], {
            "254711000331": ("Paid", "254711000331 - JANE WANJIKU"),
            "254711000332": ("Failed", None),
            "254711000333": ("Paid", "254711000333 - SOMEBODY ELSE"),
        })
        lines = {line.phone: line for line in self._lines(claim.name).values()}
        self.assertEqual([lines[p].status for p in sorted(lines)], ["Paid", "Failed", "Paid"])
        self.assertEqual((lines["254711000331"].name_matches, lines["254711000333"].name_matches), (1, 0))
        self.assertEqual(self._entries(claim.name), [("Employee", self.claimant, 800),
                                                     ("Employee", self.claimant, 1500)])
        retry = self._pay(claim.name, "retry_payees")
        self.assertEqual([r.partyb for r in frappe.get_all(ROW, filters={"parent": retry["b2c"]}, fields=["partyb"])],
                         ["254711000332"])

    def test_a_failed_payees_old_payout_is_never_retried_once_the_line_is_paid_again(self):
        claim = self._claim([{"payee_type": "Phone (B2C)", "payee_name": "_T Jane Wanjiku", "phone": "0711000351",
                              "amount": 1000}], amount=3000)
        old = self._send_b2c(self._pay(claim.name)["b2c"], {"254711000351": ("Failed", None)})[0]
        retry = self._pay(claim.name, "retry_payees")["b2c"]
        self._send_b2c(retry, {"254711000351": ("Paid", "254711000351 - JANE WANJIKU")})
        self.assertEqual(self._entries(claim.name), [("Employee", self.claimant, 1000)])
        # the first batch's failed row is still there: its own retry is refused, never a second payment
        module = "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request"
        with patch(f"{module}.make_b2c_payment_request") as send, _as(PAYER):
            self.assertRaisesRegex(frappe.ValidationError, "pays now: retry the payee from its claim",
                                   frappe.get_doc(REQ, old).retry_failed_payment)
        send.assert_not_called()
        self.assertEqual(frappe.db.get_value(REQ, old, "status"), "Failed")

    def test_a_mixed_claims_phones_are_paid_and_retried_while_its_merchants_request_waits(self):
        claim = self._claim([
            {"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell Kilimani", "shortcode": "835811",
             "amount": 2000},
            {"payee_type": "Phone (B2C)", "payee_name": "_T Jane Wanjiku", "phone": "0711000361", "amount": 1500},
            {"payee_type": "Phone (B2C)", "payee_name": "_T John Otieno", "phone": "0711000362", "amount": 800},
        ], amount=5000)
        made = self._pay(claim.name)
        till = made["b2b"][0]
        # the till's request is still a draft: the casual workers are paid all the same (B2C's guard
        # takes it off what the claim owes instead of refusing: claim_payees.others_taken_off)
        self._send_b2c(made["b2c"], {"254711000361": ("Paid", "254711000361 - JANE WANJIKU"),
                                     "254711000362": ("Failed", None)})
        retry = self._pay(claim.name, "retry_payees")
        self.assertEqual(retry["b2b"], [])
        self._send_b2c(retry["b2c"], {"254711000362": ("Paid", "254711000362 - JOHN OTIENO")})
        self._send_b2b(till)
        self._b2b_result(till, "835811 - _T SHELL KILIMANI")
        self.assertEqual([line.status for line in self._lines(claim.name).values()], ["Paid", "Paid", "Paid"])
        self.assertEqual(flt(frappe.db.get_value(CLAIM, claim.name, "total_amount_reimbursed")), 4300)

    def test_the_list_pays_the_casual_payees_of_two_claims_in_one_batch(self):
        one = self._claim([{"payee_type": "Phone (B2C)", "payee_name": "_T Jane", "phone": "0711000341",
                            "amount": 1000}])
        two = self._claim([{"payee_type": "Phone (B2C)", "payee_name": "_T John", "phone": "0711000342",
                            "amount": 700}])
        with _as(PREPARER):
            made = claim_payees.pay_claims([one.name, two.name], SETTINGS)
        rows = frappe.get_all(ROW, filters={"parent": made["b2c"]}, fields=["reference_name"], order_by="idx")
        self.assertEqual(sorted(r.reference_name for r in rows), sorted([one.name, two.name]))

    def test_an_amended_claims_payees_start_as_drafts_on_its_own_expense_line(self):
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                              "amount": 2000}])
        frappe.get_doc(CLAIM, claim.name).cancel()  # no payout yet: its line becomes Cancelled
        cancelled = frappe.get_doc(CLAIM, claim.name)
        self.assertEqual(cancelled.mpesa_payees[0].status, "Cancelled")
        # as the form's Amend copies it: every field of every row, its expense line's name too
        amended = frappe.copy_doc(cancelled)
        amended.update({"amended_from": claim.name, "docstatus": 0})
        amended.insert()
        line = amended.mpesa_payees[0]
        self.assertEqual((line.status, line.payout_doctype, line.payout_name), ("Draft", None, None))
        self.assertEqual(line.expense_detail, amended.expenses[0].name)

    # ------------------------------------------------------------ the payees' share is never the employee's
    def test_a_claims_unpaid_payees_are_never_offered_to_its_employee(self):
        # final review: the claim's approved merchants and casual payees not paid yet hold their share of what
        # it owes; Pay employee, the headline and every check on a payout to the employee leave it to them
        claim = self._claim([
            {"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811", "amount": 3000},
            {"payee_type": "Phone (B2C)", "payee_name": "_T Jane Wanjiku", "phone": "0711000381", "amount": 2000},
        ], amount=10000)
        with _as(PREPARER):
            status = employee_documents.document_status(CLAIM, claim.name)
        self.assertEqual((status["owed"], status["left"], status["payees"]), (10000, 5000, 5000))
        made = self._pay(claim.name)  # the till's draft request is counted; the phones' draft batch is not yet
        left = owed.employee_doc_left_to_pay(CLAIM, claim.name, for_update=False)
        self.assertEqual(([p.kind for p in left.parts], left.left), (["b2b", "payee"], 5000))
        self._send_b2c(made["b2c"], {"254711000381": ("Paid", "254711000381 - JANE WANJIKU")})
        left = owed.employee_doc_left_to_pay(CLAIM, claim.name, for_update=False)
        self.assertEqual((left.owed, [p.kind for p in left.parts], left.left), (8000, ["b2b"], 5000))

    # ------------------------------------------------------------ cancel and amend
    def test_a_claim_whose_payee_payouts_failed_or_were_deleted_is_cancelled_and_amended(self):
        # final review: a payee line names its payout by a plain name (a link from the submitted claim's line
        # stopped the payout being deleted or cancelled), and failed payouts and drafts no longer hold the
        # claim's cancel, the only way to correct a mistyped phone or till
        claim = self._claim([
            {"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811", "amount": 2000},
            {"payee_type": "Paybill", "payee_name": "_T Nairobi County", "shortcode": "647647",
             "account_number": "park-77", "amount": 1500},
            {"payee_type": "Phone (B2C)", "payee_name": "_T Jane Wanjiku", "phone": "0711000391", "amount": 1000},
        ], amount=5000)
        made = self._pay(claim.name)
        till, paybill = made["b2b"]
        frappe.delete_doc(B2B, paybill)  # a draft its line names is deleted (no "is linked with Expense Claim")
        self._send_b2b(till)
        self._b2b_result(till, None, paid=False)
        self._send_b2c(made["b2c"], {"254711000391": ("Failed", None)})  # the phone was mistyped
        frappe.get_doc(CLAIM, claim.name).cancel()  # not "linked with B2C Disbursement Request ..."
        cancelled = frappe.get_doc(CLAIM, claim.name)
        self.assertEqual([line.status for line in cancelled.mpesa_payees], ["Cancelled"] * 3)
        amended = frappe.copy_doc(cancelled)
        amended.update({"amended_from": claim.name, "docstatus": 0})
        amended.mpesa_payees[2].phone = "0711000392"  # corrected
        amended.insert()
        self.assertEqual([(line.status, line.payout_doctype, line.payout_name) for line in amended.mpesa_payees],
                         [("Draft", None, None)] * 3)
        # the old failed payout is never sent again: its claim is cancelled
        failed = frappe.db.get_value(ROW, {"parent": made["b2c"]}, "b2c_disbursement_request")
        module = "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request"
        with patch(f"{module}.make_b2c_payment_request") as send, _as(PAYER):
            self.assertRaises(frappe.ValidationError, frappe.get_doc(REQ, failed).retry_failed_payment)
        send.assert_not_called()

    def test_a_manager_cancels_a_payee_lines_unknown_request(self):
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                              "amount": 2000}])
        name = self._pay(claim.name)["b2b"][0]
        self._send_b2b(name)
        frappe.db.set_value(B2B, name, "status", "Unknown")  # no answer from M-Pesa
        with _as(self.manager):  # having checked the statement
            frappe.get_doc(B2B, name).cancel()  # not "linked with Expense Claim ... at Row: 1"
        self.assertEqual(frappe.db.get_value(B2B, name, "docstatus"), 2)
        claim_payees.on_payout_result(B2B, name)
        # it may still have paid: its line stays on its way, and its money is not offered to the employee
        line = next(iter(self._lines(claim.name).values()))
        self.assertEqual(line.status, "In Flight")
        self.assertEqual(owed.employee_doc_left_to_pay(CLAIM, claim.name, for_update=False).left, 8000)
        # verify (K13): nor is the claim cancelled and amended meanwhile, which would pay the till again
        self.assertRaisesRegex(frappe.ValidationError, f"cannot be cancelled while M-Pesa payouts of it are paid or on "
                                                       f"their way: {name}", frappe.get_doc(CLAIM, claim.name).cancel)
        # M-Pesa's answer since (a status query or a late Result) says it failed: the claim may be cancelled
        frappe.db.set_value(B2B, name, "status", "Failed")
        frappe.get_doc(CLAIM, claim.name).cancel()
        self.assertEqual(frappe.db.get_value(CLAIM, claim.name, "docstatus"), 2)

    def test_a_manager_settles_a_line_paid_outside_mpesa_and_its_money_goes_back_to_the_employee(self):
        # verify (K2): a merchant line paid another way (by bank while the till was down) no longer holds the
        # employee's share; M-Pesa never pays it after, and a line on its way is never settled
        claim = self._claim([
            {"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811", "amount": 2000},
            {"payee_type": "Paybill", "payee_name": "_T Nairobi County", "shortcode": "647647",
             "account_number": "park-77", "amount": 1500},
        ], amount=5000)
        till, paybill = self._lines(claim.name)
        made = self._pay(claim.name)
        frappe.delete_doc(B2B, made["b2b"][1])  # the paybill's draft: it is paid by bank instead
        self.assertEqual(owed.employee_doc_left_to_pay(CLAIM, claim.name, for_update=False).left, 1500)
        _user(self.manager, "HR User")  # reads the claim
        with _as(PAYER):
            self.assertRaises(frappe.PermissionError, claim_payees.settle_line, claim.name, paybill, "Bank ACC-PAY-T1")
        with _as(self.manager):
            self.assertRaisesRegex(frappe.ValidationError, "is Queued", claim_payees.settle_line, claim.name, till,
                                   "Bank ACC-PAY-T1")
            claim_payees.settle_line(claim.name, paybill, "Bank ACC-PAY-T1")
        line = self._lines(claim.name)[paybill]
        self.assertEqual((line.status, line.error_message),
                         ("Cancelled", f"Settled outside M-Pesa by {self.manager}: Bank ACC-PAY-T1"))
        self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": CLAIM, "reference_name": claim.name,
                                                     "content": ["like", "%settled outside M-Pesa%ACC-PAY-T1%"]}))
        # its 1,500 is the employee's again; the till's draft still holds its 2,000
        self.assertEqual(owed.employee_doc_left_to_pay(CLAIM, claim.name, for_update=False).left, 3000)
        claim_payees.sync_lines(claim.name)
        self.assertEqual(self._lines(claim.name)[paybill].status, "Cancelled")
        self.assertRaisesRegex(frappe.ValidationError, "Nothing to pay", self._pay, claim.name)
        self._send_b2b(made["b2b"][0])
        with _as(self.manager):
            self.assertRaisesRegex(frappe.ValidationError, "is In Flight", claim_payees.settle_line, claim.name, till,
                                   "Bank ACC-PAY-T2")

    def test_the_forms_cancel_lists_no_failed_mpesa_payout_of_the_claim(self):
        # verify (K14): Frappe's Cancel All dialog lists every submitted document that links the claim, and could
        # cancel none of a failed payout's (its request and its disbursement link each other). The form leaves
        # the M-Pesa payouts out (public/js/expense_claim.js), as these DocTypes; the server refuses the cancel
        # while one is paid or on its way.
        from frappe.desk.form.linked_with import get_submitted_linked_docs

        claim = self._claim([{"payee_type": "Phone (B2C)", "payee_name": "_T Jane Wanjiku", "phone": "0711000396",
                              "amount": 1000}], amount=3000)
        self._send_b2c(self._pay(claim.name)["b2c"], {"254711000396": ("Failed", None)})
        payouts = {DISB, REQ, B2B}
        listed = {d["doctype"] for d in get_submitted_linked_docs(CLAIM, claim.name)["docs"]}
        self.assertTrue(listed & payouts)
        listed = {d["doctype"] for d in get_submitted_linked_docs(CLAIM, claim.name, sorted(payouts))["docs"]}
        self.assertFalse(listed & payouts)
        frappe.get_doc(CLAIM, claim.name).cancel()  # what the form's Cancel then does

    # ------------------------------------------------------------ what a claim may not pay
    def test_an_employees_phone_and_a_type_without_payees_are_refused(self):
        self.assertRaisesRegex(frappe.ValidationError, "an employee's phone", self._claim,
                               [{"payee_type": "Phone (B2C)", "payee_name": "_T Friend", "phone": COLLEAGUE_PHONE,
                                 "amount": 500}], submit=False)
        self.assertRaisesRegex(frappe.ValidationError, "the claimant's own phone", self._claim,
                               [{"payee_type": "Phone (B2C)", "payee_name": "_T Me", "phone": CLAIMANT_PHONE,
                                 "amount": 500}], submit=False)
        self.assertRaisesRegex(frappe.ValidationError, "is not paid to merchants or payees", self._claim,
                               [{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                                 "amount": 500}], expense_type=TRAVEL, submit=False)

    def test_payees_never_add_up_to_more_than_the_claim_and_the_caps_apply_when_set(self):
        self.assertRaisesRegex(frappe.ValidationError, "more than the line's", self._claim,
                               [{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                                 "amount": 12000}], submit=False)
        frappe.db.set_single_value("M-Pesa Suite Settings", "claim_payee_max_amount", 1000)
        self.assertRaisesRegex(frappe.ValidationError, "more than may be paid at once", self._claim,
                               [{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                                 "amount": 2000}], submit=False)

    def test_payees_are_refused_while_the_suite_switch_is_off(self):
        frappe.db.set_single_value("M-Pesa Suite Settings", "allow_claim_payees", 0)
        self.assertRaisesRegex(frappe.ValidationError, "switched off", self._claim,
                               [{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                                 "amount": 500}], submit=False)

    # ------------------------------------------------------------ round 4.1 (Trident's live test, 2026-10-28)
    def test_two_payouts_of_one_claim_booked_at_once_in_their_own_transactions_both_count(self):
        """HR-EXP-2026-00712: two B2C rows of one batch booked 2 s apart left Total Amount Reimbursed at 70 of
        90; HRMS sums the claim's entries with a plain read, which missed the other booking. Each booking now
        takes the claim's lock as its first statement (owed.booking_lock). Two connections book the rows,
        each in its own transaction, the first holding its open while the second starts; both are committed
        for real, then everything this test made is deleted."""
        import threading
        import time

        from hrms.hr.doctype.expense_claim import expense_claim as hrms_claim

        from mpsa_suite.services import b2c_response_service

        claim = self._claim([
            {"payee_type": "Phone (B2C)", "payee_name": "_T Race Achieng", "phone": "0711000901", "amount": 2000},
            {"payee_type": "Phone (B2C)", "payee_name": "_T Race Barasa", "phone": "0711000902", "amount": 7000},
        ])
        made = self._pay(claim.name)
        requests = self._send_b2c(made["b2c"], {"254711000901": ("Paid", "254711000901 - _T RACE ACHIENG"),
                                                "254711000902": ("Paid", "254711000902 - _T RACE BARASA")},
                                  record=False)
        self.addCleanup(self._delete_committed, [claim.name, made["b2c"], *requests])
        self._no_commit.stop()
        frappe.db.commit()  # a second connection sees the claim, the batch and its paid requests

        site, sites_path, errors, inside = frappe.local.site, frappe.local.sites_path, [], threading.Event()
        original = hrms_claim.update_reimbursed_amount

        def slow(doc):
            original(doc)
            if threading.current_thread().name == "booking-1":
                inside.set()
                time.sleep(2)  # the first booking keeps its transaction open meanwhile

        def booking(request, name):
            def run():
                try:
                    frappe.init(site=site, sites_path=sites_path)
                    frappe.connect()
                    b2c_response_service.update_b2c_reference_status(request, enqueue_next=True)
                    frappe.db.commit()
                except Exception:
                    errors.append(frappe.get_traceback())
                finally:
                    frappe.destroy()

            return threading.Thread(target=run, name=name)

        with patch.object(hrms_claim, "update_reimbursed_amount", slow), patch("frappe.enqueue"), \
                patch("mpsa_suite.money_out.statement.after_payout"):
            first, second = booking(requests[0], "booking-1"), booking(requests[1], "booking-2")
            first.start()
            self.assertTrue(inside.wait(120), errors)
            second.start()  # starts while the first is booked but not committed
            first.join(180)
            second.join(180)
        self._no_commit.start()
        self.assertEqual(errors, [])
        frappe.db.rollback()  # nothing of this connection is pending: read what the two committed
        self.assertEqual(self._entries(claim.name), [("Employee", self.claimant, 2000),
                                                     ("Employee", self.claimant, 7000)])
        self.assertEqual(flt(frappe.db.get_value(CLAIM, claim.name, "total_amount_reimbursed")), 9000)

    def _delete_committed(self, names):
        """Delete what a test committed: ``names`` (the claim, its batch and payouts), the Payment
        Entries booked on the claim, and every row of any table that names one of them (their child
        rows, GL and Payment Ledger Entries, Comments, Versions, Error Logs, shares, emails)."""
        frappe.db.rollback()
        claims = [n for n in names if frappe.db.exists(CLAIM, n)]
        entries = frappe.get_all("Payment Entry Reference", filters={"reference_name": ["in", claims]},
                                 pluck="parent") if claims else []
        names = tuple(sorted({*names, *entries}))
        columns = ("name", "parent", "reference_name", "docname", "voucher_no", "against_voucher",
                   "against_voucher_no", "share_name", "document_name", "ref_docname")
        for table, column in frappe.db.sql(
                """SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = DATABASE()
                AND table_name LIKE 'tab%%' AND column_name IN %s""", (columns,)):
            frappe.db.sql(f"DELETE FROM `{table}` WHERE `{column}` IN %s", (names,))
        frappe.db.commit()

    def test_a_claim_whose_reimbursed_total_disagrees_is_not_paid_until_it_is_set_right(self):
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835811",
                              "amount": 2000}])
        frappe.db.set_value(CLAIM, claim.name, "total_amount_reimbursed", 500)  # as a lost update leaves it
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "Total Amount Reimbursed shows KES 500, but the entries "
                                   "booked against it add up to KES 0", self._pay, claim.name)
            self.assertRaisesRegex(frappe.ValidationError, "Total Amount Reimbursed shows KES 500",
                                   employee_documents.prepare_b2c, CLAIM, claim.name, SETTINGS)
        employee_documents.check_paid_total(CLAIM, claim.name)  # the job each payment of the claim queues
        self.assertEqual(flt(frappe.db.get_value(CLAIM, claim.name, "total_amount_reimbursed")), 0)
        self.assertTrue(frappe.db.exists("Comment", {"reference_name": claim.name,
                                                     "content": ["like", "Total Amount Reimbursed set from KES 500%"]}))
        self.assertTrue(self._pay(claim.name)["b2b"])

    def test_a_till_paid_under_the_claimants_name_is_blocked_until_a_manager_clears_it(self):
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835855",
                              "amount": 2000}])
        name = self._pay(claim.name)["b2b"][0]
        self._send_b2b(name)
        self._b2b_result(name, "835855 - _T PAYEE CLAIMANT")  # the claimant's own till
        line = next(iter(self._lines(claim.name).values()))
        self.assertEqual((line.staff_check, line.sent_by), ("Blocked", PAYER))
        self.assertIn("the name of the claimant", line.staff_note)
        self.assertTrue(frappe.db.exists("Error Log", {"method": "M-Pesa claim payee staff or self payment"}))
        self.assertRaisesRegex(frappe.ValidationError, "835855 is blocked for claim payees", self._claim,
                               [{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835855",
                                 "amount": 500}], submit=False)
        _user(self.manager, "HR User")  # reads the claim
        with _as(self.manager):
            claim_payees.clear_staff_flag(claim.name, line.name, "_T a shop the claimant does not own")
        self.assertEqual(next(iter(self._lines(claim.name).values())).staff_check, "Cleared")
        self.assertTrue(self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell",
                                      "shortcode": "835855", "amount": 500}], submit=False).name)

    def test_a_payee_batch_shows_partially_paid_once_one_row_is_paid(self):
        from mpsa_suite.services import b2c_batch_status

        claim = self._claim([
            {"payee_type": "Phone (B2C)", "payee_name": "_T Batch Wairimu", "phone": "0711000911", "amount": 1500},
            {"payee_type": "Phone (B2C)", "payee_name": "_T Batch Odhiambo", "phone": "0711000912", "amount": 1200},
        ])
        made = self._pay(claim.name)
        self._send_b2c(made["b2c"], {"254711000911": ("Paid", "254711000911 - _T BATCH WAIRIMU"),
                                     "254711000912": ("Failed", None)})
        self.assertEqual(b2c_batch_status.sync_status(made["b2c"]), "Partially Paid")
        self.assertEqual(frappe.db.get_value(DISB, made["b2c"], "status"), "Partially Paid")

    def test_the_80mm_prints_render_for_a_paid_claim_its_payees_and_each_payout(self):
        claim = self._claim([
            {"payee_type": "Till (Buy Goods)", "payee_name": "_T Print Shell", "shortcode": "835866", "amount": 2000},
            {"payee_type": "Phone (B2C)", "payee_name": "_T Print Wanjiru", "phone": "0711000921", "amount": 1500},
        ])
        made = self._pay(claim.name)
        b2b_name = made["b2b"][0]
        self._send_b2b(b2b_name)
        till_receipt = self._b2b_result(b2b_name, "835866 - _T PRINT SHELL")
        (request,) = self._send_b2c(made["b2c"], {"254711000921": ("Paid", "254711000921 - _T PRINT WANJIRU")})
        phone_receipt = frappe.db.get_value(REQ, request, "transaction_id")
        html = t.renders(CLAIM, claim.name, "Expense Claim Payment", "Merchants and Payees", till_receipt,
                         phone_receipt, "2547****0921", "To the claimant", "6,500.00")
        self.assertNotIn("254711000921", html)  # phones masked as the send summary shows them
        t.renders(B2B, b2b_name, f"{till_receipt} Confirmed. KES 2,000.00 paid to _T PRINT SHELL 835866", "_T Print Shell")
        t.renders(DISB, made["b2c"], f"{phone_receipt} Confirmed. KES 1,500.00 sent to _T PRINT WANJIRU 2547****0921")
        t.renders(REQ, request, phone_receipt, "M-Pesa Receipt", "Prepared By")
        entry = frappe.db.get_value(PE, {"reference_no": phone_receipt, "docstatus": 1}, "name")
        t.renders(PE, entry, f"{phone_receipt} Confirmed.")

    # ------------------------------------------------------------ review of 4b2f196
    def test_a_sends_gap_check_waiting_on_another_booking_of_the_claim_is_not_refused(self):
        """Review R5: a send of a claim's payout waits on the claim's lock while another payout of the claim is
        booked. Once it holds the lock, its gap check must read the stored total and the entries as they are
        now: a committed total against entries summed in the send's older snapshot refused it ("an M-Pesa
        Manager repairs it") with nothing to repair. Two connections, each its own committed transaction."""
        import threading
        import time

        from hrms.hr.doctype.expense_claim import expense_claim as hrms_claim

        from mpsa_suite.services import b2c_response_service

        claim = self._claim([
            {"payee_type": "Phone (B2C)", "payee_name": "_T Gap Achieng", "phone": "0711000931", "amount": 2000},
            {"payee_type": "Phone (B2C)", "payee_name": "_T Gap Barasa", "phone": "0711000932", "amount": 7000},
        ])
        made = self._pay(claim.name)
        requests = self._send_b2c(made["b2c"], {"254711000931": ("Paid", "254711000931 - _T GAP ACHIENG"),
                                                "254711000932": ("Paid", "254711000932 - _T GAP BARASA")},
                                  record=False)
        self.addCleanup(self._delete_committed, [claim.name, made["b2c"], *requests])
        self._no_commit.stop()
        frappe.db.commit()
        site, sites_path, errors, inside, seen = (frappe.local.site, frappe.local.sites_path, [], threading.Event(),
                                                  {})
        original = hrms_claim.update_reimbursed_amount

        def slow(doc):
            original(doc)
            if threading.current_thread().name == "booking":
                inside.set()
                time.sleep(2)  # the booking keeps its transaction (and the claim's lock) meanwhile

        def in_own_transaction(name, work):
            def run():
                try:
                    frappe.init(site=site, sites_path=sites_path)
                    frappe.connect()
                    work()
                    frappe.db.commit()
                except Exception:
                    errors.append(frappe.get_traceback())
                finally:
                    frappe.destroy()

            return threading.Thread(target=run, name=name)

        def book():
            b2c_response_service.update_b2c_reference_status(requests[1], enqueue_next=True)

        def send_check():
            frappe.db.get_value(CLAIM, claim.name, "name")  # the send has read already: its snapshot is fixed
            with owed.doc_lock(CLAIM, claim.name, timeout=60):  # as B2B's submit, waiting for the booking
                values = employee_documents.read_document(CLAIM, claim.name, for_update=True)
                seen["mixed"] = employee_documents.reimbursed_problem(CLAIM, values)  # how 4b2f196 read it
                seen["now"] = employee_documents.payable_problem(values, CLAIM, for_update=True)
                seen["total"] = flt(values.total_amount_reimbursed)

        with patch("frappe.enqueue"), patch("mpsa_suite.money_out.statement.after_payout"):
            b2c_response_service.update_b2c_reference_status(requests[0], enqueue_next=True)  # booked, committed
            frappe.db.commit()
            with patch.object(hrms_claim, "update_reimbursed_amount", slow):
                booking = in_own_transaction("booking", book)
                booking.start()
                self.assertTrue(inside.wait(120), errors)
                sender = in_own_transaction("send", send_check)
                sender.start()  # reads while the booking is not committed, then waits for the claim's lock
                booking.join(180)
                sender.join(180)
        self._no_commit.start()
        self.assertEqual(errors, [])
        self.assertEqual(seen["total"], 9000)
        self.assertIn("Total Amount Reimbursed shows KES 9,000", seen["mixed"] or "")  # R5 reproduced
        self.assertIsNone(seen["now"])  # the send goes ahead: there is no gap

    def test_send_payment_proof_on_a_batch_of_two_claimants_proves_one_row_to_its_own_claimant(self):
        """Review R1/R2: a batch paying several parties is never proved whole to all of them."""
        one = self._claim([{"payee_type": "Phone (B2C)", "payee_name": "_T Proof One", "phone": "0711000941",
                            "amount": 1000}])
        two = self._claim([{"payee_type": "Phone (B2C)", "payee_name": "_T Proof Two", "phone": "0711000942",
                            "amount": 700}], employee=self.colleague)
        with _as(PREPARER):
            made = claim_payees.pay_claims([one.name, two.name], SETTINGS)
        self._send_b2c(made["b2c"], {"254711000941": ("Paid", "254711000941 - _T PROOF ONE"),
                                     "254711000942": ("Paid", "254711000942 - _T PROOF TWO")})
        frappe.db.set_value("Employee", self.colleague, "personal_email", "_t_colleague@example.com")
        from mpsa_suite.money_out import payment_print

        choose = payment_print.proof_recipients(DISB, made["b2c"])
        self.assertEqual(len(choose["rows"]), 2)
        row = frappe.db.get_value(ROW, {"parent": made["b2c"], "partyb": "254711000942"},
                                  ["name", "b2c_disbursement_request"], as_dict=True)
        proof = payment_print.proof_recipients(DISB, made["b2c"], row=row.name)
        self.assertEqual((proof["doctype"], proof["name"], proof["recipients"]),
                         (REQ, row.b2c_disbursement_request, ["_t_colleague@example.com"]))
        t.renders(REQ, row.b2c_disbursement_request, "_T PROOF TWO")  # that payout's own proof
        # fix check S8: an Expense Approver may email the batch but has no right on its requests: refused
        # before the rows are offered (the desk does not offer the button either)
        with _as(APPROVER):
            self.assertRaisesRegex(frappe.PermissionError, "you may not send email on a B2C Disbursement Request",
                                   payment_print.proof_recipients, DISB, made["b2c"])

    # ------------------------------------------------------------ fix check of be943f5
    def test_a_staff_flag_is_about_everyone_of_the_paid_name_and_no_reader_gets_their_logins(self):
        """S2: a second employee of the paid name (a manager) is on the flag and may not clear it, nor may a
        manager whose own name M-Pesa paid. S9: the logins it names reach no reader of the claim. S4/S5: a
        request sent with Send via M-Pesa records when it was sent."""
        twin_user = _user("_t_cp_twin@example.com", "M-Pesa Manager", "HR User")
        frappe.get_doc({"doctype": "Employee", "first_name": "_T Payee Claimant", "gender": "Female",
                        "date_of_birth": "1990-01-01", "date_of_joining": "2020-01-01", "company": t.COMPANY,
                        "status": "Active", "user_id": twin_user,
                        # a manager who reads other employees' claims: no User Permission to their own only
                        "create_user_permission": 0}).insert()  # shares the claimant's name
        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835877",
                              "amount": 2000}])
        name = self._pay(claim.name)["b2b"][0]
        self._send_b2b(name)
        self.assertTrue(frappe.db.get_value(B2B, name, "send_started_on"))  # S4/S5
        self._b2b_result(name, "835877 - _T PAYEE CLAIMANT")
        line = next(iter(self._lines(claim.name).values()))
        self.assertEqual(line.staff_check, "Blocked")
        self.assertIn(twin_user, frappe.db.get_value(LINE, line.name, "staff_involved"))
        with _as(PREPARER):  # an HR User reads the claim
            loaded = frappe.client.get(CLAIM, claim.name)
            self.assertFalse(any(row.get("staff_involved") for row in loaded[claim_payees.TABLE]))
            state = claim_payees.claim_state(claim.name)
            self.assertTrue(state["lines"] and not any("staff_involved" in row for row in state["lines"]))
        with _as(twin_user):
            self.assertRaisesRegex(frappe.PermissionError, "The flag is about you", claim_payees.clear_staff_flag,
                                   claim.name, line.name, "_T not me")
        _user(self.manager, "HR User")  # reads the claim
        full_name = frappe.db.get_value("User", self.manager, "full_name")
        frappe.db.set_value("User", self.manager, "full_name", "_T Payee Claimant")  # goes by the paid name
        with _as(self.manager):
            self.assertRaisesRegex(frappe.PermissionError, "The flag is about you", claim_payees.clear_staff_flag,
                                   claim.name, line.name, "_T checked")
        frappe.db.set_value("User", self.manager, "full_name", full_name)
        with _as(self.manager):
            claim_payees.clear_staff_flag(claim.name, line.name, "_T a shop the claimant does not own")
        self.assertEqual(frappe.db.get_value(LINE, line.name, "staff_check"), "Cleared")

    def test_the_sends_gap_check_counts_the_claims_entries_as_the_installed_hrms_does(self):
        """S3: HRMS 16 leaves out a Payment Entry reference with an advance voucher (an Employee Advance's payment
        reconciled against the claim); HRMS 15 counts it. The send's locking count agrees with HRMS either way,
        so no gap is refused that Repair Reimbursed Total cannot find."""
        import hrms
        from hrms.hr.doctype.expense_claim.expense_claim import update_reimbursed_amount

        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835878",
                              "amount": 2000}])
        name = self._pay(claim.name)["b2b"][0]
        self._send_b2b(name)
        self._b2b_result(name, "835878 - _T GAP SHELL")
        ref = frappe.db.get_value("Payment Entry Reference", {"reference_name": claim.name, "docstatus": 1}, "name")
        self.assertTrue(frappe.db.has_column("Payment Entry Reference", "advance_voucher_type"))
        frappe.db.set_value("Payment Entry Reference", ref, "advance_voucher_type", "Employee Advance")
        update_reimbursed_amount(frappe.get_doc(CLAIM, claim.name))  # HRMS's own count
        doc = frappe.get_doc(CLAIM, claim.name)
        v16 = int(hrms.__version__.split(".")[0]) >= 16
        self.assertEqual(claim_payees.hrms_skips_advance_vouchers(), v16)
        self.assertEqual(flt(doc.total_amount_reimbursed), 0 if v16 else 2000)
        self.assertEqual(claim_payees.booked_now(claim.name), flt(doc.total_amount_reimbursed))
        self.assertIsNone(claim_payees.reimbursed_gap(doc, for_update=True))
        self.assertIsNone(claim_payees.reimbursed_gap(doc))

    def test_a_request_sent_with_send_via_mpesa_whose_result_never_came_is_asked_about(self):
        """S4/S5: by its send time, which a direct send records now; one sent before that, by its modified."""
        from frappe.utils import add_to_date

        from mpsa_suite.money_out import b2b_status, payment_print

        claim = self._claim([{"payee_type": "Till (Buy Goods)", "payee_name": "_T Shell", "shortcode": "835879",
                              "amount": 2000}])
        name = self._pay(claim.name)["b2b"][0]
        self._send_b2b(name)  # Send via M-Pesa: the submit, then the call
        values = frappe.db.get_value(B2B, name, ["status", "send_started_on"], as_dict=True)
        self.assertEqual(values.status, "Initiated")
        self.assertTrue(values.send_started_on)
        earlier = add_to_date(now_datetime(), hours=-2)
        frappe.db.set_value(B2B, name, {"send_started_on": earlier, "modified": earlier}, update_modified=False)
        self.assertIn(name, b2b_status.unsettled_due(500))
        frappe.db.set_value(B2B, name, {"send_started_on": None, "modified": earlier}, update_modified=False)
        self.assertIn(name, b2b_status.unsettled_due(500))  # sent before the send time was recorded
        old = add_to_date(now_datetime(), days=-8)
        frappe.db.set_value(B2B, name, {"send_started_on": None, "modified": old}, update_modified=False)
        self.assertNotIn(name, b2b_status.unsettled_due(500))  # over seven days: no longer asked about
        # S7: an employee paid by B2B for no slip, claim or advance is a salary, as salary_access reads it
        self.assertTrue(payment_print.is_salary_payout(B2B, frappe.get_doc({"doctype": B2B,
                                                                           "employee": self.claimant})))
        self.assertFalse(payment_print.is_salary_payout(B2B, frappe.get_doc(B2B, name)))  # the claim's merchant
