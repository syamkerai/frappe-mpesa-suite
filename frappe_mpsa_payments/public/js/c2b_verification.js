// Held C2B confirmations (money_in.c2b_verify): let an M-Pesa Manager release one that
// Safaricom could not confirm, after checking it on the M-Pesa statement.
frappe.ui.form.on("Integration Request", {
	refresh(frm) {
		if (
			frm.doc.integration_request_service !== "Mpesa C2B Verification" ||
			frm.doc.status === "Completed" ||
			!(frappe.user.has_role("M-Pesa Manager") || frappe.user.has_role("System Manager"))
		) {
			return;
		}
		frm.add_custom_button(__("Release Without Safaricom Confirmation"), () => {
			frappe.prompt(
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					reqd: 1,
					label: __("Why is this receipt genuine?"),
					description: __("E.g. seen on the M-Pesa statement for this paybill."),
				},
				({ reason }) =>
					frappe
						.call("frappe_mpsa_payments.money_in.c2b_verify.release", {
							integration_request: frm.doc.name,
							reason,
						})
						.then((r) => {
							frm.reload_doc();
							if (r.message) frappe.set_route("Form", "Mpesa C2B Payment Register", r.message);
						}),
				__("Release Held C2B Confirmation"),
				__("Release")
			);
		});
	},
});
