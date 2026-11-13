import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MODULE = "Mpsa Suite"


def is_app_installed(app_name: str) -> bool:
    return app_name in frappe.get_installed_apps()


def after_migrate():
    from mpsa_suite import access
    from mpsa_suite.install import ensure_lookup_indexes, ensure_roles, ensure_suite_settings

    ensure_roles()
    access.after_migrate()  # Role Profiles, credential-field access, site permission overrides
    ensure_suite_settings()
    post_entries_at_once()
    ensure_lookup_indexes()
    sync_payee_accounts()

    if is_app_installed("payments"):
        update_settings_module()

    if is_app_installed("erpnext"):
        create_custom_erpnext_fields()
        create_erpnext_property_setters()
        create_payment_entry_mpesa_fields()
        backfill_b2b_entry_status()
        create_bulk_payout_dimensions()  # round 4.5: the bulk payout rows' dimensions

    if is_app_installed("hrms"):
        create_custom_hrms_fields()
        create_claim_payee_fields()


def create_bulk_payout_dimensions():
    """The accounting dimensions a site has (Astral: Department) on M-Pesa Bulk Payout Row, which
    hooks.py lists in accounting_dimension_doctypes: ERPNext adds a dimension to those DocTypes when
    the dimension is made, not to a DocType that comes later. Idempotent; never stops the migrate."""
    if not frappe.db.exists("DocType", "M-Pesa Bulk Payout Row") or not frappe.db.exists("DocType",
                                                                                      "Accounting Dimension"):
        return
    try:
        from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
            create_accounting_dimensions_for_doctype,
        )

        create_accounting_dimensions_for_doctype("M-Pesa Bulk Payout Row")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "M-Pesa Suite: accounting dimensions not added to bulk payout rows")


def post_entries_at_once():
    """The suite's own Payment and Journal Entries (payouts, their charges, statement transfers) are
    posted at once: submitted, and in a Payment Entry workflow its first submitted state (Approved),
    which Frappe sets on submit. Never drafts left for review, since the money has left already.
    Every migrate switches both settings on; drafts made before stay as they are, for staff."""
    if frappe.db.has_column("Mpesa Settings", "submit_b2c_accounting_entries"):
        frappe.db.sql("UPDATE `tabMpesa Settings` SET submit_b2c_accounting_entries = 1 "
                      "WHERE IFNULL(submit_b2c_accounting_entries, 0) = 0")
    if frappe.db.exists("DocType", "M-Pesa Suite Settings"):
        frappe.db.set_single_value("M-Pesa Suite Settings", "submit_statement_entries", 1, update_modified=False)


def create_claim_payee_fields():
    """Merchants and casual payees paid straight from an Expense Claim (money_out.claim_payees):
    the claim's table of them and the Expense Claim Type's switch. The section stays hidden until
    the claim's form finds the Suite switch on (public/js/expense_claim_payees.js)."""
    create_fields({
        "Expense Claim": [
            {
                "fieldname": "mpesa_payees_section",
                "fieldtype": "Section Break",
                "label": "Pay Merchants and Payees by M-Pesa",
                "insert_after": "expenses",
                "hidden": 1,
            },
            {
                "fieldname": "mpesa_payees_summary",
                "fieldtype": "HTML",
                "label": "M-Pesa Payees",
                "insert_after": "mpesa_payees_section",
            },
            {
                "fieldname": "mpesa_payees",
                "fieldtype": "Table",
                "label": "M-Pesa Payees",
                "options": "M-Pesa Claim Payee",
                "insert_after": "mpesa_payees_summary",
                "description": "Merchants (till, paybill, bank account) and casual payees (phones) this claim pays directly "
                "by M-Pesa, instead of reimbursing the employee who paid them. Approved with the claim; whatever the "
                "lines leave of what the claim owes is reimbursed to the employee as usual.",
            },
        ],
        "Expense Claim Type": [
            {
                "fieldname": "allow_mpesa_payees",
                "fieldtype": "Check",
                "label": "Paid to Merchants or Casual Payees by M-Pesa",
                "insert_after": "deferred_expense_account",
                "description": "Expense lines of this type may be paid straight to a merchant's till, paybill or bank "
                "account, or to casual payees' phones, from the Expense Claim (M-Pesa Suite Settings must allow it).",
            },
        ],
    })


def sync_payee_accounts():
    """The shipped bank paybills into the site (a release updates only what an admin has not
    changed), then the payee accounts brought in step: the Employee type's old label renamed,
    bank accounts linked to their bank's row, destination keys filled. Idempotent, every migrate
    and at install (after_install runs this hook); never a patch, since each release may ship
    new rows."""
    from mpsa_suite.money_out import bank_paybills, party_accounts

    bank_paybills.sync_shipped()
    party_accounts.after_migrate()


def backfill_b2b_entry_status():
    """Paid B2B requests from before the Accounting Entry fields get their state (looked at only,
    never booked). Idempotent: only rows with no state yet are read."""
    from mpsa_suite.money_out.b2b_entries import backfill_entry_status

    backfill_entry_status()


def update_settings_module():
    current_module = frappe.db.get_value("DocType", "Mpesa Settings", "module")
    if current_module != MODULE:
        frappe.db.set_value("DocType", "Mpesa Settings", "module", MODULE)
        frappe.reload_doctype("Mpesa Settings")


def create_custom_hrms_fields():
    """Fields that link HRMS DocTypes (HRMS is a required app; installed before this one)."""
    custom_fields = {
        "B2C Payment Disbursement Reference": [
            {
                "depends_on": 'eval: doc.reference_doctype === "Salary Slip"',
                "fieldname": "payroll_entry",
                "fieldtype": "Link",
                "label": "Payroll Entry",
                "options": "Payroll Entry",
                "insert_after": "party",
            },
        ],
        # A B2B salary to an employee's bank paybill pays a Salary Slip of a Payroll Entry
        # (money_out.b2b, salary_entries).
        "Mpesa B2B Payment Request": [
            {
                "fieldname": "salary_slip",
                "fieldtype": "Link",
                "label": "Salary Slip",
                "options": "Salary Slip",
                "insert_after": "employee_mpesa_account",
                "depends_on": "eval:doc.employee",
                "search_index": 1,
                "description": "Optional. The salary this pays: it cannot exceed what the slip has left to pay, and the "
                "salary Journal Entry clears the Payroll Entry's payroll payable when M-Pesa confirms.",
            },
            {
                "fieldname": "payroll_entry",
                "fieldtype": "Link",
                "label": "Payroll Entry",
                "options": "Payroll Entry",
                "insert_after": "salary_slip",
                "depends_on": "eval:doc.salary_slip",
                "read_only": 1,
                "search_index": 1,
            },
            # A B2B to an employee's account that pays an Expense Claim or an Employee Advance
            # (money_out.b2b, employee_documents): one of the slip, the claim and the advance.
            {
                "fieldname": "expense_claim",
                "fieldtype": "Link",
                "label": "Expense Claim",
                "options": "Expense Claim",
                "insert_after": "payroll_entry",
                "depends_on": "eval:doc.employee && !doc.salary_slip && !doc.employee_advance",
                "search_index": 1,
                "description": "Optional. The Expense Claim this pays: it cannot exceed what the claim has left to pay.",
            },
            {
                "fieldname": "employee_advance",
                "fieldtype": "Link",
                "label": "Employee Advance",
                "options": "Employee Advance",
                "insert_after": "expense_claim",
                "depends_on": "eval:doc.employee && !doc.salary_slip && !doc.expense_claim",
                "search_index": 1,
                "description": "Optional. The Employee Advance this pays: it cannot exceed what the advance has left to pay.",
            },
        ],
    }
    create_fields(custom_fields)


def payout_statement_fields(insert_after: str) -> list:
    fields = [
        ("bank_transaction", "Bank Transaction", "Link", "Bank Transaction"),
        ("mpesa_charge", "M-Pesa Charge", "Currency", None),
        ("charge_bank_transaction", "Charge Bank Transaction", "Link", "Bank Transaction"),
    ]
    rows = []
    for fieldname, label, fieldtype, options in fields:
        row = {
            "fieldname": fieldname,
            "label": label,
            "fieldtype": fieldtype,
            "insert_after": insert_after,
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
            "module": MODULE,
        }
        if options:
            row["options"] = options
        rows.append(row)
        insert_after = fieldname
    return rows


def create_custom_erpnext_fields():
    custom_fields = {
        # One Mpesa Settings record per shortcode/till: map it to its books once, instead of
        # hard-coding "Mpesa-208911 - AIL" or matching bank accounts with LIKE '%code%'.
        "Mpesa Settings": [
            {
                "fieldname": "company",
                "label": "Company",
                "fieldtype": "Link",
                "options": "Company",
                "insert_after": "api_type",
                "description": "Company whose books receive this shortcode's money. Saving with a Company "
                "creates its Payment Gateway Account and the Mode of Payment 'Mpesa-<name>'.",
            },
            {
                "fieldname": "accounting_mapping_section",
                "label": "Accounting Mapping",
                "fieldtype": "Section Break",
                "insert_after": "transaction_limit",
                "collapsible": 1,
                "module": MODULE,
            },
            {
                "fieldname": "bank_account",
                "label": "Bank Account",
                "fieldtype": "Link",
                "options": "Bank Account",
                "insert_after": "accounting_mapping_section",
                "description": "Company Bank Account that represents this shortcode/till. "
                "Bank Transactions for every receipt and payout are posted here.",
                "module": MODULE,
            },
            {
                "fieldname": "accounting_mapping_column",
                "fieldtype": "Column Break",
                "insert_after": "bank_account",
                "module": MODULE,
            },
            {
                "fieldname": "cost_center",
                "label": "Cost Center",
                "fieldtype": "Link",
                "options": "Cost Center",
                "insert_after": "accounting_mapping_column",
                "description": "Used on Payment Entries created from this shortcode when none is given.",
                "module": MODULE,
            },
            {
                "fieldname": "charges_account",
                "label": "M-Pesa Charges Account",
                "fieldtype": "Link",
                "options": "Account",
                "insert_after": "cost_center",
                "description": "Expense account for Safaricom transaction charges (e.g. 'M-Pesa Charges').",
                "module": MODULE,
            },
            {
                "fieldname": "sub_accounts_section",
                "label": "Sub-Accounts",
                "fieldtype": "Section Break",
                "insert_after": "charges_account",
                "collapsible": 1,
                "module": MODULE,
            },
            {
                "fieldname": "sub_accounts",
                "label": "Sub-Accounts",
                "fieldtype": "Table",
                "options": "Mpesa Sub Account",
                "insert_after": "sub_accounts_section",
                "description": "Optional. A shortcode is several wallets: Utility (paybill money, B2C payouts), Working / "
                "MMF (bank funding, B2B payouts), Merchant (till money), Charges Paid... Map one to a Bank Account "
                "of its own (an existing one is fine) to keep its money and statement apart. A sub-account left out "
                "uses the Bank Account above, as before. The one that receives customer payments (Utility, or "
                "Merchant for a till) always stays on the Bank Account above.",
                "module": MODULE,
            },
        ],
        # wallet.balances: which wallet a line moved, and when Safaricom completed it
        "Bank Transaction": [
            {
                "fieldname": "mpesa_sub_account",
                "label": "M-Pesa Sub-Account",
                "fieldtype": "Data",
                "insert_after": "transaction_type",
                "read_only": 1,
                "module": MODULE,
            },
            {
                "fieldname": "mpesa_completed_at",
                "label": "M-Pesa Completion Time",
                "fieldtype": "Datetime",
                "insert_after": "mpesa_sub_account",
                "read_only": 1,
                "search_index": 1,
                "module": MODULE,
            },
        ],
        "Sales Invoice Payment": [
            {
                "fieldname": "phone_number",
                "label": "Phone Number",
                "fieldtype": "Data",
                "insert_after": "amount",
                "read_only_depends_on": 'eval: doc.type !== "Phone"',
                "in_list_view": 1,
                "in_standard_filter": 1,
            },
            {
                "fieldname": "initiate_stk_push",
                "label": "Initiate STK Push",
                "fieldtype": "HTML",
                "insert_after": "clearance_date",
                "depends_on": 'eval: (doc.type == "Phone") && (!doc.reference_no)',
                "in_list_view": 1,
                "in_preview": 1,
                "in_standard_filter": 1,
            },
        ],
        "Payment Entry Reference": [
            {
                "fieldname": "b2c_payment_disbursement",
                "label": "B2C Payment Disbursement",
                "fieldtype": "Link",
                "insert_after": "payment_request_outstanding",
                "options": "B2C Payment Disbursement",
            }
        ],
        "Journal Entry Account": [
            {
                "fieldname": "b2c_payment_disbursement",
                "label": "B2C Payment Disbursement",
                "fieldtype": "Link",
                "insert_after": "reference_detail_no",
                "options": "B2C Payment Disbursement",
            },
            # A salary Journal Entry of a B2B salary payout: salary_slip_paid counts the Paid request,
            # so it must not count this row as a bank payment too.
            {
                "fieldname": "mpesa_b2b_payment_request",
                "label": "M-Pesa B2B Payment Request",
                "fieldtype": "Link",
                "insert_after": "b2c_payment_disbursement",
                "options": "Mpesa B2B Payment Request",
                "read_only": 1,
                "no_copy": 1,
            },
        ],
        "Payment Request": [
            {
                "fieldname": "payment_token",
                "label": "Payment Token",
                "fieldtype": "Long Text",
                "insert_after": "column_break_iiuv",
                "hidden": 1,
            }
        ],
        "B2C Payment Disbursement": [
            {
                "fieldname": "company",
                "label": "Company",
                "fieldtype": "Link",
                "options": "Company",
                "insert_after": "posting_date",
                "reqd": 1,
            },
            {
                "fetch_from": "company.default_currency",
                "fieldname": "company_currency",
                "fieldtype": "Link",
                "hidden": 1,
                "label": "Company Currency",
                "options": "Currency",
                "read_only": 1,
                "insert_after": "company",
            },
            {
                "depends_on": "eval: doc.payment_type",
                "fieldname": "mode_of_payment",
                "fieldtype": "Link",
                "label": "Mode of Payment",
                "options": "Mode of Payment",
                "insert_after": "company_currency",
                "reqd": 1,
            },
            {
                "collapsible": 1,
                "fieldname": "accounts_section",
                "fieldtype": "Section Break",
                "label": "Accounts",
                "insert_after": "transaction_to_pay_against",
            },
            {
                "fieldname": "paid_from",
                "label": "Account Paid From",
                "fieldtype": "Link",
                "options": "Account",
                "insert_after": "accounts_section",
                "reqd": 1,
            },
            {
                "fieldname": "paid_from_account_type",
                "label": "Paid From Account Type",
                "fieldtype": "Data",
                "fetch_from": "paid_from.account_type",
                "hidden": 1,
                "insert_after": "paid_from",
            },
            {
                "fieldname": "paid_from_account_currency",
                "label": "Account Currency (From)",
                "fieldtype": "Link",
                "options": "Currency",
                "insert_after": "paid_from_account_type",
                "reqd": 1,
            },
            {
                "fieldname": "column_break_nsjt",
                "fieldtype": "Column Break",
                "insert_after": "paid_from_account_currency",
            },
            # Needed by the disbursement's own validate (validate_mandatory_fields), not here: a payout
            # whose rows pay people no party names leaves it empty, each row booked on its document's
            # accounts (money_out.partyless).
            {
                "fieldname": "paid_to",
                "label": "Account Paid To",
                "fieldtype": "Link",
                "options": "Account",
                "insert_after": "column_break_nsjt",
                "reqd": 0,
            },
            {
                "fieldname": "paid_to_account_type",
                "label": "Paid To Account Type",
                "fieldtype": "Data",
                "fetch_from": "paid_to.account_type",
                "hidden": 1,
                "insert_after": "paid_to",
            },
            {
                "fieldname": "paid_to_account_currency",
                "label": "Account Currency (To)",
                "fieldtype": "Link",
                "options": "Currency",
                "read_only": 1,
                "reqd": 1,
                "insert_after": "paid_to_account_type",
            },
        ],
        "Mpesa Express Request": [
            {
                "fieldname": "bank_transaction",
                "label": "Bank Transaction",
                "fieldtype": "Link",
                "options": "Bank Transaction",
                "insert_after": "transaction_id",
                "read_only": 1,
                "allow_on_submit": 1,
                "module": MODULE,
            }
        ],
        # money_out.statement: each Paid payout's line and its Safaricom charge's line
        "B2C Disbursement Request": payout_statement_fields("accounting_error"),
        "Mpesa B2B Payment Request": payout_statement_fields("payment_entry"),
        "Mpesa C2B Payment Register": [
            {
                "fieldname": "company",
                "label": "Company",
                "fieldtype": "Link",
                "options": "Company",
                "insert_after": "posting_time",
            },
            {
                "fetch_from": "company.default_currency",
                "fieldname": "default_currency",
                "fieldtype": "Data",
                "label": "Default Currency",
                "read_only": 1,
                "insert_after": "company",
            },
            {
                "fieldname": "customer",
                "fieldtype": "Link",
                "in_list_view": 1,
                "in_preview": 1,
                "in_standard_filter": 1,
                "label": "Customer",
                "options": "Customer",
                "insert_after": "default_currency",
            },
            {
                "fieldname": "mode_of_payment",
                "fieldtype": "Link",
                "label": "Mode of Payment",
                "options": "Mode of Payment",
                "insert_after": "customer",
            },
            {
                "fieldname": "payment_entry",
                "fieldtype": "Link",
                "label": "Payment Entry",
                "options": "Payment Entry",
                "read_only": 1,
                "insert_after": "submit_payment",
            },
            {
                "fieldname": "pos_invoice",
                "fieldtype": "Link",
                "label": "POS Invoice",
                "options": "POS Invoice",
                "read_only": 1,
                "insert_after": "submit_payment",
                "module": "Mpsa Suite",
            },
            {
                "fieldname": "sales_invoice",
                "fieldtype": "Link",
                "label": "Sales Invoice",
                "options": "Sales Invoice",
                "read_only": 1,
                "insert_after": "pos_invoice",
                "module": "Mpsa Suite",
            },
            {
                "fieldname": "currency",
                "fieldtype": "Link",
                "label": "Currency",
                "options": "Currency",
                "read_only": 1,
                "insert_after": "mode_of_payment",
            },
            {
                "fieldname": "bank_transaction",
                "label": "Bank Transaction",
                "fieldtype": "Link",
                "options": "Bank Transaction",
                "insert_after": "currency",
                "read_only": 1,
                "allow_on_submit": 1,
                "module": MODULE,
            }
        ],
        "Mpesa C2B Payment Register URL": [
            {
                "fieldname": "company",
                "fieldtype": "Link",
                "in_list_view": 1,
                "in_standard_filter": 1,
                "label": "Company",
                "options": "Company",
                "reqd": 1,
                "insert_after": "column_break_4",
            },
            {
                "fieldname": "mode_of_payment",
                "fieldtype": "Link",
                "in_list_view": 1,
                "in_standard_filter": 1,
                "label": "Mode of Payment",
                "options": "Mode of Payment",
                "reqd": 1,
                "insert_after": "company",
            },
        ],
    }
    # Frappe adds the DB columns only after a whole create_custom_fields batch. B2C Payment
    # Disbursement's Links table points at these two link fields, so on a fresh install its
    # validation ran before the columns existed ("Unknown column ... b2c_payment_disbursement").
    create_fields({dt: custom_fields.pop(dt) for dt in ("Payment Entry Reference", "Journal Entry Account")})
    create_fields(custom_fields)


def payment_entry_mpesa_fields() -> dict:
    """The STK section on Payment Entry (public/js/payment_entry.js)."""
    return {
        "Payment Entry": [
            {
                "fieldname": "custom_mpesa_section",
                "label": "M-Pesa STK Push",
                "fieldtype": "Section Break",
                "insert_after": "mode_of_payment",
                "depends_on": "eval:doc.payment_type=='Receive' && doc.party_type=='Customer'",
                "module": MODULE,
            },
            {
                "fieldname": "custom_payment_gateway_account",
                "label": "Payment Gateway Account",
                "fieldtype": "Link",
                "options": "Payment Gateway Account",
                "insert_after": "custom_mpesa_section",
                "module": MODULE,
            },
            {
                "fieldname": "custom_payment_gateway",
                "label": "Payment Gateway",
                "fieldtype": "Link",
                "options": "Payment Gateway",
                "fetch_from": "custom_payment_gateway_account.payment_gateway",
                "read_only": 1,
                "insert_after": "custom_payment_gateway_account",
                "module": MODULE,
            },
            {
                "fieldname": "custom_mpesa_column_break",
                "fieldtype": "Column Break",
                "insert_after": "custom_payment_gateway",
                "module": MODULE,
            },
            {
                "fieldname": "custom_mpesa_phone_number",
                "label": "M-Pesa Phone Number",
                "fieldtype": "Data",
                "insert_after": "custom_mpesa_column_break",
                "module": MODULE,
            },
            {
                "fieldname": "custom_mpesa_receipt_number",
                "label": "M-Pesa Receipt Number",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "custom_mpesa_phone_number",
                "module": MODULE,
            },
        ]
    }


def create_payment_entry_mpesa_fields():
    """Create the 6 Payment Entry STK fields on every migrate and at install (R5).

    They used to come only from patch payment_entry_custom_fields, which install-app records as
    done without running, so a fresh install never had them. Their Links need Payment Gateway
    (payments app) and Payment Gateway Account; outside a patch Frappe refuses a Link to a
    DocType that does not exist, so the fields wait until both are there.
    """
    needed = ("Payment Entry", "Payment Gateway", "Payment Gateway Account")
    if not all(frappe.db.exists("DocType", doctype) for doctype in needed):
        return
    create_custom_fields(payment_entry_mpesa_fields(), update=True)


def create_fields(custom_fields):
    for doctype, fields in custom_fields.items():
        for field in fields:
            field.setdefault("module", MODULE)

    create_custom_fields(custom_fields)


def create_erpnext_property_setters():
    property_setters = [
        {
            "doc_type": "Payment Request",
            "doctype_or_field": "DocType",
            "property": "field_order",
            "property_type": "Data",
            "value": (
                '["payment_request_type", "transaction_date", "phone_number", "column_break_2", '
                '"naming_series", "company", "mode_of_payment", "party_details", "party_type", '
                '"party", "party_name", "column_break_4", "reference_doctype", "reference_name", '
                '"transaction_details", "grand_total", "currency", "is_a_subscription", '
                '"column_break_18", "outstanding_amount", "party_account_currency", '
                '"subscription_section", "subscription_plans", "bank_account_details", '
                '"bank_account", "bank", "bank_account_no", "account", "column_break_11", "iban", '
                '"branch_code", "swift_number", "accounting_dimensions_section", "cost_center", '
                '"dimension_col_break", "project", "recipient_and_message", "print_format", '
                '"email_to", "subject", "column_break_9", "payment_gateway_account", "status", '
                '"make_sales_invoice", "section_break_10", "message", "message_examples", '
                '"mute_email", "payment_url", "section_break_7", "payment_gateway", '
                '"payment_account", "payment_channel", "payment_order", "amended_from", '
                '"column_break_iiuv", "payment_token"]'
            ),
        },
        {
            "doc_type": "Payment Request",
            "doctype_or_field": "DocField",
            "field_name": "phone_number",
            "property": "mandatory_depends_on",
            "property_type": "Data",
            "value": 'eval: doc.payment_channel == "Phone"',
        },
    ]
    for ps in property_setters:
        try:
            frappe.get_doc(
                {
                    "doctype": "Property Setter",
                    **ps,
                    "module": MODULE,
                }
            ).insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            pass
