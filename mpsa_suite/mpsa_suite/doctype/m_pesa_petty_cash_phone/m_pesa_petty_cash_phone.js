// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// The petty cash phone (money_out.petty_cash): its book balance against its last SMS, its top-ups, and
// the buttons the server accepts for this user (phone_state): Approve in place of Submit, Top Up by
// M-Pesa, Name Confirmed. Each is checked again on the server.
/* global mpsa_petty_cash_phone, mpsa_known_names */
frappe.provide("mpsa_petty_cash_phone");

mpsa_petty_cash_phone.METHOD = "mpsa_suite.money_out.petty_cash";
mpsa_petty_cash_phone.kes = (value) => format_currency(value || 0, "KES", 2);

frappe.ui.form.on("M-Pesa Petty Cash Phone", {
	setup(frm) {
		frm.set_query("petty_cash_account", () => ({
			filters: { company: frm.doc.company, account_type: "Cash", is_group: 0 },
		}));
		frm.set_query("charges_account", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("default_cost_center", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("holder", () => ({ filters: { company: frm.doc.company, status: "Active" } }));
		// cancelling the phone (to amend its number) never offers to cancel its payouts and entries
		frm.ignore_doctypes_on_cancel_all = [
			"B2C Payment Disbursement",
			"B2C Disbursement Request",
			"Payment Entry",
			"Journal Entry",
		];
	},

	refresh(frm) {
		mpsa_petty_cash_phone.badge(frm);
		if (frm.is_new()) return;
		frappe
			.xcall(`${mpsa_petty_cash_phone.METHOD}.phone_state`, { name: frm.doc.name })
			.then((state) => mpsa_petty_cash_phone.show(frm, state || {}));
	},

	phone(frm) {
		mpsa_petty_cash_phone.badge(frm);
	},

	expected_name(frm) {
		mpsa_petty_cash_phone.badge(frm);
	},
});

// the name M-Pesa gave the number before (mpsa_known_names.js), against the name it should show
mpsa_petty_cash_phone.badge = (frm) => {
	if (typeof mpsa_known_names === "undefined") return;
	mpsa_known_names.show(frm, "phone", {
		type: "Phone",
		number: frm.doc.phone,
		typed_name: frm.doc.mpesa_name || frm.doc.expected_name,
	});
};

mpsa_petty_cash_phone.show = (frm, state) => {
	const e = frappe.utils.escape_html;
	const kes = mpsa_petty_cash_phone.kes;
	if (frm.doc.docstatus === 0) {
		// Approve replaces Submit: the server refuses every other submit (petty_cash.check_approval)
		if (!frm.is_dirty() && frm.toolbar && frm.toolbar.current_status === "Submit") frm.page.clear_primary_action();
		frm.dashboard.set_headline(
			__("Not approved yet: an M-Pesa Manager or Accounts Manager other than {0} and the holder approves it.", [
				e(frm.doc.owner),
			]),
			"orange"
		);
		if (state.can_approve && !frm.is_dirty()) {
			frm.page.set_primary_action(__("Approve"), () => mpsa_petty_cash_phone.approve(frm));
		}
		return;
	}
	if (frm.doc.docstatus !== 1) return;
	let text = __("Book balance {0}", [kes(state.book_balance)]);
	let colour = "blue";
	if (state.last_sms) {
		text += " · " + __("last SMS {0} ({1})", [kes(state.last_sms.balance), e(state.last_sms.when || "")]);
		if (state.difference !== null && state.difference !== undefined && Math.abs(state.difference) >= 1) {
			text +=
				" · " +
				__("the books differ by {0}: a spend not recorded, or a typo", [kes(Math.abs(state.difference))]);
			colour = "red";
		}
	}
	if (state.in_flight) text += " · " + __("{0} on its way", [kes(state.in_flight)]);
	if (state.problem) {
		text = e(state.problem) + "<br>" + text;
		colour = "red";
	}
	frm.dashboard.set_headline(text, colour);
	if (state.can_top_up) {
		frm.add_custom_button(__("Top Up by M-Pesa"), () => mpsa_petty_cash_phone.top_up(frm, state), __("M-Pesa"));
	}
	if (state.can_confirm_name) {
		frm.add_custom_button(__("Name Confirmed"), () => mpsa_petty_cash_phone.confirm_name(frm), __("M-Pesa"));
	}
};

mpsa_petty_cash_phone.approve = (frm) => {
	const e = frappe.utils.escape_html;
	const doc = frm.doc;
	const lines = [
		__("Number: {0} ({1})", [e(doc.phone || ""), e(doc.expected_name || __("no name given"))]),
		__("Holder: {0} ({1})", [e(doc.holder_name || doc.holder || ""), e(doc.holder_user || "")]),
		__("Petty cash account: {0}", [e(doc.petty_cash_account || "")]),
		__("Top-up limit {0}, float ceiling {1}", [
			mpsa_petty_cash_phone.kes(doc.top_up_limit),
			mpsa_petty_cash_phone.kes(doc.float_ceiling),
		]),
	];
	frappe.confirm(
		__("Approve this petty cash phone? Once approved, its number, account and name are locked.") +
			"<br><br>" +
			lines.join("<br>"),
		() =>
			frappe
				.xcall(`${mpsa_petty_cash_phone.METHOD}.approve`, { name: doc.name })
				.then(() => frm.reload_doc())
	);
};

mpsa_petty_cash_phone.top_up = (frm, state) => {
	const d = new frappe.ui.Dialog({
		title: __("Top Up by M-Pesa"),
		fields: [
			{
				fieldtype: "HTML",
				options: __(
					"Prepares a B2C payout to {0} from the company's shortcode. An M-Pesa Payer sends it with Send via M-Pesa.",
					[frappe.utils.escape_html(frm.doc.phone || "")]
				),
			},
			{
				fieldname: "amount",
				fieldtype: "Currency",
				label: __("Amount"),
				reqd: 1,
				default: state.suggested_amount,
				description: __("At most {0} per top-up; the phone holds at most {1}.", [
					mpsa_petty_cash_phone.kes(frm.doc.top_up_limit),
					mpsa_petty_cash_phone.kes(frm.doc.float_ceiling),
				]),
			},
			{
				fieldname: "mpesa_settings",
				fieldtype: "Link",
				label: __("Pay From (Shortcode)"),
				options: "Mpesa Settings",
				get_query: () => ({ filters: { use_b2c: 1, company: frm.doc.company } }),
				description: __("Leave empty when the company has one B2C shortcode."),
			},
		],
		primary_action_label: __("Prepare Top-Up"),
		primary_action(values) {
			frappe
				.xcall(`${mpsa_petty_cash_phone.METHOD}.top_up`, {
					phone: frm.doc.name,
					amount: values.amount,
					mpesa_settings: values.mpesa_settings || null,
				})
				.then((r) => {
					d.hide();
					frappe.show_alert({ message: __("Top-up {0} prepared: send it with Send via M-Pesa.", [r.name]), indicator: "green" });
					frappe.set_route("Form", r.doctype, r.name);
				});
		},
	});
	d.show();
};

mpsa_petty_cash_phone.confirm_name = (frm) => {
	const e = frappe.utils.escape_html;
	const d = new frappe.ui.Dialog({
		title: __("Name Confirmed"),
		fields: [
			{
				fieldtype: "HTML",
				options: __("Only after checking that {0} is this phone's number and owner.", [
					"<b>" + e(frm.doc.last_receiver_name || "") + "</b>",
				]),
			},
			{ fieldname: "adopt", fieldtype: "Check", label: __("Use this name for the phone from now on"), default: 1 },
		],
		primary_action_label: __("Confirm"),
		primary_action(values) {
			frappe
				.xcall(`${mpsa_petty_cash_phone.METHOD}.confirm_name`, { name: frm.doc.name, adopt: values.adopt ? 1 : 0 })
				.then(() => {
					d.hide();
					frm.reload_doc();
				});
		},
	});
	d.show();
};
