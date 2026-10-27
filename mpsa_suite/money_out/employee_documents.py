"""M-Pesa payouts of Expense Claims and Employee Advances (WP3)."""


def before_cancel(doc, method=None):
    """Expense Claim and Employee Advance before_cancel (hooks.py doc_events): refuses the cancel
    while an M-Pesa payout of the document is paid or on its way. Nothing to check yet."""
