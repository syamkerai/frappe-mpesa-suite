// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.listview_settings["M-Pesa Payment Schedule"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colours = { Open: "orange", "Partly Paid": "blue", Paid: "green", Closed: "gray" };
		if (doc.status) return [__(doc.status), colours[doc.status] || "gray", "status,=," + doc.status];
	},
};
