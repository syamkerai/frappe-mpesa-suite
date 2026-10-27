// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// A blocked account (a name mismatch, or its bank paybill changed or its approval had no second
// named person: cancel, amend and approve it again) is the one to look at; the default is where
// payments that name no account go. money_out.party_accounts sets these fields.
frappe.listview_settings["Employee M-Pesa Account"] = {
	add_fields: ["docstatus", "is_default", "needs_reapproval", "name_mismatch", "account_type", "employee"],
	get_indicator(doc) {
		if (doc.docstatus === 2) return [__("Cancelled"), "gray", "docstatus,=,2"];
		if (doc.docstatus !== 1) return [__("Draft"), "orange", "docstatus,=,0"];
		if (cint(doc.needs_reapproval)) return [__("Blocked: Approve Again"), "red", "needs_reapproval,=,1"];
		if (cint(doc.name_mismatch)) return [__("Blocked: Name Did Not Match"), "red", "name_mismatch,=,1"];
		if (cint(doc.is_default)) return [__("Default"), "green", "is_default,=,1"];
		return [__("Approved"), "blue", "docstatus,=,1"];
	},
};
