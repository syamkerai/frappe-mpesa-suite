from frappe_mpsa_payments.install import ensure_roles


def execute():
    # Runs before DocType sync so new DocTypes can grant permissions to these roles.
    ensure_roles()
