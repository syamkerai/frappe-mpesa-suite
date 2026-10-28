"""Expense Claims and Employee Advances paid by M-Pesa (round 4, WP3) against the real database
(ERPNext and HRMS v15 and v16).

Named users only, each with the roles a site gives that job: an HR Manager prepares a phone
payout (reads claims, makes B2C payouts), an Accounts User who is also an HR User prepares a bank
one (reads claims, makes B2B requests), an Expense Approver who is also an Accounts User prepares
an advance's (HRMS lets only the Employee and the Expense Approver read advances) and sees claim
payouts but no salary, an Accounts User alone (who reads no claims) sees neither, and an M-Pesa
Payer sends with Send via M-Pesa
(send_confirm.send_for_test). Claims and advances are real HRMS documents (inserted and submitted
as test data), paid from the shortcode TESTB2BDOCS. Nothing reaches Safaricom: B2B post() and the
B2C connector are replaced and Results are written as the callbacks write them; commits made by
the code under test are replaced, so each test rolls back. Unit coverage:
tests/unit/test_employee_documents.py.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import b2b, employee_documents, owed, party_accounts, salary_access, send_confirm
from mpsa_suite.money_out.test_b2b_documents import _settings
from mpsa_suite.money_out.test_payroll_payouts import _employee

SETTINGS = "TESTB2BDOCS"
EC, EA = "Expense Claim", "Employee Advance"
B2B, PE = b2b.B2B_DOCTYPE, "Payment Entry"
DISB, ROW, REQ = "B2C Payment Disbursement", "B2C Payment Disbursement Reference", "B2C Disbursement Request"
EMPACC = party_accounts.EMPLOYEE_ACCOUNT
SUITE = "M-Pesa Suite Settings"
HR, ACCOUNTS, APPROVER = "_t_claims_hr@example.com", "_t_claims_accounts@example.com", "_t_claims_approver@example.com"
CLERK, PAYER = "_t_claims_clerk@example.com", "_t_claims_payer@example.com"
BOOKS = "_t_claims_books@example.com"  # an Accounts Manager with no HR role (Trident's System Managers hold it)
LINE_MANAGER = "_t_claims_line_manager@example.com"  # an Expense Approver limited to their own Employee record
HR_USER_ONLY = "_t_claims_hr_user_only@example.com"
PHONE = "254722000777"
CLAIM_TYPE = "_T Claims Travel"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, payout_lines_from=None, allow_one_off_b2b=0,
                    entry_reminder_days=3, schedule_maker_checker=1, send_interval_seconds=0,
                    employee_account_hold_hours=0)


def hrms() -> bool:
    return "hrms" in frappe.get_installed_apps()


def _named(email: str, *roles) -> str:
    """A System User ``email`` holding ``roles`` (never Administrator, never System Manager)."""
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0].strip("_"),
                        "send_welcome_email": 0, "user_type": "System User"}).insert(ignore_permissions=True)
    user = frappe.get_doc("User", email)
    missing = [r for r in roles if r not in {row.role for row in user.roles} and frappe.db.exists("Role", r)]
    if missing:
        user.flags.ignore_permissions = True
        user.add_roles(*missing)
    return email


def _account(name: str, root_type: str, report_type: str, account_type: str | None = None) -> str:
    existing = frappe.db.get_value("Account", {"account_name": name, "company": t.COMPANY}, "name")
    if existing:
        return existing
    parent = frappe.db.get_value("Account", {"company": t.COMPANY, "root_type": root_type, "is_group": 1}, "name",
                                 order_by="lft desc")
    return frappe.get_doc({"doctype": "Account", "account_name": name, "company": t.COMPANY, "parent_account": parent,
                           "root_type": root_type, "report_type": report_type, "account_type": account_type,
                           "account_currency": "KES", "is_group": 0}).insert().name


def _approved(employee: str, account_type: str, name: str) -> str:
    """An Employee M-Pesa Account written as approved (the approval has its own tests)."""
    if frappe.db.exists(EMPACC, name):
        return name
    values = ({"phone": PHONE, "expected_name": "Test Claimant"} if account_type == party_accounts.PHONE else
              {"shortcode": "247247", "account_number": f"C{name[-4:]}", "expected_name": "Equity Bank"})
    doc = frappe.get_doc({
        "doctype": EMPACC, "employee": employee, "account_type": account_type, "is_default": 1, "docstatus": 1,
        "proof": "/private/files/_t_claims_proof.pdf", "approved_by": "Administrator", "approved_on": now_datetime(),
        "active_from": add_days(now_datetime(), -1), "creation": now_datetime(), "modified": now_datetime(),
        "owner": "Administrator", **values})
    doc.name = name
    doc.db_insert()
    return name


@contextmanager
def _as(user):
    frappe.set_user(user)
    try:
        yield
    finally:
        frappe.set_user("Administrator")


class TestEmployeeDocuments(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not hrms():
            return
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.bank_account, cls.gl = _settings()
        _named(HR, "HR Manager")
        _named(ACCOUNTS, "Accounts User", "HR User")
        _named(APPROVER, "Accounts User", "Expense Approver")
        _named(CLERK, "Accounts User")
        _named(PAYER, "M-Pesa Payer")
        _named(BOOKS, "Accounts Manager")
        _named(LINE_MANAGER, "Expense Approver")
        _named(HR_USER_ONLY, "HR User")
        cls.payable = _account("_T Claims Payable", "Liability", "Balance Sheet", "Payable")
        cls.advances = _account("_T Claims Advances", "Asset", "Balance Sheet", "Receivable")
        # HRMS v15 fetches an advance's account from the company on every save
        # (company.default_employee_advance_account, no fetch_if_empty), replacing the one the
        # fixture gives, and then requires it to be Receivable. Point the company at the fixture's
        # (v16 fetches from the employee only when empty, so this is harmless there).
        frappe.db.set_value("Company", t.COMPANY, "default_employee_advance_account", cls.advances)
        cls.expense = _account("_T Claims Travel Expense", "Expense", "Profit and Loss")
        cls.cost_center = frappe.db.get_value("Company", t.COMPANY, "cost_center")
        if not frappe.db.exists("Expense Claim Type", CLAIM_TYPE):
            frappe.get_doc({"doctype": "Expense Claim Type", "expense_type": CLAIM_TYPE,
                            "accounts": [{"company": t.COMPANY, "default_account": cls.expense}]}).insert()
        if not frappe.db.exists("Project", {"project_name": "_T Claims Site"}):
            frappe.get_doc({"doctype": "Project", "project_name": "_T Claims Site", "company": t.COMPANY}).insert()
        cls.project = frappe.db.get_value("Project", {"project_name": "_T Claims Site"}, "name")
        cls.phone_employee = _employee("_T Claims Phone")
        cls.bank_employee = _employee("_T Claims Bank")
        cls.phone_account = _approved(cls.phone_employee, party_accounts.PHONE, "_T-MEA-CLAIM-1")
        cls.bank_account_of_employee = _approved(cls.bank_employee, party_accounts.BANK, "_T-MEA-CLAIM-2")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        if not hrms():
            self.skipTest("HRMS is not installed")
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self._patches = [patch.object(frappe.db, "commit")]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        # roles and User Permissions a test added are rolled back, not uncached
        t.forget_roles(HR, LINE_MANAGER)
        frappe.local.mpesa_suite_bank_accounts = None
        frappe.clear_document_cache(SUITE, SUITE)
        frappe.clear_cache(doctype=PE)

    # ------------------------------------------------------------ test data, as HRMS leaves it
    def _claim(self, employee, amount, project=None) -> str:
        """A submitted, approved Expense Claim (test data: inserted as Administrator)."""
        values = {"doctype": EC, "employee": employee, "company": t.COMPANY, "posting_date": nowdate(),
                  "approval_status": "Approved", "payable_account": self.payable, "cost_center": self.cost_center,
                  "project": project,
                  "expenses": [{"expense_type": CLAIM_TYPE, "default_account": self.expense, "expense_date": nowdate(),
                                "amount": amount, "sanctioned_amount": amount, "cost_center": self.cost_center}]}
        meta = frappe.get_meta(EC)
        if meta.has_field("currency"):  # v16
            values.update(currency="KES", exchange_rate=1)
        doc = frappe.get_doc(values)
        doc.flags.ignore_permissions = True
        doc.insert()
        doc.submit()
        return doc.name

    def _advance(self, employee, amount) -> str:
        doc = frappe.get_doc({"doctype": EA, "employee": employee, "company": t.COMPANY, "posting_date": nowdate(),
                              "purpose": "Site visit", "advance_amount": amount, "currency": "KES", "exchange_rate": 1,
                              "advance_account": self.advances})
        doc.flags.ignore_permissions = True
        doc.insert()
        doc.submit()
        return doc.name

    # ------------------------------------------------------------ M-Pesa, replaced
    def _ack(self, settings, endpoint, payload, **kw):
        name = kw["name"]
        return {"ResponseCode": "0", "ResponseDescription": "Accepted", "ConversationID": f"AG_{name}",
                "OriginatorConversationID": f"OC_{name}"}, None

    def _send(self, doctype, name, user=PAYER):
        """Send via M-Pesa and Confirm as ``user``; the jobs it queues are kept, not run."""
        with _as(user), patch("mpsa_suite.money_out.b2b.post", side_effect=self._ack), \
                patch("frappe.enqueue") as enqueue:
            send_confirm.send_for_test(frappe.get_doc(doctype, name))
        return list(enqueue.call_args_list)

    def _b2b_paid(self, name, public="247247 - Equity Bank Limited") -> str:
        receipt = "T" + frappe.generate_hash(length=9).upper()
        result = {
            "ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
            "ConversationID": f"AG_{name}", "OriginatorConversationID": f"OC_{name}", "TransactionID": receipt,
            "ResultParameters": {"ResultParameter": [
                {"Key": "DebitAccountBalance", "Value": "Working Account|KES|500000.00|500000.00|0.00|0.00"},
                {"Key": "TransCompletedTime", "Value": now_datetime().strftime("%Y%m%d%H%M%S")},
                {"Key": "DebitPartyCharges", "Value": "Business Pay Bill Charge|KES|2.00"},
                {"Key": "ReceiverPartyPublicName", "Value": public}]},
        }
        with patch("frappe.enqueue"):
            b2b.handle_result(result, name)
            b2b.book_payment_entry(name)
        return receipt

    def _b2c_paid(self, jobs, user=PAYER, before_result=None) -> list:
        """The create_b2c_request jobs the send queued, with Daraja's ack replaced, then each
        payout's Result written as the B2C callback writes it, and its job run. ``before_result``:
        called once the payouts are on their way, before M-Pesa answers."""
        from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import b2c_payment_disbursement as disb
        from mpsa_suite.services import b2c_response_service

        def ack(doc, callback_url):
            frappe.db.set_value(REQ, doc.name, {"status": "Initiated", "conversation_id": f"AG_{doc.name}"})
            return {"ResponseCode": "0"}

        connector = type("Connector", (), {"send_b2c_request": staticmethod(ack)})()
        requests = []
        with _as(user), patch("mpsa_suite.services.b2c_request_service.get_b2c_connector", return_value=connector), \
                patch("frappe.enqueue"):
            for job in [c for c in jobs if c.args and c.args[0].endswith("create_b2c_request")]:
                disb.create_b2c_request(job.kwargs["ref"], job.kwargs["settings"], job.kwargs["b2c_disbursement"])
                requests.append(frappe.db.get_value(ROW, job.kwargs["ref"].name, "b2c_disbursement_request"))
        if before_result:
            before_result()
        for name in requests:
            frappe.db.set_value(REQ, name, {
                "status": "Paid", "result_code": "0", "result_desc": "ok",
                "transaction_id": "T" + frappe.generate_hash(length=9).upper(),
                "transaction_completed_datetime": now_datetime(), "receiver_public_name": f"{PHONE} - TEST CLAIMANT",
                "utility_acct_avlbl_funds": 400000})
            with patch("frappe.enqueue"):
                b2c_response_service.update_b2c_reference_status(name, enqueue_next=True)
        return requests

    def _entry_on(self, doctype, name) -> frappe._dict:
        """The submitted Payment Entry referencing the document."""
        parent = frappe.db.get_value("Payment Entry Reference", {"reference_doctype": doctype, "reference_name": name,
                                                                  "docstatus": 1}, "parent")
        self.assertTrue(parent, f"no submitted Payment Entry on {name}")
        return frappe.get_doc(PE, parent)

    # ------------------------------------------------------------ B2C to the approved phone
    def test_a_claim_is_paid_to_the_approved_phone_and_hrms_marks_it_paid(self):
        claim = self._claim(self.phone_employee, 3000)
        with _as(HR):
            out = employee_documents.prepare_b2c(EC, claim)
        disbursement = frappe.get_doc(DISB, out["name"])
        row = disbursement.references[0]
        self.assertEqual((disbursement.docstatus, disbursement.owner, row.partyb, row.payee_account, row.allocated_amount),
                         (0, HR, PHONE, self.phone_account, 3000))
        jobs = self._send(DISB, out["name"])
        self.assertEqual(len(self._b2c_paid(jobs)), 1)
        entry = self._entry_on(EC, claim)
        self.assertEqual((entry.party_type, entry.party, entry.paid_to), ("Employee", self.phone_employee, self.payable))
        self.assertNotEqual(entry.paid_to, frappe.db.get_value("Company", t.COMPANY, "default_payable_account"))
        claim_doc = frappe.get_doc(EC, claim)
        self.assertEqual((flt(claim_doc.total_amount_reimbursed), claim_doc.status), (3000, "Paid"))
        self.assertEqual(frappe.db.get_value(EMPACC, self.phone_account, "last_receiver_name"),
                         f"{PHONE} - TEST CLAIMANT")  # the name check ran on the row's approved account
        with _as(HR):
            self.assertEqual(employee_documents.document_status(EC, claim)["left"], 0)
            self.assertRaisesRegex(frappe.ValidationError, "has nothing left to pay", employee_documents.prepare_b2c,
                                   EC, claim)

    def test_with_the_phone_lock_on_an_unapproved_number_is_refused_and_nobody_pays_themselves(self):
        frappe.db.set_single_value(SUITE, "b2c_approved_phones_only", 1)
        frappe.clear_document_cache(SUITE, SUITE)
        claim = self._claim(self.phone_employee, 2000)
        with _as(HR):
            out = employee_documents.prepare_b2c(EC, claim)
            doc = frappe.get_doc(DISB, out["name"])
            doc.references[0].partyb = "0733000111"
            self.assertRaisesRegex(frappe.ValidationError, "is not a phone approved", doc.save)
        frappe.db.set_value("Employee", self.phone_employee, "user_id", PAYER)
        self.assertRaisesRegex((frappe.PermissionError, frappe.ValidationError), "You are", self._send, DISB, out["name"])
        self.assertEqual(frappe.db.get_value(DISB, out["name"], "docstatus"), 0)

    def test_an_approved_phone_on_hold_is_never_passed_over_for_the_cell_number(self):
        # verification K3: with the approved-phone lock off (Trident's and Astral's setting after the upgrade), an
        # employee whose default phone is in its hold period, its new number also typed as the Cell Number, is not
        # paid to the Cell Number: the preparer is told why
        frappe.db.set_single_value(SUITE, "b2c_approved_phones_only", 0)
        frappe.clear_document_cache(SUITE, SUITE)
        frappe.db.set_value("Employee", self.phone_employee, "cell_number", "0733000777")
        frappe.db.set_value(EMPACC, self.phone_account, "active_from", add_days(now_datetime(), 1))
        claim = self._claim(self.phone_employee, 1600)
        with _as(HR):
            self.assertEqual(employee_documents.b2c_phone(self.phone_employee), ("", None))
            self.assertRaisesRegex(frappe.ValidationError, f"{self.phone_account} is on hold until",
                                   employee_documents.prepare_b2c, EC, claim)
        frappe.db.set_value(EMPACC, self.phone_account, "active_from", add_days(now_datetime(), -1))
        with _as(HR):
            out = employee_documents.prepare_b2c(EC, claim)
        row = frappe.get_doc(DISB, out["name"]).references[0]
        self.assertEqual((row.partyb, row.payee_account), (PHONE, self.phone_account))

    # ------------------------------------------------------------ B2B to the approved bank account
    def test_a_claim_is_paid_to_the_approved_bank_account_with_its_project(self):
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter

        # a site whose Payment Entries need a project (Trident's claims do)
        make_property_setter(PE, "project", "reqd", 1, "Check", validate_fields_for_doctype=False)
        claim = self._claim(self.bank_employee, 5000, project=self.project)
        with _as(ACCOUNTS):
            out = employee_documents.prepare_b2b(EC, claim)
        request = frappe.get_doc(B2B, out["name"])
        self.assertEqual((request.docstatus, request.employee, request.employee_mpesa_account, request.expense_claim,
                          request.amount, request.receiver_shortcode), (
            0, self.bank_employee, self.bank_account_of_employee, claim, 5000, "247247"))
        self._send(B2B, out["name"])  # a Payer sends what its claim caps (no manager needed)
        self.assertEqual(frappe.db.get_value(B2B, out["name"], "status"), "Initiated")
        receipt = self._b2b_paid(out["name"])
        entry = self._entry_on(EC, claim)
        self.assertEqual((entry.docstatus, entry.party_type, entry.party, entry.paid_from, entry.paid_to,
                          entry.reference_no, entry.project), (
            1, "Employee", self.bank_employee, self.gl, self.payable, receipt, self.project))
        self.assertEqual([(r.reference_doctype, r.reference_name, flt(r.allocated_amount)) for r in entry.references],
                         [(EC, claim, 5000)])
        self.assertEqual(frappe.db.get_value(EC, claim, ["total_amount_reimbursed", "status"]), (5000, "Paid"))
        self.assertEqual(frappe.db.get_value(B2B, out["name"], "entry_status"), "Entry Made")

    def test_an_advance_is_paid_by_b2b_and_hrms_counts_it_paid(self):
        advance = self._advance(self.bank_employee, 8000)
        with _as(APPROVER):
            out = employee_documents.prepare_b2b(EA, advance)
        self._send(B2B, out["name"])
        self._b2b_paid(out["name"])
        entry = self._entry_on(EA, advance)
        self.assertEqual((entry.party, entry.paid_to), (self.bank_employee, self.advances))
        self.assertEqual(frappe.db.get_value(EA, advance, ["paid_amount", "status"]), (8000, "Paid"))
        receipt = frappe.db.get_value(B2B, out["name"], "transaction_id")
        t.renders(EA, advance, receipt, "8,000.00", "Fully Paid")  # R4.1: the advance's payment advice

    def test_a_request_with_no_document_is_still_for_a_manager(self):
        with _as(ACCOUNTS):
            req = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "company": t.COMPANY, "amount": 1500,
                                  "remarks": "Allowance", "employee": self.bank_employee}).insert()
        self.assertRaisesRegex((frappe.PermissionError, frappe.ValidationError), "paid by an M-Pesa Manager",
                               self._send, B2B, req.name)

    def test_a_draft_payment_entry_on_the_claim_refuses_its_b2c_send(self):
        # final review: HRMS's Make Payment Entry (Bank) left as a draft until the transfer clears. Prepare and
        # B2B already refused ("Nothing is left to pay"); the B2C send now does too, as it reads the same parts.
        claim = self._claim(self.phone_employee, 5000)
        with _as(HR):
            out = employee_documents.prepare_b2c(EC, claim)
        entry = frappe.get_doc({"doctype": PE, "payment_type": "Pay", "company": t.COMPANY, "posting_date": nowdate(),
                                "party_type": "Employee", "party": self.phone_employee, "paid_from": self.gl,
                                "paid_to": self.payable, "paid_amount": 5000, "received_amount": 5000,
                                "source_exchange_rate": 1, "target_exchange_rate": 1, "reference_no": "BANK-T1",
                                "reference_date": nowdate(), "cost_center": self.cost_center,
                                "references": [{"reference_doctype": EC, "reference_name": claim,
                                                "allocated_amount": 5000}]})
        entry.insert()  # a draft
        self.assertRaisesRegex(frappe.ValidationError, f"less draft Payment Entry {entry.name}", self._send, DISB,
                               out["name"])
        self.assertEqual(frappe.db.get_value(DISB, out["name"], "docstatus"), 0)

    def test_a_partly_booked_b2c_payouts_rest_still_counts_once_the_bank_entry_is_cancelled(self):
        # verify (K1): the claim was paid 2,000 by bank while its B2C payout of 5,000 was on its way. The payout's
        # entry takes the 3,000 left and keeps 2,000 unallocated; staff then cancel the bank entry. HRMS shows
        # 2,000 owed again, but the employee has it on M-Pesa's entry: nothing is offered (as for a B2B payout).
        claim = self._claim(self.phone_employee, 5000)
        with _as(HR):
            out = employee_documents.prepare_b2c(EC, claim)
        jobs = self._send(DISB, out["name"])
        bank = frappe.get_doc({"doctype": PE, "payment_type": "Pay", "company": t.COMPANY, "posting_date": nowdate(),
                               "party_type": "Employee", "party": self.phone_employee, "paid_from": self.gl,
                               "paid_to": self.payable, "paid_amount": 2000, "received_amount": 2000,
                               "source_exchange_rate": 1, "target_exchange_rate": 1, "reference_no": "BANK-T2",
                               "reference_date": nowdate(), "cost_center": self.cost_center,
                               "references": [{"reference_doctype": EC, "reference_name": claim,
                                               "allocated_amount": 2000}]})
        request = self._b2c_paid(jobs, before_result=lambda: bank.insert().submit())[0]
        receipt = frappe.db.get_value(REQ, request, "transaction_id")
        entry = frappe.get_doc(PE, frappe.db.get_value(PE, {"reference_no": receipt, "docstatus": 1}))
        self.assertEqual(([(r.reference_name, flt(r.allocated_amount)) for r in entry.references],
                          flt(entry.unallocated_amount)), ([(claim, 3000)], 2000))
        self.assertEqual(frappe.db.get_value(REQ, request, "accounting_booked"), 1)
        frappe.get_doc(PE, bank.name).cancel()
        left = owed.employee_doc_left_to_pay(EC, claim, for_update=False)
        self.assertEqual((left.owed, [(p.kind, p.amount) for p in left.parts], left.left), (2000, [("b2c", 2000)], 0))
        with _as(HR):
            self.assertRaisesRegex(frappe.ValidationError, "Nothing is left to pay by M-Pesa",
                                   employee_documents.prepare_b2c, EC, claim)
        # once the 2,000 is allocated to the claim on that entry (Payment Reconciliation does; written here), the
        # entry counts it and the payout no longer does
        frappe.db.set_value("Payment Entry Reference", entry.references[0].name, "allocated_amount", 5000)
        self.assertEqual(owed.employee_doc_left_to_pay(EC, claim, for_update=False).parts, [])

    def test_an_accounts_manager_without_an_hr_role_gets_no_salary_data(self):
        # final review (decision D4): Get References for Salary Slips (net pay and phone of every employee of a
        # payroll), and a B2B request to an employee for no claim (it copies their bank account), are refused
        with _as(BOOKS):
            doc = frappe.new_doc(DISB)
            doc.update({"company": t.COMPANY, "party_type": "Employee", "transaction_to_pay_against": "Salary Slip"})
            self.assertRaisesRegex(frappe.PermissionError, "salaries are shown only",
                                   doc.get_outstanding_reference_documents,
                                   {"transaction_to_pay_against": "Salary Slip", "payroll_entry": "HR-PRUN-T-1",
                                    "company": t.COMPANY, "party_type": "Employee"})
            request = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "company": t.COMPANY, "amount": 1500,
                                      "remarks": "Allowance", "employee": self.bank_employee})
            self.assertRaisesRegex(frappe.PermissionError, "salaries are shown only", request.insert)
            self.assertFalse(request.get("employee_mpesa_account"))  # nothing of the employee's was read

    # ------------------------------------------------------------ the list, the cancel, who sees it
    def test_pay_by_mpesa_on_the_list_prepares_one_b2c_batch_and_a_b2b_request(self):
        _named(HR, "Accounts User")  # makes both kinds
        phone_claim = self._claim(self.phone_employee, 1200)
        bank_claim = self._claim(self.bank_employee, 2500)
        with _as(HR):
            out = employee_documents.prepare_bulk(EC, [phone_claim, bank_claim])
        self.assertEqual(out["skipped"], [])
        rows = frappe.get_all(ROW, filters={"parent": out["b2c"]}, fields=["reference_name", "allocated_amount"])
        self.assertEqual([(r.reference_name, flt(r.allocated_amount)) for r in rows], [(phone_claim, 1200)])
        self.assertEqual(frappe.db.get_value(B2B, out["b2b"][0], ["expense_claim", "docstatus"]), (bank_claim, 0))

    def test_one_default_role_that_reads_the_document_prepares_both_routes(self):
        # final review (area C): the suite's own rights let HR User (claims) and Expense Approver (claims and
        # advances), each alone, prepare a phone and a bank payout; no site grant, no DocPerm on HRMS DocTypes
        hr_user = _named("_t_claims_hr_user_only@example.com", "HR User")
        approver = _named("_t_claims_approver_only@example.com", "Expense Approver")
        for user in (hr_user, approver):
            bank_claim = self._claim(self.bank_employee, 1100)
            phone_claim = self._claim(self.phone_employee, 600)
            with _as(user):
                b2b_out = employee_documents.prepare_b2b(EC, bank_claim)
                b2c_out = employee_documents.prepare_b2c(EC, phone_claim)
            self.assertEqual(frappe.db.get_value(B2B, b2b_out["name"], ["docstatus", "owner", "expense_claim"]),
                             (0, user, bank_claim))
            self.assertEqual(frappe.db.get_value(DISB, b2c_out["name"], ["docstatus", "owner"]), (0, user))
        advance = self._advance(self.bank_employee, 700)
        with _as(approver):
            out = employee_documents.prepare_b2b(EA, advance)
        self.assertEqual(frappe.db.get_value(B2B, out["name"], ["employee_advance", "owner"]), (advance, approver))

    def test_a_claim_is_not_cancelled_while_a_payout_of_it_is_paid_or_on_its_way(self):
        claim = self._claim(self.bank_employee, 1800)
        with _as(ACCOUNTS):
            out = employee_documents.prepare_b2b(EC, claim)
        self._send(B2B, out["name"])
        with _as(HR):
            self.assertRaisesRegex(frappe.ValidationError, f"cannot be cancelled while M-Pesa payouts of it are paid or "
                                                           f"on their way: {out['name']}", frappe.get_doc(EC, claim).cancel)
        # paid after its request was cancelled: Frappe's link check no longer holds the claim; this does
        frappe.db.set_value(B2B, out["name"], {"docstatus": 2, "status": "Paid"})
        with _as(HR):
            self.assertRaisesRegex(frappe.ValidationError, out["name"], frappe.get_doc(EC, claim).cancel)

    def test_claim_payouts_are_shown_to_accounts_roles_who_read_claims_but_salaries_are_not(self):
        claim = self._claim(self.bank_employee, 900)
        with _as(ACCOUNTS):
            out = employee_documents.prepare_b2b(EC, claim)
            salary = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "company": t.COMPANY, "amount": 700,
                                     "remarks": "Allowance", "employee": self.bank_employee}).insert()
        both = [out["name"], salary.name]
        # Accounts User + Expense Approver: HRMS lets the Expense Approver read claims
        with _as(APPROVER):
            self.assertFalse(salary_access.can_see_salaries())
            self.assertTrue(frappe.has_permission(B2B, "read", doc=out["name"]))
            self.assertFalse(frappe.has_permission(B2B, "read", doc=salary.name))
            self.assertEqual(frappe.get_list(B2B, filters={"name": ["in", both]}, pluck="name"), [out["name"]])
        # Accounts User alone reads no claims (HRMS's defaults): the claim's payout stays hidden too
        with _as(CLERK):
            self.assertFalse(frappe.has_permission(B2B, "read", doc=out["name"]))
            self.assertEqual(frappe.get_list(B2B, filters={"name": ["in", both]}, pluck="name"), [])

    def _references(self, doctype) -> list:
        """Get References on a new B2C Payment Disbursement paying ``doctype`` (as the form sends it)."""
        doc = frappe.new_doc(DISB)
        doc.update({"company": t.COMPANY, "party_type": "Employee", "transaction_to_pay_against": doctype,
                    "company_currency": "KES", "paid_to_account_currency": "KES"})
        return doc.get_outstanding_reference_documents({"transaction_to_pay_against": doctype, "company": t.COMPANY,
                                                        "party_type": "Employee"})

    def test_get_references_lists_only_the_claims_the_user_may_read(self):
        # verification K12: an Expense Approver limited to their own Employee record by a User Permission (as a line
        # manager is) lists only the claims HRMS lets them read, here the one shared with them, with no phone of
        # anyone else's; an HR User, whom HRMS gives no read on Employee Advances, gets none
        own = _employee("_T Claims Line Manager")
        frappe.get_doc({"doctype": "User Permission", "user": LINE_MANAGER, "allow": "Employee", "for_value": own,
                        "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
        shared = self._claim(self.phone_employee, 1300)
        hidden = self._claim(self.bank_employee, 1400)
        frappe.share.add_docshare(EC, shared, LINE_MANAGER, read=1, flags={"ignore_share_permission": True})
        with _as(LINE_MANAGER):
            self.assertTrue(frappe.has_permission(EC, "read", doc=shared))
            self.assertFalse(frappe.has_permission(EC, "read", doc=hidden))
            names = {r["reference_name"] for r in self._references(EC)}
        self.assertIn(shared, names)
        self.assertNotIn(hidden, names)
        self._advance(self.phone_employee, 900)
        with _as(HR_USER_ONLY):
            self.assertRaises(frappe.PermissionError, self._references, EA)

    # ------------------------------------------------------------ who a claim's B2C payout pays
    def test_a_claim_row_pays_its_employee_and_a_cancelled_phone_account_never_blocks_the_draft(self):
        from mpsa_suite.money_out.test_b2b_documents import _supplier

        frappe.db.set_single_value(SUITE, "b2c_approved_phones_only", 0)  # a new site has it on
        frappe.clear_document_cache(SUITE, SUITE)
        supplier, _account_name = _supplier()
        claim = self._claim(self.phone_employee, 1500)
        with _as(HR):
            out = employee_documents.prepare_b2c(EC, claim)
            doc = frappe.get_doc(DISB, out["name"])
            doc.references[0].party_type, doc.references[0].party = "Supplier", supplier
            self.assertRaisesRegex(frappe.ValidationError, f"{claim} belongs to {self.phone_employee}, not Supplier",
                                   doc.save)
        # the employee's phone account is cancelled after the draft was made (the number changed)
        frappe.db.set_value(EMPACC, self.phone_account, "docstatus", 2)
        with _as(HR):
            doc = frappe.get_doc(DISB, out["name"])
            doc.save()  # not "Cannot link cancelled document": the row's account is a name, set again
            self.assertFalse(doc.references[0].payee_account)
        frappe.db.set_single_value(SUITE, "b2c_approved_phones_only", 1)
        frappe.clear_document_cache(SUITE, SUITE)
        with _as(HR):
            doc = frappe.get_doc(DISB, out["name"])
            self.assertRaisesRegex(frappe.ValidationError, "is not a phone approved", doc.save)

    def test_a_b2c_request_made_by_hand_never_pays_a_claim(self):
        claim = self._claim(self.phone_employee, 1100)
        with _as(HR):
            out = employee_documents.prepare_b2c(EC, claim)
            row = frappe.get_doc(DISB, out["name"]).references[0]
            request = frappe.get_doc({"doctype": REQ, "amount": 1100, "phone_number": PHONE, "remarks": "Claim",
                                      "mpesa_settings": SETTINGS, "payment_provider": "Mpesa", "currency": "KES",
                                      "b2c_payment_disbursement": out["name"],
                                      "b2c_payment_disbursement_reference": row.name,
                                      "reference_doctype": EC, "reference_name": claim, "party_type": "Employee",
                                      "party": self.phone_employee})
            self.assertRaisesRegex(frappe.ValidationError, "is paid by a B2C Payment Disbursement", request.insert)
