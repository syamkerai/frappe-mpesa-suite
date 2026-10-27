"""M-Pesa Connections for ERPNext/HRMS documents (WP6).

Frappe calls an ``override_doctype_dashboards`` hook with ``data`` only, never the DocType
(``Meta.get_dashboard_data`` in frappe/model/meta.py, v15 and v16), so each DocType has its own
function here, registered in hooks.py. Each returns the form's Connections unchanged until its
M-Pesa group is added in ``_connections``.
"""


def _connections(doctype: str, data=None):
    """``doctype``'s Connections with the M-Pesa records that reference the document (none yet)."""
    return data or {}


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
