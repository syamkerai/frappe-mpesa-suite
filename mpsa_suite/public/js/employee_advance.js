// M-Pesa on an Employee Advance (HRMS): what M-Pesa paid and has left to pay, and buttons that
// prepare a draft payout of the advance to the employee, to their approved phone (B2C) or bank
// account, paybill or till (B2B). An M-Pesa Payer sends it with Send via M-Pesa (employee_pay.js;
// checked again on the server).
frappe.ui.form.on("Employee Advance", {
	refresh(frm) {
		frappe.require("/assets/mpsa_suite/js/employee_pay.js", () => mpsa_employee_pay.refresh(frm));
	},
});
