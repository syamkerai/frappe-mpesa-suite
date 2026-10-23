"""Scheduler entry point for the Payments API."""

import frappe

from . import intents, webhooks


def every_minute() -> None:
    for label, fn in (("payment intents", intents.sync_open), ("webhooks", webhooks.retry_due)):
        try:
            fn()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"M-Pesa Payments API: {label} failed")
