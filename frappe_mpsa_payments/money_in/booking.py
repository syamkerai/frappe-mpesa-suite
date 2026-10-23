"""Booking matched receipts. The only path that turns a C2B register into a Payment Entry.

Booking = stamp the customer and matched document on the register, then submit it with
``submit_payment``: the register's own ``before_submit`` creates the Payment Entry (receipt
as reference, transaction date, allocation capped at what is owed).
"""

import frappe

from .common import EXPRESS, REGISTER, log_match, reversal_holding


def stamp(register, match) -> None:
    values = {"customer": match.get("customer") or register.get("customer")}
    if match.get("doctype") in ("Sales Invoice", "Sales Order"):
        values["matched_reference_doctype"] = match.doctype
        values["matched_reference_name"] = match.name
    values["match_method"] = match.get("method")
    register.db_set({k: v for k, v in values.items() if v and register.meta.has_field(k)}, update_modified=False)


def apply_match(register, match) -> None:
    from . import sales_order
    from .common import enabled

    if reversal_holding(register.name):
        return  # the money went (or is going) back to the payer: never stamp or book it
    stamp(register, match)
    log_match(
        "Matched",
        match.method,
        register=register,
        customer=match.get("customer"),
        reference_doctype=match.get("doctype"),
        reference_name=match.get("name"),
        details=match.get("details"),
    )

    if not register.get("customer") and not match.get("customer"):
        return
    if match.get("doctype") == "Sales Order" and enabled("enable_sales_order_payments"):
        sales_order.process_order(match.name)
    elif match.get("doctype") == "Sales Order" and frappe.db.get_value("Sales Order", match.name, "docstatus") != 1:
        # draft order and prepaid-order handling is off: leave it matched for staff
        log_match("Matched", "Sales Order", register=register, once=True, reference_doctype="Sales Order",
                  reference_name=match.name, details="Order is a draft; turn on prepaid orders or submit it to book.")
        return
    else:
        book_register(register.name, match.method)

    # Only now: an Express Request carrying this receipt makes the register cancel itself on
    # submit (upstream duplicate check), so the prompt is completed after the booking.
    if match.get("express") and frappe.db.get_value(REGISTER, register.name, "docstatus") == 1:
        _complete_orphan_prompt(match.express, register)


def book_register(name: str, method: str | None = None) -> str | None:
    register = frappe.get_doc(REGISTER, name)
    if register.docstatus != 0 or register.get("payment_entry"):
        return register.get("payment_entry")
    if reversal_holding(name):
        return None  # reversed: the register's before_submit would refuse it anyway
    register.submit_payment = 1
    register.flags.ignore_permissions = True
    register.submit()

    if register.payment_entry and register.get("bank_transaction"):
        # statement line was created before we knew the payer
        frappe.db.set_value(
            "Bank Transaction",
            register.bank_transaction,
            {"party_type": "Customer", "party": register.customer},
            update_modified=False,
        )
    log_match(
        "Booked",
        method,
        register=register,
        customer=register.customer,
        reference_doctype=register.get("matched_reference_doctype"),
        reference_name=register.get("matched_reference_name"),
        payment_entry=register.payment_entry,
    )
    return register.payment_entry


def link_existing_payment_entry(register, pe) -> None:
    """Receipt already booked by hand: link it so it stops showing as unmatched."""
    register.db_set(
        {"payment_entry": pe.name, "customer": register.customer or pe.party, "match_method": "Existing Payment Entry"},
        update_modified=False,
    )
    log_match("Booked", "Existing Payment Entry", register=register, customer=pe.party, payment_entry=pe.name)


def _complete_orphan_prompt(express_name: str, register) -> None:
    # db.set_value on purpose: saving the Express Request would run its duplicate-C2B check
    # and cancel this very register.
    if not frappe.db.get_value(EXPRESS, express_name, "transaction_id"):
        frappe.db.set_value(
            EXPRESS,
            express_name,
            {
                "transaction_id": register.transid,
                "status": "Completed",
                "result_desc": f"Matched to C2B receipt {register.transid} (no STK callback received)",
            },
            update_modified=False,
        )
