"""A B2C Payment Disbursement's status from its rows (round 4.1).

The batch's status was set only by the 15-minute job (or the form's button), which gave up after
four tries: a batch whose last payouts finished later stayed Initiated for good, and one with a
failed row and the rest on their way read Partially Paid though nothing was paid. Now each
payout's result brings its batch up to date (sync_status_later), and the job is a safety net
(b2c_scheduler_service.update_b2c_disbursement_statuses).
"""

import frappe
from frappe.utils import cint

DOCTYPE, ROW = "B2C Payment Disbursement", "B2C Payment Disbursement Reference"


def batch_status(statuses) -> str:
    """A disbursement's status from its rows' payment statuses (pure): Paid when every row is,
    Failed when every row failed, Partially Paid once any row is paid (the others failed or still
    on their way: what was paid shows), else Initiated while its payouts are on their way; Not
    Initiated without rows."""
    statuses = list(statuses)
    total, paid, failed = len(statuses), statuses.count("Paid"), statuses.count("Failed")
    if not total:
        return "Not Initiated"
    if paid == total:
        return "Paid"
    if failed == total:
        return "Failed"
    return "Partially Paid" if paid else "Initiated"


def sync_status(name: str) -> str | None:
    """A submitted disbursement's status brought up to its rows' (batch_status), written only when it
    changed. The disbursement is locked first, so two payouts' results recorded at once leave the
    status of the later one's rows. None for a draft or a cancelled disbursement."""
    current = frappe.db.get_value(DOCTYPE, name, ["docstatus", "status"], as_dict=True, for_update=True)
    if not current or cint(current.docstatus) != 1:
        return None
    status = batch_status(frappe.get_all(ROW, filters={"parent": name, "parenttype": DOCTYPE,
                                                       "parentfield": "references"}, pluck="payment_status"))
    if status != current.status:
        frappe.db.set_value(DOCTYPE, name, "status", status, update_modified=False)
    return status


def sync_status_later(name: str | None) -> None:
    """sync_status from a job once this commits (a payout's result was recorded on its row): the
    disbursement never stays Initiated after its payouts finished. Never raises."""
    if not name:
        return
    try:
        frappe.enqueue(f"{__name__}.sync_status", queue="short", timeout=300, enqueue_after_commit=True, name=name)
    except Exception:
        frappe.log_error(title="M-Pesa B2C batch status not queued", message=f"{name}\n{frappe.get_traceback()}")
