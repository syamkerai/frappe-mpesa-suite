"""The suite's own Payment and Journal Entries, posted at once (the owner's rule, 2026-10-28).

Every entry the suite makes for a payout, its charge, a statement transfer or money received is
inserted already submitted, in one save, never as a draft for review: the money has moved
already. Frappe runs an insert with docstatus 1 as a submit (validate, before_submit, on_update,
on_submit) and, under a workflow, saves it straight in the workflow's first submitted state
(Approved: validate_workflow, set_workflow_state_on_action). An insert then a submit would run
the workflow's Draft state on the insert: an Open Workflow Action for the Draft state's
approvers (process_workflow_actions, on_update) that the submit never completes, and the Draft
state's email after the commit.

The switches "Submit B2C Accounting Entries" (Mpesa Settings) and "Submit Transfer and Funding
Entries" (M-Pesa Suite Settings) are pinned on (migrate.post_entries_at_once); a site still
reading one off (before its migrate, or a test) gets the draft it got before.
"""

import frappe
from frappe.utils import cint, cstr

# What ERPNext does on a draft's save only (docstatus 0), which an entry inserted submitted would
# skip: a Payment Entry's advance account when the company books advances separately
# (set_liability_account), a Journal Entry's against accounts and tax withholding. The draft's
# save methods run on the entry first, as the insert of an insert then a submit ran them.
DRAFT_SAVE = ("before_validate", "validate", "before_save")


def insert_entry(entry, submit=True, ignore_permissions=None):
    """Insert ``entry`` (a new Payment or Journal Entry): posted at once when ``submit``, else a
    draft. Returns it. The checks of each are Frappe's own: a user's entry needs create and
    submit permission on its DocType (unless ``ignore_permissions``)."""
    if not cint(submit):
        entry.insert(ignore_permissions=ignore_permissions)
        return entry
    for method in DRAFT_SAVE:
        entry.run_method(method)
    entry.docstatus = 1
    entry.insert(ignore_permissions=ignore_permissions)
    return entry


# ERPNext's refusals of a day, as plain ValidationErrors (translated): Accounts Frozen Till
# (general_ledger.check_freezing_date) and books closed by a Period Closing Voucher
# (general_ledger.validate_against_pcv, its message with a closing "." on v15, without on v16).
CLOSED_MESSAGES = (
    "You are not authorized to add or update entries before {0}",
    "Books have been closed till the period ending on {0}.",
    "Books have been closed till the period ending on {0}",
)


def period_closed(exc) -> bool:
    """True when ERPNext refused an entry for its date: a closed Accounting Period
    (accounting_period.validate_accounting_period_on_doc_save, general_ledger
    validate_accounting_period), books closed by a Period Closing Voucher
    (general_ledger.validate_against_pcv) or Accounts Frozen Till (general_ledger
    check_freezing_date). The last two bind every user, Administrator too, whom the automatic
    bookings run as."""
    try:
        from erpnext.accounts.doctype.accounting_period.accounting_period import (
            ClosedAccountingPeriod,
        )
    except ImportError:
        return False
    if isinstance(exc, ClosedAccountingPeriod):
        return True
    if not isinstance(exc, frappe.ValidationError):
        return False
    text = cstr(exc).strip()
    # a translation that starts with the date has no text before it to tell it by
    prefixes = {frappe._(message).split("{0}")[0].strip() for message in CLOSED_MESSAGES}
    return any(prefix and text.startswith(prefix) for prefix in prefixes)
