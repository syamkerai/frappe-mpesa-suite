"""One live C2B register per M-Pesa receipt, whichever path the receipt arrives by.

C2B confirmations, Pull imports and Transaction Status results all insert registers. Each
goes through ``insert_register`` so they share one lock key and one duplicate check.

A cancelled register still owns its receipt. Staff cancel one when the money was refunded
in the M-PESA portal or booked another way; Pull re-reads the last 47 hours every hour, so
importing the receipt again would book it a second time. To re-book, amend the register.
A deleted register still owns it too, through its Deleted Document; restore that to re-book.
"""

import frappe

from ..daraja.guard import daraja_id
from .common import EXPRESS, REGISTER, named_lock

DELETED_DOCUMENT = "Deleted Document"

INSERTED = "inserted"
DUPLICATE = "duplicate"  # a live register already holds this receipt
CANCELLED = "cancelled"  # only a cancelled register holds this receipt; amend it to re-book
DELETED = "deleted"  # a register for this receipt was deleted; restore it to re-book
STK_OWNED = "stk"  # an STK prompt carries this receipt; the prompt books it


def insert_register(values: dict, flags: dict | None = None) -> tuple[str, str | None]:
    """Insert a register for ``values["transid"]`` unless one exists. Returns (outcome, name).

    The lock is held until the caller's transaction ends (see ``named_lock``) and the
    insert runs under a savepoint, so a failing hook does not leave half a register behind.
    """
    transid = (values.get("transid") or "").strip()
    if not transid:
        frappe.throw("M-Pesa receipt (TransID) is required")

    with named_lock(f"c2b:{transid}"):
        if frappe.db.get_value(EXPRESS, {"transaction_id": transid}, "name"):
            return STK_OWNED, None
        # locking read on the indexed transid: sees a register committed while we waited
        existing = frappe.db.get_value(
            REGISTER, {"transid": transid, "docstatus": ["<", 2]}, "name", for_update=True
        )
        if existing:
            return DUPLICATE, existing
        # locking too: a plain read may still see a just-cancelled register as live
        cancelled = frappe.db.get_value(
            REGISTER, {"transid": transid, "docstatus": 2}, "name", for_update=True
        )
        if cancelled:
            return CANCELLED, cancelled
        if _was_deleted(transid):
            return DELETED, None

        frappe.db.savepoint("mpesa_register_insert")
        try:
            doc = frappe.new_doc(REGISTER)
            doc.update(values)
            doc.transid = transid
            for key, value in (flags or {}).items():
                doc.flags[key] = value
            doc.insert(ignore_permissions=True)
        except Exception:
            frappe.db.rollback(save_point="mpesa_register_insert")
            raise
        return INSERTED, doc.name


def _was_deleted(transid: str) -> bool:
    """True if a register for ``transid`` was deleted: its Deleted Document is the tombstone.

    Not a locking read: Deleted Document has no index to lock on, so FOR UPDATE would lock the
    whole table. Only Daraja-shaped receipts reach the LIKE, and only an exact match counts.
    """
    if not daraja_id(transid):
        return False
    return any(
        frappe.parse_json(data).get("transid") == transid
        for data in frappe.get_all(
            DELETED_DOCUMENT,
            filters={"deleted_doctype": REGISTER, "data": ["like", f'%"transid": "{transid}"%']},
            pluck="data",
        )
    )
