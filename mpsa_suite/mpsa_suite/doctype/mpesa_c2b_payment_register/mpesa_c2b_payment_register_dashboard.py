def get_data():
    """Mpesa C2B Payment Register Connections: its reversals, how it was matched, and what it booked."""
    return {
        "fieldname": "c2b_register",
        "non_standard_fieldnames": {"Mpesa Match Log": "register", "Payment Entry": "name", "Bank Transaction": "name"},
        "internal_links": {"Payment Entry": "payment_entry", "Bank Transaction": "bank_transaction"},
        "transactions": [
            {"label": "M-Pesa", "items": ["Mpesa Reversal Request", "Mpesa Match Log"]},
            {"label": "Accounting", "items": ["Payment Entry", "Bank Transaction"]},
        ],
    }
