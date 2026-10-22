// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Statement Import", {
	refresh(frm) {
		if (frm.is_new() || ["Queued", "Importing"].includes(frm.doc.status)) {
			if (!frm.is_new()) {
				frm.set_intro(__("Importing… refresh in a minute."), "blue");
			}
			return;
		}
		const label = frm.doc.status === "Imported" ? __("Import Again") : __("Import Statement");
		frm.add_custom_button(label, () =>
			frm.call("import_statement").then((r) => {
				if (r.message) frappe.show_alert({ message: r.message, indicator: "blue" });
				frm.reload_doc();
			})
		);
		if (frm.doc.status === "Imported") {
			frm.add_custom_button(__("Rows"), () =>
				frappe.set_route("List", "Mpesa Statement Row", { statement_import: frm.doc.name })
			);
			if (frm.doc.left_for_staff) {
				frm.set_intro(
					__("{0} row(s) were left for you: open Rows and filter Result = Left for Staff.", [
						frm.doc.left_for_staff,
					]),
					"orange"
				);
			}
		}
	},
});
