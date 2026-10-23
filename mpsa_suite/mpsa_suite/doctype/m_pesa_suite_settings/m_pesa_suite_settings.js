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

		if (frappe.user.has_role(["System Manager", "M-Pesa Manager"])) {
			frm.add_custom_button(__("Check Permissions"), () => mpsa_permission_dialog(), __("Permissions"));
		}

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

// This site's own permission rules (Custom DocPerm) replace the suite's for a DocType. Shows how
// they differ and resets the chosen DocTypes to the suite's rules (docs/ROLES_AND_PERMISSIONS.md).
function mpsa_permission_dialog() {
	const ACCESS = "mpsa_suite.access";
	const esc = frappe.utils.escape_html;
	const rows = (list, users) =>
		list
			.map(
				(r) =>
					`<li>${esc(r.role)}${r.permlevel ? " (level " + r.permlevel + ")" : ""}: ${esc(
						r.rights.join(", ")
					)}${users && r.users ? " - " + __("{0} user(s)", [r.users]) : ""}</li>`
			)
			.join("");
	const render = (dialog, report) => {
		const found = report.overrides;
		let html = found.length
			? `<p>${__(
					"These DocTypes use this site's own permission rules instead of the suite's. A reset deletes the site's rules for that DocType; users keep their roles. Give people the M-Pesa roles they need first."
			  )}</p>`
			: `<p>${__("Every M-Pesa DocType uses the suite's permission rules.")}</p>`;
		for (const item of found) {
			html += `<h5>${esc(item.doctype)}</h5>`;
			if (item.extra.length)
				html += `<p>${__("The site grants, beyond the suite:")}</p><ul>${rows(item.extra, true)}</ul>`;
			if (item.missing.length)
				html += `<p>${__("The suite would grant, the site does not:")}</p><ul>${rows(item.missing)}</ul>`;
		}
		dialog.fields_dict.report.$wrapper.html(html);
		dialog.fields_dict.doctypes.df.options = found.map((i) => ({ label: i.doctype, value: i.doctype }));
		dialog.fields_dict.doctypes.refresh();
		dialog.get_primary_btn().toggle(found.length > 0);
	};
	const dialog = new frappe.ui.Dialog({
		title: __("M-Pesa Permissions"),
		size: "large",
		fields: [
			{ fieldname: "report", fieldtype: "HTML" },
			{ fieldname: "doctypes", fieldtype: "MultiCheck", label: __("Reset to the suite's rules"), options: [] },
		],
		primary_action_label: __("Reset Selected"),
		primary_action(values) {
			const chosen = values.doctypes || [];
			if (!chosen.length) return;
			frappe.confirm(
				__("Delete this site's own permission rules for {0}? The suite's rules apply from now on.", [
					chosen.join(", "),
				]),
				() => frappe.xcall(`${ACCESS}.reset_permissions`, { doctypes: chosen }).then((r) => render(dialog, r))
			);
		},
	});
	frappe.xcall(`${ACCESS}.permission_report`).then((r) => {
		render(dialog, r);
		dialog.show();
	});
}
