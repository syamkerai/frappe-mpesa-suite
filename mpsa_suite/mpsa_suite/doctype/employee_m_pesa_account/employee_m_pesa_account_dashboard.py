def get_data():
    """Employee M-Pesa Account Connections: the B2B payments and payment schedule lines it is paid by."""
    return {
        "fieldname": "employee_mpesa_account",
        "non_standard_fieldnames": {"M-Pesa Payment Schedule": "payee_account"},
        "dynamic_links": {"payee_account": ["Employee M-Pesa Account", "payee_account_doctype"]},
        "transactions": [{"label": "M-Pesa", "items": ["Mpesa B2B Payment Request", "M-Pesa Payment Schedule"]}],
    }
