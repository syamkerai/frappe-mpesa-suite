// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// Tick lines, then Pay Selected: a summary per payee and per shortcode with the balances, and a
// Confirm button that stays disabled for 5 seconds (the server refuses earlier too). Nothing is
// paid automatically, and no line can be sent before its Pay On.
const MPSA_SCHEDULE = "mpsa_suite.money_out.schedule";
const MPSA_DUE_FINANCE = ["System Manager", "M-Pesa Manager", "Accounts Manager", "Accounts User"];
const MPSA_DUE_PAYERS = ["System Manager", "M-Pesa Manager", "M-Pesa Payer"]; // permissions.PAYOUT_ROLES

frappe.query_reports["M-Pesa Payments Due"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "show",
			label: __("Show"),
			fieldtype: "Select",
			options: ["Due today and overdue", "Next 7 days", "In progress", "All"].join("\n"),
			default: "Due today and overdue",
		},
		{
			fieldname: "route",
			label: __("Route"),
			fieldtype: "Select",
			options: ["", "B2B Paybill", "B2B Till", "B2C Phone"].join("\n"),
		},
		{
			fieldname: "pay_from",
			label: __("Pay From"),
			fieldtype: "Link",
			options: "Mpesa Settings",
		},
		{
			fieldname: "schedule",
			label: __("Schedule"),
			fieldtype: "Link",
			options: "M-Pesa Payment Schedule",
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;
		if (column.fieldname === "planned_date" && data.overdue) {
			value += ` <span class="indicator-pill red">${__("Overdue")}</span>`;
		}
		if (column.fieldname === "problem" && data.problem) {
			const colour = { problem: "var(--red-600)", warning: "var(--orange-600)" }[data.problem_kind];
			if (colour) value = `<span style="color: ${colour}">${value}</span>`;
		}
		return value;
	},

	get_datatable_options(options) {
		return Object.assign(options, {
			checkboxColumn: true,
			events: { onCheckRow: () => mpsa_due.show_selected(frappe.query_report) },
		});
	},

	onload(report) {
		if (frappe.user.has_role(MPSA_DUE_PAYERS)) {
			report.page.add_inner_button(__("Pay Selected"), () => mpsa_due.pay(report));
		}
		if (frappe.user.has_role(MPSA_DUE_FINANCE)) {
			report.page.add_inner_button(__("Move Date"), () => mpsa_due.move(report));
			report.page.add_inner_button(__("Plan Again"), () => mpsa_due.plan_again(report));
			report.page.add_inner_button(__("Refresh Balances"), () => mpsa_due.refresh_balances(report));
		}
	},

	after_datatable_render(datatable) {
		// A refresh (Pay Selected, Move Date, Plan Again, a filter, Refresh Balances) redraws every
		// box unticked, but frappe-datatable keeps the old ticks by row number, which would now
		// point at other lines. Clear them, so only what the user sees ticked is paid or moved.
		const rows = datatable && datatable.rowmanager;
		if (rows && typeof rows.checkAll === "function") {
			rows.checkAll(false);
		} else if (rows) {
			rows.checkMap = [];
		}
		mpsa_due.show_selected(frappe.query_report);
	},
};

const mpsa_due = {
	checked(report) {
		const dt = report.datatable;
		if (!dt) return [];
		return dt.rowmanager
			.getCheckedRows()
			.map((i) => report.data[Number(i)])
			.filter((d) => d && d.line);
	},

	kes(value) {
		return format_currency(value || 0, "KES", 0);
	},

	// The "Selected" card and the page indicator follow the ticks.
	show_selected(report) {
		if (!report) return;
		const rows = mpsa_due.checked(report);
		const total = rows.reduce((sum, d) => sum + flt(d.amount), 0);
		if (rows.length) {
			report.page.set_indicator(__("Selected {0} lines: {1}", [rows.length, mpsa_due.kes(total)]), "blue");
		} else {
			report.page.clear_indicator();
		}
		const $cards = report.$summary ? report.$summary.find(".summary-item") : $();
		$cards.each(function () {
			if ($(this).find(".summary-label").text().trim() === __("Selected")) {
				$(this).find(".summary-value").text(mpsa_due.kes(total));
			}
		});
	},

	async pay(report) {
		const rows = mpsa_due.checked(report);
		if (!rows.length) {
			frappe.msgprint(__("Tick the lines to pay first."));
			return;
		}
		const r = await frappe.xcall(`${MPSA_SCHEDULE}.prepare_payment`, { lines: rows.map((d) => d.line) });
		mpsa_due.confirm(report, r);
	},

	summary_html(r) {
		const e = frappe.utils.escape_html;
		const kes = mpsa_due.kes;
		let html = "";
		if (r.count) {
			html += `<p class="text-muted">${__(
				"Read this before you confirm. Nothing is sent until you press Confirm; then the payments go to M-Pesa one at a time and this report shows how each one went."
			)}</p>`;
			html += `<h5>${__("Who is paid")}</h5><table class="table table-bordered table-sm"><thead><tr>
				<th>${__("Payee")}</th><th>${__("Pays To")}</th><th class="text-right">${__("Lines")}</th>
				<th class="text-right">${__("Amount")}</th></tr></thead><tbody>`;
			for (const p of r.parties) {
				// the supplier's ID, or a plain note for a number not saved on a supplier
				const note = p.party ? (p.payee && p.payee !== p.party ? p.party : "") : __("not saved on a supplier");
				const who = e(p.payee || p.party || "") + (note ? ` <span class="text-muted">(${e(note)})</span>` : "");
				html += `<tr><td>${who}</td><td><b>${e(p.destination || "")}</b>
					<span class="text-muted small">${e(p.route || "")}</span></td>
					<td class="text-right">${p.lines}</td><td class="text-right">${kes(p.amount)}</td></tr>`;
			}
			html += `</tbody></table>`;
			html += `<h5>${__("Paid from")}</h5><table class="table table-bordered table-sm"><thead><tr>
				<th>${__("Pay From")}</th><th>${__("Route")}</th><th class="text-right">${__("Lines")}</th>
				<th class="text-right">${__("Amount")}</th></tr></thead><tbody>`;
			for (const t of r.routes) {
				html += `<tr><td>${e(t.pay_from || "")} ${t.shortcode ? "(" + e(t.shortcode) + ")" : ""}</td>
					<td>${e(t.route || "")}</td><td class="text-right">${t.lines}</td>
					<td class="text-right">${kes(t.amount)}</td></tr>`;
			}
			html += `</tbody></table>`;
			html += `<h3 class="text-right">${__("Total: {0} in {1} payments", [kes(r.total), r.count])}</h3>`;
			for (const b of r.balances || []) {
				let text;
				if (b.amount === null || b.amount === undefined) {
					text = __("{0} {1}: no balance known yet. Press Refresh Balances.", [e(b.pay_from), e(b.sub_account)]);
				} else {
					text = __("{0} {1}: about {2} available", [e(b.pay_from), e(b.sub_account), kes(b.after)]);
					if (b.pending) text += " " + __("(after {0} still on its way)", [kes(b.pending)]);
					if (b.age) text += ", " + __("balance {0} old", [e(b.age)]);
					text += ". " + __("Selected: {0}.", [kes(b.selected)]);
					if (b.short) text += " <b>" + __("Short by {0}.", [kes(b.short)]) + "</b>";
				}
				const colour = b.short || b.amount === null || b.amount === undefined ? "orange" : "green";
				html += `<p><span class="indicator-pill ${colour}">${__("Approximate")}</span> ${text}</p>`;
			}
		}
		if ((r.warnings || []).length) {
			html += `<h5>${__("Check")}</h5><ul>` + r.warnings.map((w) => `<li style="color: var(--orange-600)">${e(w)}</li>`).join("") + "</ul>";
		}
		if ((r.not_included || []).length) {
			html += `<h5>${__("Not included")}</h5><ul>`;
			for (const n of r.not_included) {
				html += `<li>${e(n.payee || n.line)} ${kes(n.amount)}: ${e(n.reason)}</li>`;
			}
			html += "</ul>";
		}
		return html;
	},

	// Run after the report changed: an empty result renders no table, so after_datatable_render
	// would not clear the "Selected" indicator.
	refresh(report) {
		report.page.clear_indicator();
		report.refresh();
	},

	confirm(report, r) {
		const d = new frappe.ui.Dialog({
			title: r.count ? __("Confirm M-Pesa Payments") : __("Nothing Can Be Paid"),
			size: "large",
			fields: [{ fieldtype: "HTML", fieldname: "summary" }],
		});
		d.fields_dict.summary.$wrapper.html(mpsa_due.summary_html(r));
		// each Pay Selected builds a new dialog: drop the old one once closed
		const drop = () => setTimeout(() => d.$wrapper.remove(), 500);
		if (!r.token) {
			d.set_primary_action(__("Close"), () => d.hide());
			d.onhide = drop;
			d.show();
			return;
		}
		let left = r.delay || 5;
		const label = () =>
			left > 0
				? __("Confirm and Pay {0} ({1})", [mpsa_due.kes(r.total), left])
				: __("Confirm and Pay {0}", [mpsa_due.kes(r.total)]);
		let sending = false;
		d.set_primary_action(label(), async () => {
			if (left > 0 || sending) return;
			sending = true;
			d.get_primary_btn().prop("disabled", true);
			try {
				const res = await frappe.xcall(`${MPSA_SCHEDULE}.confirm_payment`, { token: r.token });
				d.hide();
				mpsa_due.show_result(res);
				mpsa_due.refresh(report);
			} catch (err) {
				sending = false;
				d.get_primary_btn().prop("disabled", false);
			}
		});
		const btn = d.get_primary_btn();
		btn.prop("disabled", true).text(label());
		// Forced reading time: Confirm stays disabled, with a visible countdown.
		const timer = setInterval(() => {
			left -= 1;
			btn.text(label());
			if (left <= 0) {
				clearInterval(timer);
				btn.prop("disabled", false);
			}
		}, 1000);
		d.onhide = () => {
			clearInterval(timer);
			drop();
		};
		d.show();
	},

	show_result(res) {
		const e = frappe.utils.escape_html;
		let html = "";
		if (res.sent.length) {
			html += `<p>${__("{0} payments ({1}) are being sent to M-Pesa, one at a time:", [res.sent.length, mpsa_due.kes(res.total)])}</p><ul>`;
			html += res.sent.map((s) => `<li>${e(s.payee)} ${mpsa_due.kes(s.amount)}: ${e(s.request)}</li>`).join("");
			html += "</ul>";
		}
		if (res.refused.length) {
			html += `<p style="color: var(--red-600)">${__("Not paid:")}</p><ul>`;
			html += res.refused.map((s) => `<li>${e(s.payee || s.line)} ${mpsa_due.kes(s.amount)}: ${e(s.reason)}</li>`).join("");
			html += "</ul>";
		}
		frappe.msgprint({ title: __("M-Pesa Payments"), message: html, indicator: res.refused.length ? "orange" : "green" });
	},

	move(report) {
		const rows = mpsa_due.checked(report);
		if (!rows.length) {
			frappe.msgprint(__("Tick the lines to move first."));
			return;
		}
		const total = rows.reduce((sum, d) => sum + flt(d.amount), 0);
		const d = new frappe.ui.Dialog({
			title: __("Move Pay On"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${__("{0} lines, {1}. Only planned lines can be moved, to today or later.", [
						rows.length,
						mpsa_due.kes(total),
					])}</p>`,
				},
				{ fieldname: "date", fieldtype: "Date", label: __("New Pay On"), reqd: 1, default: frappe.datetime.get_today() },
			],
			primary_action_label: __("Move"),
			primary_action: async (values) => {
				const res = await frappe.xcall(`${MPSA_SCHEDULE}.move_date`, {
					lines: rows.map((r) => r.line),
					date: values.date,
				});
				d.hide();
				frappe.show_alert({ message: __("{0} lines moved", [res.moved]), indicator: "green" });
				mpsa_due.refresh(report);
			},
		});
		d.show();
	},

	async plan_again(report) {
		const rows = mpsa_due.checked(report).filter((d) => d.status === "Failed");
		if (!rows.length) {
			frappe.msgprint(__("Tick the failed lines to plan again (choose Show: All)."));
			return;
		}
		frappe.confirm(__("Plan {0} failed lines again? They are paid only when someone presses Pay Selected.", [rows.length]), async () => {
			for (const row of rows) {
				await frappe.xcall(`${MPSA_SCHEDULE}.plan_again`, { line: row.line });
			}
			frappe.show_alert({ message: __("{0} lines planned again", [rows.length]), indicator: "green" });
			mpsa_due.refresh(report);
		});
	},

	async refresh_balances(report) {
		const names = new Set((report.data || []).map((d) => d.pay_from).filter(Boolean));
		const chosen = report.get_filter_value("pay_from");
		if (chosen) names.add(chosen);
		if (!names.size) {
			frappe.msgprint(__("Pick Pay From, or show some lines, to know which balances to ask for."));
			return;
		}
		for (const name of names) {
			await frappe.xcall("mpsa_suite.mpsa_suite.api.m_pesa_api.get_account_balance", { name });
		}
		frappe.show_alert({
			message: __("Asked Safaricom for the balances. They arrive in a few seconds; the report refreshes in 10 seconds."),
			indicator: "blue",
		});
		setTimeout(() => mpsa_due.refresh(report), 10000);
	},
};
