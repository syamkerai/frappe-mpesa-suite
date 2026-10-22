// "Check Status with M-Pesa" on B2C Disbursement Request: resolves a payout whose
// result never came back before anyone retries it (money_out.api.check_b2c_status).
frappe.ui.form.on("B2C Disbursement Request", {
	refresh(frm) {
		const unsettled = ["Not Initiated", "Initiated", "Status Unknown", "", null, undefined].includes(frm.doc.status);
		if (frm.doc.docstatus !== 1 || !frm.doc.mpesa_settings || !unsettled) return;
		frm.add_custom_button(
			__("Check Status with M-Pesa"),
			() =>
				frappe.call({
					method: "mpsa_suite.money_out.api.check_b2c_status",
					args: { b2c_request: frm.doc.name },
					freeze: true,
					callback: (r) => {
						if (r.exc) return;
						frappe.show_alert({
							message: __("Status query {0} sent. The payout updates when Safaricom answers.", [r.message]),
							indicator: "blue",
						});
					},
				}),
			__("Actions")
		);
	},
});
