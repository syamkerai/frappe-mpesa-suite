from mpsa_suite.handover import ran_under_old_name

from ..migrate import create_payment_entry_mpesa_fields


def execute():
    if ran_under_old_name(__name__):
        return  # R8: already ran as frappe_mpsa_payments.*; see handover.py
    # the fields now live in migrate.py and are created on every migrate and at install (R5)
    create_payment_entry_mpesa_fields()
