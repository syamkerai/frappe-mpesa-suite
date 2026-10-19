"""What a document still owes, for B2C payouts (no Frappe import; unit-tested)."""

# doctype -> fields to read for ``owed``
OWED_FIELDS = {
    "Purchase Invoice": ("outstanding_amount",),
    "Purchase Order": ("rounded_total", "grand_total", "advance_paid"),
    "Employee Advance": ("advance_amount", "paid_amount"),
    "Expense Claim": ("grand_total", "total_sanctioned_amount", "total_amount_reimbursed"),
    "Salary Slip": ("rounded_total", "net_pay"),
}


def _n(values: dict, field: str) -> float:
    try:
        return float(values.get(field) or 0)
    except (TypeError, ValueError):
        return 0.0


def owed(doctype: str, values: dict | None) -> float | None:
    """Amount the document still owes, or None when we cannot tell (unknown doctype)."""
    if doctype not in OWED_FIELDS:
        return None
    if not values:
        return 0.0
    if doctype == "Purchase Invoice":
        amount = _n(values, "outstanding_amount")
    elif doctype == "Purchase Order":
        amount = (_n(values, "rounded_total") or _n(values, "grand_total")) - _n(values, "advance_paid")
    elif doctype == "Employee Advance":
        amount = _n(values, "advance_amount") - _n(values, "paid_amount")
    elif doctype == "Expense Claim":
        total = _n(values, "grand_total") or _n(values, "total_sanctioned_amount")
        amount = total - _n(values, "total_amount_reimbursed")
    else:  # Salary Slip
        amount = _n(values, "rounded_total") or _n(values, "net_pay")
    return round(max(amount, 0.0), 2)
