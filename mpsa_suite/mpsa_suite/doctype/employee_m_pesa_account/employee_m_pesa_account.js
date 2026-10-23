// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Employee M-Pesa Account", {
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
				__("Last payment went to {0}, not {1}. Check with the employee before paying again.", [
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
							options: __("Only do this after checking with the employee that {0} is their account.", [
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
		if (frm.doc.docstatus === 1 && !frm.doc.name_mismatch && frm.doc.account_type === "Bank Paybill (B2B)") {
			frm.add_custom_button(__("Pay via M-Pesa (B2B)"), () => {
				frappe.new_doc("Mpesa B2B Payment Request", {
					employee: frm.doc.employee,
					employee_mpesa_account: frm.doc.name,
					entry_mode: "Make the entry later",
				});
			});
		}
	},

	async employee(frm) {
		// the employee's phone and bank account from the Employee record, to check against the proof
		if (!frm.doc.employee || !frm.is_new()) return;
		const { message } = await frappe.db.get_value("Employee", frm.doc.employee, ["cell_number", "bank_ac_no"]);
		if (!message) return;
		if (!frm.doc.phone && message.cell_number) frm.set_value("phone", message.cell_number);
		if (!frm.doc.account_number && message.bank_ac_no) frm.set_value("account_number", message.bank_ac_no);
	},
});
