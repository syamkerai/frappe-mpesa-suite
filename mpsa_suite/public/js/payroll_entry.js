// Payroll Entry (HRMS): pay the salaries by M-Pesa through one M-Pesa Payment Schedule. Each
// employee's default approved Employee M-Pesa Account decides the route: a bank paybill by B2B,
// a phone by B2C. Accounts prepare it; an M-Pesa Payer pays it on its day with Pay Selected.
frappe.ui.form.on("Payroll Entry", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || !frappe.model.can_read("M-Pesa Payment Schedule")) return;
		frappe
			.xcall("mpsa_suite.money_out.payroll.payroll_status", { payroll_entry: frm.doc.name })
			.then((r) => {
				if (!r) return;
				if (r.schedule) {
					frm.add_custom_button(
						__("M-Pesa Payment Schedule"),
						() => frappe.set_route("Form", "M-Pesa Payment Schedule", r.schedule),
						__("M-Pesa")
					);
					frm.add_custom_button(
						__("Pay Due Salaries"),
						() =>
							frappe.set_route("query-report", "M-Pesa Payments Due", {
								company: frm.doc.company,
								schedule: r.schedule,
								show: "Due today and overdue",
							}),
						__("M-Pesa")
					);
					return;
				}
				if (!frappe.model.can_create("M-Pesa Payment Schedule")) return;
				frm.add_custom_button(__("Create M-Pesa Payment Schedule"), () => mpsa_payroll.create(frm, r), __("M-Pesa"));
			});
	},
});

const mpsa_payroll = {
	create(frm, status) {
		const e = frappe.utils.escape_html;
		const d = new frappe.ui.Dialog({
			title: __("Pay {0} by M-Pesa", [frm.doc.name]),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options:
						`<p>${__(
							"One line per submitted salary slip, for what it still has to pay in whole shillings, to the employee's default approved M-Pesa account: a bank paybill by B2B, a phone by B2C. Employees who cannot be paid that way are listed on the schedule with the reason."
						)}</p>` +
						((status.bank_entries || []).length
							? `<p style="color: var(--red-600)">${__("This payroll already has a bank entry: {0}", [
									e(status.bank_entries.join(", ")),
							  ])}</p>`
							: ""),
				},
				{ fieldtype: "Date", fieldname: "planned_date", label: __("Pay On"), reqd: 1, default: frappe.datetime.get_today() },
				{
					fieldtype: "Link",
					fieldname: "b2b_settings",
					label: __("Pay From (B2B)"),
					options: "Mpesa Settings",
					description: __("For employees paid to a bank paybill."),
					get_query: () => ({ filters: { use_b2b: 1, company: frm.doc.company } }),
				},
				{
					fieldtype: "Link",
					fieldname: "b2c_settings",
					label: __("Pay From (B2C)"),
					options: "Mpesa Settings",
					description: __("For employees paid to their M-Pesa phone."),
					get_query: () => ({ filters: { use_b2c: 1, company: frm.doc.company } }),
				},
			],
			primary_action_label: __("Create Schedule"),
			primary_action(values) {
				frappe
					.xcall("mpsa_suite.money_out.schedule.from_payroll_entry", {
						payroll_entry: frm.doc.name,
						...values,
					})
					.then((r) => {
						d.hide();
						let message = __("{0} salaries ({1}) are planned on {2}.", [
							r.lines,
							format_currency(r.total, "KES", 0),
							e(r.name),
						]);
						if ((r.excluded || []).length) {
							message +=
								`<p>${__("Not included:")}</p><ul>` + r.excluded.map((x) => `<li>${e(x)}</li>`).join("") + "</ul>";
						}
						frappe.msgprint({ title: __("M-Pesa Payment Schedule"), message, indicator: "green" });
						frm.reload_doc();
					});
			},
		});
		d.show();
	},
};

// Round 4: what M-Pesa paid of this payroll's salary slips, has in flight and has planned, and what
// is left, for who may see salaries (money_out.payroll.payroll_salary_totals checks again). Its own
// refresh handler, so it shows for HR, who may not read the payment schedule.
frappe.ui.form.on("Payroll Entry", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || !(frappe.boot.mpsa || {}).can_see_salaries) return;
		frappe
			.xcall("mpsa_suite.money_out.payroll.payroll_salary_totals", { payroll_entry: frm.doc.name })
			.then((r) => {
				// HRMS's own headline (slips that failed) comes first
				if (!r || !r.slips || frm.doc.status === "Failed") return;
				const kes = (value) => format_currency(value || 0, "KES");
				const parts = [
					__("paid {0}", [kes(r.paid)]),
					__("in flight {0}", [kes(r.in_flight)]),
					__("planned {0}", [kes(r.planned)]),
					__("left {0} ({1} of {2} slips)", [kes(r.left), r.slips_left, r.slips]),
				];
				if (flt(r.other_paid) > 0) parts.push(__("paid outside M-Pesa {0}", [kes(r.other_paid)]));
				// every schedule naming its slips: the payroll's own, one-off salaries, selected slips;
				// links only for who may read the schedule (not HR), plain names for the rest
				const can_open = frappe.model.can_read("M-Pesa Payment Schedule");
				const links = (r.schedules || []).map((name) =>
					can_open
						? `<a href="/app/m-pesa-payment-schedule/${encodeURIComponent(name)}">${frappe.utils.escape_html(name)}</a>`
						: frappe.utils.escape_html(name)
				);
				let html = `${__("M-Pesa")}: ${parts.join(" · ")}`;
				if (links.length) html += `<br>${__("Schedules: {0}", [links.join(", ")])}`;
				frm.dashboard.set_headline(html, flt(r.left) > 0 ? "blue" : "green");
			});
	},
});
