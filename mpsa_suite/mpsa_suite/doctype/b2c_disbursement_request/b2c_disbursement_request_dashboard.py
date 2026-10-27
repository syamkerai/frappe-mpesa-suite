def get_data():
    """B2C Disbursement Request Connections: its status queries, the payout it is a row of and its
    statement line. Its Payment or Journal Entry references the payout (Accounting Entries there)."""
    return {
        "fieldname": "b2c_request",
        # listed by the request's own fields only ("name": nothing else matches)
        "non_standard_fieldnames": {"B2C Payment Disbursement": "name", "Bank Transaction": "name"},
        "internal_links": {"B2C Payment Disbursement": "b2c_payment_disbursement", "Bank Transaction": "bank_transaction"},
        "transactions": [
            {"label": "M-Pesa", "items": ["Mpesa B2C Status Query", "B2C Payment Disbursement"]},
            {"label": "Accounting", "items": ["Bank Transaction"]},
        ],
    }
