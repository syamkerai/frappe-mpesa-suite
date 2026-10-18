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


def bootstrap() -> None:
    frappe.set_user("Administrator")
    if not frappe.db.a_row_exists("Company"):
        from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

        year = now_datetime().year
        setup_complete(
            {
                "currency": "KES",
                "full_name": "Test User",
                "company_name": COMPANY,
                "timezone": "Africa/Nairobi",
                "company_abbr": ABBR,
                "industry": "Manufacturing",
                "country": "Kenya",
                "fy_start_date": f"{year}-01-01",
                "fy_end_date": f"{year}-12-31",
                "language": "english",
                "company_tagline": "Testing",
                "email": "test@example.com",
                "password": "test",
                "chart_of_accounts": "Standard",
            }
        )
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
                "api_type": "MPesa Express",
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
    doc = frappe.get_doc({"doctype": "Customer", "customer_name": customer_name or name, "customer_type": "Individual"})
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
