// Supplier tills on a submitted Purchase Order or Purchase Invoice (round 4.5):
// - Paid from Petty Cash Phone (money_out.petty_cash): paste the M-Pesa SMS the phone got, and a Payment
//   Entry from the phone's petty cash account is booked to the supplier. For the phone's holder and the
//   Accounts roles; the name on the SMS is shown next to the supplier's.
// - Pay via Staff (money_out.pay_via_staff): an Employee Advance to a staff member's approved phone, for the
//   users M-Pesa Suite Settings lists only (the server says who: document_state), and on an invoice Settle
//   Staff Advances.
// The section "Paid from petty cash phone" lists each spend (its Payment Entry, receipt and phone) and the
// staff advances. Every step is checked again on the server.
/* global mpsa_tills, mpsa_known_names */
frappe.provide("mpsa_tills");

mpsa_tills.PETTY = "mpsa_suite.money_out.petty_cash";
mpsa_tills.STAFF = "mpsa_suite.money_out.pay_via_staff";
mpsa_tills.FIELD = "mpesa_tills_html";
mpsa_tills.kes = (value) => format_currency(value || 0, "KES", 2);

mpsa_tills.wanted = (frm) => frm.doc.docstatus === 1 && (frm.doc.currency || "KES") === "KES";

mpsa_tills.refresh = (frm) => {
	const field = frm.fields_dict && frm.fields_dict[mpsa_tills.FIELD];
	if (field && field.$wrapper) field.$wrapper.empty();
	if (!mpsa_tills.wanted(frm)) return;
	const args = { doctype: frm.doctype, name: frm.doc.name };
	const quiet = (method) => frappe.xcall(method, args).catch(() => null);
	Promise.all([quiet(`${mpsa_tills.PETTY}.document_state`), quiet(`${mpsa_tills.STAFF}.document_state`)]).then(
		([petty, staff]) => {
			if (frm.doc.name !== args.name) return; // another document opened meanwhile
			mpsa_tills.render(frm, petty || {}, staff || {});
			if (petty && petty.can_record) {
				frm.add_custom_button(__("Paid from Petty Cash Phone"), () => mpsa_tills.spend(frm, petty), __("M-Pesa"));
			}
			if (staff && staff.allowed) {
				frm.add_custom_button(__("Pay via Staff"), () => mpsa_tills.pay_via_staff(frm, staff), __("M-Pesa"));
			}
			if (staff && staff.can_settle) {
				frm.add_custom_button(__("Settle Staff Advances"), () => mpsa_tills.settle(frm, staff), __("M-Pesa"));
			}
		}
	);
};

mpsa_tills.render = (frm, petty, staff) => {
	const field = frm.fields_dict && frm.fields_dict[mpsa_tills.FIELD];
	if (!field || !field.$wrapper) return;
	const e = frappe.utils.escape_html;
	const link = (doctype, name) =>
		`<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}">${e(name)}</a>`;
	const parts = [];
	const spends = petty.spends || [];
	if (spends.length) {
		const rows = spends
			.map(
				(s) =>
					`<tr><td>${link("Payment Entry", s.payment_entry)}</td><td>${e(s.receipt || "")}</td><td>${e(
						s.date || ""
					)}</td><td>${e(s.payee || "")}</td><td>${link("M-Pesa Petty Cash Phone", s.phone)}</td><td class="text-right">${mpsa_tills.kes(
						s.amount
					)}</td></tr>`
			)
			.join("");
		parts.push(
			`<p class="text-muted small">${__("Paid from petty cash phone")}</p><table class="table table-bordered table-condensed small"><thead><tr><th>${__(
				"Payment Entry"
			)}</th><th>${__("Receipt")}</th><th>${__("Date")}</th><th>${__("Paid To (SMS)")}</th><th>${__(
				"Phone"
			)}</th><th class="text-right">${__("Amount")}</th></tr></thead><tbody>${rows}</tbody></table>`
		);
	}
	const advances = staff.advances || [];
	if (advances.length) {
		const rows = advances
			.map(
				(a) =>
					`<tr><td>${link("Employee Advance", a.name)}</td><td>${e(a.employee_name || a.employee)}</td><td class="text-right">${mpsa_tills.kes(
						a.amount
					)}</td><td class="text-right">${mpsa_tills.kes(a.paid)}</td><td class="text-right">${mpsa_tills.kes(
						a.open
					)}</td></tr>`
			)
			.join("");
		parts.push(
			`<p class="text-muted small">${__("Paid via staff")}</p><table class="table table-bordered table-condensed small"><thead><tr><th>${__(
				"Employee Advance"
			)}</th><th>${__("Staff Member")}</th><th class="text-right">${__("Amount")}</th><th class="text-right">${__(
				"Paid"
			)}</th><th class="text-right">${__("Not Settled")}</th></tr></thead><tbody>${rows}</tbody></table>`
		);
	}
	field.$wrapper.html(parts.join(""));
};

// ---------------------------------------------------------------- Paid from Petty Cash Phone
mpsa_tills.spend = (frm, state) => {
	const e = frappe.utils.escape_html;
	const phones = state.phones || [];
	const d = new frappe.ui.Dialog({
		title: __("Paid from Petty Cash Phone"),
		size: "large",
		fields: [
			{
				fieldname: "phone",
				fieldtype: "Select",
				label: __("Petty Cash Phone"),
				reqd: 1,
				options: phones.map((p) => ({ value: p.name, label: `${p.label} · ${mpsa_tills.kes(p.book_balance)}` })),
				default: state.default_phone || (phones[0] && phones[0].name),
			},
			{
				fieldname: "sms",
				fieldtype: "Small Text",
				label: __("Paste the M-Pesa SMS"),
				description: __('e.g. "UKD1ABC2DE Confirmed. Ksh1,500.00 paid to SHELL KILIMANI. on 13/11/26 at 10:15 AM. New M-PESA balance is Ksh8,500.00…"'),
				onchange: () => mpsa_tills.read_sms(frm, d),
			},
			{ fieldname: "sms_info", fieldtype: "HTML" },
			{ fieldname: "fields_section", fieldtype: "Section Break", label: __("Or type it") },
			{ fieldname: "receipt", fieldtype: "Data", label: __("M-Pesa Receipt") },
			{ fieldname: "amount", fieldtype: "Currency", label: __("Amount") },
			{ fieldname: "payee", fieldtype: "Data", label: __("Paid To (Till, Paybill or Person)") },
			{ fieldname: "column_break_fields", fieldtype: "Column Break" },
			{ fieldname: "sms_date", fieldtype: "Date", label: __("Date") },
			{ fieldname: "sms_time", fieldtype: "Time", label: __("Time") },
			{ fieldname: "balance", fieldtype: "Currency", label: __("New M-Pesa Balance") },
			{
				fieldname: "charge",
				fieldtype: "Currency",
				label: __("Transaction Charge"),
				description: __("A paybill's charge, booked to the phone's charges account."),
			},
			{ fieldname: "screenshot_section", fieldtype: "Section Break" },
			{ fieldname: "screenshot", fieldtype: "Attach", label: __("Screenshot (optional)") },
		],
		primary_action_label: __("Record"),
		primary_action(values) {
			d.get_primary_btn().prop("disabled", true);
			frappe
				.xcall(`${mpsa_tills.PETTY}.record_spend`, Object.assign({ doctype: frm.doctype, name: frm.doc.name }, values))
				.then((r) => {
					d.hide();
					frappe.show_alert({
						message: __("{0} booked: {1} from the phone (book balance now {2}).", [
							e(r.payment_entry),
							mpsa_tills.kes(r.amount + (r.charge || 0)),
							mpsa_tills.kes(r.book_balance),
						]),
						indicator: "green",
					});
					frm.reload_doc();
				})
				.finally(() => d.get_primary_btn().prop("disabled", false));
		},
	});
	d.show();
};

mpsa_tills.read_sms = (frm, d) => {
	const text = (d.get_value("sms") || "").trim();
	const info = d.fields_dict.sms_info.$wrapper;
	if (!text) {
		info.empty();
		return;
	}
	const e = frappe.utils.escape_html;
	frappe
		.xcall(`${mpsa_tills.PETTY}.read_sms`, { doctype: frm.doctype, name: frm.doc.name, text })
		.then((r) => {
			d.set_values({
				receipt: r.receipt,
				amount: r.amount,
				payee: r.payee,
				sms_date: r.date,
				sms_time: r.time,
				balance: r.balance,
				charge: r.charge,
			});
			const colour = r.name_matches ? "--green-600" : "--orange-600";
			let html = `<div class="small" style="color: var(${colour})">${__("SMS: {0} · Supplier: {1}", [
				e(r.payee || ""),
				e(r.supplier_name || r.supplier || ""),
			])}${r.name_matches ? "" : " · " + __("the names differ: check the till")}</div>`;
			if (r.used) html += `<div class="small text-danger">${__("This receipt is on {0} already.", [e(r.used)])}</div>`;
			info.html(html);
			// a person paid by send money: the name M-Pesa gave that number before (mpsa_known_names.js)
			if (r.kind === "Phone" && r.phone && typeof mpsa_known_names !== "undefined") {
				mpsa_known_names
					.fetch([{ id: "sms", type: "Phone", number: r.phone, typed_name: r.payee }])
					.then((answer) => {
						const badge = mpsa_known_names.badge_html(answer.sms);
						if (badge) info.append(`<div class="small">${badge}</div>`);
					});
			}
		})
		.catch(() => info.html(`<div class="small text-danger">${__("The SMS could not be read: type its fields.")}</div>`));
};

// ---------------------------------------------------------------- Pay via Staff
mpsa_tills.pay_via_staff = (frm, state) => {
	const d = new frappe.ui.Dialog({
		title: __("Pay via Staff"),
		fields: [
			{
				fieldtype: "HTML",
				options: __(
					"An Employee Advance to the staff member's approved M-Pesa phone, who pays {0} at the till. Send its payout with Send via M-Pesa.",
					[frappe.utils.escape_html(frm.doc.supplier_name || frm.doc.supplier || "")]
				),
			},
			{
				fieldname: "employee",
				fieldtype: "Link",
				label: __("Staff Member"),
				options: "Employee",
				reqd: 1,
				get_query: () => ({ filters: { company: frm.doc.company, status: "Active" } }),
				onchange: () => mpsa_tills.staff_phone(d),
			},
			{ fieldname: "staff_phone", fieldtype: "HTML" },
			{ fieldname: "amount", fieldtype: "Currency", label: __("Amount"), reqd: 1, default: state.left },
			{ fieldname: "purpose", fieldtype: "Data", label: __("Purpose") },
		],
		primary_action_label: __("Make the Advance"),
		primary_action(values) {
			frappe
				.xcall(`${mpsa_tills.STAFF}.make_advance`, Object.assign({ doctype: frm.doctype, name: frm.doc.name }, values))
				.then((r) => {
					d.hide();
					frappe.show_alert({
						message: __("{0} made; send {1} with Send via M-Pesa.", [r.employee_advance, r.disbursement]),
						indicator: "green",
					});
					frappe.set_route("Form", "B2C Payment Disbursement", r.disbursement);
				});
		},
	});
	d.show();
};

// the staff member's approved phone, where the advance goes, and the name M-Pesa gave it before
mpsa_tills.ACCOUNTS = "mpsa_suite.mpsa_suite.doctype.employee_m_pesa_account.employee_m_pesa_account.accounts_for_employee";
mpsa_tills.staff_phone = (d) => {
	const box = d.fields_dict.staff_phone.$wrapper;
	const employee = d.get_value("employee");
	box.empty();
	if (!employee) return;
	const e = frappe.utils.escape_html;
	frappe
		.xcall(mpsa_tills.ACCOUNTS, { employee })
		.then((rows) => {
			const phones = (rows || []).filter((r) => r.docstatus === 1 && r.account_type === "Phone (B2C)");
			const usable = phones.find((r) => !r.problem && r.is_default) || phones.find((r) => !r.problem);
			if (!usable) {
				box.html(`<div class="small text-danger">${__("{0} has no approved phone that can be paid now: Pay via Staff pays an approved phone only.", [e(employee)])}</div>`);
				return;
			}
			box.html(`<div class="small">${__("Paid to {0} ({1})", [e(usable.phone || ""), e(usable.name)])}</div>`);
			if (typeof mpsa_known_names === "undefined") return;
			mpsa_known_names
				.fetch([{ id: "staff", account_doctype: "Employee M-Pesa Account", account: usable.name }])
				.then((answer) => {
					const badge = mpsa_known_names.badge_html(answer.staff);
					if (badge) box.append(`<div class="small">${badge}</div>`);
				});
		})
		.catch(() => box.empty());
};

mpsa_tills.settle = (frm, state) => {
	frappe.confirm(
		__("Book one Journal Entry settling {0} of staff advances against {1}?", [mpsa_tills.kes(state.to_settle), frm.doc.name]),
		() =>
			frappe.xcall(`${mpsa_tills.STAFF}.settle_now`, { purchase_invoice: frm.doc.name }).then((r) => {
				frappe.show_alert({ message: __("{0} booked.", [r.journal_entry]), indicator: "green" });
				frm.reload_doc();
			})
	);
};

for (const doctype of ["Purchase Order", "Purchase Invoice"]) {
	frappe.ui.form.on(doctype, { refresh: mpsa_tills.refresh });
}
