// M-Pesa on a Purchase Order: pay an advance by B2B. The invoice made from the order picks it up
// under Get Advances Paid.
frappe.ui.form.on("Purchase Order", {
	refresh(frm) {
		const doc = frm.doc;
		if (
			doc.docstatus === 1 &&
			!flt(doc.per_billed) &&
			!["On Hold", "Closed", "Completed"].includes(doc.status) &&
			(doc.currency || "KES") === "KES" &&
			// who may prepare a payout (boot.py can_prepare: not System Manager), not just create one
			frappe.boot.mpsa?.can_prepare &&
			frappe.model.can_create("Mpesa B2B Payment Request")
		) {
			frm.add_custom_button(
				__("Pay Advance via M-Pesa (B2B)"),
				() =>
					frappe.new_doc("Mpesa B2B Payment Request", {
						purchase_order: doc.name,
						// the order's company, not the user's default one
						company: doc.company,
						remarks: __("Advance on {0}", [doc.name]).slice(0, 100),
					}),
				__("M-Pesa")
			);
		}
		// what M-Pesa has on its way or planned for the order's advance, and a schedule line for it (utils.js)
		if (
			doc.docstatus === 1 &&
			!flt(doc.per_billed) &&
			!["On Hold", "Closed", "Completed"].includes(doc.status) &&
			(doc.currency || "KES") === "KES"
		) {
			if (frappe.model.can_read("Mpesa B2B Payment Request")) mpsa_payables.show_left(frm);
			if (frappe.boot.mpsa?.can_prepare && frappe.model.can_create("M-Pesa Payment Schedule")) {
				mpsa_payables.add_schedule_button(frm);
			}
		}
	},
});
