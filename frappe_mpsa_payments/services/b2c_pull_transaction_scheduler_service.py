import frappe
from frappe.utils import add_to_date, now_datetime

from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import pull_transactions

# Daraja keeps 48 hours of Pull history and does not resend failed C2B confirmations.
# After an outage Frappe runs a missed hourly job once, not once per missed hour, so each
# run reads back (nearly) all of it. insert_register skips every receipt that already has
# a register, cancelled or deleted ones included, so the re-reads book nothing twice.
PULL_LOOKBACK_HOURS = 47
# The 47h window spans several pages on a busy shortcode, and Daraja's docs disagree on
# OffSetValue (a page number in its FAQ, a row offset in its parameter table). So the last
# 2 hours, the window this job always read, are also pulled on their own and first: normally
# one page, so the usual within-the-hour recovery does not depend on how Daraja pages.
RECENT_PULL_HOURS = 2


def run_hourly_pull_transactions():
    settings_list = frappe.get_all(
        "Mpesa Settings",
        filters={"enable_hourly_pull_transactions": 1},
        fields=["name"],
    )
    end_date = now_datetime()
    start_dates = [
        add_to_date(end_date, hours=-hours) for hours in (RECENT_PULL_HOURS, PULL_LOOKBACK_HOURS)
    ]

    for row in settings_list:
        for start_date in start_dates:
            try:
                pull_transactions(
                    mpesa_settings=row.name,
                    start_date=start_date.strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=end_date.strftime("%Y-%m-%d %H:%M:%S"),
                    offset=0,
                )
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"[Hourly Pull Transaction Error] {row.name}",
                )
