// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// A sent payout shows how far its rows got; drafts and cancelled ones keep Frappe's indicators.
frappe.listview_settings["B2C Payment Disbursement"] = {
	add_fields: ["status", "docstatus"],
	get_indicator(doc) {
		const colours = {
			"Not Initiated": "gray",
			Initiated: "blue",
			"Partially Paid": "orange",
			Paid: "green",
			Failed: "red",
		};
		if (doc.docstatus === 1 && doc.status) return [__(doc.status), colours[doc.status] || "gray", "status,=," + doc.status];
	},
};
