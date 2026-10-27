"""Who may do what in the M-Pesa Suite, as data.

The DocType permissions the suite ships, the roles of its reports, the fields only M-Pesa
admins may see and the server-side check behind every whitelisted endpoint. Pure Python (no
Frappe import): the unit tests compare it with the DocType / Report JSON and with the code,
docs/ROLES_AND_PERMISSIONS.md is rendered from it (``render_markdown``), and ``access`` uses
it to tell a site's own permission rules (Custom DocPerm) from the suite's.

A change of who may do what is a change here, in the JSON and in the doc, in one commit.
"""

# One letter per DocPerm right.
RIGHTS = {
    "r": "read",
    "w": "write",
    "c": "create",
    "s": "submit",
    "x": "cancel",
    "a": "amend",
    "d": "delete",
    "R": "report",
    "e": "export",
    "p": "print",
    "m": "email",
    "h": "share",
    "S": "select",  # pick the record in a Link field; no form, list or sidebar entry (read implies it)
}

SM = "System Manager"
MM = "M-Pesa Manager"
MP = "M-Pesa Payer"
MC = "M-Pesa Collector"
MV = "M-Pesa Viewer"
AM = "Accounts Manager"
AU = "Accounts User"
HM = "HR Manager"
HU = "HR User"
SU = "Sales User"
PM = "Purchase Manager"
PU = "Purchase User"

# Column order of the matrix in the doc.
ROLES = (SM, MM, MP, MC, MV, AM, AU, HM, HU, SU, PM, PU)
SUITE_ROLES = (MM, MP, MC, MV)

ALL = "rwcsxadRepmh"
# Prepares money-out documents but does not send them (decision D1): no submit or cancel.
PREPARE = "rwcadRepmh"

# DocType -> {role: rights}. "Role@1" is that role's row at permission level 1.
# Child tables (istable) take their parent's permissions and are not listed.
DOCTYPES = {
    "B2C Disbursement Request": {
        SM: ALL, MM: ALL,
        AM: PREPARE, HM: PREPARE,
        MP: "rwsRp",
        AU: "rwcRepmh", HU: "rwcRepmh",
        MV: "rRep",
    },
    "B2C Payment Disbursement": {
        SM: ALL, MM: ALL,
        AM: PREPARE, HM: PREPARE,
        MP: "rwsRp",
        MV: "rRep",
    },
    # payouts Phase 2: Accounts prepare and move lines; a Payer pays them (maker-checker in schedule.py)
    "M-Pesa Payment Schedule": {
        SM: "rwcdRepmh", MM: "rwcdRepmh", AM: "rwcdRepmh", AU: "rwcdRepmh",
        MP: "rRep", MV: "rRep",
    },
    # payouts Phase 3: HR proposes an employee's payee account; an M-Pesa Manager approves it (submit;
    # never who entered it, party_accounts.approve). Salary data (D4): no Accounts roles, no Viewer, and
    # no Payer either (Phase 3 review: the payer sees the destination on the line it pays).
    "Employee M-Pesa Account": {
        SM: ALL, MM: ALL,
        HM: "rwcaRepmh", HU: "rwcaRep",
    },
    # Round 4: the banks' paybills that payee bank accounts are paid through. Public numbers: read by who
    # enters payee accounts (Accounts, HR, Purchase) and who pays them; changed by CONFIG_ROLES only, a
    # shipped row never deleted (money_out.bank_paybills).
    "M-Pesa Bank Paybill": {
        SM: "rwcdRe", MM: "rwcdRe",
        AM: "rRe", AU: "rRe", HM: "rRe", HU: "rRe", PM: "rRe", MP: "rRe", MV: "rRe",
    },
    "M-Pesa Suite Settings": {SM: "rwcpmh", MM: "rwcpmh"},
    "Mpesa API Client": {SM: "rwcdRepmh", MM: "rwcdRepmh"},
    "Mpesa B2B Payment Request": {
        SM: ALL, MM: ALL, MP: ALL,
        AM: PREPARE,
        AU: "rwcdRep",
        MV: "rRep",
    },
    "Mpesa B2C Status Query": {SM: "rdRep", MM: "rRep", AM: "rRep", MP: "rRep", AU: "rRep", MV: "rRep"},
    "Mpesa Balance Snapshot": {SM: "rwdRep", MM: "rwdRep", AM: "rRep", AU: "rRep", MV: "rRep"},
    "Mpesa C2B Payment Register": {
        SM: "rwdRepmh",
        MM: "rwsxaRepmh", AM: "rwsxaRepmh",
        AU: "rwsRepmh", SU: "rwsRepmh",
        MC: "rRp", MP: "rRp",
        MV: "rRep",
    },
    "Mpesa C2B Payment Register URL": {
        SM: "rwcdRepmh", MM: "rwcdRepmh",
        AM: "rRepmh", AU: "rRepmh", SU: "rRepmh",
        MV: "rRp",
    },
    "Mpesa Express Request": {
        SM: "rwcsdRepmh",
        MM: "rwcsRepmh", AM: "rwcsRepmh", AU: "rwcsRepmh", SU: "rwcsRepmh",
        MC: "rwcsRp",
        MV: "rRep",
    },
    "Mpesa Match Log": {SM: "rwcdRep", MM: "rwcdRep", AM: "rRep", AU: "rRep", MV: "rRep"},
    "Mpesa Payment Intent": {SM: "rwcdRepmh", MM: "rwcdRepmh", AM: "rRep", AU: "rRep", MV: "rRep", MC: "rRep"},
    "Mpesa Public Key Certificate": {SM: "rwcdpmh", MM: "rwc"},
    "Mpesa Reversal Request": {SM: ALL, MM: ALL, MP: ALL, AM: PREPARE, AU: "rRep", MV: "rRep"},
    "Mpesa Sandbox Test Run": {SM: "rwcdRepmh", MM: "rwcdRepmh"},
    # Below M-Pesa Manager: pick a shortcode in a Link field only. Balances: Payments Due's Refresh
    # Balances; transaction status: the C2B register list (both FINANCE_ROLES, no read needed).
    "Mpesa Settings": {
        SM: "rwcdRepmh", MM: "rwcdRepmh",
        AM: "S", AU: "S", MP: "S", MC: "S", MV: "S",
        SM + "@1": "rw", MM + "@1": "rw",
    },
    "Mpesa Statement Import": {SM: "rwcdRep", MM: "rwcdRep", AM: "rwcdRep", AU: "rRep", MV: "rRep"},
    "Mpesa Statement Row": {SM: "rwdRep", MM: "rwdRep", AM: "rRep", AU: "rRep", MV: "rRep"},
    "Mpesa Webhook Delivery": {SM: "rwcdRepmh", MM: "rwcdRepmh"},
    # Shipped by upstream under this app's folder; the payments app owns the same DocType.
    "Payment Gateway": {SM: "rwcd"},
    "Supplier M-Pesa Account": {
        SM: ALL, MM: ALL,
        AM: PREPARE,
        AU: "rwcaRepmh", MP: "rwcaRepmh", PM: "rwcaRepmh",
        MV: "rRep", PU: "rRep",
    },
}

# Credentials: permission level 1, which only SM and MM hold (DOCTYPES "@1" rows).
SECRET_FIELDS = {
    "Mpesa Settings": (
        "consumer_key",
        "consumer_secret",
        "initiator_name",
        "initiator_password",
        "online_passkey",
        "security_credential",
        "access_token",
        "expires_in",
        "token_expiry",
    ),
}

# Salary payouts (decision D4): rows that pay an employee are hidden from everyone outside
# permissions.SALARY_ROLES but their creator (money_out.salary_access, registered in hooks.py).
SALARY_DOCTYPES = (
    "M-Pesa Payment Schedule",
    "B2C Payment Disbursement",
    "B2C Disbursement Request",
    "Mpesa B2B Payment Request",
    "Mpesa B2C Status Query",
)

# DocTypes holding Password fields that only CONFIG roles may read at all.
CONFIG_ONLY = ("M-Pesa Suite Settings", "Mpesa API Client", "Mpesa Webhook Delivery")

# Script Report -> roles (Frappe also needs "report" on its ref_doctype). Frappe refuses a report
# row whose Link column points at a document the reader cannot read, so a suite role is listed only
# where it reads every Link target itself. The four upstream reports show ERPNext documents (Payment
# Request and Entry, invoices, Customer, Mode of Payment): their users reach them through ERPNext roles.
REPORTS = {
    "B2C Disbursement Summary": (SM, AM, HM),
    "C2B Payment Summary Group by Customer": (SM, AU, AM, SU),
    "M-Pesa Payments Due": (SM, AM, AU, MM, MP, MV),
    "C2B Reconciliation Report": (SM, AU, AM, SU),
    "STK Push Request Status": (SM, AU, AM, SU),
}

# Role Profiles the suite creates once (never changed after; see access.ensure_role_profiles).
# On Frappe 15 a user's Role Profile REPLACES all their roles, so each is a complete job: the work
# profiles carry the ERPNext role whose documents that job opens (Accounts User, Sales User).
ROLE_PROFILES = {
    "M-Pesa Finance Clerk": (AU, MC, MV),
    "M-Pesa Paying Officer": (AU, MP, MV),
    "M-Pesa Approver": (AU, MM, MV),
    "M-Pesa Cashier": (SU, MC),
    "M-Pesa Auditor": (MV,),
}

# ---------------------------------------------------------------- endpoints
# Evidence the unit test finds in each function (or its decorators):
#   callback   @safaricom_callback: IP allow-list, URL token, runs as the Integration User
#   api        @endpoint(): Payments API client key + secret, per-client flags
#   page       @endpoint(authenticated=False) and the pay page's 32-character token
#   roles:X    require(X) / require_for_direct_call(..., X); X a permissions.py set or a role
#   perm       frappe.has_permission / doc.check_permission
#   direct     the role gate applies to HTTP calls only (document flows check their own way)
#   via:f      calls helper f, whose own checks are listed in HELPERS
# Keys are "<module>.<function>" or "<module>.<Class>.<method>" (form buttons, run_doc_method).
_A = "mpsa_suite.mpsa_suite.api."
_D = "mpsa_suite.mpsa_suite.doctype."
_SAFARICOM = "Safaricom (guest)"

ENDPOINTS = {
    # Safaricom callbacks
    _A + "b2c.b2c_results_callback": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.account_balance_result": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.balance_query_callback": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.pull_registration_result": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.stk_push_callback": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.confirmation": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.c2b_verification_result": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.validation": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.handle_transaction_status_result": (_SAFARICOM, ("callback",)),
    _A + "m_pesa_api.handle_queue_timeout": (_SAFARICOM, ("callback",)),
    _D + "mpesa_settings.mpesa_settings.process_balance_info": (_SAFARICOM, ("callback",)),
    _D + "mpesa_settings.mpesa_settings.pull_transaction_callback": (_SAFARICOM, ("callback",)),
    "mpsa_suite.money_out.api.reversal_result": (_SAFARICOM, ("callback",)),
    "mpsa_suite.money_out.api.reversal_queue_timeout": (_SAFARICOM, ("callback",)),
    "mpsa_suite.money_out.api.b2b_result": (_SAFARICOM, ("callback",)),
    "mpsa_suite.money_out.api.b2b_queue_timeout": (_SAFARICOM, ("callback",)),
    "mpsa_suite.money_out.api.b2c_status_result": (_SAFARICOM, ("callback",)),
    "mpsa_suite.money_out.api.status_queue_timeout": (_SAFARICOM, ("callback",)),
    "mpsa_suite.sandbox_lab.api.ping": ("Sandbox Lab health check (guest)", ("callback",)),
    # Payments API (external systems) and the public pay page
    "mpsa_suite.gateway.v1.create_prompt": ("API client with can_prompt", ("api",)),
    "mpsa_suite.gateway.v1.create_payment_link": ("API client with can_create_links", ("api",)),
    "mpsa_suite.gateway.v1.get_status": ("API client (own payments)", ("api",)),
    "mpsa_suite.gateway.v1.resend_prompt": ("API client with can_prompt", ("api",)),
    "mpsa_suite.gateway.v1.cancel": ("API client (own payments)", ("api",)),
    "mpsa_suite.gateway.v1.verify_receipt": ("API client with can_verify_receipts", ("api",)),
    "mpsa_suite.gateway.v1.list_payments": ("API client (own payments)", ("api",)),
    "mpsa_suite.gateway.v1.page_prompt": ("Pay page token holder (guest, rate limited)", ("page",)),
    "mpsa_suite.gateway.v1.page_status": ("Pay page token holder (guest, rate limited)", ("page",)),
    # Guest by its old path only (R4): refuses anyone who cannot read Express Requests
    "mpsa_suite.utils.utils.convert_amount_to_kes": ("Read Mpesa Express Request", ("direct", "perm")),
    # Collect: STK prompts, receipts on invoices
    _A + "m_pesa_api.initiate_stk_push": ("COLLECT_ROLES", ("direct", "roles:COLLECT_ROLES")),
    _A + "m_pesa_api.check_transaction_status": ("COLLECT_ROLES + read request", ("direct", "roles:COLLECT_ROLES", "perm")),
    _A + "sales_invoice.initiate_invoice_stk_push": ("COLLECT_ROLES + write invoice", ("roles:COLLECT_ROLES", "perm")),
    _A + "sales_invoice.initiate_row_stk_push": ("COLLECT_ROLES + write invoice", ("roles:COLLECT_ROLES", "via:_check_payment_row_parent")),
    _A + "sales_invoice.validate_stk_push_eligibility": ("Read the invoice", ("perm",)),
    _A + "sales_invoice.get_stk_amount": ("Read the invoice", ("via:_check_payment_row_parent",)),
    _A + "sales_invoice.get_payment_gateway_from_mop": ("Read Payment Request", ("perm",)),
    _A + "sales_invoice.get_mop_from_payment_gateway": ("Read Payment Request", ("perm",)),
    _A + "payment_entry.initiate_payment_entry_stk": ("Submit the Payment Entry", ("perm",)),
    _A + "payment_entry.get_outstanding_invoices": ("Read Sales Invoice", ("perm",)),
    _A + "mpesa_quick_pay.get_mpesa_phone_mops_for_pos_profile": ("Read Mpesa C2B Payment Register", ("perm",)),
    _A + "mpesa_quick_pay.get_mpesa_payments": ("Read Mpesa C2B Payment Register", ("perm",)),
    _A + "mpesa_quick_pay.process_mpesa": ("COLLECT_ROLES + write invoice", ("roles:COLLECT_ROLES", "perm")),
    _A + "mpesa_quick_pay.update_mpesa_after_invoice_submission": ("COLLECT_ROLES + write invoice", ("roles:COLLECT_ROLES", "perm")),
    _D + "mpesa_express_request.mpesa_express_request.create_new_request": ("Create Mpesa Express Request", ("perm",)),
    _D + "mpesa_express_request.mpesa_express_request.get_request_status": ("Read the request", ("perm",)),
    _D + "mpesa_express_request.mpesa_express_request.get_stkpush_defaults": ("Create Mpesa Express Request", ("perm",)),
    _D + "mpesa_express_request.mpesa_express_request.retry_stkpush": ("Write the request", ("perm",)),
    _D + "mpesa_express_request.mpesa_express_request.MpesaExpressRequest.initiate_request": (
        "COLLECT_ROLES + write the request", ("direct", "roles:COLLECT_ROLES", "perm")),
    _D + "mpesa_express_request.mpesa_express_request.MpesaExpressRequest.reconcile_payment": (
        "ADMIN_ROLES + write the request", ("roles:ADMIN_ROLES", "perm")),
    "mpsa_suite.www.mpesa.stkpush.initiate_stk_push": ("Write the request", ("via:_check_can_push",)),
    "mpsa_suite.www.mpesa.stkpush.retry_stkpush": ("Write the request", ("via:_check_can_push",)),
    "mpsa_suite.www.mpesa.stkpush.check_payment_status": ("Read the request", ("perm",)),
    # Finance: balances, status queries, statements, entries
    _A + "m_pesa_api.get_account_balance": ("FINANCE_ROLES", ("direct", "roles:FINANCE_ROLES")),
    _A + "m_pesa_api.pull_transactions": ("FINANCE_ROLES", ("direct", "roles:FINANCE_ROLES")),
    _A + "m_pesa_api.bulk_pull_transactions": ("System Manager", ("roles:System Manager",)),
    _D + "mpesa_settings.mpesa_settings.trigger_transaction_status": ("FINANCE_ROLES", ("direct", "roles:FINANCE_ROLES")),
    _D + "mpesa_settings.mpesa_settings.MpesaSettings.get_account_balance_info": ("FINANCE_ROLES", ("roles:FINANCE_ROLES",)),
    _D + "mpesa_statement_import.mpesa_statement_import.MpesaStatementImport.import_statement": ("Write the import", ("perm",)),
    "mpsa_suite.money_out.b2b_entries.make_entry": ("FINANCE_ROLES + create PE/JE + read request", ("roles:FINANCE_ROLES", "perm")),
    "mpsa_suite.money_out.b2b_entries.refresh_entry_now": ("FINANCE_ROLES + read request", ("roles:FINANCE_ROLES", "perm")),
    "mpsa_suite.money_out.b2b_entries.entry_facts": ("FINANCE_ROLES + read request", ("roles:FINANCE_ROLES", "perm")),
    "mpsa_suite.money_out.b2b.amount_left": ("Read the invoice / order", ("perm",)),
    "mpsa_suite.money_out.b2b.order_advances": ("Read the order", ("perm",)),
    "mpsa_suite.money_out.b2b.salary_left": ("Read the Salary Slip", ("perm",)),
    "mpsa_suite.money_out.payroll.payroll_status": ("Read the Payroll Entry", ("perm",)),
    "mpsa_suite.money_out.payroll.from_payroll_entry": (
        "FINANCE_ROLES + read the Payroll Entry + create a schedule", ("roles:FINANCE_ROLES", "perm")),
    "mpsa_suite.money_out.schedule.from_payroll_entry": (
        "FINANCE_ROLES + read the Payroll Entry + create a schedule", ("via:from_payroll_entry",)),
    "mpsa_suite.utils.utils.get_mode_of_payment_account": ("Write B2C Payment Disbursement", ("perm",)),
    # Money out
    "mpsa_suite.money_out.api.check_b2c_status": ("PAYOUT_ROLES or ADMIN_ROLES + read request", ("roles:PAYOUT_OR_ADMIN", "perm")),
    "mpsa_suite.money_out.api.make_reversal": ("PAYOUT_ROLES or ADMIN_ROLES + read register", ("roles:PAYOUT_OR_ADMIN", "perm")),
    "mpsa_suite.money_out.schedule.prepare_payment": ("PAYOUT_ROLES + read the schedule", ("roles:PAYOUT_ROLES", "perm")),
    "mpsa_suite.money_out.schedule.confirm_payment": ("PAYOUT_ROLES, the user the summary was made for", ("roles:PAYOUT_ROLES",)),
    "mpsa_suite.money_out.schedule.move_date": ("FINANCE_ROLES + write the schedule", ("roles:FINANCE_ROLES", "perm")),
    "mpsa_suite.money_out.schedule.plan_again": ("FINANCE_ROLES + write the schedule", ("roles:FINANCE_ROLES", "perm")),
    "mpsa_suite.money_out.schedule.close_schedule": ("FINANCE_ROLES + write the schedule", ("roles:FINANCE_ROLES", "perm")),
    _D + "b2c_disbursement_request.b2c_disbursement_request.B2CDisbursementRequest.retry_failed_payment": (
        "PAYOUT_ROLES", ("roles:PAYOUT_ROLES",)),
    _D + "b2c_payment_disbursement.b2c_payment_disbursement.B2CPaymentDisbursement.retry_failed_payments": (
        "PAYOUT_ROLES", ("roles:PAYOUT_ROLES",)),
    _D + "b2c_payment_disbursement.b2c_payment_disbursement.B2CPaymentDisbursement.update_disbursement_status": (
        "Submit B2C Payment Disbursement", ("direct", "perm")),
    _D + "b2c_payment_disbursement.b2c_payment_disbursement.B2CPaymentDisbursement.get_outstanding_reference_documents": (
        "Create / write the disbursement", ("via:_check_can_prepare",)),
    _D + "b2c_payment_disbursement.b2c_payment_disbursement.B2CPaymentDisbursement.allocate_amount_to_references": (
        "Create / write the disbursement", ("via:_check_can_prepare",)),
    _D + "supplier_m_pesa_account.supplier_m_pesa_account.accounts_for_supplier": (
        "Read Supplier M-Pesa Account", ("via:accounts_for",)),
    _D + "supplier_m_pesa_account.supplier_m_pesa_account.SupplierMPesaAccount.confirm_receiver_name": (
        "ADMIN_ROLES", ("via:confirm_receiver_name",)),
    _D + "supplier_m_pesa_account.supplier_m_pesa_account.SupplierMPesaAccount.make_default": (
        "FINANCE_ROLES or Purchase Manager + read the account", ("via:make_default",)),
    _D + "employee_m_pesa_account.employee_m_pesa_account.accounts_for_employee": (
        "Read Employee M-Pesa Account", ("via:accounts_for",)),
    _D + "employee_m_pesa_account.employee_m_pesa_account.EmployeeMPesaAccount.confirm_receiver_name": (
        "ADMIN_ROLES", ("via:confirm_receiver_name",)),
    _D + "employee_m_pesa_account.employee_m_pesa_account.EmployeeMPesaAccount.make_default": (
        "ADMIN_ROLES or HR Manager + read the account", ("via:make_default",)),
    # Admin and configuration
    "mpsa_suite.money_in.c2b_verify.release": ("CONFIG_ROLES", ("roles:RELEASE_ROLES",)),
    _D + "mpesa_settings.mpesa_settings.register_pull_transaction": ("ADMIN_ROLES", ("direct", "roles:ADMIN_ROLES")),
    _D + "mpesa_settings.mpesa_settings.bulk_register_pull_transactions": ("System Manager", ("roles:System Manager",)),
    _D + "mpesa_settings.mpesa_settings.get_doctype_fields": ("Write Mpesa Settings", ("perm",)),
    _D + "mpesa_settings.mpesa_settings.MpesaSettings.regenerate_security_credential": ("CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    _D + "m_pesa_suite_settings.m_pesa_suite_settings.MPesaSuiteSettings.regenerate_callback_token": ("CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    _D + "m_pesa_suite_settings.m_pesa_suite_settings.MPesaSuiteSettings.reset_rejection_counter": ("CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    _D + "mpesa_api_client.mpesa_api_client.MpesaAPIClient.generate_new_secret": ("CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    _D + "mpesa_api_client.mpesa_api_client.MpesaAPIClient.rotate_webhook_secret": ("CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    _D + "mpesa_c2b_payment_register_url.mpesa_c2b_payment_register_url.MpesaC2BPaymentRegisterURL.register_again": (
        "CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    _D + "mpesa_webhook_delivery.mpesa_webhook_delivery.MpesaWebhookDelivery.retry_now": ("CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    "mpsa_suite.access.permission_report": ("CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    "mpsa_suite.access.reset_permissions": ("CONFIG_ROLES", ("roles:CONFIG_ROLES",)),
    # Sandbox Lab (a site_config switch, sandbox records only)
    "mpsa_suite.sandbox_lab.api.add_scenarios": ("ADMIN_ROLES + write the run", ("via:_run",)),
    "mpsa_suite.sandbox_lab.api.run_case": ("ADMIN_ROLES + write the run", ("via:_run",)),
    "mpsa_suite.sandbox_lab.api.run_ready": ("ADMIN_ROLES + write the run", ("via:_run",)),
    "mpsa_suite.sandbox_lab.api.reset_case": ("ADMIN_ROLES + write the run", ("via:_run",)),
    "mpsa_suite.sandbox_lab.api.check_now": ("ADMIN_ROLES + write the run", ("via:_run",)),
    "mpsa_suite.sandbox_lab.api.run_health_check": ("ADMIN_ROLES + write the run", ("via:_run",)),
    "mpsa_suite.sandbox_lab.api.simulate_c2b": ("ADMIN_ROLES", ("via:require_lab",)),
    "mpsa_suite.sandbox_lab.api.replay_callback": ("ADMIN_ROLES + read the document", ("via:require_lab", "perm")),
}

# Helpers named by "via:" and the evidence their own body must show.
HELPERS = {
    "_check_payment_row_parent": ("mpsa_suite.mpsa_suite.api.sales_invoice", ("perm",)),
    "_check_can_prepare": (_D + "b2c_payment_disbursement.b2c_payment_disbursement", ("perm",)),
    "_check_can_push": ("mpsa_suite.www.mpesa.stkpush", ("perm",)),
    "_run": ("mpsa_suite.sandbox_lab.api", ("perm", "via:require_lab")),
    "require_lab": ("mpsa_suite.sandbox_lab.guard", ("roles:ADMIN_ROLES",)),
    "confirm_receiver_name": ("mpsa_suite.money_out.party_accounts", ("roles:ADMIN_ROLES",)),
    "accounts_for": ("mpsa_suite.money_out.party_accounts", ("perm",)),
    # the roles are the account DocType's profile.default_roles
    "make_default": ("mpsa_suite.money_out.party_accounts", ("roles:default_roles", "perm")),
    "from_payroll_entry": ("mpsa_suite.money_out.payroll", ("roles:FINANCE_ROLES", "perm")),
}

GUEST_EVIDENCE = ("callback", "api", "page")


def rights_of(letters: str) -> set:
    return {RIGHTS[c] for c in letters}


def rows(doctype: str) -> dict:
    """{(role, permlevel): set of rights} the suite ships for ``doctype``."""
    out = {}
    for key, letters in DOCTYPES[doctype].items():
        role, _, level = key.partition("@")
        out[(role, int(level or 0))] = rights_of(letters)
    return out


def render_markdown(role_sets: dict) -> str:
    """The generated part of docs/ROLES_AND_PERMISSIONS.md. ``role_sets``: permissions.py's
    role tuples by name (the test reads them from the source, so this stays Frappe-free)."""
    short = {SM: "SysMgr", MM: "M-Pesa Mgr", MP: "Payer", MC: "Collector", MV: "Viewer", AM: "Acc Mgr",
             AU: "Acc User", HM: "HR Mgr", HU: "HR User", SU: "Sales User", PM: "Pur Mgr", PU: "Pur User"}
    lines = ["### DocType rights by role", ""]
    lines.append("Letters: " + ", ".join(f"`{k}` {v}" for k, v in RIGHTS.items()) + ". `L1:` is permission level 1 "
                 "(the credential fields). A blank cell is no access.")
    lines.append("")
    lines.append("| DocType | " + " | ".join(short[r] for r in ROLES) + " |")
    lines.append("|---|" + "---|" * len(ROLES))
    for doctype in sorted(DOCTYPES):
        perms = DOCTYPES[doctype]
        cells = []
        for role in ROLES:
            cell = perms.get(role, "")
            if perms.get(role + "@1"):
                cell = (cell + " " if cell else "") + "L1:" + perms[role + "@1"]
            cells.append(f"`{cell}`" if cell else "")
        lines.append(f"| {doctype} | " + " | ".join(cells) + " |")
    lines += ["", "### Role sets of the server checks (mpsa_suite/permissions.py)", ""]
    lines.append("| Set | Roles |")
    lines.append("|---|---|")
    for name in sorted(role_sets):
        lines.append(f"| `{name}` | {', '.join(role_sets[name])} |")
    lines += ["", "### Salary payouts (decision D4)", ""]
    lines.append("Rows that pay an employee (Payroll schedules and employee lines, B2C payouts and B2B requests "
                 "to an employee, their status queries) are shown only to `SALARY_ROLES`, to a role a site gave "
                 "read on Salary Slip (the employee's own role aside), and to whoever created them, whatever the "
                 "DocType rights above: " + ", ".join(SALARY_DOCTYPES) + ". Employee M-Pesa Account is closed to "
                 "other roles by its rights above. M-Pesa Payments Due and B2C Disbursement Summary leave those "
                 "rows out for everyone else. One rule for all of them, in `money_out/salary_access.py` "
                 "(`has_permission`, `query_conditions`, `report_condition`, `visible`), registered once in "
                 "hooks.py. HRMS is a required app since payouts Phase 3, so HR User and HR Manager exist on "
                 "every site.")
    lines += ["", "### Report roles", ""]
    lines.append("| Report | Roles |")
    lines.append("|---|---|")
    for report in sorted(REPORTS):
        lines.append(f"| {report} | {', '.join(REPORTS[report])} |")
    lines += ["", "### Role Profiles the suite creates", ""]
    lines.append("| Role Profile | Roles |")
    lines.append("|---|---|")
    for profile, roles in ROLE_PROFILES.items():
        lines.append(f"| {profile} | {', '.join(roles)} |")
    lines += ["", "### Whitelisted endpoints", ""]
    lines.append("Every endpoint checks on the server. `direct`: the role gate applies to HTTP calls; "
                 "document flows reach the function after their own checks.")
    lines.append("")
    lines.append("| Endpoint | Who | Server check |")
    lines.append("|---|---|---|")
    for path in sorted(ENDPOINTS):
        who, evidence = ENDPOINTS[path]
        lines.append(f"| `{path.removeprefix('mpsa_suite.')}` | {who} | {', '.join(evidence)} |")
    return "\n".join(lines) + "\n"
