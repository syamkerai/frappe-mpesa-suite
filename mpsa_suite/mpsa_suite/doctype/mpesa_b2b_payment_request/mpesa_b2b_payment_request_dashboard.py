def get_data():
    """Mpesa B2B Payment Request Connections: its accounting entry, statement line and the payment
    schedule line that sent it."""
    return {
        "fieldname": "mpesa_b2b_payment_request",
        "non_standard_fieldnames": {
            # a salary Journal Entry names the request on its rows; the others only by the request's fields
            "Journal Entry": "mpesa_b2b_payment_request",
            "M-Pesa Payment Schedule": "request",
            "Payment Entry": "name",
            "Bank Transaction": "name",
        },
        "internal_links": {
            "Payment Entry": "payment_entry",
            "Journal Entry": "journal_entry",
            "Bank Transaction": "bank_transaction",
            "M-Pesa Payment Schedule": "payment_schedule",
        },
        "transactions": [
            {"label": "Accounting", "items": ["Payment Entry", "Journal Entry", "Bank Transaction"]},
            {"label": "M-Pesa", "items": ["M-Pesa Payment Schedule"]},
        ],
    }
