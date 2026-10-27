// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// Status Unknown and Initiated are the payouts to look at before anyone pays again; a Paid one
// shows whether its accounting entry is booked.
frappe.listview_settings["B2C Disbursement Request"] = {
	add_fields: ["status", "docstatus", "accounting_booked"],
	get_indicator(doc) {
		if (doc.docstatus !== 1 || !doc.status) return;
		if (doc.status === "Paid" && !doc.accounting_booked) return [__("Paid, not booked"), "orange", "accounting_booked,=,0"];
		const colours = {
			"Not Initiated": "gray",
			Initiated: "blue",
			"Status Unknown": "orange",
			Paid: "green",
			Failed: "red",
		};
		return [__(doc.status), colours[doc.status] || "gray", "status,=," + doc.status];
	},
};
