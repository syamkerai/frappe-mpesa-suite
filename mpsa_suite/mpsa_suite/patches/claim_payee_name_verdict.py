# Copyright (c) 2026, Astral Industries Limited
# For license information, please see license.txt
"""Give Expense Claim payee lines paid before round 4.1 their Name Verdict.

Until then a line had only Name Matches (yes or no). A bank account line whose name check found
the bank's own name read as a match, though M-Pesa never shows who holds the account (Trident,
HR-EXP-2026-00713: the claimant's own bank account looked checked): it is Not Verified now, as
money_out.claim_payees.name_check says from round 4.1. A line M-Pesa named no one for, or only
the bank's number, is Not Verified; any other line that did not match Does Not Match.
Idempotent: only Paid lines with no verdict yet.
"""

import frappe
from frappe.utils import cint, cstr

from mpsa_suite.handover import ran_under_old_name

LINE = "M-Pesa Claim Payee"
BANK = "Bank Account (via Bank Paybill)"


def verdict(row) -> tuple:
    """(name_verdict, name_matches) for a line paid before round 4.1 (pure)."""
    note = cstr(row.get("name_note")).strip()
    unchecked = not note or note.startswith("Name not checked") or "only the number was checked" in note
    if row.get("payee_type") == BANK:
        return ("Not Verified", 0) if cint(row.get("name_matches")) or unchecked else ("Does Not Match", 0)
    if cint(row.get("name_matches")):
        return "Matches", 1
    return ("Not Verified", 0) if unchecked else ("Does Not Match", 0)


def execute():
    if ran_under_old_name(__name__):
        return  # R8: the patch convention; never registered under the old name
    if not frappe.db.has_column(LINE, "name_verdict"):
        return
    for row in frappe.get_all(LINE, filters={"status": "Paid", "name_verdict": ["is", "not set"]},
                              fields=["name", "payee_type", "name_matches", "name_note"]):
        value, matches = verdict(row)
        frappe.db.set_value(LINE, row.name, {"name_verdict": value, "name_matches": matches}, update_modified=False)
