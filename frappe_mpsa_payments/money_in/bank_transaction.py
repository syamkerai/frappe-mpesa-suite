"""M-Pesa receipts as Bank Transactions (the statement side of each till/paybill)."""

import frappe
from frappe import _
from frappe.utils import add_days, cstr, flt, nowdate

from .common import (
    EXPRESS,
    REGISTER,
    bank_account_for,
    enabled,
    log_match,
    mpesa_bank_accounts,
    named_lock,
    parse_transtime,
    receipt_date,
    settings_for_shortcode,
    suite_int,
)

BT = "Bank Transaction"


def find_existing(bank_account: str, receipt: str) -> str | None:
    """Live (draft or submitted) statement line for this receipt on this account."""
    if not receipt:
        return None
    for field in ("reference_number", "transaction_id"):
        name = frappe.db.get_value(
            BT,
            {"bank_account": bank_account, field: receipt, "docstatus": ["<", 2]},
            "name",
            order_by="creation asc",
            for_update=True,  # locking read: waits for / sees another worker's insert
        )
        if name:
            return name
    return None


def create_statement_line(
    *,
    bank_account: str,
    receipt: str,
    amount: float,
    date,
    description: str,
    transaction_type: str,
    withdrawal: bool = False,
    party_type: str | None = None,
    party: str | None = None,
) -> str:
    """Create and submit one Bank Transaction per receipt; returns the existing one if present."""
    with named_lock(f"bt:{bank_account}:{receipt}"):
        existing = find_existing(bank_account, receipt)
        if existing:
            return existing

        gl = frappe.db.get_value("Bank Account", bank_account, "account")
        bt = frappe.new_doc(BT)
        bt.date = date
        bt.bank_account = bank_account
        bt.company = frappe.db.get_value("Bank Account", bank_account, "company")
        bt.currency = (gl and frappe.db.get_value("Account", gl, "account_currency")) or "KES"
        bt.deposit = 0 if withdrawal else flt(amount)
        bt.withdrawal = flt(amount) if withdrawal else 0
        bt.reference_number = receipt
        bt.transaction_id = receipt
        bt.description = description[:140]
        if bt.meta.has_field("transaction_type"):
            bt.transaction_type = transaction_type
        if party_type and party:
            bt.party_type = party_type
            bt.party = party
        bt.flags.mpesa_suite = True  # our own insert: skip the duplicate guard below
        bt.flags.ignore_permissions = True
        bt.insert()
        bt.submit()
        return bt.name


def _link(doc, bank_transaction: str) -> None:
    if doc.meta.has_field("bank_transaction") and doc.get("bank_transaction") != bank_transaction:
        doc.db_set("bank_transaction", bank_transaction, update_modified=False)


def for_register(register) -> str | None:
    """Statement line for a C2B confirmation / pulled transaction."""
    if register.docstatus == 2 or not register.transid:
        return None
    bank_account = bank_account_for(settings_for_shortcode(register.businessshortcode))
    if not bank_account:
        log_match(
            "Error",
            register=register,
            once=True,
            details=_("No Bank Account mapped for shortcode {0}. Set it on Mpesa Settings → Accounting Mapping.").format(
                register.businessshortcode
            ),
        )
        return None
    payer = register.full_name or " ".join(filter(None, [register.firstname, register.lastname]))
    name = create_statement_line(
        bank_account=bank_account,
        receipt=register.transid,
        amount=register.transamount,
        date=receipt_date(register),
        description=f"{payer or 'M-Pesa payer'} | Ref: {register.billrefnumber or '-'} | Register: {register.name}",
        transaction_type="Pay Bill",
        party_type="Customer" if register.get("customer") else None,
        party=register.get("customer"),
    )
    _link(register, name)
    return name


def for_express_request(request) -> str | None:
    """Statement line for an STK receipt that never produced a C2B confirmation."""
    if request.docstatus != 1 or request.status != "Completed" or not request.transaction_id:
        return None
    owner = frappe.db.get_value(REGISTER, {"transid": request.transaction_id, "docstatus": ["<", 2]}, "name")
    if owner:
        # the C2B register owns this receipt; point the prompt at the same line so the
        # sweep stops revisiting it
        line = frappe.db.get_value(REGISTER, owner, "bank_transaction") if frappe.get_meta(REGISTER).has_field("bank_transaction") else None
        if line:
            _link(request, line)
        return line
    bank_account = bank_account_for(request.settings)
    if not bank_account:
        return None
    when = parse_transtime(request.transaction_date) if request.transaction_date else None
    name = create_statement_line(
        bank_account=bank_account,
        receipt=request.transaction_id,
        amount=request.amount,
        date=(when.date() if when else nowdate()),
        description=f"STK {request.phone_number or ''} | Ref: {request.reference_name or '-'} | Express: {request.name}",
        transaction_type="STK Push",
    )
    _link(request, name)
    return name


def sweep_missing() -> None:
    """Catch receipts that have no statement line yet (callbacks, pulls, STK, earlier failures)."""
    if not enabled("create_bank_transactions"):
        return
    since = add_days(nowdate(), -suite_int("matching_lookback_days", 45))
    reg_filters = {"docstatus": ["<", 2], "posting_date": [">=", since], "transid": ["is", "set"]}
    if frappe.get_meta(REGISTER).has_field("bank_transaction"):
        reg_filters["bank_transaction"] = ["is", "not set"]
    for name in frappe.get_all(REGISTER, filters=reg_filters, pluck="name", order_by="creation desc", limit=500):
        _safely(for_register, frappe.get_doc(REGISTER, name))

    exp_filters = {"docstatus": 1, "status": "Completed", "transaction_id": ["is", "set"], "creation": [">=", since]}
    if frappe.get_meta(EXPRESS).has_field("bank_transaction"):
        exp_filters["bank_transaction"] = ["is", "not set"]
    for name in frappe.get_all(EXPRESS, filters=exp_filters, pluck="name", order_by="creation desc", limit=500):
        _safely(for_express_request, frappe.get_doc(EXPRESS, name))


def _safely(fn, doc) -> None:
    frappe.db.savepoint("mpesa_bt")
    try:
        fn(doc)
    except Exception:
        frappe.db.rollback(save_point="mpesa_bt")
        frappe.log_error(frappe.get_traceback(), f"M-Pesa statement line failed for {doc.name}")


# ---------------------------------------------------------------- doc events
def guard_duplicate(doc, method=None) -> None:
    """Bank Transaction before_insert: one live line per receipt per M-Pesa account."""
    if doc.flags.get("mpesa_suite") or not enabled("block_duplicate_bank_transactions"):
        return
    if doc.bank_account not in mpesa_bank_accounts():
        return
    for receipt in {cstr(doc.reference_number).strip(), cstr(doc.transaction_id).strip()} - {""}:
        existing = find_existing(doc.bank_account, receipt)
        if existing:
            frappe.throw(
                _("Duplicate M-Pesa receipt {0}: already on {1}.").format(receipt, existing),
                title=_("Duplicate Receipt"),
            )


def sync_voucher_clearance(doc, method=None) -> None:
    """Bank Transaction submit / update: set clearance only when every bank leg is matched.

    ERPNext hides vouchers with a clearance date from the reconciliation tool, so an
    inter-bank transfer cleared by the first bank's line was invisible to the second.
    Only ever *adds* a missing date.
    """
    if not enabled("sync_voucher_clearance") or doc.docstatus != 1:
        return
    for row in doc.get("payment_entries") or []:
        if row.payment_document not in ("Payment Entry", "Journal Entry"):
            continue
        if frappe.db.get_value(row.payment_document, row.payment_entry, "clearance_date"):
            continue
        if _all_bank_legs_allocated(row.payment_document, row.payment_entry):
            frappe.db.set_value(row.payment_document, row.payment_entry, "clearance_date", doc.date)


def _all_bank_legs_allocated(voucher_type: str, voucher_no: str) -> bool:
    legs = frappe.db.sql(
        """
        SELECT gle.account, ABS(SUM(gle.debit - gle.credit)) AS amount
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.voucher_type = %s AND gle.voucher_no = %s AND gle.is_cancelled = 0
          AND acc.account_type = 'Bank'
        GROUP BY gle.account
        """,
        (voucher_type, voucher_no),
        as_dict=True,
    )
    if not legs:
        return False
    allocated = {
        r.account: flt(r.allocated)
        for r in frappe.db.sql(
            """
            SELECT ba.account, SUM(btp.allocated_amount) AS allocated
            FROM `tabBank Transaction Payments` btp
            JOIN `tabBank Transaction` bt ON bt.name = btp.parent
            JOIN `tabBank Account` ba ON ba.name = bt.bank_account
            WHERE btp.payment_document = %s AND btp.payment_entry = %s AND bt.docstatus = 1
            GROUP BY ba.account
            """,
            (voucher_type, voucher_no),
            as_dict=True,
        )
    }
    return all(allocated.get(leg.account, 0) + 0.01 >= flt(leg.amount) for leg in legs)
