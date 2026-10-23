"""Roles and permissions against a real site (Frappe/ERPNext v15 and v16): one user per role tries
each action, through Frappe's own request routing (frappe.api.handle) where the route matters.

The matrix itself is checked from source by tests/unit/test_permission_matrix.py; this checks what
Frappe makes of it: DocPerms after sync, field-level reads, the v1 / v2 / old-path / form-method
routes, the role gates (which frappe.only_for skipped in Frappe 15 tests), reports, Role Profiles,
and a site's own rules (Custom DocPerm). Nothing reaches Safaricom: the send functions are
replaced, and the network is blocked on the test benches.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe

from mpsa_suite import access
from mpsa_suite import permission_matrix as M
from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t

DOMAIN = "roles.example.com"
ROLES = (M.MM, M.MP, M.MC, M.MV, M.AM, M.AU, M.HM, M.HU, M.SU, M.PM, M.PU)
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
    def test_only_the_payer_roles_send_money(self):
        """D1: Accounts Manager and HR Manager prepare payouts; they do not send them."""
        for doctype in ("B2C Payment Disbursement", "B2C Disbursement Request", "Mpesa B2B Payment Request",
                        "Mpesa Reversal Request"):
            for role in (M.AM, M.HM, M.AU, M.HU, M.MV):
                with self.as_user(user_for(role)):
                    doc = frappe.new_doc(doctype)
                    with self.assertRaises(frappe.PermissionError, msg=f"{role} could send a {doctype}"):
                        doc.before_submit()

    def test_only_mpesa_managers_do_admin_work(self):
        """D2: approving payee accounts and the other ADMIN_ROLES actions left Accounts Manager."""
        from mpsa_suite.money_out import supplier_accounts

        account = frappe.new_doc("Supplier M-Pesa Account")
        account.owner = user_for(M.AU)
        account.proof = "/files/proof.pdf"
        for role in (M.AM, M.AU, M.MP):
            with self.as_user(user_for(role)), self.assertRaises(frappe.PermissionError, msg=role):
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
