// Copyright (c) 2024, Navari Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa C2B Payment Register URL", {
	refresh(frm) {
		if (frm.is_new()) return;
		// Re-send the Confirmation/Validation URLs, e.g. after host_name or the callback token changed.
		frm.add_custom_button(__("Register URLs Again"), () => {
			frappe.confirm(
				__("Send this shortcode's Confirmation and Validation URLs to Safaricom again?"),
				() =>
					frm
						.call({ method: "register_again", doc: frm.doc, freeze: true })
						.then(() => frm.reload_doc())
			);
		});
	},
});
