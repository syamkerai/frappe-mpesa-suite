"""Document events and scheduler entry points for the money-in pipeline (wired in hooks.py)."""

import frappe

from . import bank_transaction, c2b_verify, matching, payment_request, reconcile, sales_order, stk_poll, stk_verify
from ..compat import in_test
from ..money_out import statement as payout_statement
from .common import enabled, erpnext_installed


# ---------------------------------------------------------------- doc events
def register_after_insert(doc, method=None) -> None:
    """New C2B receipt: statement line now, matching right after the insert commits."""
    if enabled("create_bank_transactions") and erpnext_installed():
        frappe.db.savepoint("mpesa_register_bt")
        try:
            bank_transaction.for_register(doc)
        except Exception:
            # never lose the receipt itself because its statement line failed; the sweep retries
            frappe.db.rollback(save_point="mpesa_register_bt")
            frappe.log_error(frappe.get_traceback(), f"M-Pesa statement line failed for {doc.name}")

    if enabled("enable_matching") and doc.docstatus == 0 and not in_test():
        frappe.enqueue(
            "frappe_mpsa_payments.money_in.events.match_now",
            queue="short",
            register=doc.name,
            enqueue_after_commit=True,
        )


def match_now(register: str) -> None:
    if frappe.db.get_value("Mpesa C2B Payment Register", register, "docstatus") == 0:
        matching.match_register(register)


# ---------------------------------------------------------------- scheduler
def _run(label: str, fn) -> None:
    try:
        fn()
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"M-Pesa Suite: {label} failed")


def every_two_minutes() -> None:
    if "erpnext" not in frappe.get_installed_apps():
        return
    _run("receipt matching", matching.run)
    _run("prepaid orders", sales_order.process_pending_orders)
    _run("payment request status", payment_request.sync_statuses)
    # last: these make outbound HTTP calls
    _run("STK callback settlement", stk_verify.sweep)
    _run("STK status poll", stk_poll.run)


def every_five_minutes() -> None:
    if "erpnext" not in frappe.get_installed_apps():
        return
    _run("statement lines", bank_transaction.sweep_missing)
    _run("bank reconciliation", reconcile.run)
    _run("payout statement lines", payout_statement.sweep_missing)
    _run("payout reconciliation", payout_statement.reconcile_run)
    _run("C2B confirmation verification", c2b_verify.sweep)
