from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_payment_entry_mpesa_fields()


def create_payment_entry_mpesa_fields():
    custom_fields = {
        "Payment Entry": [
            {
                "fieldname": "custom_mpesa_section",
                "label": "M-Pesa STK Push",
                "fieldtype": "Section Break",
                "insert_after": "mode_of_payment",
                "depends_on": "eval:doc.payment_type=='Receive' && doc.party_type=='Customer'",
                "module": "Mpsa Suite",
            },
            {
                "fieldname": "custom_payment_gateway_account",
                "label": "Payment Gateway Account",
                "fieldtype": "Link",
                "options": "Payment Gateway Account",
                "insert_after": "custom_mpesa_section",
                "module": "Mpsa Suite",
            },
            {
                "fieldname": "custom_payment_gateway",
                "label": "Payment Gateway",
                "fieldtype": "Link",
                "options": "Payment Gateway",
                "fetch_from": "custom_payment_gateway_account.payment_gateway",
                "read_only": 1,
                "insert_after": "custom_payment_gateway_account",
                "module": "Mpsa Suite",
            },
            {
                "fieldname": "custom_mpesa_column_break",
                "fieldtype": "Column Break",
                "insert_after": "custom_payment_gateway",
                "module": "Mpsa Suite",
            },
            {
                "fieldname": "custom_mpesa_phone_number",
                "label": "M-Pesa Phone Number",
                "fieldtype": "Data",
                "insert_after": "custom_mpesa_column_break",
                "module": "Mpsa Suite",
            },
            {
                "fieldname": "custom_mpesa_receipt_number",
                "label": "M-Pesa Receipt Number",
                "fieldtype": "Data",
                "read_only": 1,
                "insert_after": "custom_mpesa_phone_number",
                "module": "Mpsa Suite",
            },
        ]
    }
    create_custom_fields(custom_fields, update=True)
