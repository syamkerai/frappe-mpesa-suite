// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// M-Pesa Known Name's list (round 4.5 review): the names come from M-Pesa's results and SMS, and the
// v15 list view shows a Data field as it is. The server keeps them as plain text (known_names.plain);
// the list escapes them too.
frappe.listview_settings["M-Pesa Known Name"] = {
	formatters: {
		mpesa_name: (value) => frappe.utils.escape_html(value || ""),
		received: (value) => frappe.utils.escape_html(value || ""),
		number: (value) => frappe.utils.escape_html(value || ""),
	},
};
