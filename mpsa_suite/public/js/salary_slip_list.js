// Salary Slip list (HRMS): Actions > Pay Selected Slips by M-Pesa, ONE M-Pesa Payment Schedule for
// the ticked slips, each to its employee's default approved M-Pesa account (a phone by B2C, a bank
// account, paybill or till by B2B). Slips that cannot be paid are listed with the reason. Offered
// to who may prepare a payment and see salaries (frappe.boot.mpsa); the server checks again
// (money_out.payroll.from_salary_slips). Another person pays the lines with Pay Selected.
//
// Frappe loads this after HRMS's own salary_slip_list.js: its settings are extended here (its
// onload runs first), never replaced.
(function () {
	const lists = frappe.listview_settings;
	lists["Salary Slip"] = lists["Salary Slip"] || {};
	const settings = lists["Salary Slip"];
	settings.add_fields = [...new Set([...(settings.add_fields || []), "company"])];
	const hrms_onload = settings.onload;

	settings.onload = function (listview) {
		if (hrms_onload) hrms_onload.apply(this, arguments);
		const flags = frappe.boot.mpsa || {};
		if (!flags.can_prepare || !flags.can_see_salaries || !frappe.model.can_create("M-Pesa Payment Schedule")) {
			return;
		}
		listview.page.add_actions_menu_item(__("Pay Selected Slips by M-Pesa"), () => pay(listview), false);
	};

	function pay(listview) {
		const e = frappe.utils.escape_html;
		const slips = listview.get_checked_items();
		if (!slips.length) {
			frappe.msgprint(__("Tick the salary slips to pay."));
			return;
		}
		const companies = [...new Set(slips.map((s) => s.company).filter(Boolean))];
		const company = companies.length === 1 ? companies[0] : null;
		const filters = (api) => ({ filters: company ? { [api]: 1, company } : { [api]: 1 } });
		const d = new frappe.ui.Dialog({
			title: __("Pay {0} Salary Slips by M-Pesa", [slips.length]),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${__(
						"One M-Pesa Payment Schedule with a line per slip, for what it still has to pay in whole shillings, to the employee's default approved M-Pesa account: a bank account, paybill or till by B2B, a phone by B2C. Slips that cannot be paid are listed with the reason. Another person pays the lines with Pay Selected in M-Pesa Payments Due."
					)}</p>`,
				},
				{ fieldtype: "Date", fieldname: "planned_date", label: __("Pay On"), reqd: 1, default: frappe.datetime.get_today() },
				{
					fieldtype: "Link",
					fieldname: "b2b_settings",
					label: __("Pay From (B2B)"),
					options: "Mpesa Settings",
					description: __("For employees paid to a bank account, paybill or till."),
					get_query: () => filters("use_b2b"),
				},
				{
					fieldtype: "Link",
					fieldname: "b2c_settings",
					label: __("Pay From (B2C)"),
					options: "Mpesa Settings",
					description: __("For employees paid to their M-Pesa phone."),
					get_query: () => filters("use_b2c"),
				},
			],
			primary_action_label: __("Create Schedule"),
			primary_action(values) {
				frappe
					.xcall("mpsa_suite.money_out.payroll.from_salary_slips", {
						slips: slips.map((s) => s.name),
						...values,
					})
					.then((r) => {
						d.hide();
						const link = `<a href="/app/m-pesa-payment-schedule/${encodeURIComponent(r.name)}">${e(r.name)}</a>`;
						let message = __("{0} salaries ({1}) are planned on {2}.", [
							r.lines,
							format_currency(r.total, "KES", 0),
							link,
						]);
						if ((r.excluded || []).length) {
							message +=
								`<p>${__("Not included:")}</p><ul>` + r.excluded.map((x) => `<li>${e(x)}</li>`).join("") + "</ul>";
						}
						frappe.msgprint({
							title: __("M-Pesa Payment Schedule"),
							message,
							indicator: (r.excluded || []).length ? "orange" : "green",
						});
						listview.refresh();
					});
			},
		});
		d.show();
	}
})();
