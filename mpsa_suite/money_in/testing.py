"""Test fixtures for the money-in pipeline (works on ERPNext v15 and v16).

ERPNext v16 no longer ships a ``before_tests`` bootstrap, so we complete the setup wizard
ourselves when the test site has no company.
"""

import frappe
from frappe.utils import add_days, flt, now_datetime, nowdate

COMPANY = "_Test M-Pesa Co"
ABBR = "TMC"
SETTINGS = "TESTC2B"
SHORTCODE = "174379"
ITEM = "_Test M-Pesa Service"
PRICE_LIST = "_Test M-Pesa KES Selling"


def before_tests() -> None:
    """``before_tests`` hook: let ERPNext prepare a test site its own way on each version.

    v15: ``erpnext.setup.utils.before_tests`` completes the setup wizard (warehouse types,
    price lists, fiscal year...) that Frappe's test-record builder needs.
    v16: ERPNext dropped that hook; importing ``erpnext.tests.utils`` runs its BootStrapTestData.
    Running our own setup wizard instead collides with v16's bootstrap ("Standard Buying").
    """
    frappe.set_user("Administrator")
    try:
        from erpnext.setup.utils import before_tests as erpnext_before_tests
    except ImportError:
        erpnext_before_tests = None
    if erpnext_before_tests:
        erpnext_before_tests()
    else:
        try:
            import erpnext.tests.utils  # noqa: F401  (v16: bootstraps master data on import)
        except ImportError:
            pass
    frappe.db.commit()


def bootstrap() -> None:
    frappe.set_user("Administrator")
    if not frappe.db.a_row_exists("Company"):
        before_tests()  # e.g. a single module run with --skip-before-tests
    if not frappe.db.exists("Company", COMPANY):
        frappe.get_doc(
            {
                "doctype": "Company",
                "company_name": COMPANY,
                "abbr": ABBR,
                "default_currency": "KES",
                "country": "Kenya",
                "create_chart_of_accounts_based_on": "Standard Template",
                "chart_of_accounts": "Standard",
            }
        ).insert()
    _fiscal_year()
    _mpesa_settings()
    _item()
    _price_list()


def _fiscal_year() -> None:
    """The setup wizard is skipped on sites where setup is already complete."""
    year = now_datetime().year
    start, end = f"{year}-01-01", f"{year}-12-31"
    if not frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", nowdate()], "year_end_date": [">=", nowdate()]}):
        frappe.get_doc({"doctype": "Fiscal Year", "year": f"{year} M-Pesa Test", "year_start_date": start, "year_end_date": end}).insert()


def _price_list() -> None:
    """KES selling price list, so the test documents need no exchange rate (none exists for KES offline)."""
    if not frappe.db.exists("Price List", PRICE_LIST):
        frappe.get_doc({"doctype": "Price List", "price_list_name": PRICE_LIST, "currency": "KES", "selling": 1, "enabled": 1}).insert()


def _mpesa_settings() -> None:
    if not frappe.db.exists("Mpesa Settings", SETTINGS):
        frappe.get_doc(
            {
                "doctype": "Mpesa Settings",
                "payment_gateway_name": SETTINGS,
                "use_c2b": 1,
                "use_stk": 1,
                "use_reversal": 1,
                "paybill_type": "Pay Bill",
                "sandbox": 1,
                "company": COMPANY,
                "consumer_key": "key",
                "consumer_secret": "secret",
                "online_passkey": "passkey",
                "business_shortcode": SHORTCODE,
                "till_number": SHORTCODE,
                "transaction_limit": 250000,
            }
        ).insert()
    gl = frappe.db.get_value("Mode of Payment Account", {"parent": f"Mpesa-{SETTINGS}", "company": COMPANY}, "default_account")
    if not frappe.db.exists("Bank", "Mpesa"):
        frappe.get_doc({"doctype": "Bank", "bank_name": "Mpesa"}).insert()
    bank_account = frappe.db.get_value("Bank Account", {"account": gl, "is_company_account": 1}, "name")
    if not bank_account:
        bank_account = (
            frappe.get_doc(
                {
                    "doctype": "Bank Account",
                    "account_name": f"Mpesa Till {SHORTCODE}",
                    "bank": "Mpesa",
                    "account": gl,
                    "company": COMPANY,
                    "is_company_account": 1,
                    "bank_account_no": SHORTCODE,
                }
            )
            .insert()
            .name
        )
    frappe.db.set_value("Mpesa Settings", SETTINGS, "bank_account", bank_account)


def _item() -> None:
    if not frappe.db.exists("Item", ITEM):
        frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": ITEM,
                "item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
                "is_stock_item": 0,
                "stock_uom": "Nos",
            }
        ).insert()


def enable_money_in(**overrides) -> None:
    settings = frappe.get_single("M-Pesa Suite Settings")
    for flag in (
        "create_bank_transactions",
        "block_duplicate_bank_transactions",
        "receipt_as_reference",
        "block_duplicate_payment_entries",
        "default_cost_center",
        "enable_matching",
        "match_invoice_typos",
        "match_customer_name",
        "match_payer_phone",
        "match_orphan_stk",
        "link_manual_payment_entries",
        "enable_sales_order_payments",
        "auto_reconcile_bank_transactions",
    ):
        settings.set(flag, 1)
    # Verification calls Safaricom; tests that cover it turn it on and mock the calls.
    settings.verify_c2b_confirmations = 0
    settings.verify_stk_callbacks = 0
    # Tests call the callback handlers directly (no token, no source IP); callback auth has its
    # own tests and other suites may have committed it switched on.
    settings.enforce_token_c2b = 0
    settings.enforce_token_results = 0
    settings.enforce_ip_allowlist = 0
    settings.amount_tolerance = 1
    settings.matching_lookback_days = 45
    settings.sales_order_approver = "Administrator"
    for key, value in overrides.items():
        settings.set(key, value)
    settings.save()


def customer(name: str, customer_name: str | None = None) -> str:
    existing = frappe.db.get_value("Customer", {"customer_name": customer_name or name}, "name")
    if existing and not customer_name:
        return existing
    # ERPNext's v15 test bootstrap leaves Selling Settings on the group "All Customer Groups".
    group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    doc = frappe.get_doc(
        {"doctype": "Customer", "customer_name": customer_name or name, "customer_type": "Individual", "customer_group": group}
    )
    doc.insert()
    return doc.name


def sales_invoice(cust: str, amount: float):
    si = frappe.get_doc(
        {
            "doctype": "Sales Invoice",
            "company": COMPANY,
            "customer": cust,
            "posting_date": nowdate(),
            "due_date": nowdate(),
            "currency": "KES",
            "selling_price_list": PRICE_LIST,
            "items": [{"item_code": ITEM, "qty": 1, "rate": amount}],
        }
    )
    si.insert()
    si.submit()
    return si


def sales_order(cust: str, amount: float, submit: bool = False):
    so = frappe.get_doc(
        {
            "doctype": "Sales Order",
            "company": COMPANY,
            "customer": cust,
            "transaction_date": nowdate(),
            "delivery_date": add_days(nowdate(), 2),
            "currency": "KES",
            "selling_price_list": PRICE_LIST,
            "items": [{"item_code": ITEM, "qty": 1, "rate": amount, "delivery_date": add_days(nowdate(), 2)}],
        }
    )
    so.insert()
    if submit:
        so.submit()
    return so


def confirmation_payload(amount: float, billref: str = "", msisdn: str = "", transid: str | None = None) -> dict:
    return {
        "transactiontype": "Pay Bill",
        "transid": transid or ("T" + frappe.generate_hash(length=9).upper()),
        "transtime": now_datetime().strftime("%Y%m%d%H%M%S"),
        "transamount": flt(amount),
        "businessshortcode": SHORTCODE,
        "billrefnumber": billref,
        "msisdn": msisdn,
        "firstname": "TEST",
        "lastname": "PAYER",
    }


def receipt(amount: float, billref: str = "", msisdn: str = "", transid: str | None = None) -> str:
    """Insert a C2B register directly (no commit, no sleep; errors surface in the test)."""
    doc = frappe.get_doc({"doctype": "Mpesa C2B Payment Register", **confirmation_payload(amount, billref, msisdn, transid)})
    doc.insert(ignore_permissions=True)
    return doc.name


# ---------------------------------------------------------------- named users (round 4)
# Bench tests send payouts and approve payee accounts as named users holding the role that job
# needs, never as Administrator (permissions.require_named_user) or System Manager (not a payer).
PAYER = "_t-mpesa-payer@example.com"
MANAGER = "_t-mpesa-manager@example.com"


def named_user(email: str, *roles) -> str:
    """A System User ``email`` holding at least ``roles`` (made once; commit it in setUpClass)."""
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0].strip("_"),
                        "send_welcome_email": 0, "user_type": "System User"}).insert(ignore_permissions=True)
    user = frappe.get_doc("User", email)
    missing = [r for r in roles if r not in {row.role for row in user.roles} and frappe.db.exists("Role", r)]
    if missing:
        user.flags.ignore_permissions = True
        user.add_roles(*missing)
    return email


def payer() -> str:
    """An M-Pesa Payer: sends payouts (Send via M-Pesa, Pay Selected)."""
    return named_user(PAYER, "M-Pesa Payer")


def manager() -> str:
    """An M-Pesa Manager: approves payee accounts, sends payouts no document caps, admin work."""
    return named_user(MANAGER, "M-Pesa Manager")


def send_as(user: str, doc):
    """Press Send via M-Pesa (or Approve) on the saved draft ``doc`` and Confirm, as ``user``
    (money_out.send_confirm.send_for_test: the 5-second wait skipped). Returns the document as
    saved; the session user is put back."""
    from ..money_out.send_confirm import send_for_test

    previous = frappe.session.user
    frappe.set_user(user)
    try:
        return send_for_test(doc)
    finally:
        frappe.set_user(previous)
