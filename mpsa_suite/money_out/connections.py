"""M-Pesa Connections on ERPNext and HRMS forms: an "M-Pesa" group listing the suite records that
reference the document.

Frappe calls an ``override_doctype_dashboards`` hook with ``data`` only, never the DocType
(``Meta.get_dashboard_data`` in frappe/model/meta.py, v15 and v16), so each DocType has its own
function here, registered in hooks.py. Supplier, Purchase Invoice / Order, Employee and Payroll
Entry keep theirs (supplier_dashboard, document_dashboards, payroll) and build theirs with
``add_group`` too.

How Frappe counts a Connection (frappe/desk/notifications.py, read on the v15 and v16 benches):
- the filter is ``{field: document name}``, the field from ``non_standard_fieldnames[source]`` or
  the dashboard's ``fieldname``. A field only a child table of the source has is matched in that
  table (v15 ``get_filter``, v16 the query builder), so B2C payout rows and schedule lines need
  nothing more;
- ``dynamic_links`` are keyed by that field, not by the source, and add ``{type field: DocType}``
  only for a source whose own (parent) fields include the type field: ``reference_name ->
  [Expense Claim, reference_doctype]`` narrows B2C Disbursement Request and leaves HRMS's Payment
  and Journal Entry links (no reference_doctype of their own) as they were. The list a click opens
  gets the type filter only where the field exists (a child table's, or none);
- an ``internal_links`` source is listed by the names in the document's own fields or rows. With
  none, v15 tries the external filter (an error counts 0) and v16 tries it only when the field
  exists; our internal-only sources name ``name`` as their external field, so that matches nothing
  and the list a click opens is empty, not every record;
- counts use ``frappe.get_all`` (no permission checks) on both versions, except v16's path for a
  field several child tables of the source share (``get_list``), which none of these sources hits.
  The document itself must be readable (v15 ``check_permission``, v16 ``get_lazy_doc``).
So while Frappe counts (``get_open_count``), a source the user cannot read, and on the employee
documents (``EMPLOYEE_DOCUMENTS``) a salary DocType for who may not see salaries
(money_out.salary_access), is counted on ``name``: no count, rather than one the list then hides.
The form's Connections themselves are built once for every user (frappe.desk.form.meta caches
them) and keep every link.
"""

import frappe

from ..permissions import is_direct_call
from . import salary_access

LABEL = "M-Pesa"
B2B = "Mpesa B2B Payment Request"
B2C = "B2C Payment Disbursement"
B2C_REQUEST = "B2C Disbursement Request"
SCHEDULE = "M-Pesa Payment Schedule"
REGISTER = "Mpesa C2B Payment Register"
EXPRESS = "Mpesa Express Request"
INTENT = "Mpesa Payment Intent"
MATCH_LOG = "Mpesa Match Log"
STATEMENT_ROW = "Mpesa Statement Row"
STATEMENT_IMPORT = "Mpesa Statement Import"
SETTINGS = "Mpesa Settings"
SUPPLIER_ACCOUNT = "Supplier M-Pesa Account"
EMPLOYEE_ACCOUNT = "Employee M-Pesa Account"
# The external field of a source listed by the document's own fields only: no record matches it.
NOTHING = "name"

# The M-Pesa records these documents list all pay an employee: salary data (salary_access.DOCTYPES)
# that who may not see salaries gets no count of. Employee M-Pesa Account: its own dashboard file.
EMPLOYEE_DOCUMENTS = ("Employee", "Payroll Entry", "Salary Slip", "Expense Claim", "Employee Advance",
                      EMPLOYEE_ACCOUNT)

# DocType -> add_group arguments. ``links``: {source: its field, or a field of one of its child
# tables}; ``dynamic``: {that field: the source's DocType field}; ``internal``: {source: the
# document's own field, or [table, field]}; ``fieldname``: for a DocType without Connections.
_REFERENCE = {"reference_name": "reference_doctype"}
CONNECTIONS = {
    # HRMS: reference_name is its dashboard field (Payment and Journal Entry)
    "Expense Claim": dict(
        links={B2C_REQUEST: "reference_name", B2C: "reference_name", B2B: "expense_claim", SCHEDULE: "reference_name"},
        dynamic=_REFERENCE),
    "Employee Advance": dict(
        links={B2C_REQUEST: "reference_name", B2C: "reference_name", B2B: "employee_advance",
               SCHEDULE: "reference_name"},
        dynamic=_REFERENCE),
    "Salary Slip": dict(
        links={B2B: "salary_slip", B2C_REQUEST: "reference_name", B2C: "reference_name", SCHEDULE: "reference_name"},
        dynamic=_REFERENCE, fieldname="salary_slip"),
    # money_in matching stamps matched_reference_* on the receipt; prompts and pay links name the document
    "Sales Order": dict(
        links={REGISTER: "matched_reference_name", EXPRESS: "reference_name", INTENT: "reference_name",
               MATCH_LOG: "reference_name"},
        dynamic={"matched_reference_name": "matched_reference_doctype", **_REFERENCE}),
    "Sales Invoice": dict(
        links={REGISTER: "matched_reference_name", EXPRESS: "reference_name", INTENT: "reference_name",
               MATCH_LOG: "reference_name"},
        dynamic={"matched_reference_name": "matched_reference_doctype", **_REFERENCE}),
    "POS Invoice": dict(
        links={REGISTER: "pos_invoice", EXPRESS: "reference_name"}, dynamic=_REFERENCE, fieldname="pos_invoice"),
    "Customer": dict(links={REGISTER: "customer", MATCH_LOG: "customer"}),
    "Payment Request": dict(links={EXPRESS: "reference_name"}, dynamic=_REFERENCE),
    "Payment Entry": dict(
        links={REGISTER: "payment_entry", B2B: "payment_entry", EXPRESS: "reference_name", MATCH_LOG: "payment_entry",
               STATEMENT_ROW: "entry"},
        dynamic={"entry": "entry_doctype", **_REFERENCE},
        internal={B2C: ["references", "b2c_payment_disbursement"]}),
    "Journal Entry": dict(
        links={B2B: "journal_entry", STATEMENT_ROW: "entry"},
        dynamic={"entry": "entry_doctype"},
        internal={B2B: ["accounts", "mpesa_b2b_payment_request"], B2C: ["accounts", "b2c_payment_disbursement"]}),
    # a payout's own statement line; its charge's line (charge_bank_transaction) is not listed
    "Bank Transaction": dict(
        links={REGISTER: "bank_transaction", EXPRESS: "bank_transaction", B2C_REQUEST: "bank_transaction",
               B2B: "bank_transaction", STATEMENT_ROW: "bank_transaction"},
        fieldname="bank_transaction"),
    "Bank Account": dict(links={SETTINGS: "bank_account", STATEMENT_IMPORT: "bank_account"}),
}


def _connections(doctype: str, data=None):
    """``doctype``'s Connections with the M-Pesa records that reference the document."""
    return add_group(doctype, data, **CONNECTIONS[doctype])


def add_group(doctype: str | None, data=None, links=None, dynamic=None, internal=None, fieldname=None,
              label: str = LABEL):
    """Add the ``label`` group (or more items to it) to ``doctype``'s Connections ``data``.

    ``links``: {source DocType: its field naming the document}; ``dynamic``: {that field: the
    source's DocType field}, added only where ``data`` has none for the field; ``internal``:
    {source: the document's own field or [table, field]}; ``fieldname``: the default field when
    the DocType has none. A source whose field the site lacks is left out.
    """
    data = data or {}
    links, dynamic, internal = links or {}, dynamic or {}, internal or {}
    if fieldname and not data.get("fieldname"):
        data["fieldname"] = fieldname
    non_standard = data["non_standard_fieldnames"] = data.get("non_standard_fieldnames") or {}
    hidden = _count_hidden(doctype)
    items = []
    for source, field in links.items():
        if not _has_field(source, field):
            continue
        non_standard[source] = NOTHING if hidden(source) else field
        items.append(source)
    if internal:
        internal_links = data["internal_links"] = data.get("internal_links") or {}
        for source, link in internal.items():
            internal_links[source] = link
            if source not in items:
                non_standard[source] = NOTHING
                items.append(source)
    if dynamic and doctype:
        dynamic_links = data["dynamic_links"] = data.get("dynamic_links") or {}
        for field, type_field in dynamic.items():
            dynamic_links.setdefault(field, [doctype, type_field])
    _add_items(data, label, items)
    return data


def _add_items(data, label: str, items: list) -> None:
    groups = data["transactions"] = data.get("transactions") or []
    listed = {item for group in groups for item in (group.get("items") or [])}
    new = [item for item in items if item not in listed]
    if not new:
        return
    group = next((g for g in groups if g.get("label") == label), None)
    if group is None:
        groups.append({"label": label, "items": new})
    else:
        group["items"] = list(group.get("items") or []) + new


def _has_field(doctype: str, fieldname: str) -> bool:
    """``fieldname`` on ``doctype`` or one of its child tables (custom fields included)."""
    try:
        meta = frappe.get_meta(doctype)
    except frappe.DoesNotExistError:
        return False
    if meta.has_field(fieldname):
        return True
    return any(frappe.get_meta(df.options).has_field(fieldname) for df in meta.get_table_fields())


def counting() -> bool:
    """True while Frappe counts a form's Connections for the session user (get_open_count)."""
    return is_direct_call("get_open_count")


def _count_hidden(doctype: str | None):
    """Which sources the session user gets no count of (see the module docstring): none but
    while Frappe counts."""
    if not counting():
        return lambda source: False
    shows_salaries = doctype not in EMPLOYEE_DOCUMENTS or salary_access.can_see_salaries()
    return lambda source: (not shows_salaries and source in salary_access.DOCTYPES) or not frappe.has_permission(
        source, "read")


def get_expense_claim_data(data=None):
    return _connections("Expense Claim", data)


def get_employee_advance_data(data=None):
    return _connections("Employee Advance", data)


def get_salary_slip_data(data=None):
    return _connections("Salary Slip", data)


def get_sales_order_data(data=None):
    return _connections("Sales Order", data)


def get_sales_invoice_data(data=None):
    return _connections("Sales Invoice", data)


def get_customer_data(data=None):
    return _connections("Customer", data)


def get_payment_entry_data(data=None):
    return _connections("Payment Entry", data)


def get_journal_entry_data(data=None):
    return _connections("Journal Entry", data)


def get_payment_request_data(data=None):
    return _connections("Payment Request", data)


def get_pos_invoice_data(data=None):
    return _connections("POS Invoice", data)


def get_bank_transaction_data(data=None):
    return _connections("Bank Transaction", data)


def get_bank_account_data(data=None):
    return _connections("Bank Account", data)
