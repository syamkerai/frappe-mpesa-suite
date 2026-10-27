"""Payee accounts, round 4, against the real database (ERPNext v15 and v16 with HRMS): the shipped
bank paybill list and its sync, the four account types, approval by a named manager, Make Default,
a bank paybill change blocking the accounts on it, and a supplier bank account paid by B2B.

Every send and approval is made by a named test user holding the right role (never Administrator,
never System Manager), inside send_confirm.deliberate so the deliberate-step rule lets the test's
own submit through. Nothing reaches Safaricom: post() is replaced and the Result is handed to
handle_result the way Safaricom posts it. Each test rolls back. Unit coverage:
tests/unit/test_payee_accounts.py and test_bank_paybills.py.
"""

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import frappe
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
SUPPLIER = "_T Payee Supplier"
SUITE_VALUES = dict(allow_one_off_b2b=0, schedule_maker_checker=1, supplier_account_hold_hours=0,
                    employee_account_hold_hours=0, book_payout_charges=0, reconcile_payout_lines=0)


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
