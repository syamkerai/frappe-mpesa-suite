// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// Which fields show for each account type is in the DocType (depends_on); the server cleans and
// checks every save (money_out.party_accounts). This script fills a bank account from its bank,
// names the account number the way the bank does, and offers Make Default.
const MPSA_SUPPLIER_BANK = "Bank Account (via Bank Paybill)";
const MPSA_SUPPLIER_B2B = ["Paybill", "Till (Buy Goods)", MPSA_SUPPLIER_BANK];

frappe.ui.form.on("Supplier M-Pesa Account", {
	setup(frm) {
		frm.set_query("bank_paybill", () => ({ filters: { enabled: 1 } }));
	},

	refresh(frm) {
		const e = frappe.utils.escape_html;
		const state = (frm.doc.__onload && frm.doc.__onload.mpsa) || {};
		mpsa_supplier_account.label_number(frm);
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.dashboard.set_headline(
				__("Not approved yet: a manager other than {0} must check the proof and submit this account.", [
					e(frm.doc.owner),
				]),
				"orange"
			);
		}
		// one headline shows: the most pressing is set last
		if (frm.doc.docstatus === 1 && frm.doc.account_type === "Till (Buy Goods)" && !frm.doc.last_receiver_name) {
			frm.dashboard.set_headline(__("Name not checked (till): M-Pesa's result for a till names no one."), "blue");
		}
		if (frm.doc.docstatus === 1 && state.approved >= 2 && !state.default) {
			frm.dashboard.set_headline(
				__("This supplier has {0} approved M-Pesa accounts and none is the default: press Make Default on the one payments go to when no account is picked.", [
					state.approved,
				]),
				"red"
			);
		}
		if (frm.doc.docstatus === 1 && frm.doc.needs_reapproval) {
			frm.dashboard.set_headline(
				e(frm.doc.reapproval_reason || __("Blocked: cancel and amend this account so a manager approves it again.")),
				"red"
			);
		}
		if (frm.doc.docstatus === 1 && frm.doc.name_mismatch) {
			frm.dashboard.set_headline(
				__("Last payment went to {0}, not {1}. Check with the supplier before paying again.", [
					e(frm.doc.last_receiver_name || ""),
					e(frm.doc.expected_name || ""),
				]),
				"red"
			);
			frm.add_custom_button(__("Receiver Name Confirmed"), () => {
				const d = new frappe.ui.Dialog({
					title: __("Unblock this account"),
					fields: [
						{
							fieldtype: "HTML",
							options: __("Only do this after checking with the supplier that {0} is their M-Pesa account.", [
								"<b>" + e(frm.doc.last_receiver_name || "") + "</b>",
							]),
						},
						{
							fieldname: "adopt_name",
							fieldtype: "Check",
							label: __("Use this name as the expected name from now on"),
							default: 1,
						},
					],
					primary_action_label: __("Unblock"),
					primary_action(values) {
						frm.call("confirm_receiver_name", { adopt_name: values.adopt_name ? 1 : 0 }).then(() => {
							d.hide();
							frm.reload_doc();
						});
					},
				});
				d.show();
			});
		}
		if (state.can_make_default) {
			frm.add_custom_button(__("Make Default"), () =>
				frappe.confirm(__("Make this the supplier's default M-Pesa account? Payments that name no account go here."), () =>
					frm.call("make_default").then(() => frm.reload_doc())
				)
			);
		}
		const blocked = frm.doc.name_mismatch || frm.doc.needs_reapproval;
		if (frm.doc.docstatus === 1 && !blocked && MPSA_SUPPLIER_B2B.includes(frm.doc.account_type)) {
			frm.add_custom_button(__("Pay via M-Pesa (B2B)"), () => {
				frappe.new_doc("Mpesa B2B Payment Request", {
					supplier: frm.doc.supplier,
					supplier_mpesa_account: frm.doc.name,
				});
			});
		}
	},

	account_type(frm) {
		mpsa_supplier_account.label_number(frm);
		if (frm.doc.account_type === MPSA_SUPPLIER_BANK) mpsa_supplier_account.fill_holder(frm);
	},

	bank_paybill(frm) {
		mpsa_supplier_account.from_bank(frm);
	},
});

const mpsa_supplier_account = {
	// the bank's paybill and the name M-Pesa shows for it (the server copies them again on save)
	async from_bank(frm) {
		mpsa_supplier_account.label_number(frm);
		if (!frm.doc.bank_paybill) return;
		const { message } = await frappe.db.get_value("M-Pesa Bank Paybill", frm.doc.bank_paybill, [
			"paybill",
			"receiver_name",
			"institution_name",
		]);
		if (!message) return;
		await frm.set_value("shortcode", message.paybill);
		await frm.set_value("expected_name", message.receiver_name || message.institution_name);
		mpsa_supplier_account.fill_holder(frm);
	},

	async fill_holder(frm) {
		if (frm.doc.account_holder_name || !frm.doc.supplier) return;
		const { message } = await frappe.db.get_value("Supplier", frm.doc.supplier, "supplier_name");
		if (message && message.supplier_name) frm.set_value("account_holder_name", message.supplier_name);
	},

	// "Account Number at KCB" and the bank's rule, for a bank account
	async label_number(frm) {
		let label = __("Account Number");
		let rule = __("The account number the supplier told you to use with their paybill.");
		if (frm.doc.account_type === MPSA_SUPPLIER_BANK) {
			label = __("Bank Account Number");
			rule = __("The supplier's account number at that bank, exactly as the bank wants it with its paybill.");
			if (frm.doc.bank_paybill) {
				const { message } = await frappe.db.get_value("M-Pesa Bank Paybill", frm.doc.bank_paybill, [
					"account_number_label",
					"account_number_rule",
				]);
				label = (message && message.account_number_label) || label;
				rule = (message && message.account_number_rule) || rule;
			}
		}
		frm.set_df_property("account_number", "label", label);
		frm.set_df_property("account_number", "description", rule);
	},
};
