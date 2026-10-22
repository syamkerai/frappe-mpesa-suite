# Copyright (c) 2026, Astral Industries Limited
# For license information, please see license.txt
"""Start payout statement lines at the upgrade date.

The payout sweep (money_out.statement) gives every Paid payout within Look Back (Days) a
withdrawal line, a Safaricom charge line and a charge Journal Entry. On a site upgraded with
history (Astral: 1,473 payouts already fed by its own scripts, about 1,046 of them without a
charge line) the first sweep would book hundreds of charge entries into past, possibly
closed, periods. So an upgraded site starts at today; staff move M-Pesa Suite Settings →
Payout Lines From back to fill older payouts on purpose. A new install has no history and
never runs this patch (install-app marks patches as done).
"""

import frappe
from frappe.utils import nowdate

SETTINGS = "M-Pesa Suite Settings"


def execute():
    if not frappe.db.exists("DocType", SETTINGS):
        return
    stored = frappe.db.sql("SELECT field, value FROM `tabSingles` WHERE doctype = %s", SETTINGS)
    if not stored:
        return  # never saved: nothing was fed before
    if dict(stored).get("payout_lines_from"):
        return
    frappe.db.set_single_value(SETTINGS, "payout_lines_from", nowdate())
