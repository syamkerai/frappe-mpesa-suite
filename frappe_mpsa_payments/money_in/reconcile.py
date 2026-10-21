"""Reconcile M-Pesa receipt lines (Bank Transactions) with their Payment Entries."""

import json

import frappe
from frappe.utils import add_days, flt, nowdate

from .common import EXPRESS, REGISTER, enabled, log_match, mpesa_bank_accounts, suite_int


def run(limit: int = 300) -> None:
    if not enabled("auto_reconcile_bank_transactions"):
        return
    since = add_days(nowdate(), -suite_int("matching_lookback_days", 45))
    for bank_account in sorted(mpesa_bank_accounts()):
        rows = frappe.get_all(
            "Bank Transaction",
            filters={
                "bank_account": bank_account,
                "docstatus": 1,
                "deposit": [">", 0],
                "unallocated_amount": [">", 0],
                "date": [">=", since],
            },
            fields=["name", "reference_number", "transaction_id", "deposit", "unallocated_amount"],
            order_by="date desc, creation desc",
            limit=limit,
        )
        gl = frappe.db.get_value("Bank Account", bank_account, "account")
        for bt in rows:
            frappe.db.savepoint("mpesa_recon")
            try:
                reconcile_line(bt, gl)
            except Exception:
                frappe.db.rollback(save_point="mpesa_recon")
                frappe.log_error(frappe.get_traceback(), f"M-Pesa reconciliation failed for {bt.name}")


def candidate_payment_entries(receipt: str) -> list:
    """Payment Entries that may carry this receipt, most specific first."""
    names = []
    reg_pe = frappe.db.get_value(REGISTER, {"transid": receipt, "docstatus": 1}, "payment_entry")
    if reg_pe:
        names.append(reg_pe)
    names += frappe.get_all(
        "Payment Entry", filters={"reference_no": receipt, "docstatus": 1}, pluck="name", order_by="creation asc"
    )
    # STK paid through a Payment Request and booked before the receipt became the reference:
    # ERPNext used the request name
    pr = frappe.db.get_value(
        EXPRESS, {"transaction_id": receipt, "reference_doctype": "Payment Request"}, "reference_name"
    )
    if pr:
        names += frappe.get_all(
            "Payment Entry", filters={"reference_no": pr, "docstatus": 1}, pluck="name", order_by="creation asc"
        )
    return list(dict.fromkeys(names))


def reconcile_line(bt, gl: str) -> str | None:
    from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import reconcile_vouchers

    receipt = bt.reference_number or bt.transaction_id
    if not receipt:
        return None
    for pe_name in candidate_payment_entries(receipt):
        pe = frappe.db.get_value(
            "Payment Entry", pe_name, ["paid_to", "received_amount", "clearance_date", "unallocated_amount"], as_dict=True
        )
        if not pe or pe.paid_to != gl or pe.clearance_date:
            continue
        if abs(flt(pe.received_amount) - flt(bt.unallocated_amount)) > 0.005:
            log_match("Ambiguous", "Bank Reconciliation", once=True, reference_doctype="Bank Transaction", reference_name=bt.name,
                      payment_entry=pe_name, amount=bt.deposit,
                      details=f"Receipt {receipt}: statement {flt(bt.unallocated_amount)} vs Payment Entry {flt(pe.received_amount)}")
            continue
        reconcile_vouchers(
            bt.name,
            json.dumps([{"payment_doctype": "Payment Entry", "payment_name": pe_name, "amount": flt(pe.received_amount)}]),
        )
        return pe_name
    return None
