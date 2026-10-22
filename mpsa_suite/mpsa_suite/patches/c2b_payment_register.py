import frappe

from ..migrate import after_migrate


def execute():
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
