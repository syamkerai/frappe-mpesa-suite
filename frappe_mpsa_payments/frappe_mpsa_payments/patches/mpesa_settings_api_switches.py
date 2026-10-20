# Copyright (c) 2026, Astral Industries Limited
# For license information, please see license.txt
"""Set the per-API switches on existing Mpesa Settings from the old ``api_type``.

Upstream let any record do anything, so a record keeps every API it was already used for:
its old API Type, C2B URLs registered for it or C2B receipts on its shortcode, STK prompts,
B2C payouts, B2B payments and reversals sent from it. Everything else starts off and can be
ticked on the form.
"""

import frappe
from frappe.utils import cstr

from ...daraja.apis import ALL, switches_from_legacy

SETTINGS = "Mpesa Settings"


def _used(doctype: str, filters: dict) -> bool:
    if not frappe.db.table_exists(doctype):
        return False
    if any(not frappe.db.has_column(doctype, field) for field in filters):
        return False
    return bool(frappe.db.exists(doctype, filters))


def _received_c2b(row) -> bool:
    codes = [c for c in {cstr(row.business_shortcode).strip(), cstr(row.till_number).strip()} if c]
    return bool(codes) and _used("Mpesa C2B Payment Register", {"businessshortcode": ["in", codes]})


def execute():
    if not all(frappe.db.has_column(SETTINGS, api) for api in ALL):
        return  # runs post_model_sync, so the columns exist; guard against a partial sync
    for row in frappe.get_all(SETTINGS, fields=["name", "api_type", "business_shortcode", "till_number"]):
        flags = switches_from_legacy(
            row.api_type,
            registered_c2b=_used("Mpesa C2B Payment Register URL", {"mpesa_settings": row.name})
            or _received_c2b(row),
            sent_stk=_used("Mpesa Express Request", {"settings": row.name}),
            sent_b2c=_used("B2C Disbursement Request", {"mpesa_settings": row.name})
            or _used("B2C Payment Disbursement", {"mode_of_payment": f"Mpesa-{row.name}"}),
            sent_b2b=_used("Mpesa B2B Payment Request", {"mpesa_settings": row.name}),
            sent_reversal=_used("Mpesa Reversal Request", {"mpesa_settings": row.name}),
        )
        frappe.db.set_value(SETTINGS, row.name, flags, update_modified=False)
