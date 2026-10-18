import frappe
from frappe import _
from frappe.utils import flt

from ...permissions import COLLECT_ROLES, require


@frappe.whitelist()
def get_mpesa_phone_mops_for_pos_profile(pos_profile, company):
    """Return all Phone-type Modes of Payment from a POS Profile that are successfully registered with Mpesa Urls."""
    if not pos_profile:
        return []

    profile_mops = frappe.get_all(
        "POS Payment Method",
        filters={"parent": pos_profile},
        fields=["mode_of_payment"],
    )
    if not profile_mops:
        return []

    eligible = []
    for row in profile_mops:
        mop_name = row.get("mode_of_payment")
        if not mop_name:
            continue

        mop_type = frappe.db.get_value(
            "Mode of Payment", {"name": mop_name, "enabled": 1}, "type"
        )
        if mop_type != "Phone":
            continue

        account = get_mop_account_for_mop_and_company(mop_name, company)
        if not account:
            continue

        shortcode = get_mpesa_shortcode_for_mop_and_company(mop_name, company)
        if not shortcode:
            continue

        eligible.append(
            {
                "mop": mop_name,
                "shortcode": shortcode,
                "account": account,
            }
        )

    return eligible


@frappe.whitelist()
def get_mpesa_payments():
    """
    Return pending (draft) Mpesa C2B payments scoped to the correct shortcode(s)
    POS page – filter by the single MOP selected by the user
    Invoice Desk – filter by all MOPs linked to the profile
    """
    frappe.has_permission("Mpesa C2B Payment Register", "read", throw=True)
    company = frappe.form_dict.get("company")
    search = (frappe.form_dict.get("search") or "").strip()
    mode_of_payment = frappe.form_dict.get("mode_of_payment")
    pos_profile = frappe.form_dict.get("pos_profile")

    empty = {"count": 0, "payments": [], "shortcodes": []}

    if not company:
        return empty

    if mode_of_payment:
        # POS page: single MOP selected by the user
        shortcode = get_mpesa_shortcode_for_mop_and_company(mode_of_payment, company)
        if not shortcode:
            return empty
        shortcodes = [shortcode]

    elif pos_profile:
        # Invoice desk: collect all eligible shortcodes from the profile
        mop_configs = get_mpesa_phone_mops_for_pos_profile(pos_profile, company)
        if not mop_configs:
            return empty
        # Deduplicate – two MOPs could share a shortcode (unlikely but safe)
        shortcodes = list({cfg["shortcode"] for cfg in mop_configs})

    else:
        return empty

    if len(shortcodes) == 1:
        base_filters = {"docstatus": 0, "businessshortcode": shortcodes[0]}
    else:
        base_filters = {"docstatus": 0, "businessshortcode": ["in", shortcodes]}

    total_count = frappe.db.count("Mpesa C2B Payment Register", base_filters)

    payments = []
    if len(search) >= 3:
        rows = frappe.get_all(
            "Mpesa C2B Payment Register",
            filters=base_filters,
            fields=[
                "name",
                "full_name",
                "transamount",
                "transid",
                "msisdn",
                "posting_date",
                "billrefnumber",
                "businessshortcode",
                "creation",
            ],
            order_by="creation desc",
            limit_page_length=200,
        )
        sl = search.lower()
        for p in rows:
            full_name = (p.get("full_name") or "").lower()
            transid = (p.get("transid") or "").lower()
            billref = (p.get("billrefnumber") or "").lower()

            if sl in full_name or sl in transid or sl in billref:
                payments.append(p)

    return {"count": total_count, "payments": payments, "shortcodes": shortcodes}


@frappe.whitelist()
def process_mpesa():
    """
    Link selected Mpesa C2B entries to a draft invoice and add payment rows.
    If merge_payments=1, multiple payments on the same Mode of Payment are combined into one row.
    """
    doctype = resolve_doctype(frappe.form_dict.get("doctype"))
    invoice_name = frappe.form_dict.get("invoice_name")
    customer = frappe.form_dict.get("customer")
    names_str = frappe.form_dict.get("mpesa_payments") or ""
    auto_save = int(frappe.form_dict.get("auto_save") or 0)
    auto_submit = int(frappe.form_dict.get("auto_submit") or 0)
    merge_payments = int(frappe.form_dict.get("merge_payments") or 0)

    mpesa_names = [n.strip() for n in names_str.split(",") if n.strip()]
    if not mpesa_names:
        frappe.throw(_("No Mpesa payments selected"))

    require(COLLECT_ROLES)
    invoice = frappe.get_doc(resolve_doctype(doctype), invoice_name)
    invoice.check_permission("write")
    if invoice.docstatus != 0:
        frappe.throw(_("Cannot add payments to a submitted or cancelled invoice"))

    company = invoice.company
    doctype_field = "sales_invoice" if doctype == "Sales Invoice" else "pos_invoice"

    mop_cache = {}

    def _get_mop_meta(mop_name):
        """Return cached { account, shortcode } for a MOP, resolving on first access."""
        if mop_name not in mop_cache:
            mop_cache[mop_name] = {
                "shortcode": get_mpesa_shortcode_for_mop_and_company(mop_name, company),
                "account": get_mop_account_for_mop_and_company(mop_name, company),
            }
        return mop_cache[mop_name]

    payments_added = []
    mpesa_results = []

    for mpesa_name in mpesa_names:
        mpesa = frappe.get_doc("Mpesa C2B Payment Register", mpesa_name)

        if mpesa.docstatus != 0:
            continue

        meta = _get_mop_meta(mpesa.mode_of_payment)

        if str(mpesa.businessshortcode or "") != meta["shortcode"]:
            frappe.log_error(
                f"Mpesa C2B entry {mpesa_name}: invalid shortcode {mpesa.businessshortcode} (expected {meta['shortcode']})",
                "Mpesa C2B",
            )
            continue

        mpesa_amt = flt(mpesa.transamount or 0)
        if mpesa_amt <= 0:
            continue

        # Tag and submit the register entry
        mpesa.customer = customer
        mpesa.submit_payment = 0
        mpesa.save(ignore_permissions=True)
        mpesa.submit()

        frappe.db.set_value(
            "Mpesa C2B Payment Register", mpesa_name, doctype_field, invoice.name
        )

        payments_added.append(
            {
                "mode_of_payment": mpesa.mode_of_payment,
                "amount": mpesa_amt,
                "reference": mpesa_name,
                "account": meta["account"],
            }
        )
        mpesa_results.append({"name": mpesa.name, "amount": mpesa_amt})

    if not payments_added:
        frappe.throw(_("No valid Mpesa payments were processed"))

    if merge_payments:
        # Group payments by Mode of Payment and merge multiple entries on the same MOP
        groups = {}
        for p in payments_added:
            g = groups.setdefault(
                p["mode_of_payment"],
                {"account": p["account"], "total": 0, "refs": []},
            )
            g["total"] += p["amount"]
            g["refs"].append(p["reference"])

        for mop_name, g in groups.items():
            if len(g["refs"]) > 1:
                # Multiple payments for this MOP: combine amounts, store refs in custom_reference_text
                invoice.append(
                    "payments",
                    {
                        "mode_of_payment": mop_name,
                        "amount": g["total"],
                        "account": g["account"],
                        "type": "Phone",
                        "custom_reference_text": "\n".join(g["refs"]),
                    },
                )
            else:
                # Single payment for this MOP: add as individual row
                invoice.append(
                    "payments",
                    {
                        "mode_of_payment": mop_name,
                        "amount": g["total"],
                        "account": g["account"],
                        "type": "Phone",
                        "reference_no": g["refs"][0],
                    },
                )
    else:
        for p in payments_added:
            invoice.append(
                "payments",
                {
                    "mode_of_payment": p["mode_of_payment"],
                    "amount": p["amount"],
                    "account": p["account"],
                    "type": "Phone",
                    "reference_no": p["reference"],
                },
            )

    result = {
        "success": True,
        "doctype": doctype,
        "payments_added": payments_added,
        "mpesa_payments": mpesa_results,
        "total_amount": sum(p["amount"] for p in payments_added),
        "merged": bool(merge_payments),
    }

    if auto_save:
        try:
            invoice.save(ignore_permissions=True)
            result["saved"] = True
            if auto_submit and invoice.docstatus == 0:
                if flt(invoice.outstanding_amount or 0) <= 0:
                    invoice.submit()
                    result["submitted"] = True
        except Exception as e:
            frappe.log_error(
                frappe.get_traceback(), "Mpesa C2B – Invoice Save/Submit Error"
            )
            result["error"] = str(e)

    return result


@frappe.whitelist()
def update_mpesa_after_invoice_submission():
    """
    Link Mpesa entries to an already-submitted invoice.
    Tags register entries with invoice name and submits them; does not modify payment rows.
    """
    doctype = resolve_doctype(frappe.form_dict.get("doctype"))
    invoice_name = frappe.form_dict.get("invoice_name")
    customer = frappe.form_dict.get("customer")
    names_str = frappe.form_dict.get("mpesa_payments") or ""

    mpesa_names = [n.strip() for n in names_str.split(",") if n.strip()]
    if not mpesa_names:
        frappe.throw(_("No Mpesa payments provided"))

    require(COLLECT_ROLES)
    invoice = frappe.get_doc(doctype, invoice_name)
    invoice.check_permission("write")
    if invoice.docstatus != 1:
        frappe.throw(_("Invoice must be submitted first"))

    doctype_field = "sales_invoice" if doctype == "Sales Invoice" else "pos_invoice"

    total_amount = 0
    submitted_count = 0

    try:
        for mpesa_name in mpesa_names:
            mpesa = frappe.get_doc("Mpesa C2B Payment Register", mpesa_name)

            if mpesa.docstatus != 0:
                continue

            mpesa.customer = customer
            mpesa.submit_payment = 0
            mpesa.save(ignore_permissions=True)
            mpesa.submit()

            frappe.db.set_value(
                "Mpesa C2B Payment Register",
                mpesa.name,
                doctype_field,
                invoice.name,
            )

            total_amount += flt(mpesa.transamount or 0)
            submitted_count += 1

    except Exception as e:
        frappe.throw(_("Error updating Mpesa payments: {0}").format(str(e)))

    if submitted_count != len(mpesa_names):
        frappe.log_error(
            f"Processed {submitted_count} out of {len(mpesa_names)} Mpesa payments for {doctype} {invoice_name}.",
            "Mpesa C2B",
        )

    return {"success": True, "total_amount": total_amount}


def get_mpesa_shortcode_for_mop_and_company(mop, company):
    """Return the business_shortcode for a specific MOP + company pair"""
    shortcode = frappe.db.get_value(
        "Mpesa C2B Payment Register URL",
        {
            "company": company,
            "mode_of_payment": mop,
            "register_status": "Success",
        },
        "business_shortcode",
    )
    return str(shortcode) if shortcode else None


def get_mop_account_for_mop_and_company(mop, company):
    """Return the default account for a given Mode of Payment and company."""
    mop_account = frappe.db.get_value(
        "Mode of Payment Account",
        {"parent": mop, "company": company},
        "default_account",
    )
    return mop_account if mop_account else None


def resolve_doctype(doctype):
    """Validate doctype is one we support; throw otherwise."""
    supported = ("POS Invoice", "Sales Invoice")
    if doctype not in supported:
        frappe.throw(
            _("Unsupported doctype '{0}'. Must be one of: {1}").format(
                doctype, ", ".join(supported)
            )
        )
    return doctype
