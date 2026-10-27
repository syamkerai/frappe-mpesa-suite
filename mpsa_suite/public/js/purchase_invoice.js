// M-Pesa on a Purchase Invoice: pay it by B2B, and use an order's advance before paying again.
frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		const doc = frm.doc;
		if (
			doc.docstatus === 1 &&
			flt(doc.outstanding_amount) > 0 &&
			(doc.currency || "KES") === "KES" &&
			!doc.on_hold &&
			!doc.is_return &&
			frappe.model.can_create("Mpesa B2B Payment Request")
		) {
			frm.add_custom_button(
				__("Pay via M-Pesa (B2B)"),
				() =>
					frappe.new_doc("Mpesa B2B Payment Request", {
						purchase_invoice: doc.name,
						// the invoice's company, not the user's default one
						company: doc.company,
						remarks: [doc.name, doc.bill_no].filter(Boolean).join(" ").slice(0, 100),
					}),
				__("M-Pesa")
			);
		}
		// what M-Pesa has on its way or planned for the invoice, and a schedule line for the rest (utils.js)
		if (doc.docstatus === 1 && flt(doc.outstanding_amount) > 0 && (doc.currency || "KES") === "KES") {
			if (frappe.model.can_read("Mpesa B2B Payment Request")) mpsa_payables.show_left(frm);
			if (!doc.on_hold && !doc.is_return && frappe.model.can_create("M-Pesa Payment Schedule")) {
				mpsa_payables.add_schedule_button(frm);
			}
		}
		if (doc.docstatus === 0) mpsa_show_order_advance(frm);
	},
});

// A draft invoice made from an order that holds an advance: say so before anyone pays it again.
function mpsa_show_order_advance(frm) {
	const orders = [...new Set((frm.doc.items || []).map((row) => row.purchase_order).filter(Boolean))];
	if (!orders.length) return;
	const used = (frm.doc.advances || []).reduce((total, row) => total + flt(row.allocated_amount), 0);
	frappe
		.xcall("mpsa_suite.money_out.b2b.order_advances", { purchase_orders: orders })
		.then((rows) => {
			const waiting = (rows || []).filter((row) => flt(row.amount) > 0);
			const total = waiting.reduce((sum, row) => sum + flt(row.amount), 0);
			if (!waiting.length || used >= total) return;
			const lines = waiting.map((row) =>
				__("{0} has KES {1} paid in advance: press Get Advances Paid to use it.", [
					frappe.utils.escape_html(row.purchase_order),
					format_number(row.amount, null, 2),
				])
			);
			frm.dashboard.set_headline(lines.join("<br>"), "orange");
		});
}
