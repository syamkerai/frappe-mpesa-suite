import frappe


# Batches (and their rows) looked at again this long after they were made
STATUS_SYNC_FOR_DAYS = 30


def update_b2c_disbursement_statuses():
    """Every 15 minutes, for batches of the last STATUS_SYNC_FOR_DAYS: a row whose payout (B2C
    Disbursement Request) finished but was never told (its job lost) takes the payout's result,
    and every batch not Paid gets its status from its rows (b2c_batch_status.sync_status:
    Partially Paid once a row is paid). Each result already does both; this is the safety net, so
    no batch stays Initiated after its payouts finished. Written only when something changed."""
    from frappe.utils import add_days, nowdate

    from mpsa_suite.utils.doctype_names import B2C_REQUEST_DOCTYPE

    from .b2c_batch_status import sync_status
    from .b2c_response_service import update_b2c_reference_status

    since = add_days(nowdate(), -STATUS_SYNC_FOR_DAYS)
    stale = frappe.db.sql(
        f"""SELECT req.name, req.status FROM `tabB2C Payment Disbursement Reference` ref
        JOIN `tab{B2C_REQUEST_DOCTYPE}` req ON req.name = ref.b2c_disbursement_request
        WHERE req.docstatus = 1 AND req.status IN ('Paid', 'Failed') AND req.creation >= %s
          AND COALESCE(ref.payment_status, '') != req.status
        ORDER BY req.creation LIMIT 100""",
        (since,),
        as_dict=True,
    )
    for row in stale:
        try:
            update_b2c_reference_status(row.name, enqueue_next=row.status == "Paid")  # booking is idempotent
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"[B2C row result] {row.name}"[:140])
    for name in frappe.get_all("B2C Payment Disbursement",
                               filters={"docstatus": 1, "status": ["!=", "Paid"], "creation": [">=", since]},
                               pluck="name", order_by="creation asc"):
        try:
            sync_status(name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"[B2C batch status] {name}"[:140])


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
