import re

import frappe
from frappe import _
from frappe.utils import flt

from ...permissions import COLLECT_ROLES, require


@frappe.whitelist()
def initiate_invoice_stk_push(
    invoice: str = None,
    phone_number: str = None,
    amount: float = None,
    currency: str = "KES",
    mode_of_payment: str = None,
    company: str = None,
    type: str = "Sales Invoice",
) -> dict:
    require(COLLECT_ROLES)
    if invoice and type:
        frappe.has_permission(type, "write", invoice, throw=True)
    try:
        if not (
            invoice and phone_number and amount and mode_of_payment and company and type
        ):
            frappe.throw(
                _(
                    "All fields (invoice, phone_number, amount, mode_of_payment, company, type) are required."
                )
            )

        if not isinstance(amount, (int, float)) or float(amount) <= 0:
            frappe.throw(_("Amount must be greater than zero."))

        payment_gateway = get_payment_gateway_from_mop(mode_of_payment, company)

        express_request = frappe.get_doc(
            {
                "doctype": "Mpesa Express Request",
                "reference_name": invoice,
                "reference_doctype": type,
                "phone_number": phone_number,
                "base_amount": float(amount),
                "currency": currency,
                "payment_gateway": payment_gateway,
                "settings": payment_gateway[6:],
            }
        )
        express_request.insert(ignore_permissions=True)
        express_request.submit()

        return {
            "status": "success",
            "message": f"STK Push for {invoice} initiated to {phone_number} via {payment_gateway}.",
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "initiate_invoice_stk_push Error")
        return {
            "status": "error",
            "message": _(
                "Failed to initiate STK Push. Please check the logs for more details."
            ),
        }


@frappe.whitelist()
def validate_stk_push_eligibility(docname, doctype):
    """Validate if STK push can be initiated for this document"""
    frappe.has_permission(doctype, "read", docname, throw=True)
    try:
        doc = frappe.get_doc(doctype, docname)

        if doc.docstatus != 0 and not doc.is_pos:
            return {
                "eligible": False,
                "message": _("Only allowed for POS invoices or drafts"),
            }

        if doc.outstanding_amount <= 0:
            return {"eligible": False, "message": _("No outstanding amount")}

        # Check currency compatibility
        company_currency = frappe.get_value("Company", doc.company, "default_currency")
        party_account_currency = frappe.get_value(
            "Account", doc.debit_to, "account_currency"
        )

        if not (
            doc.currency == "KES"
            or party_account_currency == "KES"
            or company_currency == "KES"
        ):
            return {"eligible": False, "message": _("Only KES payments supported")}

        # Check for Mpesa payment modes
        mpesa_modes = frappe.get_all(
            "Mode of Payment", filters={"name": ["like", "Mpesa%"]}
        )
        if not mpesa_modes:
            return {
                "eligible": False,
                "message": _("No Mpesa payment modes configured"),
            }

        # Calculate amount in KES
        amount = doc.outstanding_amount
        if party_account_currency != "KES":
            if not doc.conversion_rate:
                return {
                    "eligible": False,
                    "message": _("Conversion rate not available"),
                }
            amount = amount * doc.conversion_rate

        return {"eligible": True, "amount": amount}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "validate_stk_push_eligibility Error")
        return {
            "eligible": False,
            "message": _(
                "Error validating STK Push eligibility. Please check the logs."
            ),
        }


@frappe.whitelist()
def initiate_row_stk_push(
    name: str = None,
    phone_number: str = None,
    amount: float = None,
    currency: str = "KES",
    mode_of_payment: str = None,
    company: str = None,
) -> dict:
    require(COLLECT_ROLES)
    if name:
        _check_payment_row_parent(name, "write")
    try:
        if not (name and phone_number and amount and mode_of_payment and company):
            frappe.throw(
                _(
                    "All fields (name, phone_number, amount, mode_of_payment, company) are required."
                )
            )

        if not isinstance(amount, (int, float)) or float(amount) <= 0:
            frappe.throw(_("Amount must be greater than zero."))

        payment = frappe.get_doc("Sales Invoice Payment", name)
        if payment.type != "Phone" or payment.reference_no:
            frappe.throw(
                _("STK Push can only be initiated for Phone payments without reference")
            )

        payment_gateway = get_payment_gateway_from_mop(mode_of_payment, company)

        express_request = frappe.get_doc(
            {
                "doctype": "Mpesa Express Request",
                "reference_name": name,
                "reference_doctype": "Sales Invoice Payment",
                "phone_number": phone_number,
                "base_amount": float(amount),
                "currency": currency,
                "payment_gateway": payment_gateway,
                "settings": payment_gateway[6:],
            }
        )
        express_request.insert(ignore_permissions=True)
        express_request.submit()

        return {
            "status": "success",
            "message": f"STK Push for payment {name} initiated to {phone_number} via {payment_gateway}.",
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "initiate_row_stk_push Error")
        return {
            "status": "error",
            "message": _(
                "Failed to initiate STK Push for payment. Please check the logs for more details."
            ),
        }


def _check_payment_row_parent(payment_name: str, ptype: str) -> None:
    """Permission on a Sales Invoice Payment row is the permission on its invoice."""
    parent = frappe.db.get_value(
        "Sales Invoice Payment", payment_name, ["parent", "parenttype"], as_dict=True
    )
    if not parent:
        frappe.throw(_("Sales Invoice Payment {0} not found").format(payment_name))
    frappe.has_permission(parent.parenttype or "Sales Invoice", ptype, parent.parent, throw=True)


@frappe.whitelist()
def get_payment_gateway_from_mop(mode_of_payment: str, company: str) -> str:
    payment_gateway = None
    try:
        if not frappe.db.exists("Mode of Payment", mode_of_payment):
            return None
        mop_doc = frappe.get_doc("Mode of Payment", mode_of_payment)
        account_entry = next(
            (acc for acc in mop_doc.accounts if acc.company == company), None
        )
        if account_entry:
            payment_account = account_entry.default_account
            if frappe.db.exists(
                "Payment Gateway Account", {"payment_account": payment_account}
            ):
                try:
                    pg_account = frappe.get_doc(
                        "Payment Gateway Account", {"payment_account": payment_account}
                    )
                    if pg_account and pg_account.payment_gateway:
                        payment_gateway = pg_account.payment_gateway
                except Exception:
                    pass
            else:
                default_pg_account = frappe.get_value(
                    "Payment Gateway Account", {"is_default": 1}, "payment_gateway"
                )
                if default_pg_account:
                    payment_gateway = default_pg_account
    except Exception:
        pass

    return payment_gateway


@frappe.whitelist()
def get_mop_from_payment_gateway(payment_gateway: str, company: str) -> str:
    """Get mode of payment associated with the given payment gateway"""
    mode_of_payment = None
    try:
        if not payment_gateway or not frappe.db.exists(
            "Payment Gateway", payment_gateway
        ):
            return None

        pg_accounts = frappe.get_all(
            "Payment Gateway Account",
            filters={"payment_gateway": payment_gateway},
            fields=["payment_account"],
        )

        if not pg_accounts:
            return None

        for pg_account in pg_accounts:
            payment_account = pg_account.payment_account

            mop_accounts = frappe.get_all(
                "Mode of Payment Account",
                filters={"default_account": payment_account, "company": company},
                fields=["parent"],
            )

            if mop_accounts:
                mode_of_payment = mop_accounts[0].parent
                break

    except Exception:
        frappe.log_error(frappe.get_traceback(), "get_mop_from_payment_gateway Error")
        pass

    return mode_of_payment


@frappe.whitelist()
def get_stk_amount(payment_name: str, company: str) -> float | None:
    """
    Determine valid amount for STK push based on currency rules.
    Returns amount or None if not allowed.
    """
    _check_payment_row_parent(payment_name, "read")
    payment = frappe.get_doc("Sales Invoice Payment", payment_name)
    invoice = frappe.get_doc("Sales Invoice", payment.parent)
    company_currency = frappe.get_cached_value("Company", company, "default_currency")

    if invoice.currency == "KES":
        return payment.amount
    elif company_currency == "KES":
        return payment.base_amount
    else:
        return None


def _normalize_kenyan_phone(phone_number: str) -> str:
    """Normalize local/international Kenyan numbers to 254XXXXXXXXX."""
    digits = re.sub(r"\D", "", phone_number or "")
    if digits.startswith("254") and len(digits) >= 12:
        return digits[:12]
    if digits.startswith("0") and len(digits) >= 10:
        return "254" + digits[1:10]
    if len(digits) >= 9:
        return "254" + digits[-9:]
    return digits


def _to_bool(value) -> bool:
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


@frappe.whitelist()
def initiate_klik_pos_stk_push(
    phone_number: str = None,
    amount: float = None,
    mode_of_payment: str = None,
    company: str = None,
    currency: str = "KES",
    account_reference: str = None,
    reference_doctype: str = None,
    reference_name: str = None,
    prevent_duplicates: int | bool = 1,
) -> dict:
    """Create and submit Mpesa Express Request for Klik POS without rewriting core Mpesa logic."""
    require(COLLECT_ROLES)
    if reference_doctype and reference_name:
        frappe.has_permission(reference_doctype, "write", reference_name, throw=True)
    if not (phone_number and amount and mode_of_payment and company):
        frappe.throw(
            _(
                "All fields (phone_number, amount, mode_of_payment, company) are required."
            )
        )

    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero."))

    payment_gateway = get_payment_gateway_from_mop(mode_of_payment, company)
    if not payment_gateway:
        frappe.throw(
            _(
                "No payment gateway mapping found for Mode of Payment {0} and company {1}."
            ).format(mode_of_payment, company)
        )

    normalized_phone = _normalize_kenyan_phone(phone_number)
    prevent_dup = _to_bool(prevent_duplicates)

    if prevent_dup and account_reference:
        existing = frappe.get_all(
            "Mpesa Express Request",
            filters={
                "status": "In Progress",
                "account_reference": account_reference,
                "phone_number": normalized_phone,
                "payment_gateway": payment_gateway,
            },
            fields=[
                "name",
                "status",
                "checkout_request_id",
                "amount",
                "transaction_id",
            ],
            order_by="creation desc",
            limit=1,
        )
        if existing:
            doc = existing[0]
            return {
                "status": "success",
                "duplicate_prevented": True,
                "request_name": doc.get("name"),
                "request_status": doc.get("status") or "In Progress",
                "checkout_request_id": doc.get("checkout_request_id"),
                "transaction_id": doc.get("transaction_id"),
                "amount": flt(doc.get("amount") or amount),
            }

    express_request = frappe.get_doc(
        {
            "doctype": "Mpesa Express Request",
            "reference_name": reference_name,
            "reference_doctype": reference_doctype,
            "phone_number": normalized_phone,
            "base_amount": amount,
            "currency": currency,
            "payment_gateway": payment_gateway,
            "settings": payment_gateway[6:],
            "account_reference": account_reference,
            "status": "In Progress",
        }
    )
    express_request.insert(ignore_permissions=True)
    express_request.submit()

    return {
        "status": "success",
        "duplicate_prevented": False,
        "request_name": express_request.name,
        "request_status": express_request.status,
        "checkout_request_id": express_request.checkout_request_id,
        "transaction_id": express_request.transaction_id,
        "amount": flt(express_request.amount or amount),
        "phone_number": express_request.phone_number,
    }


@frappe.whitelist()
def get_klik_pos_stk_status(request_name: str) -> dict:
    """Fetch current STK request status for polling fallback in Klik POS."""
    if not request_name:
        frappe.throw(_("request_name is required."))

    doc = frappe.get_doc("Mpesa Express Request", request_name)
    doc.check_permission("read")
    invoice = None
    if doc.reference_doctype and doc.reference_name:
        invoice = {
            "reference_doctype": doc.reference_doctype,
            "reference_name": doc.reference_name,
        }

    return {
        "request_name": doc.name,
        "status": doc.status,
        "result_code": doc.result_code,
        "result_desc": doc.result_desc,
        "transaction_id": doc.transaction_id,
        "checkout_request_id": doc.checkout_request_id,
        "amount": flt(doc.amount or 0),
        "phone_number": doc.phone_number,
        "is_reconciled": doc.is_reconciled,
        "reference": invoice,
    }


@frappe.whitelist()
def link_klik_pos_request_to_invoice(
    request_name: str,
    invoice_name: str,
    invoice_doctype: str = "Sales Invoice",
) -> dict:
    """Attach an already-initiated STK request to an invoice once Klik POS submits it."""
    if not request_name or not invoice_name:
        frappe.throw(_("request_name and invoice_name are required."))

    require(COLLECT_ROLES)
    request_doc = frappe.get_doc("Mpesa Express Request", request_name)
    request_doc.check_permission("write")
    frappe.has_permission(invoice_doctype, "write", invoice_name, throw=True)
    request_doc.db_set("reference_doctype", invoice_doctype)
    request_doc.db_set("reference_name", invoice_name)

    return {
        "success": True,
        "request_name": request_doc.name,
        "reference_doctype": invoice_doctype,
        "reference_name": invoice_name,
        "status": request_doc.status,
        "transaction_id": request_doc.transaction_id,
    }
