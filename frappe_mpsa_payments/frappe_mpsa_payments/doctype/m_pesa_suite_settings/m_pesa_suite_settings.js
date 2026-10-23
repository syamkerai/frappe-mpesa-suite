// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("M-Pesa Suite Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Regenerate Callback Token"), () => {
			frappe.confirm(
				__(
					"Rotate the callback token? C2B confirmations registered with the old URL will be rejected if C2B token enforcement is on."
				),
				() =>
					frm.call("regenerate_callback_token").then((r) => {
						frappe.msgprint(r.message);
						frm.reload_doc();
					})
			);
		}, __("Security"));

		if (frm.doc.rejected_callbacks) {
			frm.dashboard.set_headline_alert(
				__("{0} callback(s) rejected. Last: {1}", [
					frm.doc.rejected_callbacks,
					frm.doc.last_rejected_callback_reason || "",
				]),
				"orange"
			);
			frm.add_custom_button(__("Reset Rejection Counter"), () =>
				frm.call("reset_rejection_counter").then(() => frm.reload_doc())
			, __("Security"));
		}
	},
});
