// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa API Client", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Generate New Secret"), () =>
			frappe.confirm(__("The current secret stops working immediately. Continue?"), () =>
				frm.call("generate_new_secret").then(() => frm.reload_doc())
			), __("Credentials"));
		if (frm.doc.webhook_url) {
			frm.add_custom_button(__("Rotate Webhook Secret"), () =>
				frappe.confirm(__("Your receiver must switch to the new secret. Continue?"), () =>
					frm.call("rotate_webhook_secret").then(() => frm.reload_doc())
				), __("Credentials"));
		}
		frm.set_query("mpesa_settings", () => ({ filters: { use_stk: 1 } }));
	},
});
