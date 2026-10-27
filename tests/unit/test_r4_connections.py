"""Round 4 (WP6): the M-Pesa Connections group on ERPNext / HRMS forms and the suite's own.

What Frappe asks the database for each Connection is transcribed below from
frappe/desk/notifications.py as read on the v15 and v16 benches (get_external_links,
get_dynamic_link_filters, v16's get_child_doctypes_with_field), and run against the fields the
suite's DocTypes really have (their JSON plus the custom fields migrate.py adds) and the ERPNext /
HRMS fields that decide a dynamic link (the same on both versions). The real counts on a site are
in mpsa_suite/money_out/test_connections.py (bench).
"""

import ast
import copy
import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import fake_frappe
import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
DOCTYPES = APP / "mpsa_suite" / "doctype"
B2B, B2C, B2C_REQUEST = "Mpesa B2B Payment Request", "B2C Payment Disbursement", "B2C Disbursement Request"
B2C_ROW, SCHEDULE, LINE = "B2C Payment Disbursement Reference", "M-Pesa Payment Schedule", "M-Pesa Payment Schedule Line"
REGISTER, EXPRESS, INTENT, MATCH_LOG = ("Mpesa C2B Payment Register", "Mpesa Express Request", "Mpesa Payment Intent",
                                        "Mpesa Match Log")
STATEMENT_ROW = "Mpesa Statement Row"
PE, JE, PR = "Payment Entry", "Journal Entry", "Payment Request"

# ERPNext / HRMS fields that matter here (bench JSON, v15 = v16 for these): a parent field decides
# a dynamic link; child tables are where a field the parent lacks is matched.
ERP_FIELDS = {
    PE: ({"party_type", "party", "bank_account", "reference_no", "payment_type"}, ["Payment Entry Reference"]),
    "Payment Entry Reference": ({"reference_doctype", "reference_name", "payment_request"}, []),
    JE: ({"cheque_no", "voucher_type"}, ["Journal Entry Account"]),
    "Journal Entry Account": ({"reference_type", "reference_name", "party_type", "party", "account"}, []),
    PR: ({"reference_doctype", "reference_name", "party_type", "party", "bank_account", "outstanding_amount"}, []),
    "Bank Account": ({"party_type", "party", "account", "is_company_account"}, []),
    "Bank Transaction": ({"party_type", "party", "bank_account"}, ["Bank Transaction Payments"]),
    "Bank Transaction Payments": ({"payment_document", "payment_entry"}, []),
    "Integration Request": ({"reference_doctype", "reference_docname"}, []),
    "Salary Slip": ({"employee", "payroll_entry", "journal_entry"}, []),
}


def _custom_fields(monkeypatch) -> dict:
    """{doctype: {fieldname}} migrate.py adds (HRMS and ERPNext fields)."""
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field.create_custom_fields = lambda fields, update=False: None
    for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
    migrate = importlib.import_module("mpsa_suite.mpsa_suite.migrate")
    made = []
    monkeypatch.setattr(migrate, "create_fields", lambda fields: made.append(fields))
    migrate.create_custom_hrms_fields()
    migrate.create_custom_erpnext_fields()
    out = {}
    for batch in made + [migrate.payment_entry_mpesa_fields()]:
        for doctype, fields in batch.items():
            out.setdefault(doctype, set()).update(f["fieldname"] for f in fields)
    return out


# Table field -> child DocType, where an internal link reads the document's rows
TABLE_FIELDS = {PE: {"references": "Payment Entry Reference"}, JE: {"accounts": "Journal Entry Account"},
                "Bank Transaction": {"payment_entries": "Bank Transaction Payments"}}


def _schema(custom: dict) -> dict:
    """{doctype: (fields, child tables)} of the suite's DocTypes and the ERPNext ones above; the
    suite's table fields go to TABLE_FIELDS."""
    schema = {dt: (set(fields), list(tables)) for dt, (fields, tables) in ERP_FIELDS.items()}
    for path in DOCTYPES.glob("*/*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("doctype") != "DocType":
            continue
        fields = {f["fieldname"] for f in data["fields"]}
        tables = {f["fieldname"]: f["options"] for f in data["fields"] if f["fieldtype"] in ("Table", "Table MultiSelect")}
        schema[data["name"]] = (fields, list(tables.values()))
        TABLE_FIELDS[data["name"]] = tables
    for doctype, fields in custom.items():
        schema.setdefault(doctype, (set(), []))[0].update(fields)
    return schema


class Meta:
    def __init__(self, doctype, schema):
        self.name = doctype
        self.fields, self.tables = schema.get(doctype, (set(), []))

    def has_field(self, fieldname):
        return fieldname in self.fields

    def get_table_fields(self):
        return [fake_frappe._dict(options=t) for t in self.tables]


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        schema = _schema(_custom_fields(monkeypatch))
        frappe.get_meta = lambda doctype, *a, **kw: Meta(doctype, schema)
        frappe.denied = set()
        frappe.has_permission = lambda doctype, ptype="read", *a, **kw: doctype not in frappe.denied
        connections = importlib.import_module("mpsa_suite.money_out.connections")
        salary_access = importlib.import_module("mpsa_suite.money_out.salary_access")
        monkeypatch.setattr(salary_access, "can_see_salaries", lambda user=None: True)
        yield types.SimpleNamespace(frappe=frappe, schema=schema, connections=connections,
                                    salary_access=salary_access, monkeypatch=monkeypatch)
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- what Frappe does with the data
def _parent_has(schema, doctype, field):
    return field in schema.get(doctype, (set(), []))[0]


def _child_tables_with(schema, doctype, field):
    """v16 get_child_doctypes_with_field: child tables carrying the field, none when the parent has it."""
    if _parent_has(schema, doctype, field):
        return []
    return [t for t in schema.get(doctype, (set(), []))[1] if _parent_has(schema, t, field)]


def _table_of(schema, doctype, field):
    """Where get_filter (v15) / the query builder (v16) match ``field``: the parent, else the first
    child table that has it (None: an unknown column, the query fails)."""
    if field == "name" or _parent_has(schema, doctype, field):
        return doctype
    tables = _child_tables_with(schema, doctype, field)
    return tables[0] if tables else None


def counted(schema, data, source, version, name="DOC-1"):
    """(call, filters, {filter field: table}) for ``source`` in frappe.desk.notifications."""
    fieldname = (data.get("non_standard_fieldnames") or {}).get(source, data.get("fieldname"))
    filters = {}
    dynamic = (data.get("dynamic_links") or {}).get(fieldname)
    if dynamic and _parent_has(schema, source, dynamic[1]):  # get_dynamic_link_filters
        filters[dynamic[1]] = dynamic[0]
    call = "get_all"
    if version == 16 and len(_child_tables_with(schema, source, fieldname)) > 1:
        call = "get_list"  # get_external_links_in_child_tables
    filters[fieldname] = name
    return call, filters, {f: _table_of(schema, source, f) for f in filters}


def route_filter(schema, data, source, name="DOC-1"):
    """What a click on the Connection filters the list by (form/dashboard.js get_document_filter,
    then list_view parse_filters_from_route_options: a field no table has is dropped)."""
    fieldname = (data.get("non_standard_fieldnames") or {}).get(source, data.get("fieldname"))
    out = {fieldname: name}
    dynamic = (data.get("dynamic_links") or {}).get(fieldname)
    if dynamic:
        out[dynamic[1]] = dynamic[0]
    return {f: v for f, v in out.items() if _table_of(schema, source, f)}


def _items(data, label="M-Pesa"):
    return next((g["items"] for g in data["transactions"] if g.get("label") == label), [])


# The data Frappe hands the hook (module get_data + add_doctype_links), from the bench sources
HRMS_EXPENSE_CLAIM = {
    "fieldname": "reference_name",
    "internal_links": {"Employee Advance": ["advances", "employee_advance"]},
    "transactions": [{"label": "Payment", "items": [PE, JE]}, {"label": "Reference", "items": ["Employee Advance"]}],
    "non_standard_fieldnames": {},
}
HRMS_EMPLOYEE_ADVANCE = {
    "fieldname": "employee_advance",
    "non_standard_fieldnames": {PE: "reference_name", JE: "reference_name"},
    "transactions": [{"items": ["Expense Claim"]}, {"items": [PE, JE]}],
    "internal_links": {},
}
ERPNEXT_SALES_INVOICE = {
    "fieldname": "sales_invoice",
    "non_standard_fieldnames": {"Delivery Note": "against_sales_invoice", JE: "reference_name", PE: "reference_name",
                                PR: "reference_name", "Sales Invoice": "return_against"},
    "internal_links": {"Sales Order": ["items", "sales_order"]},
    "transactions": [{"label": "Payment", "items": [PE, PR, JE]}, {"label": "Reference", "items": ["POS Invoice"]}],
}
ERPNEXT_SALES_ORDER = {
    "fieldname": "sales_order",
    "non_standard_fieldnames": {JE: "reference_name", PE: "reference_name", PR: "reference_name"},
    "internal_links": {"Quotation": ["items", "prevdoc_docname"]},
    "transactions": [{"label": "Payment", "items": [PE, PR, JE]}],
}
ERPNEXT_CUSTOMER = {
    "fieldname": "customer",
    "non_standard_fieldnames": {PE: "party", "Quotation": "party_name", "Bank Account": "party"},
    "dynamic_links": {"party_name": ["Customer", "quotation_to"], "party": ["Customer", "party_type"]},
    "transactions": [{"label": "Payments", "items": [PE, "Bank Account"]}],
    "internal_links": {},
}
# JSON links only: Bank Transaction Payments (is_child_table) gives fieldname payment_entry
ERPNEXT_PAYMENT_ENTRY = {
    "fieldname": "payment_entry",
    "non_standard_fieldnames": {},
    "internal_links": {"Bank Transaction": ["payment_entries", "payment_entry"]},
    "transactions": [{"label": None, "items": ["Bank Transaction"]}],
}
ERPNEXT_PAYMENT_REQUEST = {
    "fieldname": "payment_request",
    "internal_links": {PE: ["references", "payment_request"]},
    "transactions": [{"label": "Payment", "items": [PE, "Payment Order"]}],
    "non_standard_fieldnames": {},
}
ERPNEXT_PURCHASE_INVOICE = {
    "fieldname": "purchase_invoice",
    "non_standard_fieldnames": {JE: "reference_name", PE: "reference_name", PR: "reference_name"},
    "internal_links": {"Purchase Order": ["items", "purchase_order"]},
    "transactions": [{"label": "Payment", "items": [PE, PR, JE]}],
}
ERPNEXT_SUPPLIER = {
    "fieldname": "supplier",
    "non_standard_fieldnames": {PE: "party", "Bank Account": "party"},
    "dynamic_links": {"party": ["Supplier", "party_type"]},
    "transactions": [{"label": "Payments", "items": [PE, "Bank Account"]}],
    "internal_links": {},
}
HRMS_EMPLOYEE = {
    "fieldname": "employee",
    "non_standard_fieldnames": {"Bank Account": "party", "Employee Grievance": "raised_by"},
    "transactions": [{"label": "Payroll", "items": ["Salary Slip", "Bank Account"]}],
    "internal_links": {},
}
HRMS_PAYROLL_ENTRY = {
    "fieldname": "payroll_entry",
    "non_standard_fieldnames": {JE: "reference_name", PE: "reference_name"},
    "transactions": [{"items": ["Salary Slip", JE]}],
    "internal_links": {},
}
EMPTY = {"transactions": [], "non_standard_fieldnames": {}, "internal_links": {}}
BANK_ACCOUNT = {"fieldname": "bank_account", "non_standard_fieldnames": {PR: "bank_account", "Bank Transaction": "bank_account"},
                "internal_links": {}, "transactions": [{"label": "Transactions", "items": [PR, "Bank Transaction"]}]}


def _hooked(env, doctype, base):
    c = env.connections
    hooks = {
        "Expense Claim": c.get_expense_claim_data, "Employee Advance": c.get_employee_advance_data,
        "Salary Slip": c.get_salary_slip_data, "Sales Order": c.get_sales_order_data,
        "Sales Invoice": c.get_sales_invoice_data, "Customer": c.get_customer_data,
        "Payment Entry": c.get_payment_entry_data, "Journal Entry": c.get_journal_entry_data,
        "Payment Request": c.get_payment_request_data, "POS Invoice": c.get_pos_invoice_data,
        "Bank Transaction": c.get_bank_transaction_data, "Bank Account": c.get_bank_account_data,
    }
    if doctype in hooks:
        return hooks[doctype](data=copy.deepcopy(base))
    if doctype == "Supplier":
        return importlib.import_module("mpsa_suite.money_out.supplier_dashboard").get_data(data=copy.deepcopy(base))
    if doctype in ("Purchase Invoice", "Purchase Order"):
        return importlib.import_module("mpsa_suite.money_out.document_dashboards").get_data(data=copy.deepcopy(base))
    payroll = importlib.import_module("mpsa_suite.money_out.payroll")
    if doctype == "Employee":
        return payroll.get_employee_dashboard_data(data=copy.deepcopy(base))
    return payroll.get_dashboard_data(data=copy.deepcopy(base))


BASES = {
    "Expense Claim": HRMS_EXPENSE_CLAIM, "Employee Advance": HRMS_EMPLOYEE_ADVANCE, "Salary Slip": EMPTY,
    "Sales Order": ERPNEXT_SALES_ORDER, "Sales Invoice": ERPNEXT_SALES_INVOICE, "Customer": ERPNEXT_CUSTOMER,
    "Payment Entry": ERPNEXT_PAYMENT_ENTRY, "Journal Entry": ERPNEXT_PAYMENT_ENTRY, "Payment Request": ERPNEXT_PAYMENT_REQUEST,
    "POS Invoice": EMPTY, "Bank Transaction": EMPTY, "Bank Account": BANK_ACCOUNT, "Supplier": ERPNEXT_SUPPLIER,
    "Purchase Invoice": ERPNEXT_PURCHASE_INVOICE,
    "Purchase Order": dict(ERPNEXT_PURCHASE_INVOICE, fieldname="purchase_order"),
    "Employee": HRMS_EMPLOYEE, "Payroll Entry": HRMS_PAYROLL_ENTRY,
}

# (dashboard, source) -> filters Frappe counts with, on v15 and on v16, for document DOC-1
EXPECTED = {
    ("Expense Claim", B2C_REQUEST): {"reference_doctype": "Expense Claim", "reference_name": "DOC-1"},
    ("Expense Claim", B2C): {"reference_name": "DOC-1"},
    ("Expense Claim", B2B): {"expense_claim": "DOC-1"},
    ("Expense Claim", SCHEDULE): {"reference_name": "DOC-1"},
    ("Expense Claim", PE): {"reference_name": "DOC-1"},  # HRMS's, unchanged: no reference_doctype of its own
    ("Expense Claim", JE): {"reference_name": "DOC-1"},
    ("Employee Advance", B2C_REQUEST): {"reference_doctype": "Employee Advance", "reference_name": "DOC-1"},
    ("Employee Advance", B2B): {"employee_advance": "DOC-1"},
    ("Employee Advance", B2C): {"reference_name": "DOC-1"},
    ("Employee Advance", PE): {"reference_name": "DOC-1"},
    ("Salary Slip", B2B): {"salary_slip": "DOC-1"},
    ("Salary Slip", B2C_REQUEST): {"reference_doctype": "Salary Slip", "reference_name": "DOC-1"},
    ("Salary Slip", SCHEDULE): {"reference_name": "DOC-1"},
    ("Sales Order", REGISTER): {"matched_reference_doctype": "Sales Order", "matched_reference_name": "DOC-1"},
    ("Sales Order", EXPRESS): {"reference_doctype": "Sales Order", "reference_name": "DOC-1"},
    ("Sales Order", INTENT): {"reference_doctype": "Sales Order", "reference_name": "DOC-1"},
    ("Sales Order", MATCH_LOG): {"reference_doctype": "Sales Order", "reference_name": "DOC-1"},
    ("Sales Order", PR): {"reference_doctype": "Sales Order", "reference_name": "DOC-1"},  # now narrowed too
    ("Sales Invoice", REGISTER): {"matched_reference_doctype": "Sales Invoice", "matched_reference_name": "DOC-1"},
    ("Sales Invoice", EXPRESS): {"reference_doctype": "Sales Invoice", "reference_name": "DOC-1"},
    ("Sales Invoice", PR): {"reference_doctype": "Sales Invoice", "reference_name": "DOC-1"},
    ("Sales Invoice", PE): {"reference_name": "DOC-1"},
    ("Customer", REGISTER): {"customer": "DOC-1"},
    ("Customer", MATCH_LOG): {"customer": "DOC-1"},
    ("Customer", PE): {"party_type": "Customer", "party": "DOC-1"},  # ERPNext's, kept
    ("Payment Request", EXPRESS): {"reference_doctype": "Payment Request", "reference_name": "DOC-1"},
    ("Payment Entry", REGISTER): {"payment_entry": "DOC-1"},
    ("Payment Entry", B2B): {"payment_entry": "DOC-1"},
    ("Payment Entry", EXPRESS): {"reference_doctype": "Payment Entry", "reference_name": "DOC-1"},
    ("Payment Entry", MATCH_LOG): {"payment_entry": "DOC-1"},
    ("Payment Entry", STATEMENT_ROW): {"entry_doctype": "Payment Entry", "entry": "DOC-1"},
    ("Payment Entry", B2C): {"name": "DOC-1"},  # internal only: its rows name it
    ("Journal Entry", B2B): {"journal_entry": "DOC-1"},  # after the JE's own rows
    ("Journal Entry", STATEMENT_ROW): {"entry_doctype": "Journal Entry", "entry": "DOC-1"},
    ("Journal Entry", B2C): {"name": "DOC-1"},
    ("POS Invoice", REGISTER): {"pos_invoice": "DOC-1"},
    ("POS Invoice", EXPRESS): {"reference_doctype": "POS Invoice", "reference_name": "DOC-1"},
    ("Bank Transaction", REGISTER): {"bank_transaction": "DOC-1"},
    ("Bank Transaction", EXPRESS): {"bank_transaction": "DOC-1"},
    ("Bank Transaction", B2C_REQUEST): {"bank_transaction": "DOC-1"},
    ("Bank Transaction", B2B): {"bank_transaction": "DOC-1"},
    ("Bank Transaction", STATEMENT_ROW): {"bank_transaction": "DOC-1"},
    ("Bank Account", "Mpesa Settings"): {"bank_account": "DOC-1"},
    ("Bank Account", "Mpesa Statement Import"): {"bank_account": "DOC-1"},
    ("Supplier", "Supplier M-Pesa Account"): {"supplier": "DOC-1"},
    ("Supplier", B2B): {"supplier": "DOC-1"},
    ("Supplier", B2C_REQUEST): {"party_type": "Supplier", "party": "DOC-1"},
    ("Supplier", B2C): {"party_type": "Supplier", "party": "DOC-1"},  # header type, row party
    ("Supplier", SCHEDULE): {"party": "DOC-1"},
    ("Purchase Invoice", B2B): {"purchase_invoice": "DOC-1"},
    ("Purchase Invoice", B2C_REQUEST): {"reference_doctype": "Purchase Invoice", "reference_name": "DOC-1"},
    ("Purchase Invoice", B2C): {"reference_name": "DOC-1"},
    ("Purchase Invoice", SCHEDULE): {"reference_name": "DOC-1"},
    ("Purchase Invoice", PR): {"reference_doctype": "Purchase Invoice", "reference_name": "DOC-1"},
    ("Purchase Order", B2B): {"purchase_order": "DOC-1"},
    ("Purchase Order", B2C_REQUEST): {"reference_doctype": "Purchase Order", "reference_name": "DOC-1"},
    ("Employee", "Employee M-Pesa Account"): {"employee": "DOC-1"},
    ("Employee", B2B): {"employee": "DOC-1"},
    ("Employee", B2C_REQUEST): {"party_type": "Employee", "party": "DOC-1"},
    ("Employee", B2C): {"party_type": "Employee", "party": "DOC-1"},
    ("Employee", SCHEDULE): {"party": "DOC-1"},
    ("Employee", "Bank Account"): {"party_type": "Employee", "party": "DOC-1"},  # HRMS's, now narrowed
    ("Payroll Entry", SCHEDULE): {"payroll_doctype": "Payroll Entry", "payroll_entry": "DOC-1"},
    ("Payroll Entry", B2B): {"payroll_entry": "DOC-1"},
    ("Payroll Entry", B2C): {"payroll_entry": "DOC-1"},
    ("Payroll Entry", "Salary Slip"): {"payroll_entry": "DOC-1"},
}

# Sources matched in a child table of theirs, not the parent
IN_CHILD = {(B2C, "reference_name"): B2C_ROW, (B2C, "party"): B2C_ROW, (B2C, "payroll_entry"): B2C_ROW,
            (SCHEDULE, "reference_name"): LINE, (SCHEDULE, "party"): LINE, (PE, "reference_name"): "Payment Entry Reference",
            (JE, "reference_name"): "Journal Entry Account"}


@pytest.mark.parametrize("version", [15, 16])
@pytest.mark.parametrize("key", sorted(EXPECTED), ids=lambda k: f"{k[0]}-{k[1]}" if isinstance(k, tuple) else str(k))
def test_frappe_counts_each_connection_with_the_right_filter_on_both_versions(env, key, version):
    doctype, source = key
    data = _hooked(env, doctype, BASES[doctype])
    listed = {i for g in data["transactions"] for i in g["items"]}
    assert source in listed, (doctype, source)
    call, filters, tables = counted(env.schema, data, source, version)
    assert call == "get_all"  # v16's get_list path for a field shared by several child tables: never hit
    assert filters == EXPECTED[key]
    for field, table in tables.items():
        assert table == IN_CHILD.get((source, field), source), (doctype, source, field)


@pytest.mark.parametrize("doctype", sorted(BASES))
def test_the_hook_keeps_what_erpnext_and_hrms_had(env, doctype):
    base = BASES[doctype]
    data = _hooked(env, doctype, base)
    for group in base["transactions"]:
        assert any(g.get("label") == group.get("label") and set(group["items"]) <= set(g["items"])
                   for g in data["transactions"]), group
    for key in ("non_standard_fieldnames", "internal_links", "dynamic_links"):
        for name, value in (base.get(key) or {}).items():
            assert data[key][name] == value, (key, name)
    assert data.get("fieldname") == base.get("fieldname") or not base.get("fieldname")
    # one M-Pesa group, no DocType twice
    items = [i for g in data["transactions"] for i in g["items"]]
    assert len(items) == len(set(items))
    assert sum(1 for g in data["transactions"] if g.get("label") == "M-Pesa") == 1


@pytest.mark.parametrize("doctype", sorted(BASES))
def test_a_click_opens_the_list_of_what_was_counted(env, doctype):
    data = _hooked(env, doctype, BASES[doctype])
    assert data.get("fieldname"), "without a fieldname the desk opens the list unfiltered"
    for source in _items(data):
        if source in (data.get("internal_links") or {}):
            continue  # opened by the names of the document's own rows
        _, filters, _ = counted(env.schema, data, source, 15)
        route = route_filter(env.schema, data, source)
        # the same filters, or narrower: the type field of a dynamic link only a child table has
        assert filters.items() <= route.items(), source
        extra = set(route) - set(filters)
        if extra:
            field = data["non_standard_fieldnames"].get(source, data["fieldname"])
            assert extra == {data["dynamic_links"][field][1]}, source


def test_hooking_twice_adds_nothing_more(env):
    once = _hooked(env, "Expense Claim", HRMS_EXPENSE_CLAIM)
    twice = env.connections.get_expense_claim_data(data=copy.deepcopy(once))
    assert twice == once


def test_dashboards_without_connections_of_their_own_get_a_fieldname(env):
    assert _hooked(env, "Bank Transaction", EMPTY)["fieldname"] == "bank_transaction"
    assert _hooked(env, "POS Invoice", EMPTY)["fieldname"] == "pos_invoice"
    assert _hooked(env, "Salary Slip", EMPTY)["fieldname"] == "salary_slip"
    assert _hooked(env, "Payment Entry", ERPNEXT_PAYMENT_ENTRY)["fieldname"] == "payment_entry"  # its own kept


def test_journal_and_payment_entries_list_payouts_by_their_own_rows(env):
    je = _hooked(env, "Journal Entry", ERPNEXT_PAYMENT_ENTRY)
    assert je["internal_links"][B2B] == ["accounts", "mpesa_b2b_payment_request"]
    assert je["internal_links"][B2C] == ["accounts", "b2c_payment_disbursement"]
    assert je["internal_links"]["Bank Transaction"] == ["payment_entries", "payment_entry"]
    pe = _hooked(env, "Payment Entry", ERPNEXT_PAYMENT_ENTRY)
    assert pe["internal_links"][B2C] == ["references", "b2c_payment_disbursement"]
    assert pe["non_standard_fieldnames"][B2C] == "name"


def test_a_source_whose_field_the_site_lacks_is_left_out(env):
    fields, tables = env.schema[B2B]
    env.schema[B2B] = (fields - {"expense_claim"}, tables)
    data = _hooked(env, "Expense Claim", HRMS_EXPENSE_CLAIM)
    assert B2B not in _items(data) and B2B not in data["non_standard_fieldnames"]
    assert B2C_REQUEST in _items(data)


def test_purchase_dashboards_tell_invoice_from_order_by_their_fieldname(env):
    pi = _hooked(env, "Purchase Invoice", ERPNEXT_PURCHASE_INVOICE)
    po = _hooked(env, "Purchase Order", BASES["Purchase Order"])
    assert pi["dynamic_links"]["reference_name"] == ["Purchase Invoice", "reference_doctype"]
    assert po["dynamic_links"]["reference_name"] == ["Purchase Order", "reference_doctype"]
    # an unknown fieldname: no B2B and no type filter, and nothing breaks
    data = importlib.import_module("mpsa_suite.money_out.document_dashboards").get_data(
        data={"fieldname": "something", "transactions": []})
    assert B2B not in _items(data) and "dynamic_links" not in data and B2C_REQUEST in _items(data)


# ---------------------------------------------------------------- counts nobody may see
def _counting(env, *, salaries=True):
    env.monkeypatch.setattr(env.connections, "counting", lambda: True)
    env.monkeypatch.setattr(env.salary_access, "can_see_salaries", lambda user=None: salaries)


@pytest.mark.parametrize("doctype", ["Employee", "Payroll Entry", "Salary Slip"])
def test_salary_payouts_are_not_counted_for_who_may_not_see_them(env, doctype):
    _counting(env, salaries=False)
    data = _hooked(env, doctype, BASES[doctype])
    salary = [s for s in _items(data) if s in env.salary_access.DOCTYPES]
    assert salary
    for source in salary:
        assert data["non_standard_fieldnames"][source] == "name", source
        assert counted(env.schema, data, source, 16)[1] == {"name": "DOC-1"}  # matches no record


@pytest.mark.parametrize("doctype, field", [("Expense Claim", "expense_claim"), ("Employee Advance", "employee_advance")])
def test_claim_and_advance_payouts_are_counted_for_who_reads_them_without_seeing_salaries(env, doctype, field):
    # plan C.8: a claim's or advance's payouts are not salary data (salary_access shows them to who
    # reads the claim), so its Connections count them like any source the user can read
    assert doctype not in env.connections.EMPLOYEE_DOCUMENTS
    _counting(env, salaries=False)
    # reads it only as an Employee: its payouts stay hidden, as on the payout lists
    assert _hooked(env, doctype, BASES[doctype])["non_standard_fieldnames"][B2B] == "name"
    env.monkeypatch.setattr(env.salary_access, "reads_documents", lambda user=None: (doctype,))
    data = _hooked(env, doctype, BASES[doctype])
    assert data["non_standard_fieldnames"][B2B] == field
    assert counted(env.schema, data, B2B, 16)[1] == {field: "DOC-1"}
    assert data["non_standard_fieldnames"].get(B2C_REQUEST, "reference_name") != "name"
    env.frappe.denied = {B2B}  # a payout DocType the user cannot read is still not counted
    assert _hooked(env, doctype, BASES[doctype])["non_standard_fieldnames"][B2B] == "name"


def test_the_salary_rule_hides_nothing_from_who_may_see_salaries_or_outside_employee_documents(env):
    _counting(env, salaries=True)
    assert _hooked(env, "Employee", HRMS_EMPLOYEE)["non_standard_fieldnames"][B2B] == "employee"
    _counting(env, salaries=False)
    assert _hooked(env, "Supplier", ERPNEXT_SUPPLIER)["non_standard_fieldnames"][B2B] == "supplier"
    assert _hooked(env, "Purchase Invoice", ERPNEXT_PURCHASE_INVOICE)["non_standard_fieldnames"][B2B] == "purchase_invoice"


def test_a_source_the_user_cannot_read_is_not_counted(env):
    _counting(env)
    env.frappe.denied = {REGISTER, "Employee M-Pesa Account"}
    customer = _hooked(env, "Customer", ERPNEXT_CUSTOMER)
    assert customer["non_standard_fieldnames"][REGISTER] == "name"
    assert customer["non_standard_fieldnames"][MATCH_LOG] == "customer"
    assert _hooked(env, "Employee", HRMS_EMPLOYEE)["non_standard_fieldnames"]["Employee M-Pesa Account"] == "name"


def test_the_form_connections_cached_for_everyone_keep_every_link(env):
    # not counting: whoever builds the cached form meta, every field is the real one
    env.monkeypatch.setattr(env.salary_access, "can_see_salaries", lambda user=None: False)
    env.frappe.denied = {REGISTER, B2B}
    assert _hooked(env, "Employee", HRMS_EMPLOYEE)["non_standard_fieldnames"][B2B] == "employee"
    assert _hooked(env, "Customer", ERPNEXT_CUSTOMER)["non_standard_fieldnames"][REGISTER] == "customer"


def test_counting_is_the_get_open_count_request_only(env):
    frappe = env.frappe
    assert env.connections.counting() is False  # no request (a job, a test, migrate)
    frappe.local.request = types.SimpleNamespace(path="/api/method/frappe.desk.notifications.get_open_count")
    frappe.local.form_dict = fake_frappe._dict(cmd="frappe.desk.notifications.get_open_count")
    assert env.connections.counting() is True
    frappe.local.request = types.SimpleNamespace(path="/api/method/frappe.desk.form.load.getdoctype")
    frappe.local.form_dict = fake_frappe._dict(cmd="frappe.desk.form.load.getdoctype")
    assert env.connections.counting() is False


# ---------------------------------------------------------------- the suite's own Connections
SUITE_DASHBOARDS = sorted(DOCTYPES.glob("*/*_dashboard.py"))


def _suite_data(path):
    """The file's get_data(), loaded on its own (the doctype package imports Frappe's logger)."""
    spec = importlib.util.spec_from_file_location(f"_dashboard_{path.parent.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get_data()


def _json_links(path):
    data = json.loads(path.with_name(path.name.replace("_dashboard.py", ".json")).read_text(encoding="utf-8"))
    return data["name"], data.get("links") or []


@pytest.mark.parametrize("path", SUITE_DASHBOARDS, ids=lambda p: p.parent.name)
def test_suite_dashboards_name_fields_their_sources_have(env, path):
    data = _suite_data(path)
    doctype, links = _json_links(path)
    assert data.get("fieldname")
    internal = data.get("internal_links") or {}
    for source, link in internal.items():
        if isinstance(link, str):  # a field of the document
            assert _parent_has(env.schema, doctype, link), (doctype, link)
        else:  # [table field, its field]
            table, field = link
            assert _parent_has(env.schema, TABLE_FIELDS[doctype][table], field), (doctype, link)
    json_sources = {link["link_doctype"] for link in links}
    for group in data["transactions"]:
        for source in group["items"]:
            if source in json_sources:
                continue  # the DocType's own links give its field
            field = data.get("non_standard_fieldnames", {}).get(source, data["fieldname"])
            if source in internal:
                assert field in ("name",) or _table_of(env.schema, source, field), (doctype, source, field)
                continue
            assert _table_of(env.schema, source, field), (doctype, source, field)
            dynamic = (data.get("dynamic_links") or {}).get(field)
            if dynamic:
                assert dynamic[0] == doctype and _table_of(env.schema, source, dynamic[1]), (doctype, source)


def test_the_schedule_lists_its_b2c_requests_through_its_lines(env):
    data = _suite_data(DOCTYPES / "m_pesa_payment_schedule" / "m_pesa_payment_schedule_dashboard.py")
    assert data["internal_links"][B2C_REQUEST] == ["lines", "b2c_request"]
    assert "b2c_request" in env.schema[LINE][0]
    assert data["non_standard_fieldnames"][B2C_REQUEST] == "name"
    doctype, links = _json_links(DOCTYPES / "m_pesa_payment_schedule" / "m_pesa_payment_schedule_dashboard.py")
    # its JSON links (B2B, B2C payouts by payment_schedule) land in the same group, once
    assert {link["link_doctype"] for link in links} <= set(_items(data))


def test_payee_accounts_list_schedule_lines_by_their_dynamic_payee_link(env):
    for folder, doctype in (("supplier_m_pesa_account", "Supplier M-Pesa Account"),
                            ("employee_m_pesa_account", "Employee M-Pesa Account")):
        data = _suite_data(DOCTYPES / folder / f"{folder}_dashboard.py")
        for version in (15, 16):
            call, filters, tables = counted(env.schema, data, SCHEDULE, version)
            assert (call, filters, tables) == ("get_all", {"payee_account": "DOC-1"}, {"payee_account": LINE})
        assert route_filter(env.schema, data, SCHEDULE) == {"payee_account": "DOC-1", "payee_account_doctype": doctype}
        assert counted(env.schema, data, B2B, 15)[1] == {data["fieldname"]: "DOC-1"}


def test_an_employee_m_pesa_account_counts_no_salary_payouts_for_who_may_not_see_them(env):
    """Its B2B requests and schedule lines pay an employee (salary_access.DOCTYPES): the Employee
    form's rule. Frappe counts with get_all, so a static dashboard told System Manager (read on the
    account, no salaries) how often the employee was paid."""
    path = DOCTYPES / "employee_m_pesa_account" / "employee_m_pesa_account_dashboard.py"
    _counting(env, salaries=False)
    data = _suite_data(path)
    assert data["non_standard_fieldnames"] == {B2B: "name", SCHEDULE: "name"}
    for version in (15, 16):
        for source in (B2B, SCHEDULE):
            assert counted(env.schema, data, source, version)[1] == {"name": "DOC-1"}  # matches no record
    _counting(env, salaries=True)
    data = _suite_data(path)
    assert data["non_standard_fieldnames"] == {B2B: "employee_mpesa_account", SCHEDULE: "payee_account"}
    # the form's Connections, built once for everyone (not counting), keep every link
    env.monkeypatch.setattr(env.connections, "counting", lambda: False)
    env.monkeypatch.setattr(env.salary_access, "can_see_salaries", lambda user=None: False)
    assert _suite_data(path)["non_standard_fieldnames"][B2B] == "employee_mpesa_account"


def test_the_intent_lists_its_prompts_by_reference_and_type(env):
    data = _suite_data(DOCTYPES / "mpesa_payment_intent" / "mpesa_payment_intent_dashboard.py")
    assert counted(env.schema, data, EXPRESS, 16)[1] == {"reference_doctype": INTENT, "reference_name": "DOC-1"}


def test_no_dashboard_module_adds_an_endpoint():
    for path in SUITE_DASHBOARDS + [APP / "money_out" / "connections.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert not any("whitelist" in ast.unparse(d) for d in node.decorator_list), (path, node.name)
