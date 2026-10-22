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
			frm.add_custom_button(__("Receiver Name Confirmed"), () => {
				const d = new frappe.ui.Dialog({
					title: __("Unblock this account"),
					fields: [
						{
							fieldtype: "HTML",
							options: __("Only do this after checking with the supplier that {0} is their M-Pesa account.", [
								"<b>" + frappe.utils.escape_html(frm.doc.last_receiver_name || "") + "</b>",
							]),
						},
						{
							fieldname: "adopt_name",
							fieldtype: "Check",
							label: __("Use this name as the expected name from now on"),
							default: 1,
						},
					],
					primary_action_label: __("Unblock"),
					primary_action(values) {
						frm.call("confirm_receiver_name", { adopt_name: values.adopt_name ? 1 : 0 }).then(() => {
							d.hide();
							frm.reload_doc();
						});
					},
				});
				d.show();
			});
		}
		if (frm.doc.docstatus === 1 && !frm.doc.name_mismatch) {
			frm.add_custom_button(__("Pay via M-Pesa (B2B)"), () => {
				frappe.new_doc("Mpesa B2B Payment Request", {
					supplier: frm.doc.supplier,
					supplier_mpesa_account: frm.doc.name,
				});
			});
		}
	},
});
