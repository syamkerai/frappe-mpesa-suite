// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

const MPSA_SCHEDULE_API = "mpsa_suite.money_out.schedule";
const MPSA_SCHEDULE_ORDER_CLOSED = ["On Hold", "Closed", "Completed"];
// the approved-account DocType of each party type
const MPSA_PARTY_ACCOUNT = { Supplier: "Supplier M-Pesa Account", Employee: "Employee M-Pesa Account" };
const MPSA_ACCOUNTS_FOR = {
	Supplier: ["mpsa_suite.mpsa_suite.doctype.supplier_m_pesa_account.supplier_m_pesa_account.accounts_for_supplier", "supplier"],
	Employee: ["mpsa_suite.mpsa_suite.doctype.employee_m_pesa_account.employee_m_pesa_account.accounts_for_employee", "employee"],
};

frappe.ui.form.on("M-Pesa Payment Schedule", {
	setup(frm) {
		frm.set_query("b2b_settings", () => ({
			filters: { use_b2b: 1, ...(frm.doc.company ? { company: frm.doc.company } : {}) },
		}));
		frm.set_query("b2c_settings", () => ({
			filters: { use_b2c: 1, ...(frm.doc.company ? { company: frm.doc.company } : {}) },
		}));
		frm.set_query("payee_account", "lines", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			const field = row.party_type === "Employee" ? "employee" : "supplier";
			return { filters: { docstatus: 1, ...(row.party ? { [field]: row.party } : {}) } };
		});
		frm.set_query("reference_name", "lines", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			const filters = { docstatus: 1, company: frm.doc.company };
			if (row.reference_doctype === "Salary Slip") {
				if (row.party) filters.employee = row.party;
				filters.status = ["!=", "Withheld"];
				return { filters };
			}
			if (row.party) filters.supplier = row.party;
			if (row.reference_doctype === "Purchase Invoice") filters.outstanding_amount = [">", 0];
			if (row.reference_doctype === "Purchase Order") {
				filters.per_billed = 0;
				filters.status = ["not in", MPSA_SCHEDULE_ORDER_CLOSED];
			}
			return { filters };
		});
	},

	refresh(frm) {
		if (frm.is_new()) return;
		const lines = frm.doc.lines || [];
		const today = frappe.datetime.get_today();
		const due = lines.filter((l) => l.status === "Planned" && l.planned_date <= today);
		if (due.length) {
			const total = due.reduce((sum, l) => sum + flt(l.amount), 0);
			frm.dashboard.set_headline(
				__("{0} lines are due ({1}). An M-Pesa Payer pays them with Pay Due Lines.", [
					due.length,
					format_currency(total, "KES", 0),
				]),
				"orange"
			);
		}
		frm.add_custom_button(__("Pay Due Lines"), () =>
			frappe.set_route("query-report", "M-Pesa Payments Due", {
				company: frm.doc.company,
				schedule: frm.doc.name,
				show: "Due today and overdue",
			})
		);
		if (frm.doc.closed || !frappe.model.can_write("M-Pesa Payment Schedule")) return;
		frm.add_custom_button(__("Move Date"), () => mpsa_schedule.move(frm), __("Lines"));
		frm.add_custom_button(__("Plan Again"), () => mpsa_schedule.plan_again(frm), __("Lines"));
		frm.add_custom_button(__("Close Schedule"), () => mpsa_schedule.close(frm), __("Lines"));
	},

	company(frm) {
		if (frm.doc.b2b_settings) frm.set_value("b2b_settings", null);
		if (frm.doc.b2c_settings) frm.set_value("b2c_settings", null);
	},
});

frappe.ui.form.on("M-Pesa Payment Schedule Line", {
	lines_add(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.planned_date) frappe.model.set_value(cdt, cdn, "planned_date", frappe.datetime.get_today());
		if (MPSA_PARTY_ACCOUNT[row.party_type]) {
			frappe.model.set_value(cdt, cdn, "payee_account_doctype", MPSA_PARTY_ACCOUNT[row.party_type]);
		}
	},

	party_type(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, "payee_account_doctype", MPSA_PARTY_ACCOUNT[row.party_type] || null);
		frappe.model.set_value(cdt, cdn, "party", null);
		// a salary pays its Salary Slip (HRMS sites only)
		const hrms = frappe.boot && frappe.boot.versions && frappe.boot.versions.hrms;
		if (row.party_type === "Employee" && hrms) frappe.model.set_value(cdt, cdn, "reference_doctype", "Salary Slip");
	},

	async party(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, "payee_account", null);
		const how = MPSA_ACCOUNTS_FOR[row.party_type];
		if (!row.party || !how) return;
		frappe.model.set_value(cdt, cdn, "payee_account_doctype", MPSA_PARTY_ACCOUNT[row.party_type]);
		// the default approved account, or the only one (the server checks again on save)
		const accounts = await frappe.xcall(how[0], { [how[1]]: row.party });
		const approved = (accounts || []).filter((a) => a.docstatus === 1);
		const pick = approved.find((a) => a.is_default) || (approved.length === 1 ? approved[0] : null);
		if (pick) frappe.model.set_value(cdt, cdn, "payee_account", pick.name);
	},

	reference_doctype(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "reference_name", null);
	},

	async reference_name(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.reference_doctype || !row.reference_name) return;
		const slip = row.reference_doctype === "Salary Slip";
		const r = slip
			? await frappe.xcall("mpsa_suite.money_out.b2b.salary_left", { salary_slip: row.reference_name })
			: await frappe.xcall("mpsa_suite.money_out.b2b.amount_left", {
					doctype: row.reference_doctype,
					name: row.reference_name,
			  });
		if (!r) return;
		const payee = slip ? r.employee : r.supplier;
		if (!row.party && payee) await frappe.model.set_value(cdt, cdn, "party", payee);
		if (!flt(row.amount)) frappe.model.set_value(cdt, cdn, "amount", Math.floor(r.left));
		const stops = [...(r.problems || []), ...(r.supplier_hold ? [r.supplier_hold] : [])];
		frappe.show_alert({
			message: stops.length
				? stops.map(frappe.utils.escape_html).join("<br>")
				: __("Left to pay on {0}: {1}", [frappe.utils.escape_html(row.reference_name), format_currency(r.left, "KES", 0)]),
			indicator: stops.length ? "red" : "blue",
		});
	},
});

const mpsa_schedule = {
	selected(frm, status) {
		const rows = frm.fields_dict.lines.grid.get_selected_children();
		return rows.filter((r) => r.status === status);
	},

	move(frm) {
		const rows = mpsa_schedule.selected(frm, "Planned");
		if (!rows.length) {
			frappe.msgprint(__("Tick the planned lines to move in the table first."));
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __("Move Pay On"),
			fields: [
				{ fieldname: "date", fieldtype: "Date", label: __("New Pay On"), reqd: 1, default: frappe.datetime.get_today() },
			],
			primary_action_label: __("Move {0} Lines", [rows.length]),
			primary_action: async (values) => {
				if (frm.is_dirty()) await frm.save();
				await frappe.xcall(`${MPSA_SCHEDULE_API}.move_date`, { lines: rows.map((r) => r.name), date: values.date });
				d.hide();
				frm.reload_doc();
			},
		});
		d.show();
	},

	plan_again(frm) {
		const rows = mpsa_schedule.selected(frm, "Failed");
		if (!rows.length) {
			frappe.msgprint(__("Tick the failed lines to plan again in the table first."));
			return;
		}
		frappe.confirm(__("Plan {0} failed lines again? They are paid only when someone presses Pay Selected.", [rows.length]), async () => {
			for (const row of rows) {
				await frappe.xcall(`${MPSA_SCHEDULE_API}.plan_again`, { line: row.name });
			}
			frm.reload_doc();
		});
	},

	close(frm) {
		frappe.confirm(__("Close this schedule? Its planned lines are cancelled; payments already sent carry on."), async () => {
			await frappe.xcall(`${MPSA_SCHEDULE_API}.close_schedule`, { name: frm.doc.name });
			frm.reload_doc();
		});
	},
};
