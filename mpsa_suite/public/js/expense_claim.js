// M-Pesa on an Expense Claim (HRMS): what M-Pesa paid and has left to pay, and buttons that
// prepare a draft payout to the employee, to their approved phone (B2C) or bank account, paybill or
// till (B2B), for what the claim still owes. An M-Pesa Payer sends it with Send via M-Pesa
// (employee_pay.js; checked again on the server). Merchants and casual payees: their own script.
frappe.ui.form.on("Expense Claim", {
	setup(frm) {
		// Cancel never lists the claim's M-Pesa payouts to cancel with it (Frappe's Cancel All, which could
		// not cancel them: a payout and its disbursement link each other). The server refuses the cancel while
		// one is paid or on its way and lets a failed one go (money_out.employee_documents.before_cancel).
		frm.ignore_doctypes_on_cancel_all = [
			...new Set([
				...(frm.ignore_doctypes_on_cancel_all || []),
				"B2C Payment Disbursement",
				"B2C Disbursement Request",
				"Mpesa B2B Payment Request",
			]),
		];
	},

	refresh(frm) {
		frappe.require("/assets/mpsa_suite/js/employee_pay.js", () => mpsa_employee_pay.refresh(frm));
	},
});
