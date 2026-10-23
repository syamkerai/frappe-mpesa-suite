"""permissions.require / is_direct_call on every HTTP route, and the site side (access.py):
Role Profiles, credential-field access under a site's own rules, overrides and the reset."""

import importlib
import types

import fake_frappe
import pytest


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        perms = importlib.import_module("mpsa_suite.permissions")
        yield perms, frappe
    finally:
        fake_frappe.uninstall(saved)


def _request(frappe, path, **form):
    frappe.local.request = types.SimpleNamespace(path=path)
    frappe.local.form_dict = fake_frappe._dict(form)


# ---------------------------------------------------------------- require
def test_require_refuses_without_the_role_even_in_tests(env):
    perms, frappe = env
    frappe.user_roles = {"Accounts User"}
    frappe.flags.in_test = True  # frappe.only_for lets everyone through here on Frappe 15
    frappe.only_for = lambda roles, message=False: None
    with pytest.raises(frappe.PermissionError):
        perms.require(perms.CONFIG_ROLES)
    perms.require(perms.FINANCE_ROLES)


def test_require_lets_administrator_through(env):
    perms, frappe = env
    frappe.user_roles = set()
    frappe.session.user = "Administrator"
    perms.require(perms.CONFIG_ROLES)


def test_sending_money_and_admin_work_are_for_the_suite_roles_only(env):
    # the user's decisions D1 and D2 (2026-10-23)
    perms, _ = env
    assert perms.PAYOUT_ROLES == ("System Manager", "M-Pesa Manager", "M-Pesa Payer")
    assert perms.ADMIN_ROLES == perms.CONFIG_ROLES == ("System Manager", "M-Pesa Manager")
    for role in ("Accounts Manager", "Accounts User", "HR Manager", "HR User"):
        assert role not in perms.PAYOUT_ROLES + perms.ADMIN_ROLES, role


def test_config_roles_are_inside_every_other_set(env):
    perms, _ = env
    for name in ("COLLECT_ROLES", "FINANCE_ROLES", "PAYOUT_ROLES", "ADMIN_ROLES"):
        assert set(perms.CONFIG_ROLES) <= set(getattr(perms, name)), name


# ---------------------------------------------------------------- is_direct_call
@pytest.mark.parametrize(
    "path,form",
    [
        ("/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.initiate_stk_push", {}),
        ("/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.initiate_stk_push/", {}),
        ("/api/method/frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.initiate_stk_push", {}),
        ("/api/v2/method/mpsa_suite.mpsa_suite.api.m_pesa_api.initiate_stk_push", {}),
        # v2 resolves names a DocType's controller module holds, imported ones too
        ("/api/v2/method/Mpesa Express Request/initiate_stk_push", {}),
        ("/", {"cmd": "mpsa_suite.mpsa_suite.api.m_pesa_api.initiate_stk_push"}),
    ],
)
def test_direct_call_on_every_route(env, path, form):
    perms, frappe = env
    _request(frappe, path, **form)
    assert perms.is_direct_call("initiate_stk_push")


@pytest.mark.parametrize(
    "path,form",
    [
        ("/api/method/run_doc_method", {"cmd": "run_doc_method", "method": "initiate_request"}),
        ("/api/method/runserverobj", {"method": "initiate_request"}),
        ("/api/v2/document/Mpesa Express Request/MPESA-1/method/initiate_request", {}),
    ],
)
def test_form_method_routes_count_as_direct(env, path, form):
    perms, frappe = env
    _request(frappe, path, **form)
    assert perms.is_direct_call("initiate_request")


@pytest.mark.parametrize(
    "path,form",
    [
        ("/api/method/frappe.desk.form.save.savedocs", {"action": "Submit"}),
        ("/api/method/mpsa_suite.gateway.v1.create_prompt", {}),
        ("/api/method/run_doc_method", {"cmd": "run_doc_method", "method": "retry_stkpush"}),
        ("/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.initiate_stk_push_later", {}),
    ],
)
def test_document_flows_are_not_direct(env, path, form):
    perms, frappe = env
    _request(frappe, path, **form)
    assert not perms.is_direct_call("initiate_stk_push")
    assert not perms.is_direct_call("initiate_request")


def test_no_request_is_not_direct(env):
    perms, frappe = env
    frappe.local.request = None
    assert not perms.is_direct_call("initiate_stk_push")


def test_require_for_direct_call_gates_the_v2_doctype_route(env):
    perms, frappe = env
    frappe.user_roles = {"Website User"}
    _request(frappe, "/api/v2/method/Mpesa Settings/register_pull_transaction")
    with pytest.raises(frappe.PermissionError):
        perms.require_for_direct_call("register_pull_transaction", perms.ADMIN_ROLES)


# ---------------------------------------------------------------- access.py
class Meta:
    def __init__(self, fields):
        self.fields = [fake_frappe._dict(fieldname=f, permlevel=lvl) for f, lvl in fields.items()]


@pytest.fixture
def site(env):
    _, frappe = env
    access = importlib.import_module("mpsa_suite.access")
    custom = frappe.db.table("Custom DocPerm")
    frappe.cleared = []
    frappe.defaults = {}
    frappe.comments_on_settings = []
    frappe.db.exists = lambda doctype, name=None: doctype in ("DocType", "Role", "Role Profile") and (
        doctype != "Role Profile" or name in frappe.db.table("Role Profile")
    )
    frappe.get_meta = lambda doctype: Meta({"consumer_key": 1, "security_credential": 1, "business_shortcode": 0})
    frappe.clear_cache = lambda doctype=None: frappe.cleared.append(doctype)
    frappe.db.get_default = lambda key: frappe.defaults.get(key)
    frappe.db.set_default = lambda key, value: frappe.defaults.__setitem__(key, value)

    def get_all(doctype, filters=None, fields=None, order_by=None, **kw):
        rows = [r for r in frappe.db.table(doctype).values() if all(r.get(k) == v for k, v in (filters or {}).items())]
        return [fake_frappe._dict({f: r.get(f) for f in fields}) for r in rows] if fields else rows

    frappe.get_all = get_all

    def new_doc(doctype):
        doc = fake_frappe._dict(doctype=doctype, roles=[])
        doc.append = lambda field, row: doc[field].append(row)
        doc.insert = lambda ignore_permissions=False: frappe.db.table(doctype).__setitem__(doc.role_profile, doc)
        return doc

    frappe.new_doc = new_doc
    return access, frappe, custom


def _custom(custom, role, level=0, **rights):
    name = f"CDP-{len(custom) + 1}"
    custom[name] = dict(name=name, parent=rights.pop("parent", "Mpesa Settings"), role=role, permlevel=level,
                        if_owner=0, **{r: rights.get(r, 0) for r in ("read", "write", "create", "submit", "cancel",
                                                                      "amend", "delete", "report", "export",
                                                                      "print", "email", "share")})


def test_role_profiles_created_once_and_never_changed(site):
    access, frappe, _ = site
    created = access.ensure_role_profiles()
    assert created == list(access.matrix.ROLE_PROFILES)
    profile = frappe.db.table("Role Profile")["M-Pesa Approver"]
    assert [r["role"] for r in profile.roles] == ["Accounts User", "M-Pesa Manager", "M-Pesa Viewer"]
    profile.roles.append({"role": "Sales User"})  # an admin edited it
    assert access.ensure_role_profiles() == []
    assert len(profile.roles) == 4


def test_secret_fields_need_nothing_where_the_suite_rules_apply(site):
    access, frappe, custom = site
    assert access.ensure_secret_field_access() == []
    assert custom == {}


def test_secret_fields_follow_the_sites_editors(site):
    access, frappe, custom = site
    inserted = []
    frappe.get_doc = lambda d: types.SimpleNamespace(insert=lambda ignore_permissions=False: inserted.append(d))
    _custom(custom, "System Manager", read=1, write=1, create=1)
    _custom(custom, "Accounts User", read=1, report=1, export=1)  # a reader: must not get level 1
    _custom(custom, "Super Admin", read=1, write=1)
    _custom(custom, "Super Admin", 1, read=1)  # already has a level-1 row: left as the site set it
    added = access.ensure_secret_field_access()
    assert added == [("Mpesa Settings", "System Manager", 1)]
    assert inserted[0]["role"] == "System Manager" and inserted[0]["permlevel"] == 1 and inserted[0]["write"] == 1
    assert "Mpesa Settings" in frappe.cleared


def test_overrides_show_extra_and_missing_rights(site):
    access, frappe, custom = site
    _custom(custom, "System Manager", read=1, write=1, create=1, delete=1, report=1, export=1, print=1, email=1, share=1)
    _custom(custom, "Accounts User", read=1, report=1, export=1)
    _custom(custom, "B2C User", read=1, write=1, create=1, submit=1, parent="B2C Payment Disbursement")
    found = {item["doctype"]: item for item in access.overrides()}
    assert sorted(found) == ["B2C Payment Disbursement", "Mpesa Settings"]
    settings = found["Mpesa Settings"]
    assert {"role": "Accounts User", "permlevel": 0, "rights": ["export", "read", "report"]} in settings["extra"]
    assert {"role": "M-Pesa Manager", "permlevel": 1, "rights": ["read", "write"]} in settings["missing"]
    b2c = found["B2C Payment Disbursement"]
    assert b2c["extra"] == [{"role": "B2C User", "permlevel": 0, "rights": ["create", "read", "submit", "write"]}]


def test_overrides_reported_once_per_change(site):
    access, frappe, custom = site
    _custom(custom, "Accounts User", read=1, export=1)
    access.report_overrides()
    access.report_overrides()
    assert len(frappe.errors) == 1
    _custom(custom, "Sales User", read=1)
    access.report_overrides()
    assert len(frappe.errors) == 2


def test_reset_needs_an_mpesa_admin(site):
    access, frappe, custom = site
    frappe.user_roles = {"Accounts Manager"}
    _custom(custom, "Accounts User", read=1, export=1)
    with pytest.raises(frappe.PermissionError):
        access.reset_permissions(["Mpesa Settings"])
    assert len(custom) == 1


def test_reset_only_suite_doctypes(site):
    access, frappe, _ = site
    frappe.user_roles = {"M-Pesa Manager"}
    with pytest.raises(frappe.ValidationError):
        access.reset_permissions(["User"])
    with pytest.raises(frappe.ValidationError):
        access.reset_permissions(["Payment Gateway"])  # the payments app's


def test_reset_deletes_the_site_rows_and_records_who(site):
    access, frappe, custom = site
    frappe.user_roles = {"M-Pesa Manager"}
    frappe.session.user = "admin@example.com"
    _custom(custom, "Accounts User", read=1, export=1)
    _custom(custom, "B2C User", read=1, submit=1, parent="B2C Payment Disbursement")
    deleted = []
    frappe.db.count = lambda doctype, filters: sum(1 for r in custom.values() if r["parent"] == filters["parent"])
    frappe.db.delete = lambda doctype, filters: deleted.append(filters["parent"])
    frappe.db.sql = lambda *a, **kw: [[0]]
    comments = []
    frappe.get_doc = lambda doctype, *a: types.SimpleNamespace(add_comment=lambda kind, text: comments.append(text))
    access.reset_permissions('["Mpesa Settings"]')
    assert deleted == ["Mpesa Settings"]
    assert "Mpesa Settings" in frappe.cleared
    assert "admin@example.com" in comments[0] and "1 site rows" in comments[0]


# ---------------------------------------------------------------- maker-checker on direct payouts (D3)
class PayoutDoc(fake_frappe._dict):
    """Just what maker_checker reads of a Frappe document."""

    TYPES = {"amount": "Currency", "allocated_amount": "Currency"}

    @property
    def meta(self):
        return types.SimpleNamespace(get_field=lambda f: fake_frappe._dict(fieldtype=self.TYPES.get(f, "Data")))

    def get_doc_before_save(self):
        return self.before


@pytest.fixture
def mc(env, monkeypatch):
    _, frappe = env
    module = importlib.import_module("mpsa_suite.money_out.maker_checker")
    monkeypatch.setattr(module, "enabled", lambda: True)
    frappe.session.user = "payer@example.com"

    def doc(owner="clerk@example.com", modified_by="clerk@example.com", before_amount=3000, **values):
        row = dict(doctype="Mpesa B2B Payment Request", name="MB2B-1", owner=owner, amount=3000,
                   mpesa_settings="S", supplier="ACME", supplier_mpesa_account="MSA-1")
        row.update(values)
        before = PayoutDoc(row, modified_by=modified_by, before=None)
        before["amount"] = before_amount
        return PayoutDoc(row, before=before)

    return module, frappe, doc


def test_another_person_sends_what_the_clerk_prepared(mc):
    module, _, doc = mc
    module.check(doc())


@pytest.mark.parametrize("owner,modified_by", [("payer@example.com", "clerk@example.com"),
                                               ("clerk@example.com", "payer@example.com")])
def test_who_created_or_last_saved_it_cannot_send_it(mc, owner, modified_by):
    module, frappe, doc = mc
    with pytest.raises(frappe.ValidationError, match="another person must send it"):
        module.check(doc(owner=owner, modified_by=modified_by))


def test_a_change_made_while_sending_is_refused(mc):
    module, frappe, doc = mc
    with pytest.raises(frappe.ValidationError, match="changed while being sent"):
        module.check(doc(before_amount=300))
    module.check(doc(before_amount="3000.00"))  # the same amount, as an API might send it


def test_child_rows_count_for_a_b2c_disbursement(mc):
    module, frappe, _ = mc
    rows = [PayoutDoc(party="EMP-1", partyb="254700000001", allocated_amount=1000)]
    changed = [PayoutDoc(party="EMP-1", partyb="254799999999", allocated_amount=1000)]
    base = dict(doctype="B2C Payment Disbursement", name="B2C-1", owner="clerk@example.com", mode_of_payment="Mpesa-X")
    before = PayoutDoc(base, references=rows, modified_by="clerk@example.com", before=None)
    module.check(PayoutDoc(base, references=rows, before=before))
    with pytest.raises(frappe.ValidationError, match="changed while being sent"):
        module.check(PayoutDoc(base, references=changed, before=before))


def test_switched_off_or_administrator_is_not_checked(mc, monkeypatch):
    module, frappe, doc = mc
    frappe.session.user = "Administrator"
    module.check(doc(owner="Administrator", modified_by="Administrator"))
    frappe.session.user = "payer@example.com"
    monkeypatch.setattr(module, "enabled", lambda: False)
    module.check(doc(owner="payer@example.com"))


# ---------------------------------------------------------------- salary payouts (D4)
@pytest.fixture
def sal(env):
    _, frappe = env
    module = importlib.import_module("mpsa_suite.money_out.salary_access")
    frappe.db.escape = lambda value: "'" + str(value).replace("'", "''") + "'"
    frappe.session.user = "clerk@example.com"
    return module, frappe


class SalaryDoc(fake_frappe._dict):
    def is_new(self):
        return False


@pytest.mark.parametrize("roles,expected", [
    ({"HR Manager"}, True), ({"HR User"}, True), ({"M-Pesa Payer"}, True), ({"M-Pesa Manager"}, True),
    ({"Accounts Manager"}, False), ({"Accounts User"}, False), ({"M-Pesa Viewer"}, False),
    ({"Employee"}, False),  # reads only its own slip in HRMS
])
def test_who_sees_salary(sal, roles, expected):
    module, frappe = sal
    frappe.user_roles = roles
    assert module.can_see_salaries() is expected


def test_administrator_sees_salary(sal):
    module, frappe = sal
    frappe.user_roles = set()
    assert module.can_see_salaries("Administrator")


def test_query_condition_hides_salary_rows_but_keeps_the_users_own(sal):
    module, frappe = sal
    frappe.user_roles = {"Accounts User"}
    condition = module.query_conditions("clerk@example.com", doctype="B2C Disbursement Request")
    assert "COALESCE(`tabB2C Disbursement Request`.party_type, '') = 'Employee'" in condition
    assert "owner = 'clerk@example.com'" in condition
    assert module.query_conditions("clerk@example.com", doctype="Supplier") == ""
    frappe.user_roles = {"HR User"}
    assert module.query_conditions("clerk@example.com", doctype="B2C Disbursement Request") == ""


@pytest.mark.parametrize("doc,salary", [
    (dict(doctype="B2C Disbursement Request", party_type="Employee"), True),
    (dict(doctype="B2C Disbursement Request", party_type="Supplier", reference_doctype="Salary Slip"), True),
    (dict(doctype="B2C Disbursement Request", party_type="Supplier"), False),
    (dict(doctype="Mpesa B2B Payment Request", employee="HR-EMP-1"), True),
    (dict(doctype="Mpesa B2B Payment Request", supplier="ACME"), False),
    (dict(doctype="M-Pesa Payment Schedule", purpose="Payroll", lines=[]), True),
    (dict(doctype="M-Pesa Payment Schedule", purpose="General", payroll_entry="HR-PRUN-1", lines=[]), True),
    (dict(doctype="M-Pesa Payment Schedule", purpose="General", lines=[dict(party_type="Employee")]), True),
    (dict(doctype="M-Pesa Payment Schedule", purpose="General", lines=[dict(party_type="Supplier")]), False),
    (dict(doctype="B2C Payment Disbursement", party_type="", references=[dict(reference_doctype="Salary Slip")]), True),
    (dict(doctype="B2C Payment Disbursement", party_type="Supplier", references=[dict(party_type="Supplier")]), False),
])
def test_has_permission_hides_salary_documents(sal, doc, salary):
    module, frappe = sal
    frappe.user_roles = {"Accounts Manager"}
    record = SalaryDoc(doc, owner="hr@example.com")
    assert module.has_permission(record, "read") is (not salary)
    assert module.has_permission(SalaryDoc(doc, owner="clerk@example.com"), "read") is True  # own record
    frappe.user_roles = {"HR Manager"}
    assert module.has_permission(record, "read") is True


def test_salary_hooks_cover_every_salary_doctype(sal):
    import ast
    from pathlib import Path

    from mpsa_suite import permission_matrix as M

    module, _ = sal
    source = (Path(__file__).resolve().parents[2] / "mpsa_suite" / "hooks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    listed = next(ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) and n.targets[0].id == "_SALARY_DOCTYPES")
    assert tuple(listed) == M.SALARY_DOCTYPES
    assert source.count("permission_query_conditions =") == 1 and source.count("has_permission =") == 1
    assert 'permission_query_conditions = {dt: "mpsa_suite.money_out.salary_access.query_conditions"' in source
    assert 'has_permission = {dt: "mpsa_suite.money_out.salary_access.has_permission"' in source
    assert set(module.DOCTYPES) == set(module._NOT_SALARY) == set(M.SALARY_DOCTYPES)
