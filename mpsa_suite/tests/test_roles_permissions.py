"""Roles and permissions against a real site (Frappe/ERPNext v15 and v16): one user per role tries
each action, through Frappe's own request routing (frappe.api.handle) where the route matters.

The matrix itself is checked from source by tests/unit/test_permission_matrix.py; this checks what
Frappe makes of it: DocPerms after sync, field-level reads, the v1 / v2 / old-path / form-method
routes, the role gates (which frappe.only_for skipped in Frappe 15 tests), reports, Role Profiles,
and a site's own rules (Custom DocPerm). Nothing reaches Safaricom: the send functions are
replaced, and the network is blocked on the test benches.
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from mpsa_suite import access
from mpsa_suite import permission_matrix as M
from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t

DOMAIN = "roles.example.com"
ROLES = (M.MM, M.MP, M.MC, M.MV, M.AM, M.AU, M.HM, M.HU, M.EA, M.SU, M.PM, M.PU)
PTYPES = ("read", "select", "write", "create", "submit", "cancel", "amend", "delete", "report", "export", "print",
          "email", "share")
SECRETS = M.SECRET_FIELDS["Mpesa Settings"]
API = "mpsa_suite.mpsa_suite.api.m_pesa_api"
SETTINGS_MODULE = "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings"
EXPRESS = "Mpesa Express Request"


def user_for(role: str) -> str:
    return f"{role.lower().replace(' ', '-')}@{DOMAIN}"


WEB_USER = f"website@{DOMAIN}"
NOBODY = f"no-mpesa@{DOMAIN}"  # a desk user with an unrelated role


def _user(email: str, roles=(), user_type: str = "System User") -> str:
    if not frappe.db.exists("User", email):
        doc = frappe.new_doc("User")
        doc.update({"email": email, "first_name": email.split("@")[0], "user_type": user_type,
                    "send_welcome_email": 0, "enabled": 1})
        for role in roles:
            if frappe.db.exists("Role", role):  # HR roles come with ERPNext; skip any a site lacks
                doc.append("roles", {"role": role})
        doc.insert(ignore_permissions=True)
    return email


class TestRolesPermissions(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, {"initiator_name": "testapi", "security_credential": "dummy"})
        for role in ROLES:
            _user(user_for(role), [role])
        _user(WEB_USER, user_type="Website User")
        _user(NOBODY, ["Blogger"] if frappe.db.exists("Role", "Blogger") else [])

    def setUp(self):
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user("Administrator")

    # ------------------------------------------------------------ helpers
    @contextmanager
    def as_user(self, user):
        frappe.set_user(user)
        try:
            yield
        finally:
            frappe.set_user("Administrator")

    def call(self, path: str, **form):
        """POST ``path`` through Frappe's API router as the current user."""
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

    def refused(self, path: str, **form):
        with self.assertRaises(frappe.PermissionError, msg=f"{frappe.session.user} was let through {path}"):
            self.call(path, **form)

    def express_request(self) -> str:
        doc = frappe.get_doc({"doctype": EXPRESS, "payment_gateway": f"Mpesa-{t.SETTINGS}", "settings": t.SETTINGS,
                              "currency": "KES", "base_amount": 10, "amount": 10, "phone_number": "254708374149"})
        doc.insert(ignore_permissions=True)
        return doc.name

    # ------------------------------------------------------------ DocPerms
    def test_doctype_permissions_are_the_matrix(self):
        wrong = []
        for role in ROLES:
            user = user_for(role)
            held = set(frappe.get_roles(user))
            if role not in held:
                continue  # the role does not exist on this site
            for doctype in M.DOCTYPES:
                if doctype in access.NOT_OURS:
                    continue
                rows = M.rows(doctype)
                for ptype in PTYPES:
                    expected = any(ptype in rights or (ptype == "select" and "read" in rights)  # read implies select
                                   for (r, level), rights in rows.items() if r in held and level == 0)
                    if ptype in ("submit", "cancel", "amend") and not frappe.get_meta(doctype).is_submittable:
                        expected = False
                    actual = bool(frappe.has_permission(doctype, ptype, user=user))
                    if actual != expected:
                        wrong.append(f"{role}: {doctype} {ptype} is {actual}, matrix {expected}")
        self.assertEqual(wrong, [])

    def test_settings_below_mpesa_manager_are_picked_not_opened(self):
        """Select only: the record can be chosen in a Link field; its form and fields stay closed."""
        from frappe.desk.search import search_link

        for role in (M.AM, M.AU, M.MV, M.MC, M.MP):
            with self.as_user(user_for(role)):
                self.assertTrue(frappe.has_permission("Mpesa Settings", "select"), role)
                self.assertFalse(frappe.has_permission("Mpesa Settings", "read"), role)
                with self.assertRaises(frappe.PermissionError, msg=role):
                    frappe.client.get("Mpesa Settings", t.SETTINGS)
                found = search_link("Mpesa Settings", t.SETTINGS)
                found = found if found is not None else frappe.response.get("results")
                self.assertIn(t.SETTINGS, str(found), role)
                try:
                    rows = frappe.get_list("Mpesa Settings", fields=["name", "consumer_key", "security_credential",
                                                                     "initiator_name"], filters={"name": t.SETTINGS})
                except frappe.PermissionError:
                    rows = []
                self.assertEqual([k for r in rows for k, v in r.items() if k != "name" and v], [], role)
        with self.as_user(user_for(M.MM)):
            doc = frappe.client.get("Mpesa Settings", t.SETTINGS)
            self.assertEqual(doc.get("consumer_key"), "key")
            self.assertEqual(doc.get("security_credential"), "dummy")

    def test_viewer_reads_and_changes_nothing(self):
        name = self.express_request()
        with self.as_user(user_for(M.MV)):
            for doctype in ("B2C Payment Disbursement", EXPRESS, "Mpesa C2B Payment Register", "Mpesa B2B Payment Request"):
                frappe.get_list(doctype, limit=1)
            frappe.get_doc(EXPRESS, name).check_permission("read")
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc({"doctype": EXPRESS, "payment_gateway": f"Mpesa-{t.SETTINGS}", "currency": "KES",
                                "base_amount": 10, "amount": 10}).insert()

    # ------------------------------------------------------------ routes and role gates
    def test_v2_doctype_route_no_longer_skips_the_gates(self):
        """/api/v2/method/<DocType>/<name> runs names imported into the controller module."""
        stk = dict(payment_gateway=f"Mpesa-{t.SETTINGS}", phone_number="254708374149", request_amount="10")
        with patch(f"{API}.process_request", return_value={}) as sent:
            for user in (WEB_USER, NOBODY, user_for(M.MV), user_for(M.PU)):
                with self.as_user(user):
                    self.refused(f"/api/v2/method/{EXPRESS}/initiate_stk_push", **stk)
                    self.refused("/api/v2/method/Mpesa Settings/get_account_balance", name=t.SETTINGS)
                    self.refused("/api/v2/method/Mpesa Settings/trigger_transaction_status",
                                 mpesa_settings=t.SETTINGS, transaction_id="UJIB2AMYHE")
                    self.refused("/api/v2/method/Mpesa Settings/register_pull_transaction",
                                 mpesa_settings=t.SETTINGS, callback_url="https://attacker.example.com/x")
            self.assertFalse(sent.called)
            with self.as_user(user_for(M.MC)):
                self.call(f"/api/v2/method/{EXPRESS}/initiate_stk_push", **stk)
            self.assertTrue(sent.called)
        with self.as_user(user_for(M.AU)):  # finance, not admin
            self.refused("/api/v2/method/Mpesa Settings/register_pull_transaction", mpesa_settings=t.SETTINGS)
        with self.as_user(user_for(M.MM)):  # through the gate, stopped by the record's own check
            with self.assertRaises(frappe.ValidationError) as caught:
                self.call("/api/v2/method/Mpesa Settings/register_pull_transaction", mpesa_settings=t.SETTINGS)
            self.assertNotIsInstance(caught.exception, frappe.PermissionError)

    def test_v1_and_old_paths_keep_their_gates(self):
        stk = dict(payment_gateway=f"Mpesa-{t.SETTINGS}", phone_number="254708374149", request_amount="10")
        with patch(f"{API}.process_request", return_value={}) as sent, self.as_user(user_for(M.PU)):
            self.refused(f"/api/method/{API}.initiate_stk_push", **stk)
            self.refused("/api/method/frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.initiate_stk_push", **stk)
            self.refused(f"/api/v2/method/{API}.initiate_stk_push", **stk)
            self.assertFalse(sent.called)

    def test_form_buttons_need_more_than_read(self):
        name = self.express_request()
        with patch(f"{API}.process_request", return_value={}) as sent:
            with self.as_user(user_for(M.MV)):
                self.refused("/api/method/run_doc_method", dt=EXPRESS, dn=name, method="initiate_request")
                self.refused("/api/method/run_doc_method", dt="Mpesa Settings", dn=t.SETTINGS,
                             method="get_account_balance_info")
            self.assertFalse(sent.called)
            with self.as_user(user_for(M.MC)):
                self.call("/api/method/run_doc_method", dt=EXPRESS, dn=name, method="initiate_request")
            self.assertTrue(sent.called)
        with patch(f"{SETTINGS_MODULE}.in_test", return_value=False),                 patch(f"{SETTINGS_MODULE}.get_account_balance") as balance:
            with self.as_user(user_for(M.AU)):  # cannot open the settings form (select only)
                self.refused("/api/method/run_doc_method", dt="Mpesa Settings", dn=t.SETTINGS,
                             method="get_account_balance_info")
            with self.as_user(user_for(M.MM)):
                self.call("/api/method/run_doc_method", dt="Mpesa Settings", dn=t.SETTINGS,
                          method="get_account_balance_info")
            balance.assert_called_once_with(t.SETTINGS)
        with patch(f"{API}.process_request", return_value={}),                 patch(f"{API}.generate_security_credential", return_value="x"):
            with self.as_user(user_for(M.AU)):  # Refresh Balances on M-Pesa Payments Due: FINANCE_ROLES
                self.call(f"/api/method/{API}.get_account_balance", name=t.SETTINGS)
            with self.as_user(user_for(M.MV)):
                self.refused(f"/api/method/{API}.get_account_balance", name=t.SETTINGS)

    def test_config_actions_refuse_other_roles_in_tests_too(self):
        """frappe.only_for let everyone through in Frappe 15 tests; permissions.require does not."""
        settings = frappe.get_single("M-Pesa Suite Settings")
        mpesa = frappe.get_doc("Mpesa Settings", t.SETTINGS)
        for role in (M.AM, M.AU, M.MP, M.MV):
            with self.as_user(user_for(role)):
                with self.assertRaises(frappe.PermissionError, msg=role):
                    settings.regenerate_callback_token()
                with self.assertRaises(frappe.PermissionError, msg=role):
                    mpesa.regenerate_security_credential()
                with self.assertRaises(frappe.PermissionError, msg=role):
                    access.permission_report()

    def test_helpers_refuse_strangers(self):
        name = self.express_request()
        with self.as_user(user_for(M.MV)):
            self.refused(f"/api/method/{SETTINGS_MODULE}.get_doctype_fields", doctype="Customer")
        with self.as_user(user_for(M.MM)):
            self.call(f"/api/method/{SETTINGS_MODULE}.get_doctype_fields", doctype="Customer")
        with self.as_user(NOBODY):
            self.refused("/api/method/mpsa_suite.www.mpesa.stkpush.check_payment_status", name=name)
            self.refused("/api/method/mpsa_suite.utils.utils.get_mode_of_payment_account",
                         mode_of_payment=f"Mpesa-{t.SETTINGS}", company=t.COMPANY)
            self.refused("/api/method/mpsa_suite.mpsa_suite.api.sales_invoice.get_payment_gateway_from_mop",
                         mode_of_payment=f"Mpesa-{t.SETTINGS}", company=t.COMPANY)
            self.refused("/api/method/mpsa_suite.mpsa_suite.api.mpesa_quick_pay.get_mpesa_phone_mops_for_pos_profile",
                         pos_profile="x", company=t.COMPANY)
        with self.as_user(user_for(M.HM)):  # prepares payroll disbursements
            self.call("/api/method/mpsa_suite.utils.utils.get_mode_of_payment_account",
                      mode_of_payment=f"Mpesa-{t.SETTINGS}", company=t.COMPANY)

    def test_guest_conversion_helper_is_refused(self):
        with self.as_user("Guest"):
            self.refused("/api/method/mpsa_suite.utils.utils.convert_amount_to_kes", currency="KES", amount="10")
        with self.as_user(user_for(M.MC)):
            self.call("/api/method/mpsa_suite.utils.utils.convert_amount_to_kes", currency="KES", amount="10")

    # ------------------------------------------------------------ reports
    def test_report_roles(self):
        from frappe.desk.query_report import run

        with self.as_user(user_for(M.AU)):
            run("STK Push Request Status", filters={})
        for role in (M.MV, M.MC, M.MP):  # the upstream reports show ERPNext documents
            with self.as_user(user_for(role)), self.assertRaises(frappe.PermissionError, msg=role):
                run("C2B Reconciliation Report", filters={})
        for role in (M.MP, M.MV, M.AU):
            with self.as_user(user_for(role)):
                run("M-Pesa Payments Due", filters={"show": "All"})

    def test_suite_roles_read_every_link_of_their_reports(self):
        """Frappe refuses a report row whose Link column points at a document the reader cannot
        read (query_report.get_filtered_data): a suite role listed on a report reads every target."""
        from frappe.desk.query_report import get_filtered_data

        for report, roles in M.REPORTS.items():
            doc = frappe.get_doc("Report", report)
            module = frappe.get_module(
                f"mpsa_suite.mpsa_suite.report.{frappe.scrub(report)}.{frappe.scrub(report)}")
            columns = (getattr(module, "get_columns", None) or getattr(module, "columns"))()
            row = {c["fieldname"]: "X" for c in columns if c.get("fieldtype") == "Link"}
            for role in set(roles) & set(M.SUITE_ROLES):
                with self.as_user(user_for(role)):
                    try:
                        get_filtered_data(doc.ref_doctype, columns, [frappe._dict(row)], user_for(role))
                    except frappe.PermissionError as e:
                        self.fail(f"{role} on {report}: {e}")

    # ------------------------------------------------------------ site side
    def test_role_profiles_exist(self):
        for name, roles in M.ROLE_PROFILES.items():
            profile = frappe.get_doc("Role Profile", name)
            self.assertEqual(sorted(r.role for r in profile.roles), sorted(roles), name)

    def test_site_rules_reported_secrets_kept_for_editors_and_reset(self):
        doctype = "Mpesa Settings"
        rows = [dict(role=M.SM, read=1, write=1, create=1, delete=1, report=1, export=1),
                dict(role=M.AU, read=1, report=1, export=1)]
        try:
            for i, row in enumerate(rows, 1):
                frappe.get_doc(dict(doctype="Custom DocPerm", parent=doctype, parenttype="DocType",
                                    parentfield="permissions", permlevel=0, idx=i, **row)).insert(ignore_permissions=True)
            frappe.clear_cache(doctype=doctype)

            self.assertEqual(access.ensure_secret_field_access(), [(doctype, M.SM, 1)])
            self.assertEqual(access.ensure_secret_field_access(), [])  # idempotent
            sm_user = _user(f"sysmgr@{DOMAIN}", [M.SM])
            with self.as_user(sm_user):
                self.assertEqual(frappe.client.get(doctype, t.SETTINGS).get("consumer_key"), "key")
            with self.as_user(user_for(M.AU)):
                self.assertTrue(frappe.has_permission(doctype, "export"))  # the site's own rule
                self.assertIsNone(frappe.client.get(doctype, t.SETTINGS).get("consumer_key"))
            self.assertFalse(frappe.has_permission(doctype, "read", user=user_for(M.MM)))

            found = {i["doctype"]: i for i in access.overrides()}[doctype]
            self.assertIn({"role": M.AU, "permlevel": 0, "rights": ["export", "read", "report"]}, found["extra"])
            self.assertIn({"role": M.MM, "permlevel": 1, "rights": ["read", "write"]}, found["missing"])

            with self.as_user(user_for(M.AM)), self.assertRaises(frappe.PermissionError):
                access.reset_permissions([doctype])
            with self.as_user(user_for(M.MM)):
                report = access.reset_permissions([doctype])
            self.assertNotIn(doctype, [i["doctype"] for i in report["overrides"]])
            self.assertFalse(frappe.db.exists("Custom DocPerm", {"parent": doctype}))
            self.assertFalse(frappe.has_permission(doctype, "export", user=user_for(M.AU)))
            self.assertTrue(frappe.has_permission(doctype, "read", user=user_for(M.MM)))
            comment = frappe.get_all("Comment", filters={"reference_doctype": "M-Pesa Suite Settings"},
                                     fields=["content"], order_by="creation desc", limit=1)
            self.assertIn(doctype, comment[0].content)
        finally:
            frappe.db.delete("Custom DocPerm", {"parent": doctype})
            frappe.clear_cache(doctype=doctype)

    # ------------------------------------------------------------ the user's decisions (2026-10-23)
    PAYOUTS = ("B2C Payment Disbursement", "B2C Disbursement Request", "Mpesa B2B Payment Request",
               "Mpesa Reversal Request")

    def test_only_the_payer_roles_send_money(self):
        """D1: Accounts Manager and HR Manager prepare payouts; they do not send them. Round 4: nor does
        System Manager (only named payers send). Checked inside the deliberate step (Send via M-Pesa's
        confirm), where the role gate is what refuses them."""
        from mpsa_suite.money_out.send_confirm import deliberate

        sm_user = _user(f"sysmgr@{DOMAIN}", [M.SM])
        for doctype in self.PAYOUTS:
            for user in [user_for(r) for r in (M.AM, M.HM, M.AU, M.HU, M.MV)] + [sm_user]:
                with self.as_user(user):
                    doc = frappe.new_doc(doctype)
                    with deliberate("test"):
                        with self.assertRaises(frappe.PermissionError, msg=f"{user} could send a {doctype}"):
                            doc.before_submit()

    def test_no_plain_submit_sends_money_and_administrator_never_does(self):
        """Round 4 (decision 2): a payout is sent only by Send via M-Pesa; the list's Submit, REST,
        frappe.client.submit and run_doc_method all end in before_submit, which refuses them, a payer
        too. The shared Administrator login is refused even inside the deliberate step (decision 1)."""
        from mpsa_suite.money_out.send_confirm import deliberate

        frappe.db.set_single_value("M-Pesa Suite Settings", "refuse_administrator_payouts", 1)
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
        for doctype in self.PAYOUTS:
            with self.as_user(user_for(M.MP)):
                with self.assertRaisesRegex(frappe.ValidationError, "Send via M-Pesa", msg=doctype):
                    frappe.new_doc(doctype).before_submit()
            with deliberate("test"):
                with self.assertRaisesRegex(frappe.PermissionError, "Use your own login", msg=doctype):
                    frappe.new_doc(doctype).before_submit()

    def test_send_via_mpesa_is_for_the_payer_roles_on_every_route(self):
        name = self._raw({"doctype": "Mpesa B2B Payment Request", "amount": 1500, "supplier": "_T Nobody",
                          "mpesa_settings": t.SETTINGS}, user_for(M.AU))
        path = "/api/method/mpsa_suite.money_out.send_confirm.prepare_send"
        for role in (M.AM, M.AU, M.MV, M.HM):
            with self.as_user(user_for(role)):
                self.refused(path, doctype="Mpesa B2B Payment Request", name=name)
        from mpsa_suite.money_out import send_confirm

        confirm = "/api/method/mpsa_suite.money_out.send_confirm.confirm_send"
        with patch.object(send_confirm, "_clock", return_value=1000.0):  # no time passes between the calls
            with self.as_user(user_for(M.MP)):  # through the gate: the summary of what it would send
                answer = json.loads(self.call(path, doctype="Mpesa B2B Payment Request", name=name).get_data())
            self.assertEqual((answer["message"]["name"], answer["message"]["action"]), (name, "Send via M-Pesa"))
            token = answer["message"]["token"]
            self.assertTrue(token)
            # Confirm through the API: not for another payer, not before the 5 seconds (the token stays)
            with self.as_user(user_for(M.MM)), self.assertRaisesRegex(frappe.ValidationError, "made for another user"):
                self.call(confirm, doctype="Mpesa B2B Payment Request", name=name, token=token)
            with self.as_user(user_for(M.MP)), self.assertRaisesRegex(frappe.ValidationError, "Read the summary first"):
                self.call(confirm, doctype="Mpesa B2B Payment Request", name=name, token=token)
        self.assertEqual(frappe.db.get_value("Mpesa B2B Payment Request", name, "docstatus"), 0)
        self.assertTrue(send_confirm.get_cache().get_value(send_confirm._token_key(token)))  # not used up

    def test_system_manager_configures_and_reads_but_prepares_nothing(self):
        """Decision 1 (2026-10-27): System Manager reads the payouts, their schedule and the supplier
        accounts; it creates, changes and deletes none of them, has no access at all to Employee M-Pesa
        Accounts (employees' salary phones and bank accounts), and the schedule's prepare endpoints
        refuse it. Its rights on the suite's settings stay."""
        from mpsa_suite.money_out import payroll, schedule
        from mpsa_suite.mpsa_suite.doctype.employee_m_pesa_account import (
            employee_m_pesa_account,
        )

        sm_user = _user(f"sysmgr@{DOMAIN}", [M.SM])
        for doctype in ("B2C Disbursement Request", "B2C Payment Disbursement", "Mpesa B2B Payment Request",
                        "Mpesa Reversal Request", "Supplier M-Pesa Account", "M-Pesa Payment Schedule"):
            for ptype in ("read", "report", "export", "print"):
                self.assertTrue(frappe.has_permission(doctype, ptype, user=sm_user), f"{doctype} {ptype}")
            for ptype in ("create", "write", "delete", "submit", "cancel", "amend", "email", "share"):
                self.assertFalse(frappe.has_permission(doctype, ptype, user=sm_user), f"{doctype} {ptype}")
        for ptype in ("read", "select", "report", "export", "create", "write"):
            self.assertFalse(frappe.has_permission("Employee M-Pesa Account", ptype, user=sm_user), ptype)
        for doctype in ("M-Pesa Suite Settings", "Mpesa Settings", "M-Pesa Bank Paybill", "Mpesa API Client"):
            self.assertTrue(frappe.has_permission(doctype, "write", user=sm_user), doctype)
        with self.as_user(sm_user):
            self.assertRaises(frappe.PermissionError, employee_m_pesa_account.accounts_for_employee, "HR-EMP-X")
            for name, call in (("move_date", lambda: schedule.move_date(["X"], frappe.utils.nowdate())),
                               ("plan_again", lambda: schedule.plan_again("X")),
                               ("close_schedule", lambda: schedule.close_schedule("X")),
                               ("from_payroll_entry", lambda: payroll.from_payroll_entry("X"))):
                with self.assertRaises(frappe.PermissionError, msg=name):
                    call()

    def test_who_reads_a_claim_or_advance_by_default_may_prepare_its_payouts(self):
        """Round 4 final review (plan section 1, Roles): with HRMS's own DocPerms and the suite's alone (no site
        grant, nothing shipped on HRMS DocTypes), each default role that reads Expense Claims (HR Manager, HR
        User, Expense Approver) or Employee Advances (Expense Approver) passes every server gate of Pay employee
        on both routes (employee_documents.prepare_b2c / prepare_b2b / prepare_bulk: PREPARE_ROLES, read on
        the document's DocType, create on the payout), and its desk shows the buttons (boot can_prepare)."""
        from mpsa_suite import boot
        from mpsa_suite.permissions import PREPARE_ROLES

        if "hrms" not in frappe.get_installed_apps():
            self.skipTest("HRMS is not installed")
        readers = {"Expense Claim": (M.HM, M.HU, M.EA), "Employee Advance": (M.EA,)}
        for doctype, roles in readers.items():
            for role in roles:
                user = user_for(role)
                self.assertIn(role, frappe.get_roles(user), role)
                self.assertIn(role, PREPARE_ROLES)
                self.assertTrue(frappe.has_permission(doctype, "read", user=user), f"HRMS: {role} reads {doctype}")
                for payout in ("B2C Payment Disbursement", "Mpesa B2B Payment Request"):
                    self.assertTrue(frappe.has_permission(payout, "create", user=user), f"{role} creates {payout}")
                    for ptype in ("submit", "cancel"):  # prepares, never sends
                        self.assertFalse(frappe.has_permission(payout, ptype, user=user), f"{role} {ptype} {payout}")
                self.assertTrue(boot.flags(user)["can_prepare"], role)
                self.assertFalse(boot.flags(user)["can_pay"], role)
        # an Expense Approver sees no salary (a claim's payout is not one)
        self.assertFalse(boot.flags(user_for(M.EA))["can_see_salaries"])

    def test_only_mpesa_managers_do_admin_work(self):
        """D2: approving payee accounts and the other ADMIN_ROLES actions left Accounts Manager; D5:
        System Manager too. Inside the deliberate step (Approve's confirm), where the roles decide."""
        from mpsa_suite.money_out import supplier_accounts
        from mpsa_suite.money_out.send_confirm import deliberate

        account = frappe.new_doc("Supplier M-Pesa Account")
        account.owner = user_for(M.AU)
        account.proof = "/files/proof.pdf"
        with deliberate("test"):
            for user in [user_for(r) for r in (M.AM, M.AU, M.MP)] + [_user(f"sysmgr@{DOMAIN}", [M.SM])]:
                with self.as_user(user), self.assertRaises(frappe.PermissionError, msg=user):
                    supplier_accounts.approve(account)
            with self.as_user(user_for(M.MM)):
                supplier_accounts.approve(account)
        self.assertEqual(account.approved_by, user_for(M.MM))

    def test_maker_checker_on_direct_payouts(self):
        """D3: who sends must not be who created or last saved it, nor change it while sending."""
        from mpsa_suite.money_out import maker_checker

        def disbursement(owner, modified_by, partyb="254708374149"):
            doc = frappe.get_doc({"doctype": "B2C Payment Disbursement", "name": "B2C-MC-1", "owner": owner,
                                  "mode_of_payment": f"Mpesa-{t.SETTINGS}",
                                  "references": [{"party": "EMP-1", "partyb": partyb, "allocated_amount": 1500}]})
            before = frappe.get_doc(doc.as_dict())
            before.modified_by = modified_by
            before.references[0].partyb = "254708374149"
            doc._doc_before_save = before
            return doc

        clerk, payer = user_for(M.AU), user_for(M.MP)
        frappe.db.set_single_value("M-Pesa Suite Settings", "schedule_maker_checker", 1)
        with self.as_user(payer):
            maker_checker.check(disbursement(clerk, clerk))
            for owner, saver in ((payer, clerk), (clerk, payer)):
                with self.assertRaises(frappe.ValidationError):
                    maker_checker.check(disbursement(owner, saver))
            with self.assertRaises(frappe.ValidationError):
                maker_checker.check(disbursement(clerk, clerk, partyb="254799999999"))
        frappe.db.set_single_value("M-Pesa Suite Settings", "schedule_maker_checker", 0)
        try:
            with self.as_user(payer):
                maker_checker.check(disbursement(payer, payer))  # one-person team
        finally:
            frappe.db.set_single_value("M-Pesa Suite Settings", "schedule_maker_checker", 1)

    def test_a_payer_cannot_rewrite_what_m_pesa_answered(self):
        """A save after submit needs submit (Frappe), so the payers: one cannot save a payout as Failed
        or never sent (status and send marker allow on submit for the Result), which a payment
        schedule would pay again. The preparers cannot save a submitted payout at all."""
        clerk, payer = user_for(M.AU), user_for(M.MP)
        name = self._raw({"doctype": "B2C Disbursement Request", "amount": 700, "party_type": "Supplier",
                          "party": "X", "phone_number": "254708374149", "mpesa_settings": t.SETTINGS}, clerk)
        started = frappe.utils.now_datetime().replace(microsecond=0)
        frappe.db.set_value("B2C Disbursement Request", name,
                            {"docstatus": 1, "status": "Not Initiated", "send_started_on": started})
        for user in (clerk, payer):
            with self.as_user(user):
                doc = frappe.get_doc("B2C Disbursement Request", name)
                doc.update({"status": "Failed", "send_started_on": None})
                doc.flags.ignore_links = doc.flags.ignore_mandatory = True  # fixtures, not permissions
                if user == clerk:
                    self.assertRaises(frappe.PermissionError, doc.save)
                else:
                    doc.save()
        saved = frappe.db.get_value("B2C Disbursement Request", name, ["status", "send_started_on"], as_dict=True)
        self.assertEqual((saved.status, frappe.utils.get_datetime(saved.send_started_on)), ("Not Initiated", started))
        doc = frappe.get_doc("B2C Disbursement Request", name)
        doc.status = "Paid"
        doc.flags.ignore_links = doc.flags.ignore_mandatory = True
        doc.save(ignore_permissions=True)  # as the Result does
        self.assertEqual(frappe.db.get_value("B2C Disbursement Request", name, "status"), "Paid")

    def test_payer_opens_payments_due(self):
        """What the M-Pesa Payments Due page asks of the server when it loads, as a payer."""
        import inspect

        from frappe.desk import query_report
        from frappe.desk.search import search_link

        js_filters = [{"fieldname": "company", "fieldtype": "Link", "options": "Company"},
                      {"fieldname": "show", "fieldtype": "Select"}, {"fieldname": "route", "fieldtype": "Select"},
                      {"fieldname": "pay_from", "fieldtype": "Link", "options": "Mpesa Settings"},
                      {"fieldname": "schedule", "fieldtype": "Link", "options": "M-Pesa Payment Schedule"}]
        filters = {"company": t.COMPANY, "show": "All", "pay_from": t.SETTINGS}
        extra = {"js_filters": js_filters} if "js_filters" in inspect.signature(query_report.run).parameters else {}
        validate = "validate_link_and_fetch" if hasattr(frappe.client, "validate_link_and_fetch") else "validate_link"
        officer = _user(f"paying-officer@{DOMAIN}", [M.AU, M.MP, M.MV])
        steps = {
            "get_script": lambda: query_report.get_script("M-Pesa Payments Due"),
            "run": lambda: query_report.run("M-Pesa Payments Due", filters=filters, **extra),
            "search Company": lambda: search_link("Company", ""),
            "search Mpesa Settings": lambda: search_link("Mpesa Settings", ""),
            "validate Company": lambda: self.call(f"/api/method/frappe.client.{validate}", doctype="Company",
                                                  docname=t.COMPANY),
            "validate Mpesa Settings": lambda: self.call(f"/api/method/frappe.client.{validate}",
                                                         doctype="Mpesa Settings", docname=t.SETTINGS),
        }
        failed = []
        for user in (user_for(M.MP), officer):
            with self.as_user(user):
                for label, step in steps.items():
                    frappe.local.message_log = []
                    try:
                        step()
                    except Exception as e:
                        failed.append(f"{user} {label}: {type(e).__name__}: {e}")
        self.assertEqual(failed, [])

    # ------------------------------------------------------------ salary payouts (decision D4)
    def _raw(self, values, owner):
        """A record as the payout code leaves it, without its validations (they need full fixtures)."""
        doc = frappe.get_doc(values)
        doc.flags.ignore_validate = doc.flags.ignore_links = doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        frappe.db.set_value(doc.doctype, doc.name, "owner", owner, update_modified=False)
        return doc.name

    def test_salary_payouts_only_for_hr_and_the_pay_roles(self):
        hr = user_for(M.HM)
        salary = {
            "B2C Disbursement Request": self._raw({"doctype": "B2C Disbursement Request", "amount": 1500,
                                                   "party_type": "Employee", "party": "HR-EMP-X",
                                                   "phone_number": "254708374149", "mpesa_settings": t.SETTINGS}, hr),
            "Mpesa B2B Payment Request": self._raw({"doctype": "Mpesa B2B Payment Request", "amount": 1500,
                                                    "employee": "HR-EMP-X", "mpesa_settings": t.SETTINGS}, hr),
            "M-Pesa Payment Schedule": self._raw({"doctype": "M-Pesa Payment Schedule", "title": "T payroll",
                                                  "company": t.COMPANY, "purpose": "Payroll"}, hr),
        }
        supplier = self._raw({"doctype": "B2C Disbursement Request", "amount": 1500, "party_type": "Supplier",
                              "party": "X", "phone_number": "254708374149", "mpesa_settings": t.SETTINGS}, hr)
        for role in (M.AU, M.AM, M.MV):
            user = user_for(role)
            for doctype, name in salary.items():
                if not frappe.has_permission(doctype, "read", user=user):
                    continue  # the role reads none of this DocType anyway
                self.assertFalse(frappe.has_permission(doctype, "read", doc=name, user=user), f"{role} {doctype}")
                with self.as_user(user):
                    self.assertNotIn(name, frappe.get_list(doctype, filters={"name": name}, pluck="name"), role)
            with self.as_user(user):
                if frappe.has_permission("B2C Disbursement Request", "read"):
                    self.assertIn(supplier, frappe.get_list("B2C Disbursement Request",
                                                            filters={"name": supplier}, pluck="name"), role)
        for role in (M.HM, M.MP, M.MM):
            user = user_for(role)
            for doctype, name in salary.items():
                if frappe.has_permission(doctype, "read", user=user):
                    self.assertTrue(frappe.has_permission(doctype, "read", doc=name, user=user), f"{role} {doctype}")
        own = self._raw({"doctype": "B2C Disbursement Request", "amount": 900, "party_type": "Employee",
                         "party": "HR-EMP-Y", "phone_number": "254708374149", "mpesa_settings": t.SETTINGS},
                        user_for(M.AU))
        self.assertTrue(frappe.has_permission("B2C Disbursement Request", "read", doc=own, user=user_for(M.AU)))
        self.assertFalse(frappe.has_permission("Employee M-Pesa Account", "read", user=user_for(M.AU)))
        self.assertFalse(frappe.has_permission("Employee M-Pesa Account", "read", user=user_for(M.MV)))

    def test_salary_lines_stay_out_of_the_reports(self):
        from mpsa_suite.mpsa_suite.report.b2c_disbursement_summary import b2c_disbursement_summary as summary
        from mpsa_suite.mpsa_suite.report.m_pesa_payments_due import m_pesa_payments_due as due

        hr = user_for(M.HM)
        schedule = self._raw({"doctype": "M-Pesa Payment Schedule", "title": "T payroll lines", "company": t.COMPANY,
                              "purpose": "Payroll", "lines": [{"planned_date": frappe.utils.nowdate(),
                                                               "party_type": "Employee", "party": "HR-EMP-X",
                                                               "amount": 1200, "status": "Planned"}]}, hr)
        payroll = self._raw({"doctype": "B2C Payment Disbursement", "company": t.COMPANY, "party_type": "Employee",
                             "posting_date": frappe.utils.nowdate(), "payment_type": "Mpesa Disbursement",
                             "references": [{"party_type": "Employee", "party": "HR-EMP-X",
                                             "allocated_amount": 1200}]}, hr)
        with self.as_user(user_for(M.AU)):
            self.assertNotIn(schedule, {r.parent for r in due._rows({"show": "All"})})
        with self.as_user(user_for(M.MP)):
            self.assertIn(schedule, {r.parent for r in due._rows({"show": "All"})})
        with self.as_user(user_for(M.AM)):
            self.assertNotIn(payroll, str(summary.get_data({})))
        with self.as_user(user_for(M.HM)):
            self.assertIn(payroll, str(summary.get_data({})))
