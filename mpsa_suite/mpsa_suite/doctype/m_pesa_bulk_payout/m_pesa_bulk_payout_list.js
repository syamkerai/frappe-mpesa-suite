// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.listview_settings["M-Pesa Bulk Payout"] = {
	add_fields: ["status", "progress"],
	get_indicator(doc) {
		const colours = {
			Draft: "red",
			"To Pay": "orange",
			Paying: "blue",
			"Partly Paid": "yellow",
			Paid: "green",
			Cancelled: "gray",
		};
		if (doc.status) return [__(doc.status), colours[doc.status] || "gray", "status,=," + doc.status];
	},
};
