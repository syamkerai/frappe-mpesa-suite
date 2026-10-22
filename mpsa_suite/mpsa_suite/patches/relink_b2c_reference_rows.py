# Copyright (c) 2026, Astral Industries Limited
# For license information, please see license.txt
"""Restore the request link on B2C disbursement rows that lost it.

An older ``update_disbursement_status`` saved the whole disbursement and wrote stale rows
back, wiping ``b2c_disbursement_request`` that the payout job had just set. A row without
its link loses the amend guard in ``before_submit``, drops out of ``open_payouts`` once its
disbursement is cancelled, and cannot be booked. The request still names its row
(``b2c_payment_disbursement_reference``), so the link is set back from there: the latest
submitted request when several name the same row, else the latest cancelled one (a row of a
disbursement cancelled with its requests: Cancel All). Rows that have a link keep it, so no
row is taken from the request that holds it.
"""

import frappe

from mpsa_suite.handover import ran_under_old_name

REQUEST = "B2C Disbursement Request"
ROW = "B2C Payment Disbursement Reference"


def execute():
    if ran_under_old_name(__name__):
        return  # R8: already ran as frappe_mpsa_payments.*; see handover.py
    if not (frappe.db.table_exists(REQUEST) and frappe.db.table_exists(ROW)):
        return
    if not (
        frappe.db.has_column(REQUEST, "b2c_payment_disbursement_reference")
        and frappe.db.has_column(ROW, "b2c_disbursement_request")
    ):
        return
    latest = {}
    for row, request in frappe.db.sql(
        f"""
        SELECT req.b2c_payment_disbursement_reference, req.name
        FROM `tab{REQUEST}` req
        JOIN `tab{ROW}` ref ON ref.name = req.b2c_payment_disbursement_reference
        WHERE req.docstatus IN (1, 2) AND COALESCE(ref.b2c_disbursement_request, '') = ''
        ORDER BY req.docstatus DESC, req.creation, req.name
        """
    ):
        latest[row] = request  # cancelled first, each oldest first: the latest submitted wins
    for row, request in latest.items():
        frappe.db.set_value(ROW, row, "b2c_disbursement_request", request, update_modified=False)
