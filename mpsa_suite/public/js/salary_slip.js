// M-Pesa on a Salary Slip (HRMS), for who may see salaries (frappe.boot.mpsa): a headline of what
// M-Pesa paid, has in flight and has planned, and what is left; and Pay by M-Pesa, a one-off or
// off-cycle salary as a one-line M-Pesa Payment Schedule to any of the employee's approved
// accounts (the default preselected): a phone by B2C, a bank account, paybill or till by B2B.
// Paying it stays Pay Selected in M-Pesa Payments Due, by another person. The server checks every
// rule again (money_out.payroll).
frappe.ui.form.on("Salary Slip", {
	refresh(frm) {
		const flags = frappe.boot.mpsa || {};
		if (frm.doc.docstatus !== 1 || !flags.can_see_salaries) return;
		frappe
			.xcall("mpsa_suite.money_out.payroll.salary_slip_status", { salary_slip: frm.doc.name })
			.then((r) => {
				if (!r) return;
				mpsa_salary_slip.headline(frm, r);
				if (flags.can_prepare && r.can_prepare && r.payable >= 1 && !(r.problems || []).length) {
					frm.add_custom_button(__("Pay by M-Pesa"), () => mpsa_salary_slip.pay(frm, r), __("M-Pesa"));
				}
				// HR sees the headline but may not read the schedule: its names stay text there
				const can_open = frappe.model.can_read("M-Pesa Payment Schedule");
				for (const name of can_open ? r.schedules || [] : []) {
					frm.add_custom_button(
						__("Schedule {0}", [name]),
						() => frappe.set_route("Form", "M-Pesa Payment Schedule", name),
						__("M-Pesa")
					);
				}
			});
	},
});

const mpsa_salary_slip = {
	B2C: "B2C Phone",

	kes: (value) => format_currency(value || 0, "KES"),

	headline(frm, r) {
		const e = frappe.utils.escape_html;
		const kes = mpsa_salary_slip.kes;
		const planned = (r.schedules || []).length ? ` (${r.schedules.map(e).join(", ")})` : "";
		const parts = [
			__("paid {0}", [kes(r.paid)]),
			__("in flight {0}", [kes(r.in_flight)]),
			__("planned {0}", [kes(r.planned)]) + planned,
			__("left {0}", [kes(r.left)]),
		];
		if (flt(r.other_paid) > 0) parts.push(__("paid outside M-Pesa {0}", [kes(r.other_paid)]));
		let html = `${__("M-Pesa")}: ${parts.join(" · ")}`;
		const problems = r.problems || [];
		if (problems.length) html += "<br>" + problems.map(e).join("<br>");
		frm.dashboard.set_headline(html, problems.length ? "orange" : flt(r.left) > 0 ? "blue" : "green");
	},

	pay(frm, r) {
		const e = frappe.utils.escape_html;
		const accounts = r.accounts || [];
		if (!accounts.length) {
			frappe.msgprint(
				__(
					"{0} has no approved M-Pesa account: add one under Employee > M-Pesa Accounts and have a manager approve it.",
					[e(frm.doc.employee)]
				)
			);
			return;
		}
		const by_name = Object.fromEntries(accounts.map((a) => [a.name, a]));
		const is_b2c = (name) => (by_name[name] || {}).route === mpsa_salary_slip.B2C;
		const label = (a) =>
			`${a.account_type}: ${a.destination}` +
			(a.is_default ? ` (${__("default")})` : "") +
			(a.problem ? ` · ${a.problem}` : "");
		const only = (names) => ((names || []).length === 1 ? names[0] : "");
		const first = r.default_account || accounts[0].name;
		let d;
		// Pay From follows the account's route: B2C for a phone, B2B for the rest
		function toggle(name) {
			if (!d) return;
			const b2c = is_b2c(name || d.get_value("payee_account"));
			d.set_df_property("b2b_settings", "hidden", b2c ? 1 : 0);
			d.set_df_property("b2b_settings", "reqd", b2c ? 0 : 1);
			d.set_df_property("b2c_settings", "hidden", b2c ? 0 : 1);
			d.set_df_property("b2c_settings", "reqd", b2c ? 1 : 0);
		}
		d = new frappe.ui.Dialog({
			title: __("Pay {0} by M-Pesa", [frm.doc.name]),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${__(
						"A one-line M-Pesa Payment Schedule for what this slip has left, {0} in whole shillings. Another person pays it with Pay Selected in M-Pesa Payments Due, not before its Pay On.",
						[mpsa_salary_slip.kes(r.payable)]
					)}</p>`,
				},
				{
					fieldtype: "Select",
					fieldname: "payee_account",
					label: __("Pay To"),
					reqd: 1,
					options: accounts.map((a) => ({ label: label(a), value: a.name })),
					default: first,
					onchange: () => toggle(),
				},
				{ fieldtype: "Date", fieldname: "planned_date", label: __("Pay On"), reqd: 1, default: frappe.datetime.get_today() },
				{
					fieldtype: "Link",
					fieldname: "b2b_settings",
					label: __("Pay From (B2B)"),
					options: "Mpesa Settings",
					default: only(r.b2b_settings),
					description: __("For a bank account, paybill or till."),
					get_query: () => ({ filters: { use_b2b: 1, company: frm.doc.company } }),
				},
				{
					fieldtype: "Link",
					fieldname: "b2c_settings",
					label: __("Pay From (B2C)"),
					options: "Mpesa Settings",
					default: only(r.b2c_settings),
					description: __("For an M-Pesa phone."),
					get_query: () => ({ filters: { use_b2c: 1, company: frm.doc.company } }),
				},
			],
			primary_action_label: __("Create Schedule"),
			primary_action(values) {
				const b2c = is_b2c(values.payee_account);
				frappe
					.xcall("mpsa_suite.money_out.payroll.from_salary_slip", {
						slip: frm.doc.name,
						payee_account: values.payee_account,
						planned_date: values.planned_date,
						b2b_settings: b2c ? null : values.b2b_settings,
						b2c_settings: b2c ? values.b2c_settings : null,
					})
					.then((answer) => {
						d.hide();
						frappe.show_alert({
							message: __("{0} planned on {1}", [mpsa_salary_slip.kes(answer.amount), e(answer.name)]),
							indicator: "green",
						});
						frappe.set_route("Form", "M-Pesa Payment Schedule", answer.name);
					});
			},
		});
		d.show();
		toggle(first);
	},
};
