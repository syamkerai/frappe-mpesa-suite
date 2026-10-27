// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// Initiated and Unknown reversals wait for Safaricom: do not reverse the same payment again.
frappe.listview_settings["Mpesa Reversal Request"] = {
	add_fields: ["status", "docstatus"],
	get_indicator(doc) {
		const colours = {
			"Not Initiated": "gray",
			Initiated: "blue",
			Unknown: "orange",
			Reversed: "green",
			"Already Reversed": "green",
			Failed: "red",
		};
		if (doc.docstatus === 1 && doc.status) return [__(doc.status), colours[doc.status] || "gray", "status,=," + doc.status];
	},
};
