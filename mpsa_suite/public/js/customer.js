// M-Pesa on a Customer: what it paid by M-Pesa in the last 90 days (its booked C2B receipts).
// Read only; its receipts, prompts and matches are under Connections.
frappe.ui.form.on("Customer", {
	refresh(frm) {
		if (frm.is_new() || !frappe.model.can_read("Mpesa C2B Payment Register")) return;
		const most = 1000;
		frappe.db
			.get_list("Mpesa C2B Payment Register", {
				filters: {
					customer: frm.doc.name,
					docstatus: 1,
					posting_date: [">=", frappe.datetime.add_days(frappe.datetime.get_today(), -90)],
				},
				fields: ["transamount"],
				limit: most,
			})
			.then((rows) => {
				if (!rows || !rows.length) return;
				const total = rows.reduce((sum, row) => sum + flt(row.transamount), 0);
				frm.dashboard.add_indicator(
					__("M-Pesa received in 90 days: KES {0} ({1} payments)", [
						format_number(total, null, 2),
						rows.length >= most ? __("{0} or more", [most]) : rows.length,
					]),
					"green"
				);
			});
	},
});
