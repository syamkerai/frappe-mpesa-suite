def get_data():
    """M-Pesa Payment Schedule Connections: the B2B requests and B2C payouts it sent (the DocType's
    own links), and the B2C requests its phone lines became."""
    return {
        "fieldname": "payment_schedule",
        # listed by the lines' b2c_request only ("name": nothing else matches)
        "non_standard_fieldnames": {"B2C Disbursement Request": "name"},
        "internal_links": {"B2C Disbursement Request": ["lines", "b2c_request"]},
        "transactions": [
            {"label": "M-Pesa", "items": ["Mpesa B2B Payment Request", "B2C Payment Disbursement", "B2C Disbursement Request"]}
        ],
    }
