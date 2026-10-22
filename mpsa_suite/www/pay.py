"""Public pay page: /pay/<token> (route rule in hooks.py).

Anyone with the link can see the amount and description and send the prompt to a phone;
nothing else about the customer or the merchant's books is exposed.
"""

import frappe
from frappe import _

no_cache = 1


def get_context(context):
    from mpsa_suite.gateway.auth import ApiError
    from mpsa_suite.gateway.v1 import _intent_by_token, page_view

    token = frappe.form_dict.get("token") or ""
    context.no_cache = 1
    context.show_sidebar = False
    try:
        intent = _intent_by_token(token)
    except ApiError:
        context.http_status_code = 404  # read by the template page renderer
        context.payment = None
        context.title = _("Payment not found")
        return context

    # Read-only render; the page's first status poll refreshes the payment (and commits).
    view = page_view(intent)
    company = frappe.db.get_value(
        "Mpesa Settings",
        frappe.db.get_value("Mpesa API Client", intent.client, "mpesa_settings"),
        "company",
    ) if frappe.get_meta("Mpesa Settings").has_field("company") else None

    context.payment = view
    context.phone_locked = bool(intent.phone)  # merchant fixed the number: page cannot redirect the prompt
    context.token = token
    context.merchant = company or frappe.db.get_single_value("Website Settings", "app_name") or "M-Pesa"
    context.title = _("Pay {0}").format(context.merchant)
    return context
