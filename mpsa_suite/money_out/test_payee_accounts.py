"""Payee accounts, round 4, against the real database (ERPNext v15 and v16 with HRMS): the shipped
bank paybill list and its sync, the four account types, approval by a named manager (also through
the Approve step itself, send_confirm, with a bank paybill changed since the draft was saved),
Make Default (also the form's own call through Frappe's router), a save after approval never
unblocking an account, a bank paybill change blocking the accounts on it, a supplier bank account
and an employee till paid by B2B.

Every send and approval is made by a named test user holding the right role (never Administrator,
never System Manager), inside send_confirm.deliberate so the deliberate-step rule lets the test's
own submit through, or with send_confirm.send_for_test (t.send_as). Nothing reaches Safaricom:
post() is replaced and the Result is handed to handle_result the way Safaricom posts it. Each test
rolls back. Unit coverage: tests/unit/test_payee_accounts.py and test_bank_paybills.py.
"""

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.client import set_value as client_set_value
from frappe.utils import add_days, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import b2b, bank_paybills, employee_accounts, party_accounts, send_confirm, supplier_accounts
from mpsa_suite.money_out.test_b2b_documents import _settings
from mpsa_suite.money_out.test_payroll_payouts import _employee, _user

SETTINGS = "TESTB2BDOCS"
BANKS, SUP, EMP = party_accounts.BANK_PAYBILL, party_accounts.SUPPLIER_ACCOUNT, party_accounts.EMPLOYEE_ACCOUNT
B2B = b2b.B2B_DOCTYPE
BANK, PAYBILL, TILL, PHONE = party_accounts.BANK, party_accounts.PAYBILL, party_accounts.TILL, party_accounts.PHONE
HR, MANAGER, PREPARER, PAYER, BUYER = ("_t_payee_hr@example.com", "_t_payee_manager@example.com",
                                       "_t_payee_accounts@example.com", "_t_payee_payer@example.com",
                                       "_t_payee_buyer@example.com")
SYSMGR = "_t_payee_sysmgr@example.com"  # configures the suite only (decision 1, 2026-10-27)
SUPPLIER = "_T Payee Supplier"
SUITE_VALUES = dict(allow_one_off_b2b=0, schedule_maker_checker=1, supplier_account_hold_hours=0,
                    employee_account_hold_hours=0, book_payout_charges=0, reconcile_payout_lines=0)
RUN_DOC_METHOD = "/api/method/run_doc_method"


@contextmanager
def _as(user):
    frappe.set_user(user)
    try:
        yield
    finally:
        frappe.set_user("Administrator")


def _supplier() -> str:
    name = frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER}, "name")
    if not name:
        group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
        name = frappe.get_doc({"doctype": "Supplier", "supplier_name": SUPPLIER, "supplier_group": group}).insert().name
    return name


def _approved(doctype: str, name: str, **values) -> str:
    """An account written as approved (the approval has its own test), with its destination key."""
    row = {"doctype": doctype, "docstatus": 1, "proof": "/private/files/_t_payee_proof.pdf",
           "approved_by": MANAGER, "approved_on": now_datetime(), "active_from": add_days(now_datetime(), -1),
           "creation": now_datetime(), "modified": now_datetime(), "owner": HR, "entered_by": HR, **values}
    row["destination_key"] = party_accounts.destination_key(row)
    doc = frappe.get_doc(row)
    doc.name = name
    doc.db_insert()
    return name


def _call(path: str, **form):
    """POST ``path`` through Frappe's API router as the session user, as the browser sends it (the
    helper of mpsa_suite/tests/test_roles_permissions.py)."""
    from frappe.app import make_form_dict
    from werkzeug.test import EnvironBuilder
    from werkzeug.wrappers import Request

    import frappe.api

    request = Request(EnvironBuilder(path=path, method="POST", data=form).get_environ())
    saved = getattr(frappe.local, "request", None), frappe.local.form_dict, frappe.local.response
    frappe.local.request = request
    frappe.local.response = frappe._dict(docs=[])  # as a fresh request has it
    try:
        make_form_dict(request)
        return frappe.api.handle(request)
    finally:
        frappe.local.request, frappe.local.form_dict, frappe.local.response = saved


class TestPayeeAccounts(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        _settings()
        _user(HR, ("HR Manager", "HR User"))
        _user(MANAGER, ("M-Pesa Manager",))
        _user(PREPARER, ("Accounts User",))
        _user(PAYER, ("M-Pesa Payer",))
        _user(BUYER, ("Purchase Manager",))
        _user(SYSMGR, ("System Manager",))
        bank_paybills.sync_shipped()  # what install and every migrate run
        cls.supplier = _supplier()
        cls.employee = _employee("_T Payee Bank")
        cls.other_employee = _employee("_T Payee Other")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")

    # ------------------------------------------------------------ the shipped list
    def test_the_shipped_list_is_on_the_site_and_a_second_sync_changes_nothing(self):
        kcb = frappe.db.get_value(BANKS, {"paybill": "522522"}, ["name", "receiver_name", "enabled", "is_shipped"],
                                  as_dict=True)
        self.assertEqual((kcb.receiver_name, kcb.enabled, kcb.is_shipped), ("KCB Paybill AC", 1, 1))
        self.assertLessEqual(frappe.db.count(BANKS, {"is_shipped": 1}), 50)
        self.assertEqual(bank_paybills.sync_shipped(), [])  # migrate twice: nothing to do

    def test_an_admins_edit_survives_a_release(self):
        with _as(MANAGER):
            doc = frappe.get_doc(BANKS, "KCB")
            doc.account_number_rule = "Our own note"
            doc.save()
        shipped = bank_paybills.load_shipped()
        kcb = next(r for r in shipped if r["code"] == "KCB")
        kcb.update(account_number_rule="A new rule", source="Researched again")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bank_paybills.json"
            path.write_text(json.dumps(shipped), encoding="utf-8")
            bank_paybills.sync_shipped(path)
        row = frappe.db.get_value(BANKS, "KCB", ["account_number_rule", "source"], as_dict=True)
        self.assertEqual((row.account_number_rule, row.source), ("Our own note", "Researched again"))
        self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": BANKS, "reference_name": "KCB",
                                                     "content": ["like", "%Our own note%"]}))

    def test_only_mpesa_admins_change_the_list_and_a_shipped_row_is_never_deleted(self):
        for user in (PREPARER, HR, BUYER, PAYER):
            self.assertTrue(frappe.has_permission(BANKS, "read", user=user), user)
            self.assertFalse(frappe.has_permission(BANKS, "write", user=user), user)
        with _as(PREPARER), self.assertRaises(frappe.PermissionError):
            doc = frappe.get_doc(BANKS, "KCB")
            doc.flags.ignore_permissions = True  # a site's own rights: the server still refuses
            doc.account_number_rule = "x"
            doc.save()
        with _as(MANAGER), self.assertRaisesRegex(frappe.ValidationError, "disable it instead"):
            frappe.delete_doc(BANKS, "KCB")

    # ------------------------------------------------------------ entering and approving
    def test_hr_enters_a_bank_account_and_a_named_manager_approves_it(self):
        with _as(HR):
            doc = frappe.get_doc({"doctype": EMP, "employee": self.employee, "account_type": BANK,
                                  "bank_paybill": "KCB", "account_number": "1234 567 890 123",
                                  "proof": "/private/files/_t_payee_letter.pdf"}).insert()
        self.assertEqual((doc.shortcode, doc.account_number, doc.expected_name, doc.destination_key),
                         ("522522", "1234567890123", "KCB Paybill AC", "PB:522522:1234567890123"))
        self.assertEqual(doc.account_holder_name, frappe.db.get_value("Employee", self.employee, "employee_name"))
        with _as(HR), self.assertRaises(frappe.PermissionError):
            frappe.get_doc(EMP, doc.name).submit()
        with send_confirm.deliberate("bench test"), self.assertRaises(frappe.PermissionError):
            frappe.get_doc(EMP, doc.name).submit()  # the shared Administrator login (setting on by default)
        with _as(MANAGER), send_confirm.deliberate("bench test"):
            approved = frappe.get_doc(EMP, doc.name)
            approved.submit()
        self.assertEqual((approved.docstatus, approved.approved_by), (1, MANAGER))

    def test_approve_never_approves_a_bank_paybill_changed_since_the_draft_was_saved(self):
        """The Approve step itself (send_confirm: prepare_send, then confirm_send, which submits): its
        summary and token show the draft as saved, and the approval copies the bank's row again."""
        with _as(HR):
            doc = frappe.get_doc({"doctype": EMP, "employee": self.other_employee, "account_type": BANK,
                                  "bank_paybill": "KCB", "account_number": "1234567890127",
                                  "proof": "/private/files/_t_payee_letter.pdf"}).insert()
        with _as(MANAGER):
            summary = send_confirm.prepare_send(EMP, doc.name)
            # a CONFIG role edits the bank's row: no approved account is on it, and drafts are never flagged
            kcb = frappe.get_doc(BANKS, "KCB")
            kcb.paybill = "522598"
            kcb.save()
        self.assertIn("522522", summary["rows"][0]["destination"])
        self.assertRaisesRegex(frappe.ValidationError, "The bank's paybill changed from 522522 to 522598",
                               t.send_as, MANAGER, frappe.get_doc(EMP, doc.name))
        self.assertEqual(frappe.db.get_value(EMP, doc.name, ["docstatus", "shortcode"]), (0, "522522"))
        with _as(HR):
            frappe.get_doc(EMP, doc.name).save()  # saved again: the draft, and so the summary, show 522598
        with _as(MANAGER):
            self.assertIn("522598", send_confirm.prepare_send(EMP, doc.name)["rows"][0]["destination"])
        approved = t.send_as(MANAGER, frappe.get_doc(EMP, doc.name))
        self.assertEqual((approved.docstatus, approved.shortcode, approved.approved_by, approved.entered_by),
                         (1, "522598", MANAGER, HR))  # copying the bank's number again is nobody's change

    def test_a_banks_paybill_typed_as_a_plain_paybill_is_refused(self):
        with _as(BUYER), self.assertRaisesRegex(frappe.ValidationError, "bank paybill: pick Account Type Bank Account"):
            frappe.get_doc({"doctype": SUP, "supplier": self.supplier, "account_type": PAYBILL, "shortcode": "522522",
                            "account_number": "1234567890123", "expected_name": SUPPLIER,
                            "proof": "/private/files/_t_payee_letter.pdf"}).insert()

    def test_an_employees_phone_is_never_a_suppliers_and_the_employee_is_not_named(self):
        _approved(EMP, "_T-MEA-PAYEE-P", employee=self.employee, account_type=PHONE, phone="254722000881",
                  expected_name="Test Name", is_default=1)
        with _as(BUYER), self.assertRaises(frappe.ValidationError) as caught:
            frappe.get_doc({"doctype": SUP, "supplier": self.supplier, "account_type": PHONE, "phone": "0722000881",
                            "expected_name": SUPPLIER, "proof": "/private/files/_t_payee_letter.pdf"}).insert()
        self.assertIn("an employee", str(caught.exception))
        self.assertNotIn(self.employee, str(caught.exception))  # a Purchase Manager sees no salary data
        self.assertFalse(frappe.has_permission(EMP, "read", user=PREPARER))

    # ------------------------------------------------------------ defaults
    def test_make_default_is_for_hr_or_admins_and_leaves_a_version(self):
        phone = _approved(EMP, "_T-MEA-PAYEE-D1", employee=self.other_employee, account_type=PHONE,
                          phone="254722000882", expected_name="Test Name", is_default=1)
        bank = _approved(EMP, "_T-MEA-PAYEE-D2", employee=self.other_employee, account_type=BANK, bank_paybill="KCB",
                         shortcode="522522", account_number="1234567890124", expected_name="KCB Paybill AC",
                         account_holder_name="Test Name")
        with _as(PREPARER), self.assertRaises(frappe.PermissionError):
            frappe.get_doc(EMP, bank).make_default()
        with _as(HR):
            frappe.get_doc(EMP, bank).make_default()
        self.assertEqual((frappe.db.get_value(EMP, phone, "is_default"), frappe.db.get_value(EMP, bank, "is_default")),
                         (0, 1))
        self.assertTrue(frappe.db.exists("Version", {"ref_doctype": EMP, "docname": bank}))
        self.assertEqual(party_accounts.pick_account(EMP, self.other_employee), bank)
        with _as(MANAGER), self.assertRaisesRegex(frappe.ValidationError, "Use Make Default"):
            doc = frappe.get_doc(EMP, phone)
            doc.is_default = 1
            doc.save()  # update after submit: the default changes only through Make Default

    def test_the_forms_make_default_works_for_hr_and_accounts_but_not_system_manager(self):
        """The form calls run_doc_method with dt and dn, through Frappe's router: HR Manager and Accounts
        User hold no submit permission on the accounts (sent with the form's copy, Frappe would check one
        as a save after submit and refuse them). System Manager and the shared Administrator login never
        change where payments go."""
        _approved(EMP, "_T-MEA-PAYEE-F1", employee=self.other_employee, account_type=PHONE, phone="254722000883",
                  expected_name="Test Name", is_default=1)
        bank = _approved(EMP, "_T-MEA-PAYEE-F2", employee=self.other_employee, account_type=BANK, bank_paybill="KCB",
                         shortcode="522522", account_number="1234567890128", expected_name="KCB Paybill AC",
                         account_holder_name="Test Name")
        _approved(SUP, "_T-MSA-PAYEE-F1", supplier=self.supplier, account_type=PAYBILL, shortcode="888883",
                  account_number="TP-3", expected_name=SUPPLIER, is_default=1)
        till = _approved(SUP, "_T-MSA-PAYEE-F2", supplier=self.supplier, account_type=TILL, shortcode="777773",
                         expected_name=SUPPLIER)
        for user in (SYSMGR, "Administrator"):
            with _as(user), self.assertRaises(frappe.PermissionError, msg=user):
                _call(RUN_DOC_METHOD, dt=SUP, dn=till, method="make_default")
        self.assertEqual(frappe.db.get_value(SUP, till, "is_default"), 0)
        with _as(PREPARER):
            _call(RUN_DOC_METHOD, dt=SUP, dn=till, method="make_default")
        self.assertEqual(supplier_accounts.pick_account(self.supplier), till)
        with _as(HR):
            _call(RUN_DOC_METHOD, dt=EMP, dn=bank, method="make_default")
        self.assertEqual(party_accounts.default_account(EMP, self.other_employee), bank)

    def test_a_save_after_approval_never_unblocks_an_account(self):
        """Frappe enforces neither read only nor anything but Allow on Submit on a save after submit
        (PUT /api/resource, frappe.client.set_value): the block is set and cleared by the suite only."""
        account = _approved(EMP, "_T-MEA-PAYEE-B", employee=self.employee, account_type=PHONE, phone="254722000884",
                            expected_name="Test Name", is_default=1, name_mismatch=1,
                            last_receiver_name="999999 - JOHN")
        supplier_account = _approved(SUP, "_T-MSA-PAYEE-B", supplier=self.supplier, account_type=PAYBILL,
                                     shortcode="888884", account_number="TP-4", expected_name=SUPPLIER,
                                     needs_reapproval=1, reapproval_reason="Blocked for the test")
        # write but no submit right: Frappe refuses any save of an approved account
        for user, doctype, name in ((HR, EMP, account), (PREPARER, SUP, supplier_account)):
            with _as(user), self.assertRaises((frappe.PermissionError, frappe.ValidationError), msg=user):
                doc = frappe.get_doc(doctype, name)
                doc.update({"name_mismatch": 0, "needs_reapproval": 0})
                doc.save()
        # an M-Pesa Manager holds submit: still refused, so unblocking stays Receiver Name Confirmed (a
        # named login, a note) or cancel, amend and a second person's approval
        with _as(MANAGER):
            for doctype, name, values in ((EMP, account, {"name_mismatch": 0}),
                                          (EMP, account, {"last_receiver_name": "254722000884 - TEST NAME"}),
                                          (SUP, supplier_account, {"needs_reapproval": 0, "reapproval_reason": ""})):
                with self.subTest(values=values), self.assertRaisesRegex(frappe.ValidationError,
                                                                         "set by M-Pesa Suite only"):
                    doc = frappe.get_doc(doctype, name)
                    doc.update(values)
                    doc.save()  # what PUT /api/resource does
            with self.assertRaisesRegex(frappe.ValidationError, "set by M-Pesa Suite only"):
                client_set_value(EMP, account, {"name_mismatch": 0})
        self.assertEqual(frappe.db.get_value(EMP, account, ["name_mismatch", "last_receiver_name"]),
                         (1, "999999 - JOHN"))
        self.assertEqual(frappe.db.get_value(SUP, supplier_account, "needs_reapproval"), 1)
        self.assertIn("is blocked", employee_accounts.usable_problem(employee_accounts.get_account(account),
                                                                      self.employee))

    # ------------------------------------------------------------ a bank paybill change
    def test_a_paybill_change_blocks_the_account_until_approved_again(self):
        account = _approved(EMP, "_T-MEA-PAYEE-C", employee=self.employee, account_type=BANK, bank_paybill="KCB",
                            shortcode="522522", account_number="1234567890125", expected_name="KCB Paybill AC",
                            account_holder_name="Test Name")
        with _as(MANAGER):
            kcb = frappe.get_doc(BANKS, "KCB")
            kcb.paybill = "522599"
            kcb.save()
        acc = employee_accounts.get_account(account)
        self.assertEqual(acc.needs_reapproval, 1)
        self.assertIn("changed from 522522 to 522599", employee_accounts.usable_problem(acc, self.employee))
        with _as(MANAGER):
            kcb = frappe.get_doc(BANKS, "KCB")
            kcb.paybill = "522522"
            kcb.save()
        self.assertIsNone(employee_accounts.usable_problem(employee_accounts.get_account(account), self.employee))

    # ------------------------------------------------------------ after migrate
    def test_paybill_and_till_rows_saved_before_the_upgrade_stay_payable(self):
        paybill = _approved(SUP, "_T-MSA-PAYEE-PB", supplier=self.supplier, account_type=PAYBILL, shortcode="888882",
                            account_number="TP-1", expected_name=SUPPLIER, is_default=1)
        till = _approved(SUP, "_T-MSA-PAYEE-TL", supplier=self.supplier, account_type=TILL, shortcode="777772",
                         expected_name=SUPPLIER)
        old = _approved(EMP, "_T-MEA-PAYEE-OLD", employee=self.employee, account_type="Bank Paybill (B2B)",
                        shortcode="247247", account_number="0123", expected_name="Equity Bank")
        for doctype, name in ((SUP, paybill), (SUP, till), (EMP, old)):
            frappe.db.set_value(doctype, name, "destination_key", None, update_modified=False)
        party_accounts.after_migrate()
        party_accounts.after_migrate()  # idempotent
        self.assertEqual(frappe.db.get_value(SUP, paybill, "destination_key"), "PB:888882:TP1")
        self.assertEqual(frappe.db.get_value(SUP, till, "destination_key"), "TILL:777772")
        row = frappe.db.get_value(EMP, old, ["account_type", "bank_paybill", "destination_key"], as_dict=True)
        self.assertEqual((row.account_type, row.destination_key), (BANK, "PB:247247:0123"))
        self.assertEqual(row.bank_paybill, frappe.db.get_value(BANKS, {"paybill": "247247"}, "name"))
        for name in (paybill, till):
            self.assertIsNone(supplier_accounts.usable_problem(supplier_accounts.get_account(name), self.supplier))
        self.assertEqual(supplier_accounts.pick_account(self.supplier), paybill)

    # ------------------------------------------------------------ paying a bank account
    def test_a_supplier_bank_account_is_paid_by_b2b_and_kcbs_name_matches(self):
        account = _approved(SUP, "_T-MSA-PAYEE-BK", supplier=self.supplier, account_type=BANK, bank_paybill="KCB",
                            shortcode="522522", account_number="1234567890126", expected_name="KCB Paybill AC",
                            account_holder_name=SUPPLIER, is_default=1)
        with _as(PREPARER):
            request = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "amount": 100, "company": t.COMPANY,
                                      "supplier": self.supplier, "supplier_mpesa_account": account,
                                      "remarks": "T payee bank"}).insert()
        self.assertEqual((request.command_id, request.receiver_shortcode, request.account_reference),
                         ("BusinessPayBill", "522522", "1234567890126"))
        ack = {"ResponseCode": "0", "ResponseDescription": "Accepted", "ConversationID": "AG_T_PAYEE_BK",
               "OriginatorConversationID": "OC_T_PAYEE_BK"}
        with _as(PAYER), send_confirm.deliberate("bench test"), patch("mpsa_suite.money_out.b2b.post",
                                                                      return_value=(ack, None)):
            frappe.get_doc(B2B, request.name).submit()
        result = {
            "ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
            "ConversationID": "AG_T_PAYEE_BK", "OriginatorConversationID": "OC_T_PAYEE_BK",
            "TransactionID": "TPAYEEBK01",
            "ResultParameters": {"ResultParameter": [
                {"Key": "TransCompletedTime", "Value": now_datetime().strftime("%Y%m%d%H%M%S")},
                {"Key": "ReceiverPartyPublicName", "Value": "522522 - KCB Paybill AC"},  # as M-Pesa names a bank
            ]},
        }
        with patch("frappe.enqueue"):
            b2b.handle_result(result, request.name)
        self.assertEqual(frappe.db.get_value(B2B, request.name, ["status", "receiver_name_mismatch"]), ("Paid", 0))
        self.assertEqual(frappe.db.get_value(SUP, account, ["name_mismatch", "last_receiver_name"]),
                         (0, "522522 - KCB Paybill AC"))

    def test_an_employees_till_is_paid_by_buy_goods_with_no_account_number(self):
        till = _approved(EMP, "_T-MEA-PAYEE-TL", employee=self.other_employee, account_type=TILL, shortcode="835819",
                         expected_name="Test Till", is_default=1)
        # a payment to an employee that no document caps is salary data (decision D4): prepared by an
        # Accounts User who may also see salaries (HR User), never by an Accounts role alone
        frappe.get_doc("User", PREPARER).add_roles("HR User")
        try:
            with _as(PREPARER):
                request = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "amount": 1200,
                                          "company": t.COMPANY, "employee": self.other_employee,
                                          "remarks": "T payee till"}).insert()
        finally:
            frappe.get_doc("User", PREPARER).remove_roles("HR User")
        self.assertEqual((request.employee_mpesa_account, request.command_id, request.receiver_shortcode,
                          request.account_reference or None, request.entry_mode),
                         (till, "BusinessBuyGoods", "835819", None, "Make the entry later"))
        ack = {"ResponseCode": "0", "ResponseDescription": "Accepted", "ConversationID": "AG_T_PAYEE_TL",
               "OriginatorConversationID": "OC_T_PAYEE_TL"}
        # no Salary Slip caps it: an M-Pesa Manager sends it, with Send via M-Pesa
        with patch("mpsa_suite.money_out.b2b.post", return_value=(ack, None)) as post:
            request = t.send_as(MANAGER, request)
        payload = post.call_args.args[2]
        # Daraja's Buy Goods body lists AccountReference: the request's name, never an account number
        self.assertEqual((payload["CommandID"], payload["PartyB"], payload["AccountReference"]),
                         ("BusinessBuyGoods", "835819", request.name))
        balance = "{Amount={CurrencyCode=KES, MinimumAmount=500000, BasicAmount=5000.00}}"
        result = {
            "ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
            "ConversationID": "AG_T_PAYEE_TL", "OriginatorConversationID": "OC_T_PAYEE_TL",
            "TransactionID": "TPAYEETL01",
            # what a till's Result holds (Trident, 2026-10-22): a balance and the amount, no receiver
            "ResultParameters": {"ResultParameter": [
                {"Key": "DebitAccountCurrentBalance", "Value": balance},
                {"Key": "InitiatorAccountCurrentBalance", "Value": balance},
                {"Key": "Amount", "Value": 1200.0},
            ]},
        }
        with patch("frappe.enqueue"):
            b2b.handle_result(result, request.name)
        self.assertEqual(frappe.db.get_value(B2B, request.name, ["status", "receiver_name_mismatch"]), ("Paid", 0))
        # a till's Result names no one: nothing is checked, nothing blocked
        self.assertEqual(frappe.db.get_value(EMP, till, ["name_mismatch", "last_receiver_name"]), (0, None))

    # ------------------------------------------------------------ final review (area B): cancel, amend, approve
    def _paid_b2b(self, doctype: str, account: str) -> str:
        """A Paid B2B request naming the account: a paid (or failed) request stays submitted."""
        tag = frappe.generate_hash(length=8).upper()
        values = ({"supplier": self.supplier, "supplier_mpesa_account": account} if doctype == SUP
                  else {"employee": self.other_employee, "employee_mpesa_account": account})
        acc = frappe.db.get_value(doctype, account, ["shortcode", "account_number"], as_dict=True)
        doc = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "amount": 100, "company": t.COMPANY,
                              "command_id": "BusinessPayBill", "receiver_shortcode": acc.shortcode,
                              "account_reference": acc.account_number, "status": "Paid", "docstatus": 1,
                              "transaction_id": f"T{tag}", "remarks": "T paid", "owner": PREPARER,
                              "creation": now_datetime(), "modified": now_datetime(), **values})
        doc.name = f"_T-MB2B-{tag}"
        doc.db_insert()
        return doc.name

    def _amend(self, doctype: str, account: str, user: str):
        """What the desk's Amend makes (frappe.model.copy_doc with from_amend: every field copied, the
        no-copy ones too), saved by ``user``."""
        with _as(user):
            amended = frappe.copy_doc(frappe.get_doc(doctype, account))  # ignore_no_copy, as Amend copies
            amended.docstatus = 0
            amended.amended_from = account
            amended.insert()
        return amended

    def test_an_account_paid_by_b2b_is_cancelled_amended_and_approved_again(self):
        """Before: Frappe refused the cancel (LinkExistsError: linked with Mpesa B2B Payment Request), so a
        compromised number stayed payable and "cancel and amend" could not be followed (v15 and v16)."""
        cases = (
            (SUP, "_T-MSA-PAYEE-RA", PREPARER, dict(supplier=self.supplier, account_type=PAYBILL, shortcode="888885",
                                                     account_number="TP-5", expected_name=SUPPLIER)),
            (EMP, "_T-MEA-PAYEE-RA", HR, dict(employee=self.other_employee, account_type=BANK, bank_paybill="KCB",
                                              shortcode="522522", account_number="1234567890130",
                                              expected_name="KCB Paybill AC", account_holder_name="Test Name")),
        )
        for doctype, name, maker, values in cases:
            # blocked since it was paid: its bank paybill changed, or M-Pesa paid another name
            account = _approved(doctype, name, is_default=1, needs_reapproval=1, name_mismatch=1,
                                last_receiver_name="999999 - SOMEONE ELSE",
                                reapproval_reason="Blocked for the test: cancel and amend it.", **values)
            paid = self._paid_b2b(doctype, account)
            with _as(MANAGER):
                frappe.get_doc(doctype, account).cancel()
            self.assertEqual(frappe.db.get_value(doctype, account, "docstatus"), 2, doctype)
            self.assertEqual(frappe.db.get_value(B2B, paid, ["docstatus", "status"]), (1, "Paid"))  # left as it was
            amended = self._amend(doctype, account, maker)
            self.assertEqual((amended.needs_reapproval, amended.name_mismatch, amended.last_receiver_name),
                             (0, 0, None))  # the old block is not copied into the new account
            approved = t.send_as(MANAGER, frappe.get_doc(doctype, amended.name))
            self.assertEqual((approved.docstatus, approved.approved_by, approved.entered_by), (1, MANAGER, maker))
            acc = party_accounts.get_account(doctype, approved.name)
            party = acc.get(party_accounts.profile(doctype).party_field)
            self.assertIsNone(party_accounts.usable_problem(acc, party, doctype), doctype)
            # the only approved account of the party again: payments that name none go to it
            self.assertEqual(party_accounts.pick_account(doctype, party, party_accounts.B2B_TYPES), approved.name)

    def test_an_account_approved_by_who_entered_it_is_blocked_by_migrate_until_approved_again(self):
        """Handover F3 (Trident MSA-00002, entered and approved with the shared Administrator login): the
        upgrade blocks such accounts; cancel, amend and a named manager approval make one payable again."""
        till = _approved(SUP, "_T-MSA-PAYEE-SELF", supplier=self.supplier, account_type=TILL, shortcode="777774",
                         expected_name=SUPPLIER, is_default=1, owner="Administrator", entered_by=None,
                         approved_by="Administrator")
        named = _approved(SUP, "_T-MSA-PAYEE-NAMED", supplier=self.supplier, account_type=TILL, shortcode="777775",
                          expected_name=SUPPLIER)
        blocked = party_accounts.block_unchecked_approvals()
        self.assertGreaterEqual(blocked[SUP], 1)
        self.assertEqual(frappe.db.get_value(SUP, till, ["needs_reapproval", "reapproval_reason"]),
                         (1, party_accounts.SELF_APPROVED))
        self.assertEqual(frappe.db.get_value(SUP, named, "needs_reapproval"), 0)
        self.assertIsNone(supplier_accounts.pick_account(self.supplier))  # the blocked default is not skipped
        with _as(PREPARER), self.assertRaisesRegex(frappe.ValidationError, "is blocked: Approved by the person"):
            frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "amount": 100, "company": t.COMPANY,
                            "supplier": self.supplier, "remarks": "T blocked"}).insert()
        self.assertEqual(party_accounts.block_unchecked_approvals()[SUP], 0)  # once per account
        with _as(MANAGER):
            frappe.get_doc(SUP, till).cancel()
        self.assertEqual(supplier_accounts.pick_account(self.supplier), named)  # the only one left: the default
        amended = self._amend(SUP, till, PREPARER)
        approved = t.send_as(MANAGER, frappe.get_doc(SUP, amended.name))
        self.assertEqual((approved.docstatus, approved.needs_reapproval, approved.approved_by), (1, 0, MANAGER))
        self.assertIsNone(supplier_accounts.usable_problem(supplier_accounts.get_account(approved.name),
                                                           self.supplier))

    def test_a_new_default_on_hold_is_waited_for_not_skipped(self):
        """Plan B.4: a supplier moved to a new paybill (the default, in its hold period). A request that names
        no account is refused with the hold, never filled with the old paybill; the payer may name it."""
        old = _approved(SUP, "_T-MSA-PAYEE-OLD", supplier=self.supplier, account_type=PAYBILL, shortcode="888886",
                        account_number="TP-6", expected_name=SUPPLIER)
        new = _approved(SUP, "_T-MSA-PAYEE-NEW", supplier=self.supplier, account_type=PAYBILL, shortcode="888887",
                        account_number="TP-7", expected_name=SUPPLIER, is_default=1,
                        active_from=add_days(now_datetime(), 1))
        self.assertIsNone(supplier_accounts.pick_account(self.supplier))
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, f"{new} is on hold until", frappe.get_doc({
                "doctype": B2B, "mpesa_settings": SETTINGS, "amount": 100, "company": t.COMPANY,
                "supplier": self.supplier, "remarks": "T hold"}).insert)
            request = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "amount": 100, "company": t.COMPANY,
                                      "supplier": self.supplier, "supplier_mpesa_account": old,
                                      "remarks": "T named"}).insert()
        self.assertEqual(request.receiver_shortcode, "888886")
