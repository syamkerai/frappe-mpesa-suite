import frappe


def update_b2c_disbursement_statuses():
    """
    Scheduler job to retry outstanding disbursements (retry_count ≤ 3)
    """
    disbs = frappe.get_all(
        "B2C Payment Disbursement",
        filters={
            "docstatus": 1,
            "retry_count": ["<=", 3],
            "status": ["not in", ["Paid", "Not Initiated"]],
        },
        fields=["name"],
    )
    for d in disbs:
        try:
            doc = frappe.get_doc("B2C Payment Disbursement", d.name)
            result = doc.update_disbursement_status()
            frappe.log(f"[B2C Retry] {d.name}: {result}")
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"[B2C Retry Error] {d.name}")


# A payout whose send timed out, or whose Result never came, gets a Transaction Status
# query (money_out.b2c_status) instead of a resend. One query per payout per hour.
STATUS_CHECK_AFTER_MINUTES = {"Status Unknown": 5, "Initiated": 60}
STATUS_CHECK_EVERY_MINUTES = 60
STATUS_CHECK_FOR_DAYS = 7


def check_unsettled_payouts(limit: int = 50) -> None:
    from frappe.utils import add_to_date, now_datetime

    from mpsa_suite.utils.doctype_names import B2C_REQUEST_DOCTYPE

    try:
        from mpsa_suite.money_out.b2c_status import QUERY_DOCTYPE, start_query
    except ImportError:
        return  # Daraja money-out APIs not installed in this build
    if not frappe.db.exists("DocType", QUERY_DOCTYPE):
        return

    now = now_datetime()
    names = []
    for status, minutes in STATUS_CHECK_AFTER_MINUTES.items():
        names += frappe.get_all(
            B2C_REQUEST_DOCTYPE,
            filters={
                "docstatus": 1,
                "status": status,
                "mpesa_settings": ["is", "set"],
                "result_code": ["is", "not set"],
                "modified": ["<", add_to_date(now, minutes=-minutes)],
                "creation": [">=", add_to_date(now, days=-STATUS_CHECK_FOR_DAYS)],
            },
            pluck="name",
            order_by="modified asc",
            limit=limit,
        )
    recent_cutoff = add_to_date(now, minutes=-STATUS_CHECK_EVERY_MINUTES)
    for name in names[:limit]:
        if frappe.db.exists(QUERY_DOCTYPE, {"b2c_request": name, "creation": [">", recent_cutoff]}):
            continue
        try:
            start_query(name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"B2C status check failed for {name}"[:140])
