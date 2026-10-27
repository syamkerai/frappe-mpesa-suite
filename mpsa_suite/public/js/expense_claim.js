// M-Pesa on an Expense Claim (HRMS): what M-Pesa paid and has left to pay, and buttons that
// prepare a draft payout to the employee, to their approved phone (B2C) or bank account, paybill or
// till (B2B), for what the claim still owes. An M-Pesa Payer sends it with Send via M-Pesa
// (employee_pay.js; checked again on the server). Merchants and casual payees: their own script.
frappe.ui.form.on("Expense Claim", {
	refresh(frm) {
		frappe.require("/assets/mpsa_suite/js/employee_pay.js", () => mpsa_employee_pay.refresh(frm));
	},
});
