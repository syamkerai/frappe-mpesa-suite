"""Which Bank Account each of a shortcode's sub-accounts posts to.

An M-PESA shortcode is several wallets: Utility (paybill collections, B2C payouts), Working /
MMF (funding from the bank, B2B payouts), Merchant (till collections), Charges Paid,
Organization Settlement, Float. Mpesa Settings → Accounting Mapping → Sub-Accounts maps any
of them to its own Bank Account (and so its own GL). Nothing is required: a sub-account left
out posts to the shortcode's Bank Account, exactly as before, so a site upgraded from
Navari's app (one Bank Account per shortcode) keeps working unchanged until staff map more.

The sub-account that receives the shortcode's customer payments (Utility for a paybill,
Merchant for a till) is always the shortcode's own Bank Account: its receipts are booked to
the Mode of Payment's account, and a line on another GL could never be reconciled with them.
"""

import frappe
from frappe.utils import cstr

from ..money_in.common import bank_account_for

SUB_ACCOUNT_DOCTYPE = "Mpesa Sub Account"
UTILITY, WORKING, MERCHANT = "Utility", "Working", "Merchant"
SUB_ACCOUNTS = (UTILITY, WORKING, MERCHANT, "Charges Paid", "Organization Settlement", "Float")


def receiving(settings_name: str | None) -> str:
    """The sub-account a shortcode's customer payments land in: Merchant for a till, else Utility."""
    if settings_name and frappe.db.get_value("Mpesa Settings", settings_name, "paybill_type") == "Buy Goods":
        return MERCHANT
    return UTILITY


def mapped(settings_name: str | None) -> dict:
    """{sub-account: Bank Account} as mapped on Mpesa Settings (empty when nothing is mapped)."""
    if not settings_name or not frappe.db.table_exists(SUB_ACCOUNT_DOCTYPE):
        return {}
    rows = frappe.get_all(
        SUB_ACCOUNT_DOCTYPE,
        filters={"parent": settings_name, "parenttype": "Mpesa Settings"},
        fields=["sub_account", "bank_account"],
        order_by="idx asc",
    )
    return {r.sub_account: r.bank_account for r in rows if r.sub_account and r.bank_account}


def bank_account(settings_name: str | None, sub_account: str | None = None) -> str | None:
    """The Bank Account for one of the shortcode's sub-accounts: its own if mapped, else the shortcode's."""
    if sub_account and sub_account != receiving(settings_name):
        own = mapped(settings_name).get(sub_account)
        if own:
            return own
    return bank_account_for(settings_name)


def gl(settings_name: str | None, sub_account: str) -> str | None:
    """The GL of a sub-account mapped to a Bank Account of its own, else None (use the usual account)."""
    if sub_account == receiving(settings_name):
        return None
    own = mapped(settings_name).get(sub_account)
    return frappe.db.get_value("Bank Account", own, "account") if own else None


def b2c_utility_gl(disbursement_row) -> str | None:
    """A B2C payout's entry pays from Utility: its GL when Utility has a Bank Account of its own
    (a till's Utility account), else None and the entry keeps its usual account."""
    request = getattr(disbursement_row, "b2c_disbursement_request", None)
    settings = request and frappe.db.get_value("B2C Disbursement Request", request, "mpesa_settings")
    return gl(settings, UTILITY) if settings else None


def separate(settings_name: str | None, sub_account: str) -> bool:
    """True when the sub-account posts to a Bank Account other than the shortcode's."""
    return bank_account(settings_name, sub_account) != bank_account_for(settings_name)


def all_bank_accounts() -> set:
    """Every Bank Account mapped to a sub-account (they carry M-Pesa money too)."""
    if not frappe.db.table_exists(SUB_ACCOUNT_DOCTYPE):
        return set()
    return set(
        frappe.get_all(
            SUB_ACCOUNT_DOCTYPE,
            filters={"parenttype": "Mpesa Settings", "bank_account": ["is", "set"]},
            pluck="bank_account",
            order_by="creation asc",
        )
    )


def from_balance_text(value) -> str | None:
    """The sub-account a result's balance names: "Working Account|KES|1476.00|..." -> "Working"."""
    name = cstr(value).split("|", 1)[0].strip().lower()
    if not name or "{" in name:
        return None
    for key, words in (
        (WORKING, ("working", "mmf")),
        (UTILITY, ("utility",)),
        (MERCHANT, ("merchant",)),
        ("Charges Paid", ("charges paid",)),
        ("Organization Settlement", ("settlement",)),
        ("Float", ("float",)),
    ):
        if any(w in name for w in words):
            return key
    return None


def validate_settings(doc) -> None:
    """Mpesa Settings validate: each sub-account once, in the shortcode's company; the receiving
    sub-account only on the shortcode's own Bank Account."""
    rows = doc.get("sub_accounts") or []
    seen = set()
    company = doc.get("company")
    main = "Merchant" if doc.get("paybill_type") == "Buy Goods" else UTILITY
    main_account = doc.get("bank_account")
    for row in rows:
        if row.sub_account in seen:
            frappe.throw(f"Sub-account {row.sub_account} is mapped twice")
        seen.add(row.sub_account)
        values = frappe.db.get_value("Bank Account", row.bank_account, ["company", "is_company_account", "account"],
                                     as_dict=True)
        if not values or not values.is_company_account:
            frappe.throw(f"{row.bank_account} (sub-account {row.sub_account}) must be a company Bank Account")
        if not values.account:
            frappe.throw(f"Bank Account {row.bank_account} has no GL account")
        if company and values.company != company:
            frappe.throw(f"Bank Account {row.bank_account} belongs to {values.company}, not {company}")
        if row.sub_account == main and main_account and row.bank_account != main_account:
            frappe.throw(
                f"{main} receives this shortcode's customer payments, so it stays on {main_account} (Bank Account "
                f"above). Remove the {main} row, or change the Bank Account above."
            )
