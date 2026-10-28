"""Round 4 foundation: the named-login rule (permissions.require_named_user), PREPARE_ROLES, the
desk flags (boot.py), what maker-checker watches, the new hooks and their stubs, the new M-Pesa
Suite Settings fields and the Expense Claim / Employee Advance links on the B2B request."""

import ast
import importlib
import json
import sys
import types
from pathlib import Path

import fake_frappe
import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
SETTINGS_JSON = APP / "mpsa_suite" / "doctype" / "m_pesa_suite_settings" / "m_pesa_suite_settings.json"
ALL_ROLES = {"System Manager", "M-Pesa Manager", "M-Pesa Payer", "M-Pesa Collector", "Accounts Manager",
             "Accounts User", "HR Manager", "HR User", "Sales User"}


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        perms = importlib.import_module("mpsa_suite.permissions")
        yield perms, frappe
    finally:
        fake_frappe.uninstall(saved)


def _settings(frappe, **values):
    frappe.get_cached_doc = lambda doctype, *a, **kw: fake_frappe._dict(values)


# ---------------------------------------------------------------- PREPARE_ROLES
def test_preparers_are_accounts_hr_and_the_mpesa_manager(env):
    perms, _ = env
    # final review: HRMS's Expense Approver too, the only default role reading Employee Advances
    assert perms.PREPARE_ROLES == ("M-Pesa Manager", "Accounts Manager", "Accounts User", "HR Manager", "HR User",
                                   "Expense Approver")
    assert "M-Pesa Payer" not in perms.PREPARE_ROLES and "System Manager" not in perms.PREPARE_ROLES


# ---------------------------------------------------------------- require_named_user
def test_a_named_user_is_never_refused(env):
    perms, frappe = env
    _settings(frappe, refuse_administrator_payouts=1)
    frappe.session.user = "payer@example.com"
    perms.require_named_user()
    assert perms.administrator_refused() is False


@pytest.mark.parametrize("values", [dict(refuse_administrator_payouts=1), dict(refuse_administrator_payouts="1"),
                                    dict(refuse_administrator_payouts=None), {}])
def test_administrator_is_refused_while_the_setting_is_on_or_not_yet_migrated(env, values):
    perms, frappe = env
    _settings(frappe, **values)
    frappe.session.user = "Administrator"
    with pytest.raises(frappe.PermissionError, match="Use your own login"):
        perms.require_named_user()


def test_administrator_is_refused_without_the_settings_record(env):
    perms, frappe = env  # fake_frappe.get_cached_doc raises DoesNotExistError
    frappe.session.user = "Administrator"
    with pytest.raises(frappe.PermissionError, match="shared Administrator login is not allowed"):
        perms.require_named_user()


@pytest.mark.parametrize("value", [0, "0"])
def test_administrator_may_send_when_the_setting_is_off(env, value):
    perms, frappe = env
    _settings(frappe, refuse_administrator_payouts=value)
    frappe.session.user = "Administrator"
    perms.require_named_user()


@pytest.mark.parametrize("flag", ["in_install", "in_migrate", "in_patch"])
def test_install_migrate_and_patches_are_not_refused(env, flag):
    perms, frappe = env
    _settings(frappe, refuse_administrator_payouts=1)
    frappe.session.user = "Administrator"
    frappe.flags[flag] = True
    perms.require_named_user()


def test_bench_tests_are_refused_too(env):
    # tests send and approve as named users holding the right roles
    perms, frappe = env
    _settings(frappe, refuse_administrator_payouts=1)
    frappe.session.user = "Administrator"
    frappe.flags.in_test = True
    with pytest.raises(frappe.PermissionError):
        perms.require_named_user()


def test_another_user_can_be_asked_about(env):
    perms, frappe = env
    _settings(frappe, refuse_administrator_payouts=1)
    frappe.session.user = "payer@example.com"
    assert perms.administrator_refused("Administrator") is True


# ---------------------------------------------------------------- desk flags (boot.py)
@pytest.fixture
def boot(env):
    perms, frappe = env
    _settings(frappe, refuse_administrator_payouts=1)
    return importlib.import_module("mpsa_suite.boot"), frappe


@pytest.mark.parametrize("roles,expected", [
    ({"Accounts User"}, dict(can_prepare=True, can_collect=True)),
    ({"HR User"}, dict(can_prepare=True, can_see_salaries=True)),
    ({"Expense Approver"}, dict(can_prepare=True)),  # prepares the claims and advances it reads, sees no salary
    ({"M-Pesa Payer"}, dict(can_pay=True, can_see_salaries=True)),
    ({"M-Pesa Manager"}, dict(can_prepare=True, can_pay=True, can_approve=True, can_collect=True, can_configure=True,
                              can_see_salaries=True)),
    ({"Sales User"}, dict(can_collect=True)),
    ({"Blogger"}, {}),
])
def test_flags_follow_the_role_sets(boot, roles, expected):
    module, frappe = boot
    frappe.session.user = "someone@example.com"
    frappe.user_roles = roles
    # maker_checker: the Suite setting (on unless switched off), for every signed-in user
    assert module.flags() == dict(dict.fromkeys(module.FLAGS, False), maker_checker=True, **expected)


def test_the_maker_checker_flag_follows_the_setting(boot):
    module, frappe = boot
    frappe.session.user, frappe.user_roles = "someone@example.com", {"HR User"}
    assert module.flags()["maker_checker"] is True
    _settings(frappe, refuse_administrator_payouts=1, schedule_maker_checker=0)
    assert module.flags()["maker_checker"] is False


def test_administrator_gets_no_send_or_approve_while_refused(boot):
    module, frappe = boot
    frappe.session.user = "Administrator"
    frappe.user_roles = ALL_ROLES
    flags = module.flags()
    assert flags["can_pay"] is flags["can_approve"] is False
    assert flags["can_prepare"] and flags["can_configure"] and flags["can_see_salaries"]
    _settings(frappe, refuse_administrator_payouts=0)
    assert module.flags()["can_pay"] and module.flags()["can_approve"]


def test_guest_gets_nothing(boot):
    module, frappe = boot
    frappe.session.user = "Guest"
    frappe.user_roles = ALL_ROLES
    assert module.flags() == dict.fromkeys(module.FLAGS, False)


def test_boot_session_puts_the_flags_on_bootinfo(boot):
    module, frappe = boot
    frappe.user_roles = {"M-Pesa Payer"}
    bootinfo = fake_frappe._dict()
    module.boot_session(bootinfo)
    assert set(bootinfo.mpsa) == set(module.FLAGS)
    assert bootinfo.mpsa["can_pay"] is True


# ---------------------------------------------------------------- maker-checker watches the destination
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
    return module, frappe


B2B_ROW = dict(doctype="Mpesa B2B Payment Request", name="MB2B-1", owner="clerk@example.com", amount=3000,
               mpesa_settings="S", employee="EMP-1", employee_mpesa_account="EMA-1", expense_claim="EC-1",
               receiver_shortcode="522522", account_reference="1234567890", command_id="BusinessPayBill")


def _sent(row, **changes):
    before = PayoutDoc(row, modified_by="clerk@example.com", before=None)
    return PayoutDoc(dict(row, **changes), before=before)


@pytest.mark.parametrize("field,value", [
    ("employee", "EMP-2"), ("employee_mpesa_account", "EMA-2"), ("salary_slip", "SAL-1"),
    ("purchase_invoice", "PI-1"), ("purchase_order", "PO-1"), ("expense_claim", "EC-2"),
    ("employee_advance", "EA-1"), ("receiver_shortcode", "247247"), ("account_reference", "999"),
    ("command_id", "BusinessBuyGoods"),
])
def test_b2b_who_where_and_for_what_cannot_change_while_sending(mc, field, value):
    module, frappe = mc
    module.check(_sent(B2B_ROW))
    with pytest.raises(frappe.ValidationError, match="changed while being sent"):
        module.check(_sent(B2B_ROW, **{field: value}))


def test_a_field_the_site_does_not_have_reads_as_empty_on_both_sides(mc):
    module, _ = mc
    row = {k: v for k, v in B2B_ROW.items() if k != "expense_claim"}
    module.check(_sent(row))


@pytest.mark.parametrize("field,value", [("reference_doctype", "Salary Slip"), ("reference_name", "EC-2")])
def test_b2c_rows_document_cannot_change_while_sending(mc, field, value):
    module, frappe = mc
    base = dict(doctype="B2C Payment Disbursement", name="B2C-1", owner="clerk@example.com", mode_of_payment="Mpesa-X")
    row = dict(party="EMP-1", partyb="254700000001", allocated_amount=1000, reference_doctype="Expense Claim",
               reference_name="EC-1")
    before = PayoutDoc(base, references=[PayoutDoc(row)], modified_by="clerk@example.com", before=None)
    module.check(PayoutDoc(base, references=[PayoutDoc(row)], before=before))
    with pytest.raises(frappe.ValidationError, match="changed while being sent"):
        module.check(PayoutDoc(base, references=[PayoutDoc(row, **{field: value})], before=before))


@pytest.mark.parametrize("field,value", [("reference_doctype", "Salary Slip"), ("reference_name", "EC-2"),
                                         ("party_type", "Supplier"), ("party", "EMP-2")])
def test_b2c_request_party_and_document_cannot_change_while_sending(mc, field, value):
    module, frappe = mc
    row = dict(doctype="B2C Disbursement Request", name="B2CR-1", owner="clerk@example.com", amount=700,
               phone_number="254700000001", mpesa_settings="S", reference_doctype="Expense Claim",
               reference_name="EC-1", party_type="Employee", party="EMP-1")
    module.check(_sent(row))
    with pytest.raises(frappe.ValidationError, match="changed while being sent"):
        module.check(_sent(row, **{field: value}))


# ---------------------------------------------------------------- hooks
def _hooks() -> dict:
    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    return {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }


def _hook(name):
    return ast.literal_eval(_hooks()[name])


def _defines(dotted: str) -> bool:
    """``dotted`` (module.function) names a function defined in the app's source."""
    module, _, func = dotted.rpartition(".")
    path = ROOT.joinpath(*module.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body)


def test_desk_scripts_include_the_send_confirmation():
    assert _hook("app_include_js") == ["/assets/mpsa_suite/js/utils.js", "/assets/mpsa_suite/js/mpsa_send.js"]
    assert (APP / "public" / "js" / "mpsa_send.js").is_file()


def _scripts(hook: dict) -> list:
    """Every script path of a doctype_js / doctype_list_js hook (a DocType may have a list of them)."""
    return [p for value in hook.values() for p in (value if isinstance(value, list) else [value])]


def test_every_form_and_list_script_exists():
    doctype_js, list_js = _hook("doctype_js"), _hook("doctype_list_js")
    for doctype in ("Expense Claim", "Employee Advance", "Salary Slip", "Sales Order", "Customer"):
        assert doctype in doctype_js, doctype
    assert set(list_js) == {"Expense Claim", "Employee Advance", "Salary Slip"}
    for path in _scripts(doctype_js) + _scripts(list_js):
        assert (APP / path).is_file(), path


def test_list_scripts_extend_hrms_list_settings_never_replace_them():
    for path in _scripts(_hook("doctype_list_js")):
        code = [line for line in (APP / path).read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("//")]
        assert not any("listview_settings[" in line and "=" in line for line in code), path


def test_dashboards_keep_the_old_entries_and_add_one_function_per_doctype():
    dashboards = _hook("override_doctype_dashboards")
    assert dashboards["Supplier"] == "mpsa_suite.money_out.supplier_dashboard.get_data"
    assert dashboards["Payroll Entry"] == "mpsa_suite.money_out.payroll.get_dashboard_data"
    new = ("Expense Claim", "Employee Advance", "Salary Slip", "Sales Order", "Sales Invoice", "Customer",
           "Payment Entry", "Journal Entry", "Payment Request", "POS Invoice", "Bank Transaction", "Bank Account")
    targets = [dashboards[doctype] for doctype in new]
    assert len(set(targets)) == len(new)  # Frappe does not say which DocType it asks for
    for target in dashboards.values():
        assert _defines(target), target


def test_connections_extend_the_dashboards_they_are_given(env):
    # WP6 fills them: tests/unit/test_r4_connections.py has what each one adds
    _, frappe = env
    frappe.get_meta = lambda doctype, *a, **kw: types.SimpleNamespace(has_field=lambda f: True,
                                                                     get_table_fields=lambda: [])
    connections = importlib.import_module("mpsa_suite.money_out.connections")
    for target in _hook("override_doctype_dashboards").values():
        if target.startswith("mpsa_suite.money_out.connections."):
            data = {"fieldname": "reference_name", "transactions": [{"label": "Payment", "items": ["Payment Entry"]}]}
            assert getattr(connections, target.rpartition(".")[2])(data=data) is data
            assert data["transactions"][0] == {"label": "Payment", "items": ["Payment Entry"]}
            assert data["transactions"][1]["label"] == "M-Pesa"


def test_claim_and_advance_cancel_and_the_boot_hook_are_wired():
    events = _hook("doc_events")
    for doctype in ("Expense Claim", "Employee Advance"):
        assert events[doctype]["before_cancel"] == "mpsa_suite.money_out.employee_documents.before_cancel"
    assert _hook("boot_session") == "mpsa_suite.boot.boot_session"
    for dotted in ("mpsa_suite.money_out.employee_documents.before_cancel", "mpsa_suite.boot.boot_session"):
        assert _defines(dotted), dotted
    # the existing ones stay
    assert events["Salary Slip"] == {"before_cancel": "mpsa_suite.money_out.payroll.slip_before_cancel"}


def test_the_new_modules_add_no_endpoint():
    # money_out/send_confirm.py has two since WP1 (tests/unit/test_send_confirm.py) and
    # money_out/employee_documents.py four since WP3 (tests/unit/test_employee_documents.py)
    for name in ("boot.py", "money_out/connections.py"):
        tree = ast.parse((APP / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert not any("whitelist" in ast.unparse(d) for d in node.decorator_list), (name, node.name)


def test_stubs_do_nothing_yet(env):
    employee_documents = importlib.import_module("mpsa_suite.money_out.employee_documents")
    assert employee_documents.before_cancel(fake_frappe._dict(doctype="Expense Claim"), "before_cancel") is None
    send_confirm = importlib.import_module("mpsa_suite.money_out.send_confirm")  # built in WP1
    with send_confirm.deliberate("Pay Selected"):
        send_confirm.assert_deliberate(fake_frappe._dict(doctype="Mpesa B2B Payment Request"))


# ---------------------------------------------------------------- M-Pesa Suite Settings
NEW_SETTINGS = {
    "payout_safety_section": ("Section Break", None),
    "refuse_administrator_payouts": ("Check", "1"),
    "b2c_approved_phones_only": ("Check", "0"),
    "claim_payees_section": ("Section Break", None),
    "claim_payable_workflow_states": ("Small Text", None),
    "allow_claim_payees": ("Check", "0"),
    "claim_payee_max_amount": ("Currency", "0"),
    "claim_payee_daily_cap": ("Currency", "0"),
    "suggest_supplier_after": ("Int", "3"),
}


def test_the_new_settings_with_their_defaults_on_the_money_out_tab():
    data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    fields = {f["fieldname"]: f for f in data["fields"]}
    assert sorted(fields) == sorted(data["field_order"])
    order = data["field_order"]
    money_out = order[order.index("money_out_tab"):order.index("accounts_tab")]
    for name, (fieldtype, default) in NEW_SETTINGS.items():
        assert name in money_out, name
        assert (fields[name]["fieldtype"], fields[name].get("default")) == (fieldtype, default), name
    assert fields["payout_safety_section"]["label"] == "Payout Safety"
    assert fields["claim_payees_section"]["label"] == "Expense Claim Payees"
    assert "Administrator" in fields["refuse_administrator_payouts"]["label"]
    assert "0 = no cap" in fields["claim_payee_max_amount"]["label"]
    assert "0 = no cap" in fields["claim_payee_daily_cap"]["label"]


# ---------------------------------------------------------------- B2B links to claims and advances
def test_the_b2b_request_links_expense_claims_and_advances(env, monkeypatch):
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field.create_custom_fields = lambda fields, update=False: None
    for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
    migrate = importlib.import_module("mpsa_suite.mpsa_suite.migrate")
    made = []
    monkeypatch.setattr(migrate, "create_fields", lambda fields: made.append(fields))
    migrate.create_custom_hrms_fields()
    fields = {f["fieldname"]: f for f in made[0]["Mpesa B2B Payment Request"]}
    slip = fields["salary_slip"]
    for name, options, after in (("expense_claim", "Expense Claim", "payroll_entry"),
                                 ("employee_advance", "Employee Advance", "expense_claim")):
        field = fields[name]
        assert (field["fieldtype"], field["options"], field["insert_after"]) == ("Link", options, after)
        assert field["search_index"] == slip["search_index"] == 1
        assert not field.get("read_only") and not field.get("no_copy") and not slip.get("no_copy")
        assert field["depends_on"].startswith("eval:doc.employee")
