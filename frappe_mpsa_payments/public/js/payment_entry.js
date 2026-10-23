frappe.ui.form.on("Payment Entry", {
	refresh(frm) {
		if (
			frm.doc.docstatus === 0 &&
			!frm.is_new() &&
			frm.doc.payment_type === "Receive" &&
			frm.doc.party_type === "Customer" &&
			frm.doc.custom_payment_gateway_account
		) {
			frm.add_custom_button(__("Initiate M-Pesa STK Push"), async () => {
				if (frm.is_dirty()) {
					await frm.save();
				}
				frappe.call({
					method: "frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry.initiate_payment_entry_stk",
					args: { payment_entry: frm.doc.name },
					freeze: true,
					freeze_message: __("Sending STK Push..."),
					callback: (r) => {
						if (!r.exc && r.message && r.message.route) {
							// Redirect to the STK status page. It handles polling,
							// retries, and returns once the Payment Entry is submitted.
							window.location.href = r.message.route;
						}
					},
				});
			});
		}
	},
});
