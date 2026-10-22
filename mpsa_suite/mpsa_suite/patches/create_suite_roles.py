from mpsa_suite.handover import ran_under_old_name
from mpsa_suite.install import ensure_roles


def execute():
    if ran_under_old_name(__name__):
        return  # R8: already ran as frappe_mpsa_payments.*; see handover.py
    # Runs before DocType sync so new DocTypes can grant permissions to these roles.
    ensure_roles()
