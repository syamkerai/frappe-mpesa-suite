// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("M-Pesa Bank Paybill", {
	refresh(frm) {
		if (frm.is_new()) return;
		if (frm.doc.is_shipped) {
			frm.dashboard.set_headline(
				__("Shipped with M-Pesa Suite. A release changes only the fields you have not changed; disable the row instead of deleting it."),
				"blue"
			);
		}
		if (!frm.doc.enabled) {
			frm.dashboard.set_headline(
				__("Switched off: new payee accounts cannot pick it. Check the paybill and the account number rule with the institution before enabling it."),
				"orange"
			);
		}
		const seen = frm.doc.observed_receiver_name;
		if (seen && seen !== frm.doc.receiver_name && frm.perm[0] && frm.perm[0].write) {
			frm.add_custom_button(__("Use the Name M-Pesa Showed"), () =>
				frappe.confirm(
					__("Check every payment to accounts at this paybill against {0} from now on?", [
						"<b>" + frappe.utils.escape_html(seen) + "</b>",
					]),
					() => frm.set_value("receiver_name", seen).then(() => frm.save())
				)
			);
		}
	},
});
