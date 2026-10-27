// M-Pesa on a Sales Order: ask the customer's phone for what the order still owes (a Payment
// Request whose submit sends the prompt, money_in.collect; utils.js mpsa_collect).
frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		const doc = frm.doc;
		if (
			doc.docstatus === 1 &&
			!["Closed", "On Hold", "Completed"].includes(doc.status) &&
			flt(doc.per_billed) < 100 &&
			(doc.currency || "KES") === "KES" &&
			flt(doc.rounded_total || doc.grand_total) - flt(doc.advance_paid) > 0 &&
			mpsa_collect.can_request()
		) {
			mpsa_collect.add_button(frm);
		}
	},
});
