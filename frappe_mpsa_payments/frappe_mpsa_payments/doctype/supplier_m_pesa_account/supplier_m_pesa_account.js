// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Supplier M-Pesa Account", {
	refresh(frm) {
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.dashboard.set_headline(
				__("Not approved yet: a manager other than {0} must check the proof and submit this account.", [
					frappe.utils.escape_html(frm.doc.owner),
				]),
				"orange"
			);
		}
		if (frm.doc.docstatus === 1 && frm.doc.name_mismatch) {
			frm.dashboard.set_headline(
				__("Last payment went to {0}, not {1}. Check with the supplier before paying again.", [
					frappe.utils.escape_html(frm.doc.last_receiver_name || ""),
					frappe.utils.escape_html(frm.doc.expected_name || ""),
				]),
				"red"
			);
		}
		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Pay via M-Pesa (B2B)"), () => {
				frappe.new_doc("Mpesa B2B Payment Request", {
					supplier: frm.doc.supplier,
					supplier_mpesa_account: frm.doc.name,
				});
			});
		}
	},
});
