"""M-Pesa Connections against a real site (ERPNext v15 and v16 with HRMS), counted by Frappe itself
(frappe.desk.notifications.get_open_count): B2C payout rows and payment schedule lines (child
tables), B2C requests, receipts and prompts (dynamic links), the suite's own forms, and no count of
salary payouts for who may not see them.

Records are written straight to the database (db_insert): only what links them matters here. Each
test is rolled back. The users are named test users holding one role each (never Administrator or
System Manager). Unit coverage, with what Frappe asks the database transcribed for both versions:
tests/unit/test_r4_connections.py.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.desk.notifications import get_open_count

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import connections

B2B, B2C, B2C_REQUEST = "Mpesa B2B Payment Request", "B2C Payment Disbursement", "B2C Disbursement Request"
SCHEDULE, REGISTER, EXPRESS, MATCH_LOG = ("M-Pesa Payment Schedule", "Mpesa C2B Payment Register",
                                          "Mpesa Express Request", "Mpesa Match Log")
DOMAIN = "r4-connections.example.com"
ACCOUNTS, MANAGER = f"accounts@{DOMAIN}", f"manager@{DOMAIN}"
ROLES = {ACCOUNTS: "Accounts Manager", MANAGER: "M-Pesa Manager"}
SUPPLIER = "_T Connections Supplier"


def _user(email: str, role: str) -> str:
    if not frappe.db.exists("User", email):
        doc = frappe.new_doc("User")
        doc.update({"email": email, "first_name": email.split("@")[0], "user_type": "System User",
                    "send_welcome_email": 0, "enabled": 1})
        doc.append("roles", {"role": role})
        doc.insert(ignore_permissions=True)
    return email


def _supplier() -> str:
    name = frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER}, "name")
    if name:
        return name
    group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    return frappe.get_doc({"doctype": "Supplier", "supplier_name": SUPPLIER, "supplier_group": group}).insert().name


def _raw(doctype: str, **values) -> str:
    """A record written as it stands (no validation): its links are what is counted."""
    doc = frappe.get_doc({"doctype": doctype, **values})
    doc.name = f"_T-{frappe.generate_hash(length=10)}"
    doc.db_insert()
    for table in doc.meta.get_table_fields():
        for idx, row in enumerate(doc.get(table.fieldname) or [], 1):
            row.update({"parent": doc.name, "parenttype": doctype, "parentfield": table.fieldname, "idx": idx,
                        "name": frappe.generate_hash(length=10)})
            row.db_insert()
    return doc.name


@contextmanager
def _counting():
    """As in the request the desk sends to count a form's Connections."""
    request = SimpleNamespace(path="/api/method/frappe.desk.notifications.get_open_count")
    with patch.object(frappe.local, "request", request, create=True), \
            patch.dict(frappe.local.form_dict, {"cmd": "frappe.desk.notifications.get_open_count"}):
        yield


class TestConnections(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        for email, role in ROLES.items():
            _user(email, role)
        cls.supplier = _supplier()
        cls.customer = t.customer("_T Connections Customer")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        if hasattr(frappe.db, "set_execution_timeout"):
            frappe.db.set_execution_timeout(0)  # get_open_count limits each query to 1 s
        frappe.db.rollback()

    @contextmanager
    def as_user(self, user):
        frappe.set_user(user)
        try:
            yield
        finally:
            frappe.set_user("Administrator")

    def _counts(self, doctype, name, user=ACCOUNTS) -> dict:
        with self.as_user(user):
            found = get_open_count(doctype, name)["count"]
        return {d["doctype"]: d["count"] for d in found["external_links_found"] + found["internal_links_found"]}

    # ------------------------------------------------------------ ERPNext forms
    def test_supplier_counts_its_payout_rows_schedule_lines_and_b2c_requests(self):
        supplier = self.supplier
        _raw(B2C_REQUEST, party_type="Supplier", party=supplier, amount=100, status="Paid")
        _raw(B2C_REQUEST, party_type="Employee", party=supplier, amount=100)  # another party type: not this one
        _raw(B2C, party_type="Supplier", company=t.COMPANY, references=[
            {"reference_doctype": "Purchase Invoice", "reference_name": "_T-PINV-1", "party_type": "Supplier",
             "party": supplier, "allocated_amount": 100},
            {"reference_doctype": "Purchase Invoice", "reference_name": "_T-PINV-2", "party_type": "Supplier",
             "party": supplier, "allocated_amount": 50}])
        _raw(SCHEDULE, title="_T", company=t.COMPANY, lines=[
            {"party_type": "Supplier", "party": supplier, "amount": 10, "planned_date": frappe.utils.nowdate()},
            {"party_type": "Supplier", "party": supplier, "amount": 20, "planned_date": frappe.utils.nowdate()}])
        counts = self._counts("Supplier", supplier)
        self.assertEqual(counts[B2C_REQUEST], 1)
        self.assertEqual(counts[B2C], 1)  # one payout, however many of its rows pay the supplier
        self.assertEqual(counts[SCHEDULE], 1)
        self.assertEqual(counts[B2B], 0)

    def test_sales_invoice_counts_the_receipts_prompts_and_matches_of_that_invoice(self):
        si = t.sales_invoice(self.customer, 500)
        _raw(REGISTER, transid="_T" + frappe.generate_hash(length=8), transamount=500,
             matched_reference_doctype="Sales Invoice", matched_reference_name=si.name)
        # the same name as a Sales Order's: not this invoice's
        _raw(REGISTER, transid="_T" + frappe.generate_hash(length=8), transamount=500,
             matched_reference_doctype="Sales Order", matched_reference_name=si.name)
        _raw(EXPRESS, reference_doctype="Sales Invoice", reference_name=si.name, settings=t.SETTINGS, amount=500)
        _raw(MATCH_LOG, reference_doctype="Sales Invoice", reference_name=si.name, result="Matched")
        counts = self._counts("Sales Invoice", si.name)
        self.assertEqual((counts[REGISTER], counts[EXPRESS], counts[MATCH_LOG]), (1, 1, 1))
        self.assertIn("Payment Entry", counts)  # ERPNext's own Connections still counted

    def test_customer_counts_its_receipts(self):
        _raw(REGISTER, transid="_T" + frappe.generate_hash(length=8), transamount=10, customer=self.customer)
        self.assertEqual(self._counts("Customer", self.customer)[REGISTER], 1)

    def test_every_hooked_form_builds_its_mpesa_group_from_fields_the_site_has(self):
        # this app's hooks only: get_hooks merges every app's, and HRMS hooks Holiday List, Task,
        # Project and Timesheet, which have no M-Pesa group
        hooked = frappe.get_hooks("override_doctype_dashboards", app_name="mpsa_suite")
        self.assertIn("Sales Invoice", hooked)
        self.assertNotIn("Holiday List", hooked)
        for doctype in hooked:
            if not frappe.db.exists("DocType", doctype):
                continue
            data = frappe.get_meta(doctype).get_dashboard_data()
            group = next((g for g in data.transactions if g.get("label") == "M-Pesa"), None)
            self.assertTrue(group and group["items"], doctype)
            self.assertTrue(data.fieldname, doctype)
            for source in group["items"]:
                if source in (data.get("internal_links") or {}):
                    continue
                field = (data.non_standard_fieldnames or {}).get(source, data.fieldname)
                meta = frappe.get_meta(source)
                self.assertTrue(meta.has_field(field) or any(
                    frappe.get_meta(df.options).has_field(field) for df in meta.get_table_fields()),
                    f"{doctype}: {source}.{field}")

    # ------------------------------------------------------------ salary payouts
    def test_salary_payouts_are_not_counted_for_who_may_not_see_salaries(self):
        with self.as_user(ACCOUNTS), _counting():
            self.assertTrue(connections.counting())
            employee = frappe.get_meta("Employee").get_dashboard_data()
            supplier = frappe.get_meta("Supplier").get_dashboard_data()
        self.assertEqual(employee.non_standard_fieldnames[B2B], "name")
        self.assertEqual(employee.non_standard_fieldnames[B2C_REQUEST], "name")
        self.assertEqual(supplier.non_standard_fieldnames[B2B], "supplier")
        with self.as_user(MANAGER), _counting():
            employee = frappe.get_meta("Employee").get_dashboard_data()
        self.assertEqual(employee.non_standard_fieldnames[B2B], "employee")
        # an employee's M-Pesa account lists the same salary payouts: the same rule
        with self.as_user(ACCOUNTS), _counting():
            account = frappe.get_meta("Employee M-Pesa Account").get_dashboard_data()
        self.assertEqual((account.non_standard_fieldnames[B2B], account.non_standard_fieldnames[SCHEDULE]),
                         ("name", "name"))
        with self.as_user(MANAGER), _counting():
            account = frappe.get_meta("Employee M-Pesa Account").get_dashboard_data()
        self.assertEqual((account.non_standard_fieldnames[B2B], account.non_standard_fieldnames[SCHEDULE]),
                         ("employee_mpesa_account", "payee_account"))
        # the form's own Connections, built once for everyone, keep every link
        with self.as_user(ACCOUNTS):
            self.assertFalse(connections.counting())
            employee = frappe.get_meta("Employee").get_dashboard_data()
        self.assertEqual(employee.non_standard_fieldnames[B2B], "employee")

    # ------------------------------------------------------------ the suite's own forms
    def test_suite_forms_count_their_records(self):
        b2b = _raw(B2B, supplier=self.supplier, amount=10, mpesa_settings=t.SETTINGS)
        request = _raw(B2C_REQUEST, party_type="Supplier", party=self.supplier, amount=100, mpesa_settings=t.SETTINGS)
        schedule = _raw(SCHEDULE, title="_T", company=t.COMPANY, lines=[
            {"party_type": "Supplier", "party": self.supplier, "amount": 10, "planned_date": frappe.utils.nowdate(),
             "request": b2b},
            {"party_type": "Supplier", "party": self.supplier, "amount": 100, "planned_date": frappe.utils.nowdate(),
             "b2c_request": request}])
        frappe.db.set_value(B2B, b2b, "payment_schedule", schedule)
        register = _raw(REGISTER, transid="_T" + frappe.generate_hash(length=8), transamount=5)
        _raw("Mpesa Reversal Request", c2b_register=register, mpesa_settings=t.SETTINGS)
        _raw(MATCH_LOG, register=register, result="Unmatched")
        _raw(EXPRESS, reference_doctype="Sales Invoice", reference_name="_T-SINV", settings=t.SETTINGS, amount=5)

        self.assertEqual(self._counts(SCHEDULE, schedule, MANAGER)[B2C_REQUEST], 1)  # through its lines
        self.assertEqual(self._counts(B2B, b2b, MANAGER)[SCHEDULE], 1)
        counts = self._counts(REGISTER, register, MANAGER)
        self.assertEqual((counts["Mpesa Reversal Request"], counts[MATCH_LOG]), (1, 1))
        counts = self._counts("Mpesa Settings", t.SETTINGS, MANAGER)
        self.assertGreaterEqual(counts[EXPRESS], 1)
        self.assertGreaterEqual(counts[B2C_REQUEST], 1)
        self.assertGreaterEqual(counts[B2B], 1)

    # ------------------------------------------------------------ a claim's payouts (final review, area B)
    def test_who_reads_a_claim_gets_its_mpesa_payouts_counted(self):
        """Plan C.8: a claim's payouts are not salaries. An Accounts User who is also an Expense Approver (no
        salary visibility) reads the claim, so its B2B request and B2C request are counted on it; an
        employee's salary payouts are still not counted for them."""
        if "hrms" not in frappe.get_installed_apps():
            self.skipTest("HRMS is not installed")
        reader = _user(f"claims@{DOMAIN}", "Accounts User")
        frappe.get_doc("User", reader).add_roles("Expense Approver")
        employee = "_T-EMP-CONNECTIONS"  # written as it stands: only the links are counted
        claim = _raw("Expense Claim", employee=employee, company=t.COMPANY, posting_date=frappe.utils.nowdate(),
                     approval_status="Approved")
        _raw(B2B, employee=employee, expense_claim=claim, amount=10, mpesa_settings=t.SETTINGS, status="Initiated")
        _raw(B2C_REQUEST, party_type="Employee", party=employee, reference_doctype="Expense Claim",
             reference_name=claim, amount=10, mpesa_settings=t.SETTINGS)
        with self.as_user(reader), _counting():  # as the desk asks: the salary rule applies
            found = get_open_count("Expense Claim", claim)["count"]
            claim_data = frappe.get_meta("Expense Claim").get_dashboard_data()
            employee_data = frappe.get_meta("Employee").get_dashboard_data()
        counts = {d["doctype"]: d["count"] for d in found["external_links_found"] + found["internal_links_found"]}
        self.assertEqual((counts[B2B], counts[B2C_REQUEST]), (1, 1))
        self.assertEqual(claim_data.non_standard_fieldnames[B2B], "expense_claim")
        self.assertEqual(employee_data.non_standard_fieldnames[B2B], "name")  # salaries: still no count
