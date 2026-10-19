// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa B2B Payment Request", {
	setup(frm) {
		frm.set_query("purchase_invoice", () => {
			const filters = { docstatus: 1, outstanding_amount: [">", 0] };
			if (frm.doc.supplier) filters.supplier = frm.doc.supplier;
			return { filters };
		});
	},
	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.status === "Unknown") {
			frm.dashboard.set_headline(
				__("M-Pesa did not answer. The payment may have gone through: check the statement before paying again."),
				"red"
			);
		}
	},
	purchase_invoice(frm) {
		if (!frm.doc.purchase_invoice) return;
		frappe.db
			.get_value("Purchase Invoice", frm.doc.purchase_invoice, ["supplier", "company", "outstanding_amount"])
			.then(({ message }) => {
				if (!message) return;
				frm.set_value("supplier", message.supplier);
				frm.set_value("company", message.company);
				if (!frm.doc.amount) frm.set_value("amount", Math.floor(message.outstanding_amount));
			});
	},
});
