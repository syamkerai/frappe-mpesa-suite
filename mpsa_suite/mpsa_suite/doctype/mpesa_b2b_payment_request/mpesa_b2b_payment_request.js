// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

const MPSA_B2B = "mpsa_suite.money_out.b2b";
const MPSA_B2B_ENTRIES = "mpsa_suite.money_out.b2b_entries";
const MPSA_FINANCE_ROLES = ["System Manager", "M-Pesa Manager", "Accounts Manager", "Accounts User"];
const MPSA_ORDER_CLOSED = ["On Hold", "Closed", "Completed"];

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
		frm.set_query("purchase_order", () => {
			const filters = { docstatus: 1, per_billed: 0, status: ["not in", MPSA_ORDER_CLOSED] };
			if (frm.doc.supplier) filters.supplier = frm.doc.supplier;
			return { filters };
		});
		frm.set_query("mpesa_settings", () => {
			const filters = { use_b2b: 1 };
			if (frm.doc.company) filters.company = frm.doc.company;
			return { filters };
		});
	},
	onload(frm) {
		// opened from a Purchase Invoice or Order (Pay via M-Pesa): fill in what to pay
		if (frm.is_new()) mpsa_b2b.load_left(frm, true);
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
		if (frm.doc.docstatus === 1 && frm.doc.status === "Not Initiated" && frm.doc.payment_schedule) {
			frm.dashboard.set_headline(
				__("Approved on payment schedule {0}: it is being sent to M-Pesa with the other payments of its batch.", [
					frappe.utils.escape_html(frm.doc.payment_schedule),
				]),
				"blue"
			);
		}
		if (frm.doc.docstatus === 0 && !frm.is_new()) mpsa_b2b.load_left(frm, false);
		mpsa_b2b.entry_actions(frm);
	},
	supplier(frm) {
		if (frm.doc.supplier_mpesa_account && !frm.mpsa_filling) frm.set_value("supplier_mpesa_account", null);
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
		if (frm.doc.purchase_invoice && frm.doc.purchase_order) frm.set_value("purchase_order", null);
		mpsa_b2b.load_left(frm, true);
	},
	purchase_order(frm) {
		mpsa_b2b.load_left(frm, true);
	},
});

const mpsa_b2b = {
	document(frm) {
		if (frm.doc.purchase_invoice) return ["Purchase Invoice", frm.doc.purchase_invoice];
		if (frm.doc.purchase_order) return ["Purchase Order", frm.doc.purchase_order];
		return [null, null];
	},

	// What is left to pay on the invoice or order, and why; ``fill``: take supplier, company,
	// shortcode and amount from it.
	async load_left(frm, fill) {
		const [doctype, name] = mpsa_b2b.document(frm);
		if (!doctype) {
			frm.toggle_display("checked_earlier_payouts", false);
			return;
		}
		const r = await frappe.xcall(`${MPSA_B2B}.amount_left`, {
			doctype,
			name,
			request: frm.is_new() ? null : frm.doc.name,
		});
		if (!r || frm.doc.docstatus !== 0) return;
		if (fill) {
			frm.mpsa_filling = true;
			try {
				if (r.supplier && frm.doc.supplier !== r.supplier) await frm.set_value("supplier", r.supplier);
				// the company follows the document (the server does the same); a shortcode of
				// another company no longer fits
				if (r.company && frm.doc.company !== r.company) {
					await frm.set_value("company", r.company);
					if (frm.doc.mpesa_settings && !r.mpesa_settings.includes(frm.doc.mpesa_settings)) {
						await frm.set_value("mpesa_settings", null);
					}
				}
				if (!frm.doc.mpesa_settings && r.mpesa_settings.length === 1) {
					await frm.set_value("mpesa_settings", r.mpesa_settings[0]);
				}
				if (!frm.doc.supplier_mpesa_account && r.supplier_mpesa_account) {
					await frm.set_value("supplier_mpesa_account", r.supplier_mpesa_account);
				}
				await frm.set_value("amount", Math.floor(r.left));
			} finally {
				frm.mpsa_filling = false;
			}
		}
		mpsa_b2b.show_left(frm, name, r);
	},

	show_left(frm, name, r) {
		const e = frappe.utils.escape_html;
		const kes = (value) => format_number(value, null, 2);
		const stops = [...(r.problems || []), ...(r.supplier_hold ? [r.supplier_hold] : [])];
		if (stops.length) {
			frm.dashboard.set_headline(stops.map(e).join("<br>"), "red");
		} else {
			const s = r.summary;
			let text = __("Left to pay on {0}: KES {1}. Outstanding {2}", [e(name), kes(s.left), kes(s.outstanding)]);
			if (s.in_progress) text += " − " + __("in progress {0}", [kes(s.in_progress)]);
			if (s.drafts) text += " − " + __("draft entries {0}", [kes(s.drafts)]);
			if (s.planned) text += " − " + __("planned on a payment schedule {0}", [kes(s.planned)]);
			if (s.order_advance) text += " − " + __("order advance {0}", [kes(s.order_advance)]);
			if ((r.parts || []).length) text += "<br><span class='small'>" + r.parts.map(e).join("; ") + "</span>";
			frm.dashboard.set_headline(text, "blue");
		}
		const earlier = r.earlier_payouts || [];
		frm.toggle_display("checked_earlier_payouts", earlier.length > 0);
		if (earlier.length) {
			frm.set_intro(
				__("{0} already got M-Pesa payments that are not on any invoice. Check that none of them paid this invoice:", [
					e(r.supplier),
				]) +
					"<ul>" +
					earlier.map((line) => `<li>${e(line)}</li>`).join("") +
					"</ul>",
				"orange"
			);
		}
	},

	// Paid, but its accounting entry is not made (or needs a look): say why, and offer the fix.
	entry_actions(frm) {
		const doc = frm.doc;
		if (doc.status !== "Paid" || !doc.entry_status || doc.entry_status === "Entry Made") return;
		frm.dashboard.set_headline(
			frappe.utils.escape_html(doc.entry_note || doc.entry_status),
			doc.entry_status === "Needs Checking" ? "red" : "orange"
		);
		if (!frappe.user.has_role(MPSA_FINANCE_ROLES)) return;
		if (!doc.payment_entry && !doc.journal_entry) {
			frm.add_custom_button(__("Make Entry"), () => mpsa_b2b.make_entry(frm), __("Accounting"));
		}
		frm.add_custom_button(
			__("Check Entry Again"),
			() =>
				frappe
					.xcall(`${MPSA_B2B_ENTRIES}.refresh_entry_now`, { name: doc.name })
					.then(() => frm.reload_doc()),
			__("Accounting")
		);
	},

	async make_entry(frm) {
		const f = await frappe.xcall(`${MPSA_B2B_ENTRIES}.entry_facts`, { name: frm.doc.name });
		const e = frappe.utils.escape_html;
		const facts = [
			[__("Amount"), "KES " + format_number(f.amount, null, 2)],
			[__("Paid on"), frappe.datetime.str_to_user(f.date)],
			[__("M-Pesa receipt"), f.receipt],
			[__("Paid to"), f.paid_to],
			[__("From account"), f.gl],
			[__("Charge entry"), f.charge_entry || __("none yet")],
		]
			.map(([label, value]) => `<div><span class="text-muted">${label}:</span> ${e(value || "-")}</div>`)
			.join("");
		const kind = f.purchase_invoice
			? "Purchase Invoice"
			: f.purchase_order
			? "Purchase Order"
			: f.supplier
			? "Supplier Advance"
			: "Journal Entry";
		const supplier_filter = () => (d.get_value("supplier") ? { supplier: d.get_value("supplier") } : {});
		const d = new frappe.ui.Dialog({
			title: __("Make Entry"),
			fields: [
				{ fieldtype: "HTML", fieldname: "facts", options: `<div class="small mb-3">${facts}</div>` },
				{
					fieldtype: "Select",
					fieldname: "kind",
					label: __("Entry Type"),
					reqd: 1,
					options: ["Purchase Invoice", "Purchase Order", "Supplier Advance", "Journal Entry"].join("\n"),
					default: kind,
				},
				{
					fieldtype: "Link",
					fieldname: "supplier",
					label: __("Supplier"),
					options: "Supplier",
					default: f.supplier,
					depends_on: "eval:doc.kind!='Journal Entry'",
				},
				{
					fieldtype: "Link",
					fieldname: "purchase_invoice",
					label: __("Purchase Invoice"),
					options: "Purchase Invoice",
					default: f.purchase_invoice,
					depends_on: "eval:doc.kind=='Purchase Invoice'",
					mandatory_depends_on: "eval:doc.kind=='Purchase Invoice'",
					get_query: () => ({ filters: { docstatus: 1, outstanding_amount: [">", 0], ...supplier_filter() } }),
				},
				{
					fieldtype: "Link",
					fieldname: "purchase_order",
					label: __("Purchase Order"),
					options: "Purchase Order",
					default: f.purchase_order,
					depends_on: "eval:doc.kind=='Purchase Order'",
					mandatory_depends_on: "eval:doc.kind=='Purchase Order'",
					get_query: () => ({
						filters: { docstatus: 1, per_billed: 0, status: ["not in", MPSA_ORDER_CLOSED], ...supplier_filter() },
					}),
				},
				{
					fieldtype: "Link",
					fieldname: "account",
					label: __("Account"),
					options: "Account",
					depends_on: "eval:doc.kind=='Journal Entry'",
					mandatory_depends_on: "eval:doc.kind=='Journal Entry'",
					description: __("What the money paid for: an expense, or a bank account for a transfer."),
					get_query: () => ({
						filters: { company: f.company, is_group: 0, account_type: ["not in", ["Receivable", "Payable"]] },
					}),
				},
				{
					fieldtype: "Link",
					fieldname: "cost_center",
					label: __("Cost Center"),
					options: "Cost Center",
					default: f.cost_center,
					depends_on: "eval:doc.kind=='Journal Entry'",
					get_query: () => ({ filters: { company: f.company, is_group: 0 } }),
				},
				{ fieldtype: "Small Text", fieldname: "remark", label: __("Remark") },
				{ fieldtype: "Check", fieldname: "submit", label: __("Submit the entry now"), default: 1 },
			],
			primary_action_label: __("Make Entry"),
			primary_action(values) {
				frappe.xcall(`${MPSA_B2B_ENTRIES}.make_entry`, { name: frm.doc.name, ...values }).then((r) => {
					d.hide();
					frappe.show_alert({ message: __("{0} {1} made", [r.doctype, r.name]), indicator: "green" });
					frm.reload_doc();
				});
			},
		});
		d.show();
	},
};
