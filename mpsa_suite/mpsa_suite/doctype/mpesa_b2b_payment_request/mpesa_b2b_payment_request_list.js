// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// A Paid payment shows whether its accounting entry is made; a cancelled one paid later shows too.
frappe.listview_settings["Mpesa B2B Payment Request"] = {
	add_fields: ["status", "entry_status", "docstatus"],
	has_indicator_for_cancelled: true,
	get_indicator(doc) {
		if (doc.status === "Paid") {
			if (doc.entry_status === "Needs Checking") return [__("Paid, check entry"), "red", "entry_status,=,Needs Checking"];
			if (doc.entry_status === "Draft Entry") return [__("Paid, draft entry"), "orange", "entry_status,=,Draft Entry"];
			if (doc.entry_status === "Entry Made") return [__("Paid"), "green", "status,=,Paid"];
			return [__("Paid, no entry"), "orange", "entry_status,=,Entry Not Made"];
		}
		if (doc.docstatus === 2) return [__("Cancelled"), "red", "docstatus,=,2"];
		const colours = { "Not Initiated": "gray", Initiated: "blue", Failed: "red", Unknown: "orange" };
		if (doc.status) return [__(doc.status), colours[doc.status] || "gray", "status,=," + doc.status];
	},
};
