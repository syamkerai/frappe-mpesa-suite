// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Reversal Request", {
	setup(frm) {
		frm.set_query("c2b_register", () => ({ filters: { docstatus: ["<", 2] } }));
	},
	refresh(frm) {
		if (frm.doc.docstatus === 1 && ["Initiated", "Unknown"].includes(frm.doc.status)) {
			frm.dashboard.set_headline(
				__("Waiting for Safaricom's result. Do not create another reversal for this payment."),
				"orange"
			);
		}
	},
});
