// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa B2B Payment Request", {
	setup(frm) {
		frm.set_query("supplier_mpesa_account", () => ({
			filters: { docstatus: 1, ...(frm.doc.supplier ? { supplier: frm.doc.supplier } : {}) },
		}));
		frm.set_query("purchase_invoice", () => {
			const filters = { docstatus: 1, outstanding_amount: [">", 0] };
			if (frm.doc.supplier) filters.supplier = frm.doc.supplier;
			return { filters };
		});
	},
	refresh(frm) {
		if (frm.doc.receiver_name_mismatch) {
			frm.dashboard.set_headline(
				__("M-Pesa paid {0}, which does not match the supplier's account. Check before paying again.", [
					frappe.utils.escape_html(frm.doc.receiver_public_name || ""),
				]),
				"red"
			);
		}
		if (frm.doc.docstatus === 1 && frm.doc.status === "Unknown") {
			frm.dashboard.set_headline(
				__("M-Pesa did not answer. The payment may have gone through: check the statement before paying again."),
				"red"
			);
		}
	},
	supplier(frm) {
		if (frm.doc.supplier_mpesa_account) frm.set_value("supplier_mpesa_account", null);
	},
	supplier_mpesa_account(frm) {
		// Display only: the server copies the destination from the approved account on save.
		if (!frm.doc.supplier_mpesa_account) return;
		frappe.db
			.get_value("Supplier M-Pesa Account", frm.doc.supplier_mpesa_account, [
				"supplier",
				"account_type",
				"shortcode",
				"account_number",
			])
			.then(({ message }) => {
				if (!message) return;
				if (!frm.doc.supplier) frm.set_value("supplier", message.supplier);
				frm.set_value(
					"command_id",
					message.account_type === "Paybill" ? "BusinessPayBill" : "BusinessBuyGoods"
				);
				frm.set_value("receiver_shortcode", message.shortcode);
				frm.set_value("account_reference", message.account_number || "");
			});
	},
	purchase_invoice(frm) {
		if (!frm.doc.purchase_invoice) return;
		frappe.db
			.get_value("Purchase Invoice", frm.doc.purchase_invoice, ["supplier", "company", "outstanding_amount"])
			.then(({ message }) => {
				if (!message) return;
				frm.set_value("supplier", message.supplier);
				frm.set_value("company", message.company);
				if (!frm.doc.amount) frm.set_value("amount", Math.floor(message.outstanding_amount));
			});
	},
});
