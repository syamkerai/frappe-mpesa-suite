import frappe

from mpsa_suite.handover import ran_under_old_name

from ..migrate import after_migrate


def execute():
    if ran_under_old_name(__name__):
        return  # R8: already ran as frappe_mpsa_payments.*; see handover.py
    after_migrate()

    c2b_payments = frappe.get_all(
        "Mpesa C2B Payment Register",
        fields=["name"],
        filters={"currency": ""},
    )

    for payment in c2b_payments:
        frappe.db.set_value(
            "Mpesa C2B Payment Register",
            payment.name,
            "currency",
            "KES",
            update_modified=False,
        )
