"""Maker-checker on payouts sent by submitting a document (decision D3, 2026-10-23).

Who sends a direct B2B request, a B2C Payment Disbursement or a direct B2C Disbursement Request
must not be who created it or last saved it, and the submitting save must not change what is
paid or where. Shares M-Pesa Suite Settings' switch with the payment schedule
(``schedule_maker_checker``: on by default, off for one-person teams).

Not checked here: Administrator (installs, tests, console), requests a payment schedule makes
(its own maker-checker ran at Pay Selected) and the B2C requests of a disbursement that was
checked when it was submitted.
"""

import frappe
from frappe import _
from frappe.utils import cstr, flt

# What is paid and where, per DocType: (fields, (child table, row fields)).
PAID = {
    "Mpesa B2B Payment Request": (("amount", "mpesa_settings", "supplier", "supplier_mpesa_account"), None),
    "B2C Payment Disbursement": (("mode_of_payment",), ("references", ("party", "partyb", "allocated_amount"))),
    "B2C Disbursement Request": (("amount", "phone_number", "mpesa_settings"), None),
}
NUMERIC = ("Currency", "Float", "Int", "Percent")


def enabled() -> bool:
    from .schedule import maker_checker_on

    return maker_checker_on()


def _values(doc, fields) -> tuple:
    """Typed as stored: an API submit may carry "10" where the database holds 10.0."""
    out = []
    for field in fields:
        df = doc.meta.get_field(field)
        value = doc.get(field)
        out.append(flt(value, 2) if df and df.fieldtype in NUMERIC else cstr(value).strip())
    return tuple(out)


def _paid(doc) -> tuple:
    fields, table = PAID[doc.doctype]
    rows = tuple(_values(row, table[1]) for row in doc.get(table[0]) or []) if table else ()
    return _values(doc, fields), rows


def check(doc) -> None:
    """Throw unless someone other than the document's makers sends it, unchanged."""
    user = frappe.session.user
    if user == "Administrator" or not enabled():
        return
    before = doc.get_doc_before_save()
    makers = {doc.owner, before.modified_by if before else None}
    if user in makers:
        frappe.throw(
            _("You created or last changed {0}: another person must send it (maker-checker, M-Pesa Suite Settings).")
            .format(doc.name),
            title=_("Another person must send this"),
        )
    if before and _paid(doc) != _paid(before):
        frappe.throw(
            _("{0} was changed while being sent. Save the change first; then another person sends it.").format(doc.name),
            title=_("Another person must send this"),
        )
