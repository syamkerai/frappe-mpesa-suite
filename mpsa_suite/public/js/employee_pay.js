// M-Pesa on Expense Claims and Employee Advances (HRMS): the form's headline and pay buttons and the
// list's Pay by M-Pesa, loaded by expense_claim.js, employee_advance.js and their list scripts.
// Only drafts are prepared here; an M-Pesa Payer sends each payout with Send via M-Pesa. Every
// check runs again on the server (mpsa_suite.money_out.employee_documents).
frappe.provide("mpsa_employee_pay");

mpsa_employee_pay.METHOD = "mpsa_suite.money_out.employee_documents";
mpsa_employee_pay.B2C = "B2C Payment Disbursement";
mpsa_employee_pay.B2B = "Mpesa B2B Payment Request";

mpsa_employee_pay.money = (value) => format_currency(value, "KES", 0);

mpsa_employee_pay.link = (doctype, name) =>
	`<a href="${frappe.utils.get_form_link(doctype, name)}">${frappe.utils.escape_html(name)}</a>`;

// ---------------------------------------------------------------- the form
mpsa_employee_pay.refresh = function (frm) {
	const doc = frm.doc;
	const boot = frappe.boot.mpsa || {};
	if (doc.docstatus !== 1 || !(boot.can_prepare || boot.can_pay)) return;
	frappe
		.xcall(`${mpsa_employee_pay.METHOD}.document_status`, { doctype: frm.doctype, name: doc.name })
		.then((r) => {
			if (!r || frm.doc.name !== doc.name) return;
			mpsa_employee_pay.headline(frm, r);
			// what the server would refuse is not offered: who may prepare, a payable document, money left
			if (!boot.can_prepare || !r.can_prepare || r.problem || !(r.amount > 0)) return;
			if (frappe.model.can_create(mpsa_employee_pay.B2C)) {
				frm.add_custom_button(
					__("Pay employee (phone)"),
					() => mpsa_employee_pay.pay_phone(frm, r),
					__("M-Pesa")
				);
			}
			if (frappe.model.can_create(mpsa_employee_pay.B2B)) {
				frm.add_custom_button(
					__("Pay employee (bank / paybill / till)"),
					() => mpsa_employee_pay.pay_b2b(frm, r),
					__("M-Pesa")
				);
			}
		});
};

mpsa_employee_pay.headline = function (frm, r) {
	const money = mpsa_employee_pay.money;
	const e = frappe.utils.escape_html;
	let text = __("M-Pesa: paid {0} · in flight {1} · left {2}", [
		money(r.paid),
		money(r.in_flight),
		money(r.left),
	]);
	if (r.drafts_planned > 0) text += " · " + __("drafts and planned {0}", [money(r.drafts_planned)]);
	// the claim's merchants and casual payees not paid yet: held back from the employee's share
	if (r.payees > 0) text += " · " + __("for payees {0}", [money(r.payees)]);
	const drafts = (r.drafts || []).map((d) => mpsa_employee_pay.link(d.doctype, d.name));
	if (drafts.length) text += "<br>" + __("Waiting to be sent: {0}", [drafts.join(", ")]);
	if (r.problem && r.left > 0) text += "<br>" + e(r.problem);
	const colour = r.problem && r.left > 0 ? "orange" : r.left > 0 ? "blue" : "green";
	frm.dashboard.set_headline(text, colour);
};

mpsa_employee_pay.intro = function (r, where) {
	return `<p>${__("A draft payout of {0} to {1}: what this document still has to pay, in whole shillings.", [
		mpsa_employee_pay.money(r.amount),
		where,
	])}</p><p class="text-muted">${__(
		"Nothing is sent now: an M-Pesa Payer who did not prepare it sends it with Send via M-Pesa."
	)}</p>`;
};

mpsa_employee_pay.pay_from = function (options, label) {
	// one shortcode: no question asked
	return (options || []).length === 1
		? []
		: [{ fieldtype: "Select", fieldname: "mpesa_settings", label, reqd: 1, options: (options || []).join("\n") }];
};

mpsa_employee_pay.open = function (dialog, out) {
	dialog.hide();
	frappe.show_alert({ message: __("{0} prepared", [out.name]), indicator: "green" });
	frappe.set_route("Form", out.doctype, out.name);
};

mpsa_employee_pay.pay_phone = function (frm, r) {
	const e = frappe.utils.escape_html;
	if (!r.phone) {
		frappe.msgprint(
			r.approved_phones_only
				? __(
						"{0} has no approved phone that can be paid now. Add one under Employee > M-Pesa Accounts and have a manager approve it.",
						[e(frm.doc.employee)]
				  )
				: __("{0} has no approved phone and no Cell Number.", [e(frm.doc.employee)])
		);
		return;
	}
	const where = r.phone_approved
		? __("the approved phone {0}", [e(r.phone)])
		: __("the Cell Number {0} (not an approved phone)", [e(r.phone)]);
	const d = new frappe.ui.Dialog({
		title: __("Pay employee (phone)"),
		fields: [
			{ fieldtype: "HTML", fieldname: "intro", options: mpsa_employee_pay.intro(r, where) },
			...mpsa_employee_pay.pay_from(r.b2c_settings, __("Pay From (B2C)")),
		],
		primary_action_label: __("Prepare Payout"),
		primary_action(values) {
			frappe
				.xcall(`${mpsa_employee_pay.METHOD}.prepare_b2c`, {
					doctype: frm.doctype,
					name: frm.doc.name,
					mpesa_settings: values.mpesa_settings || (r.b2c_settings || [])[0],
				})
				.then((out) => mpsa_employee_pay.open(d, out));
		},
	});
	d.show();
};

mpsa_employee_pay.pay_b2b = function (frm, r) {
	const e = frappe.utils.escape_html;
	const accounts = r.b2b_accounts || [];
	if (!accounts.length) {
		frappe.msgprint(
			__(
				"{0} has no approved bank, paybill or till account that can be paid now. Add one under Employee > M-Pesa Accounts and have a manager approve it.",
				[e(frm.doc.employee)]
			)
		);
		return;
	}
	const d = new frappe.ui.Dialog({
		title: __("Pay employee (bank / paybill / till)"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "intro",
				options: mpsa_employee_pay.intro(r, __("the employee's approved account below")),
			},
			{
				fieldtype: "Select",
				fieldname: "employee_mpesa_account",
				label: __("Pay To"),
				reqd: 1,
				options: accounts.map((a) => ({ value: a.name, label: a.label })),
				default: r.b2b_default || (accounts.length === 1 ? accounts[0].name : undefined),
			},
			...mpsa_employee_pay.pay_from(r.b2b_settings, __("Pay From (B2B)")),
		],
		primary_action_label: __("Prepare Payout"),
		primary_action(values) {
			frappe
				.xcall(`${mpsa_employee_pay.METHOD}.prepare_b2b`, {
					doctype: frm.doctype,
					name: frm.doc.name,
					employee_mpesa_account: values.employee_mpesa_account,
					mpesa_settings: values.mpesa_settings || (r.b2b_settings || [])[0],
				})
				.then((out) => mpsa_employee_pay.open(d, out));
		},
	});
	d.show();
};

// ---------------------------------------------------------------- the list (Actions > Pay by M-Pesa)
mpsa_employee_pay.bulk = function (listview, doctype) {
	const names = listview.get_checked_items(true);
	if (!names.length) {
		frappe.msgprint(__("Tick the documents to pay first."));
		return;
	}
	const query = (flag) => () => ({ filters: { [flag]: 1 } });
	const d = new frappe.ui.Dialog({
		title: __("Pay {0} by M-Pesa", [__(doctype)]),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "intro",
				options: `<p>${__(
					"One draft B2C Payment Disbursement for the employees paid by phone, and a draft B2B request for each one paid to a bank account, paybill or till (their default approved account), each for what the document still has to pay. Documents that cannot be paid are listed with the reason."
				)}</p><p class="text-muted">${__(
					"Nothing is sent now: an M-Pesa Payer who did not prepare them sends each payout with Send via M-Pesa."
				)}</p>`,
			},
			{
				fieldtype: "Link",
				fieldname: "b2c_settings",
				label: __("Pay From (B2C)"),
				options: "Mpesa Settings",
				description: __("Leave blank when your company has one B2C shortcode."),
				get_query: query("use_b2c"),
			},
			{
				fieldtype: "Link",
				fieldname: "b2b_settings",
				label: __("Pay From (B2B)"),
				options: "Mpesa Settings",
				description: __("Leave blank when your company has one B2B shortcode."),
				get_query: query("use_b2b"),
			},
		],
		primary_action_label: __("Prepare Payouts"),
		primary_action(values) {
			frappe
				.xcall(`${mpsa_employee_pay.METHOD}.prepare_bulk`, { doctype, names, ...values })
				.then((r) => {
					d.hide();
					mpsa_employee_pay.show_bulk(r);
					listview.refresh();
				});
		},
	});
	d.show();
};

mpsa_employee_pay.show_bulk = function (r) {
	const e = frappe.utils.escape_html;
	const link = mpsa_employee_pay.link;
	const lines = [];
	if (r.b2c) lines.push(__("B2C Payment Disbursement {0}: {1} payouts to phones.", [link(mpsa_employee_pay.B2C, r.b2c), r.b2c_rows]));
	if ((r.b2b || []).length) {
		lines.push(__("B2B requests: {0}.", [r.b2b.map((name) => link(mpsa_employee_pay.B2B, name)).join(", ")]));
	}
	if (!lines.length) lines.push(__("Nothing was prepared."));
	let message = lines.map((line) => `<p>${line}</p>`).join("");
	if ((r.skipped || []).length) {
		message +=
			`<p>${__("Not prepared:")}</p><ul>` +
			r.skipped.map((s) => `<li>${e(s.name)}: ${e(s.reason)}</li>`).join("") +
			"</ul>";
	}
	frappe.msgprint({
		title: __("M-Pesa payouts prepared"),
		message,
		indicator: r.b2c || (r.b2b || []).length ? "green" : "orange",
	});
};
