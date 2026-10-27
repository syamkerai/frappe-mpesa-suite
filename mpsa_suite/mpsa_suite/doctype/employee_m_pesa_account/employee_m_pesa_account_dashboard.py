def get_data():
    """Employee M-Pesa Account Connections: the B2B payments and payment schedule lines it is paid by.
    Salary data: built as the Employee form's (money_out.connections), so who may not see salaries
    gets no count of them."""
    # absolute: the unit tests load this file on its own, outside its package
    from mpsa_suite.money_out.connections import B2B, EMPLOYEE_ACCOUNT, SCHEDULE, add_group

    return add_group(
        EMPLOYEE_ACCOUNT, {},
        links={B2B: "employee_mpesa_account", SCHEDULE: "payee_account"},
        dynamic={"payee_account": "payee_account_doctype"}, fieldname="employee_mpesa_account",
    )
