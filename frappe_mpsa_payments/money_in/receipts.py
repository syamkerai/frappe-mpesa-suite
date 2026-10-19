"""One live C2B register per M-Pesa receipt, whichever path the receipt arrives by.

C2B confirmations, Pull imports and Transaction Status results all insert registers. Each
goes through ``insert_register`` so they share one lock key and one duplicate check.
"""

import frappe

from .common import EXPRESS, REGISTER, named_lock

INSERTED = "inserted"
DUPLICATE = "duplicate"  # a live register already holds this receipt
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
