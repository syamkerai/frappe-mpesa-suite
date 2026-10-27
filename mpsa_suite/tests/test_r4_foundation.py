"""Round 4 foundation against a real site (Frappe/ERPNext v15 and v16 with HRMS): the new M-Pesa
Suite Settings fields after migrate, the Expense Claim / Employee Advance links on the B2B request,
the named-login rule, the desk flags for named users, what maker-checker watches, and the new
form / list scripts and Connections hooks as Frappe loads them.

Unit coverage is in tests/unit/test_r4_foundation.py. Each test is rolled back; the users are
named test users holding one role each (never Administrator or System Manager).
"""

from contextlib import contextmanager

import frappe
from frappe.utils import cint

from mpsa_suite import boot
from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in.testing import allow_test_users
from mpsa_suite.money_out import maker_checker
from mpsa_suite.permissions import require_named_user

SUITE = "M-Pesa Suite Settings"
B2B = "Mpesa B2B Payment Request"
B2C_REQUEST = "B2C Disbursement Request"
DOMAIN = "r4-foundation.example.com"
PAYER, MANAGER, ACCOUNTS, HR = (f"{name}@{DOMAIN}" for name in ("payer", "manager", "accounts", "hr"))
ROLES = {PAYER: "M-Pesa Payer", MANAGER: "M-Pesa Manager", ACCOUNTS: "Accounts User", HR: "HR User"}


def _user(email: str, role: str) -> str:
    if not frappe.db.exists("User", email):
        doc = frappe.new_doc("User")
        doc.update({"email": email, "first_name": email.split("@")[0], "user_type": "System User",
                    "send_welcome_email": 0, "enabled": 1})
        doc.append("roles", {"role": role})
        doc.insert(ignore_permissions=True)
    return email


class TestR4Foundation(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.refusal_after_migrate = frappe.db.get_single_value(SUITE, "refuse_administrator_payouts")
        allow_test_users()
        for email, role in ROLES.items():
            _user(email, role)

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.set_single_value(SUITE, {"refuse_administrator_payouts": 1, "schedule_maker_checker": 1})

    def tearDown(self):
        frappe.set_user("Administrator")

    @contextmanager
    def as_user(self, user):
        frappe.set_user(user)
        try:
            yield
        finally:
            frappe.set_user("Administrator")

    # ------------------------------------------------------------ settings and fields
    def test_settings_fields_after_migrate(self):
        meta = frappe.get_meta(SUITE)
        expected = {"refuse_administrator_payouts": ("Check", "1"), "b2c_approved_phones_only": ("Check", "0"),
                    "claim_payable_workflow_states": ("Small Text", None), "allow_claim_payees": ("Check", "0"),
                    "claim_payee_max_amount": ("Currency", "0"), "claim_payee_daily_cap": ("Currency", "0"),
                    "suggest_supplier_after": ("Int", "3")}
        for fieldname, (fieldtype, default) in expected.items():
            df = meta.get_field(fieldname)
            self.assertIsNotNone(df, fieldname)
            self.assertEqual((df.fieldtype, df.default or None), (fieldtype, default), fieldname)

    def test_the_refusal_is_on_after_migrate(self):
        # what install / migrate left (backfill_single_defaults), read before setUp wrote it
        self.assertEqual(cint(self.refusal_after_migrate), 1)

    def test_b2b_request_links_expense_claims_and_advances(self):
        meta = frappe.get_meta(B2B)
        for fieldname, options in (("expense_claim", "Expense Claim"), ("employee_advance", "Employee Advance")):
            df = meta.get_field(fieldname)
            self.assertIsNotNone(df, fieldname)
            self.assertEqual((df.fieldtype, df.options, cint(df.search_index)), ("Link", options, 1))
            self.assertTrue(frappe.db.has_column(B2B, fieldname), fieldname)
            indexed = frappe.db.sql(f"SHOW INDEX FROM `tab{B2B}` WHERE Column_name = %s", (fieldname,))
            self.assertTrue(indexed, f"{fieldname} has no index")

    # ------------------------------------------------------------ named login
    def test_administrator_may_not_send_or_approve(self):
        with self.assertRaises(frappe.PermissionError):
            require_named_user()
        frappe.flags.in_migrate = True
        try:
            require_named_user()  # a migrate is never refused
        finally:
            frappe.flags.in_migrate = False
        frappe.db.set_single_value(SUITE, "refuse_administrator_payouts", 0)
        require_named_user()

    def test_named_users_are_not_refused(self):
        for user in (PAYER, MANAGER):
            with self.as_user(user):
                require_named_user()

    # ------------------------------------------------------------ desk flags
    def test_desk_flags_of_named_users(self):
        expected = {
            PAYER: dict(can_pay=True, can_see_salaries=True),
            MANAGER: dict(can_prepare=True, can_pay=True, can_approve=True, can_collect=True, can_configure=True,
                          can_see_salaries=True),
            ACCOUNTS: dict(can_prepare=True, can_collect=True),
            HR: dict(can_prepare=True, can_see_salaries=True),
        }
        for user, flags in expected.items():
            with self.as_user(user):
                bootinfo = frappe._dict()
                boot.boot_session(bootinfo)
                self.assertEqual(bootinfo.mpsa, dict(dict.fromkeys(boot.FLAGS, False), **flags), user)

    def test_administrator_gets_no_pay_or_approve_flag(self):
        self.assertIn("mpsa_suite.boot.boot_session", frappe.get_hooks("boot_session"))
        flags = boot.flags("Administrator")
        self.assertFalse(flags["can_pay"] or flags["can_approve"])
        self.assertTrue(flags["can_configure"])

    # ------------------------------------------------------------ maker-checker
    def _sent(self, doctype, values, **changes):
        doc = frappe.get_doc(dict(values, doctype=doctype, owner=ACCOUNTS))
        before = frappe.get_doc(doc.as_dict())
        before.modified_by = ACCOUNTS
        doc.update(changes)
        doc._doc_before_save = before
        return doc

    def test_maker_checker_watches_the_destination_and_the_document(self):
        b2b = {"name": "MB2B-R4-1", "amount": 1500, "mpesa_settings": "X", "command_id": "BusinessPayBill",
               "receiver_shortcode": "522522", "account_reference": "1234567890", "employee": "EMP-X",
               "expense_claim": "EC-X"}
        request = {"name": "B2CR-R4-1", "amount": 700, "phone_number": "254708374149", "mpesa_settings": "X",
                   "party_type": "Employee", "party": "EMP-X", "reference_doctype": "Expense Claim",
                   "reference_name": "EC-X"}
        with self.as_user(PAYER):
            maker_checker.check(self._sent(B2B, b2b))
            maker_checker.check(self._sent(B2C_REQUEST, request))
            for field, value in (("receiver_shortcode", "247247"), ("account_reference", "999"),
                                 ("expense_claim", "EC-Y"), ("employee_advance", "EA-Y")):
                with self.assertRaises(frappe.ValidationError, msg=field):
                    maker_checker.check(self._sent(B2B, b2b, **{field: value}))
            for field, value in (("party", "EMP-Y"), ("reference_name", "EC-Y")):
                with self.assertRaises(frappe.ValidationError, msg=field):
                    maker_checker.check(self._sent(B2C_REQUEST, request, **{field: value}))

    # ------------------------------------------------------------ scripts and Connections
    def test_form_and_list_scripts_load_after_hrms(self):
        from frappe.desk.form.meta import get_meta

        for doctype, form, listing in (("Expense Claim", "expense_claim.js", "expense_claim_list.js"),
                                       ("Employee Advance", "employee_advance.js", "employee_advance_list.js"),
                                       ("Salary Slip", "salary_slip.js", "salary_slip_list.js")):
            meta = get_meta(doctype, cached=False)
            self.assertIn(f"mpsa_suite/public/js/{form}", meta.get("__js") or "", doctype)
            list_js = meta.get("__list_js") or ""
            ours = list_js.find(f"mpsa_suite/public/js/{listing}")
            self.assertGreaterEqual(ours, 0, doctype)
            hrms = list_js.find(f"/doctype/{frappe.scrub(doctype)}/{frappe.scrub(doctype)}_list.js")
            if hrms >= 0:  # HRMS's own list settings first, ours extend them
                self.assertLess(hrms, ours, doctype)
        for doctype, form in (("Sales Order", "sales_order.js"), ("Customer", "customer.js")):
            self.assertIn(f"mpsa_suite/public/js/{form}", get_meta(doctype, cached=False).get("__js") or "")

    def test_connections_still_build_on_every_hooked_doctype(self):
        for doctype in frappe.get_hooks("override_doctype_dashboards"):
            if frappe.db.exists("DocType", doctype):
                frappe.get_meta(doctype).get_dashboard_data()
        groups = frappe.get_meta("Supplier").get_dashboard_data().transactions
        self.assertTrue(any(g.get("label") == "M-Pesa" for g in groups))
