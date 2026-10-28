// Expense Claim list (HRMS): Pay Casual Payees by M-Pesa, one draft B2C batch for the phone payees of
// the ticked claims (money_out.claim_payees.pay_claims). An M-Pesa Payer sends it with Send via M-Pesa
// (with maker-checker on, one who did not prepare it).
//
// Frappe loads this after HRMS's own expense_claim_list.js and the suite's expense_claim_list.js:
// it extends frappe.listview_settings["Expense Claim"] (HRMS's onload and the others run first),
// never replaces it.
(() => {
	const key = "Expense Claim";
	const all = frappe.listview_settings;
	all[key] = all[key] || {};
	const settings = all[key];
	const earlier_onload = settings.onload;
	settings.onload = function (listview) {
		if (earlier_onload) earlier_onload.apply(this, arguments);
		if (!(frappe.boot.mpsa && frappe.boot.mpsa.can_prepare)) return;
		listview.page.add_actions_menu_item(__("Pay Casual Payees by M-Pesa"), () => mpsa_pay_casual_payees(listview), false);
	};
})();

function mpsa_pay_casual_payees(listview) {
	const maker_checker = Boolean((frappe.boot.mpsa || {}).maker_checker);
	const claims = listview.get_checked_items(true);
	if (!claims.length) {
		frappe.msgprint(__("Tick the Expense Claims whose casual payees to pay."));
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __("Pay Casual Payees by M-Pesa"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "intro",
				options: `<p>${__(
					"One draft B2C batch for the phone payees of the {0} ticked claims that are not paid yet (or failed). The claims must be approved, of one company and one payable account.",
					[claims.length]
				)} ${
					maker_checker
						? __("Nothing is sent now: an M-Pesa Payer who did not prepare it sends it with Send via M-Pesa.")
						: __("Nothing is sent now: an M-Pesa Payer sends it with Send via M-Pesa.")
				}</p>`,
			},
			{
				fieldtype: "Link",
				fieldname: "b2c_settings",
				label: __("Pay From (B2C)"),
				options: "Mpesa Settings",
				description: __("Leave empty when the company has one B2C shortcode."),
				get_query: () => ({ filters: { use_b2c: 1 } }),
			},
		],
		primary_action_label: __("Prepare the Batch"),
		primary_action(values) {
			frappe
				.xcall("mpsa_suite.money_out.claim_payees.pay_claims", { claims, b2c_settings: values.b2c_settings || null })
				.then((r) => {
					d.hide();
					frappe.msgprint({
						title: __("M-Pesa Batch Prepared"),
						indicator: "green",
						message:
							`<p>${__("{0} payees ({1}) are ready to send in {2}.", [
								r.lines,
								format_currency(r.total, "KES", 0),
								frappe.utils.get_form_link("B2C Payment Disbursement", r.b2c, true),
							])}</p>` +
							`<p>${
								maker_checker
									? __("An M-Pesa Payer who did not prepare it sends it with Send via M-Pesa.")
									: __("An M-Pesa Payer sends it with Send via M-Pesa.")
							}</p>`,
					});
					listview.refresh();
				});
		},
	});
	d.show();
}
