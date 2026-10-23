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
    assert {"role": "Accounts User", "permlevel": 0, "rights": ["export"]} in settings["extra"]
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
