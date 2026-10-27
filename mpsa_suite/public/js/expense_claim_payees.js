// Expense Claim (HRMS): pay merchants (till, paybill, bank account) and casual payees (phones) straight
// from the claim by M-Pesa, instead of reimbursing the employee who paid them (money_out.claim_payees).
// The claimant enters the lines in draft, the expense approver approves them with the claim, someone who
// prepares payouts presses Pay Merchants and Payees (drafts only), and an M-Pesa Payer who did not
// prepare them sends each with Send via M-Pesa. Buttons show only to who the server accepts; the
// server checks everything again.
const MPSA_PAYEES_API = "mpsa_suite.money_out.claim_payees";
const MPSA_PAYEE_TYPE = {
	till: "Till (Buy Goods)",
	paybill: "Paybill",
	bank: "Bank Account (via Bank Paybill)",
	phone: "Phone (B2C)",
};

frappe.ui.form.on("Expense Claim", {
	setup(frm) {
		frm.set_query("bank_paybill", "mpesa_payees", () => ({ filters: { enabled: 1 } }));
	},

	refresh(frm) {
		mpsa_payees.refresh(frm);
	},
});

frappe.ui.form.on("M-Pesa Claim Payee", {
	mpesa_payees_add(frm, cdt, cdn) {
		const saved = mpsa_payees.saved_expenses(frm);
		if (saved.length === 1) frappe.model.set_value(cdt, cdn, "expense_detail", saved[0].name);
	},

	form_render(frm, cdt, cdn) {
		mpsa_payees.set_expense_options(frm);
		mpsa_payees.row_notes(frm, cdn);
	},

	payee_type(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		// only the fields of the chosen type stay (the server clears the others too)
		if (row.payee_type === MPSA_PAYEE_TYPE.phone) {
			["shortcode", "account_number", "bank_paybill"].forEach((f) => frappe.model.set_value(cdt, cdn, f, null));
		} else {
			frappe.model.set_value(cdt, cdn, "phone", null);
			if (row.payee_type !== MPSA_PAYEE_TYPE.bank) frappe.model.set_value(cdt, cdn, "bank_paybill", null);
			if (row.payee_type === MPSA_PAYEE_TYPE.till) frappe.model.set_value(cdt, cdn, "account_number", null);
		}
		mpsa_payees.show_destination(cdt, cdn);
	},

	async bank_paybill(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		let paybill = null;
		if (row.bank_paybill) {
			// the bank's paybill is copied, never typed (the server copies it again on save)
			const r = await frappe.db.get_value("M-Pesa Bank Paybill", row.bank_paybill, "paybill").catch(() => null);
			paybill = (r && r.message && r.message.paybill) || null;
		}
		frappe.model.set_value(cdt, cdn, "shortcode", paybill);
		mpsa_payees.show_destination(cdt, cdn);
	},

	shortcode(frm, cdt, cdn) {
		mpsa_payees.show_destination(cdt, cdn);
	},

	account_number(frm, cdt, cdn) {
		mpsa_payees.show_destination(cdt, cdn);
	},

	phone(frm, cdt, cdn) {
		mpsa_payees.show_destination(cdt, cdn);
	},

	expense_detail(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const line = (frm.doc.expenses || []).find((r) => r.name === row.expense_detail);
		frappe.model.set_value(cdt, cdn, "expense_type", line ? line.expense_type : null);
	},
});

const mpsa_payees = {
	// what claim_state said for this form (frm.mpsa_payees): switch, lines, totals, suggestions
	state(frm) {
		return frm.mpsa_payees || {};
	},

	refresh(frm) {
		this.set_expense_options(frm);
		const rows = frm.doc.mpesa_payees || [];
		this.show(frm, rows.length > 0 || this.state(frm).enabled);
		frappe
			.xcall(`${MPSA_PAYEES_API}.claim_state`, { claim: frm.doc.name })
			.then((r) => {
				frm.mpsa_payees = r || {};
				// a payout's result reached the lines since the form was loaded: show it
				if (frm.mpsa_payees.changed && !frm.is_dirty()) {
					frm.reload_doc();
					return;
				}
				this.show(frm, frm.mpsa_payees.enabled || rows.length > 0);
				this.summary(frm);
				this.buttons(frm);
			})
			.catch(() => null);
	},

	show(frm, visible) {
		if (frm.fields_dict.mpesa_payees_section) frm.toggle_display("mpesa_payees_section", !!visible);
	},

	saved_expenses(frm) {
		return (frm.doc.expenses || []).filter((r) => !r.__islocal);
	},

	expense_label(frm, r) {
		const amount = flt(r.sanctioned_amount) || flt(r.amount);
		return `#${r.idx} ${r.expense_type || ""} ${format_currency(amount, frm.doc.currency || "KES", 0)}`;
	},

	set_expense_options(frm) {
		const grid = frm.fields_dict.mpesa_payees && frm.fields_dict.mpesa_payees.grid;
		if (!grid) return;
		const options = [{ value: "", label: "" }].concat(
			this.saved_expenses(frm).map((r) => ({ value: r.name, label: this.expense_label(frm, r) }))
		);
		grid.update_docfield_property("expense_detail", "options", options);
	},

	show_destination(cdt, cdn) {
		const row = locals[cdt][cdn];
		let shown = "";
		if (row.payee_type === MPSA_PAYEE_TYPE.phone) shown = row.phone || "";
		else if (row.payee_type === MPSA_PAYEE_TYPE.till) shown = row.shortcode || "";
		else shown = [row.shortcode, row.account_number].filter(Boolean).join(" / ");
		frappe.model.set_value(cdt, cdn, "destination", shown);
	},

	summary(frm) {
		const field = frm.fields_dict.mpesa_payees_summary;
		if (!field) return;
		const e = frappe.utils.escape_html;
		const s = this.state(frm);
		const t = s.totals || {};
		const kes = (v) => format_currency(flt(v), "KES", 0);
		const parts = [];
		if ((s.lines || []).length) {
			parts.push(
				`<p><b>${__("Payees")}</b>: ${__("paid {0}", [kes(t["Paid"])])} &middot; ${__("in flight {0}", [
					kes(flt(t["In Flight"]) + flt(t["Queued"])),
				])} &middot; ${__("failed {0}", [kes(t["Failed"])])} &middot; ${__("left {0}", [kes(t["Draft"])])}</p>`
			);
		}
		if (s.payable_problem) parts.push(`<p style="color: var(--orange-600)">${e(s.payable_problem)}</p>`);
		(s.lines || [])
			.filter((l) => l.status === "Paid" && !l.name_matches && l.name_note)
			.forEach((l) => parts.push(`<p style="color: var(--red-600)">${__("Row #{0}", [l.idx])}: ${e(l.name_note)}</p>`));
		(s.suggestions || []).forEach((x, i) =>
			parts.push(
				`<p style="color: var(--blue-600)">${e(x.message)} <button class="btn btn-xs btn-default mpsa-suggest" data-i="${i}">${__(
					"Make Supplier Account"
				)}</button></p>`
			)
		);
		if (s.reimbursed && frappe.boot.mpsa && frappe.boot.mpsa.can_approve) {
			parts.push(
				`<p style="color: var(--orange-600)">${__(
					"Total Amount Reimbursed shows {0}, but the entries booked against the claim add up to {1}: press Repair Reimbursed Total.",
					[kes(s.reimbursed.stored), kes(s.reimbursed.booked)]
				)}</p>`
			);
		}
		field.$wrapper.html(parts.join(""));
		field.$wrapper.find(".mpsa-suggest").on("click", (ev) => {
			// a new Supplier M-Pesa Account with the destination filled in: the user picks the supplier
			const x = s.suggestions[cint($(ev.currentTarget).attr("data-i"))];
			if (x) frappe.new_doc("Supplier M-Pesa Account", x.account);
		});
	},

	row_notes(frm, cdn) {
		const grid_row = frm.fields_dict.mpesa_payees.grid.grid_rows_by_docname[cdn];
		const field = grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict.notes;
		if (!field) return;
		const e = frappe.utils.escape_html;
		const s = this.state(frm);
		const notes = (s.suggestions || []).filter((x) => x.line === cdn).map((x) => e(x.message));
		const line = (s.lines || []).find((l) => l.name === cdn);
		// the payout and the supplier account are plain names on the line (a link would stop them being
		// cancelled or deleted while the claim is submitted): linked here
		if (line && line.payout_doctype && line.payout_name) {
			notes.push(`${__("Payout")}: ${frappe.utils.get_form_link(line.payout_doctype, line.payout_name, true)}`);
		}
		if (line && line.supplier_account) {
			notes.push(
				`${__("Approved Supplier Account")}: ${frappe.utils.get_form_link("Supplier M-Pesa Account", line.supplier_account, true)}`
			);
		}
		if (line && line.status === "Paid" && line.name_note) notes.push(e(line.name_note));
		if (line && line.status === "Cancelled" && line.error_message) notes.push(e(line.error_message));
		field.$wrapper.html(notes.map((n) => `<p class="text-muted">${n}</p>`).join(""));
	},

	buttons(frm) {
		const flags = frappe.boot.mpsa || {};
		const s = this.state(frm);
		const lines = s.lines || [];
		if (frm.doc.docstatus === 0 && s.enabled && frm.perm && frm.perm[0] && frm.perm[0].write) {
			frm.add_custom_button(__("Paste Payees"), () => this.paste(frm), __("M-Pesa"));
		}
		if (frm.doc.docstatus !== 1) return;
		if (flags.can_prepare && s.enabled && !s.payable_problem) {
			if (lines.some((l) => l.status === "Draft")) {
				frm.add_custom_button(__("Pay Merchants and Payees"), () => this.pay(frm, "pay_payees", "Draft"), __("M-Pesa"));
			}
			if (lines.some((l) => l.status === "Failed")) {
				frm.add_custom_button(__("Retry Failed Payees"), () => this.pay(frm, "retry_payees", "Failed"), __("M-Pesa"));
			}
		}
		if (flags.can_approve && s.reimbursed) {
			frm.add_custom_button(__("Repair Reimbursed Total"), () => this.repair(frm), __("M-Pesa"));
		}
		if (flags.can_approve && lines.some((l) => l.status === "Draft" || l.status === "Failed")) {
			frm.add_custom_button(__("Settle Payee Outside M-Pesa"), () => this.settle(frm), __("M-Pesa"));
		}
	},

	// a Draft or Failed line M-Pesa will not pay (paid by bank instead): Cancelled with how it was paid, so its
	// money is no longer held from the employee (claim_payees.settle_line, a named M-Pesa Manager's)
	settle(frm) {
		const rows = Object.fromEntries((frm.doc.mpesa_payees || []).map((r) => [r.name, r]));
		const open = (this.state(frm).lines || []).filter((l) => l.status === "Draft" || l.status === "Failed");
		const options = open.map((l) => {
			const r = rows[l.name] || {};
			return {
				value: l.name,
				label: `#${l.idx} ${r.payee_name || ""} ${format_currency(flt(r.amount), "KES", 0)} (${__(l.status)})`,
			};
		});
		frappe.prompt(
			[
				{
					fieldtype: "Select",
					fieldname: "line",
					label: __("Payee Row"),
					reqd: 1,
					options,
					default: open.length === 1 ? open[0].name : undefined,
				},
				{
					fieldtype: "Small Text",
					fieldname: "reason",
					label: __("How It Was Paid"),
					reqd: 1,
					description: __("The bank Payment Entry or other reference. M-Pesa will never pay this row."),
				},
			],
			(values) =>
				frappe.xcall(`${MPSA_PAYEES_API}.settle_line`, { claim: frm.doc.name, ...values }).then(() => {
					frappe.show_alert({ message: __("Payee settled outside M-Pesa"), indicator: "green" });
					frm.reload_doc();
				}),
			__("Settle Payee Outside M-Pesa"),
			__("Settle")
		);
	},

	pay(frm, method, status) {
		const statuses = Object.fromEntries((this.state(frm).lines || []).map((l) => [l.name, l.status]));
		const rows = (frm.doc.mpesa_payees || []).filter((r) => statuses[r.name] === status);
		const merchants = rows.filter((r) => r.payee_type !== MPSA_PAYEE_TYPE.phone);
		const phones = rows.filter((r) => r.payee_type === MPSA_PAYEE_TYPE.phone);
		const total = rows.reduce((sum, r) => sum + flt(r.amount), 0);
		const fields = [
			{
				fieldtype: "HTML",
				fieldname: "intro",
				options: `<p>${__(
					"Draft payouts for {0} payees ({1}): {2} merchants by B2B, one request each, and {3} phones in one B2C batch. Nothing is sent now: an M-Pesa Payer who did not prepare them sends each with Send via M-Pesa.",
					[rows.length, format_currency(total, "KES", 0), merchants.length, phones.length]
				)}</p>`,
			},
		];
		if (merchants.length) {
			fields.push({
				fieldtype: "Link",
				fieldname: "b2b_settings",
				label: __("Pay From (B2B)"),
				options: "Mpesa Settings",
				description: __("Leave empty when the company has one B2B shortcode."),
				get_query: () => ({ filters: { use_b2b: 1, company: frm.doc.company } }),
			});
		}
		if (phones.length) {
			fields.push({
				fieldtype: "Link",
				fieldname: "b2c_settings",
				label: __("Pay From (B2C)"),
				options: "Mpesa Settings",
				description: __("Leave empty when the company has one B2C shortcode."),
				get_query: () => ({ filters: { use_b2c: 1, company: frm.doc.company } }),
			});
		}
		const d = new frappe.ui.Dialog({
			title: status === "Failed" ? __("Retry Failed Payees") : __("Pay Merchants and Payees"),
			fields,
			primary_action_label: __("Prepare Payouts"),
			primary_action: (values) => {
				frappe.xcall(`${MPSA_PAYEES_API}.${method}`, { claim: frm.doc.name, ...values }).then((r) => {
					d.hide();
					mpsa_payees.made(r);
					frm.reload_doc();
				});
			},
		});
		d.show();
	},

	made(r) {
		const links = (r.b2b || [])
			.map((name) => frappe.utils.get_form_link("Mpesa B2B Payment Request", name, true))
			.concat(r.b2c ? [frappe.utils.get_form_link("B2C Payment Disbursement", r.b2c, true)] : []);
		frappe.msgprint({
			title: __("M-Pesa Payouts Prepared"),
			indicator: "green",
			message:
				`<p>${__("{0} payees ({1}) are ready to send:", [r.lines, format_currency(r.total, "KES", 0)])}</p>` +
				`<ul>${links.map((l) => `<li>${l}</li>`).join("")}</ul>` +
				`<p>${__("An M-Pesa Payer who did not prepare them sends each with Send via M-Pesa.")}</p>`,
		});
	},

	repair(frm) {
		frappe.confirm(
			__("Set Total Amount Reimbursed from the Payment and Journal Entries booked against this claim (HRMS's own count)?"),
			() =>
				frappe.xcall(`${MPSA_PAYEES_API}.repair_reimbursed`, { claim: frm.doc.name }).then((r) => {
					frappe.show_alert({
						message: r.repaired
							? __("Repaired: {0} to {1}", [format_currency(r.from, "KES"), format_currency(r.to, "KES")])
							: __("Nothing to repair"),
						indicator: "green",
					});
					frm.reload_doc();
				})
		);
	},

	paste(frm) {
		const e = frappe.utils.escape_html;
		const saved = this.saved_expenses(frm);
		let checked = null;
		const d = new frappe.ui.Dialog({
			title: __("Paste Payees"),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${__(
						"One payee per row: name, phone, amount, separated by tabs (copied from a spreadsheet) or commas. A first row naming the columns may come first. Each phone is paid by B2C, so it must be an M-Pesa (Safaricom) number, and never an employee's."
					)}</p>`,
				},
				{
					fieldtype: "Select",
					fieldname: "expense_detail",
					label: __("Expense Line"),
					reqd: 1,
					options: saved.map((r) => ({ value: r.name, label: this.expense_label(frm, r) })),
					default: saved.length === 1 ? saved[0].name : undefined,
					description: saved.length ? "" : __("Save the claim with its expense lines first."),
				},
				{ fieldtype: "Small Text", fieldname: "text", label: __("Rows") },
				{
					fieldtype: "Attach",
					fieldname: "file_url",
					label: __("Or Upload a CSV or XLSX File"),
					change: () => reset(),
				},
				{ fieldtype: "HTML", fieldname: "result" },
			],
			primary_action_label: __("Check"),
			primary_action: (values) => {
				if (checked) {
					add(values);
					return;
				}
				if (!values.text && !values.file_url) {
					frappe.msgprint(__("Paste the rows or upload a file."));
					return;
				}
				frappe
					.xcall(`${MPSA_PAYEES_API}.parse_payees`, {
						text: values.text || null,
						file_url: values.file_url || null,
						claim: frm.is_new() ? null : frm.doc.name,
						employee: frm.doc.employee,
						existing: (frm.doc.mpesa_payees || []).map((r) => r.phone).filter(Boolean),
					})
					.then((r) => show(r));
			},
		});
		const show = (r) => {
			const rows = (r.rows || []).map(
				(x) =>
					`<tr${x.errors.length ? ' style="color: var(--red-600)"' : ""}><td>${x.row}</td><td>${e(x.payee_name)}</td>` +
					`<td>${e(x.phone)}</td><td>${x.amount === null ? "" : format_currency(x.amount, "KES", 0)}</td>` +
					`<td>${e(x.errors.join("; "))}</td></tr>`
			);
			d.fields_dict.result.$wrapper.html(
				`<p>${__("{0} rows can be added; {1} have problems and are left out.", [r.good, r.bad])}</p>` +
					`<table class="table table-bordered table-condensed"><thead><tr><th>${__("Row")}</th><th>${__("Name")}</th>` +
					`<th>${__("Phone")}</th><th>${__("Amount")}</th><th>${__("Problem")}</th></tr></thead>` +
					`<tbody>${rows.join("")}</tbody></table>`
			);
			if (!r.good) return;
			checked = r;
			d.get_primary_btn().html(__("Add {0} Payees", [r.good]));
		};
		const add = (values) => {
			const line = saved.find((x) => x.name === values.expense_detail) || {};
			checked.rows
				.filter((r) => !r.errors.length)
				.forEach((r) =>
					frm.add_child("mpesa_payees", {
						payee_type: MPSA_PAYEE_TYPE.phone,
						payee_name: r.payee_name,
						phone: r.phone,
						destination: r.phone,
						amount: r.amount,
						expense_detail: values.expense_detail,
						expense_type: line.expense_type,
					})
				);
			frm.refresh_field("mpesa_payees");
			frm.dirty();
			d.hide();
			frappe.show_alert({ message: __("{0} payees added: save the claim.", [checked.good]), indicator: "green" });
		};
		// a change to what is pasted needs a new check
		const reset = () => {
			if (!checked) return;
			checked = null;
			d.get_primary_btn().html(__("Check"));
		};
		d.fields_dict.text.$input.on("input", reset);
		d.fields_dict.expense_detail.$input.on("change", reset);
		d.show();
	},
};
