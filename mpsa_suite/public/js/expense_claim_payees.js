// Expense Claim (HRMS): pay merchants (till, paybill, bank account) and casual payees (phones) straight
// from the claim by M-Pesa, instead of reimbursing the employee who paid them (money_out.claim_payees).
// The claimant enters the lines in draft, the expense approver approves them with the claim, someone who
// prepares payouts presses Pay Merchants and Payees (drafts only), and an M-Pesa Payer who did not
// prepare them (maker-checker on) sends each with Send via M-Pesa. Buttons show only to who the server
// accepts; the server checks everything again.
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
		mpsa_payees.mpesa_name(cdt, cdn);
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

	can_write(frm) {
		return Boolean(frm.perm && frm.perm[0] && frm.perm[0].write);
	},

	// the payees of an expense line after a paste: those on it already and the good pasted rows
	line_total(frm, line, r) {
		const on_line = (frm.doc.mpesa_payees || [])
			.filter((p) => p.expense_detail === line.name)
			.reduce((sum, p) => sum + flt(p.amount), 0);
		return flt(on_line + flt(r.good_total), 0);
	},

	// what a paste does to its expense line: within it (the rest is reimbursed to the claimant) or more than it
	// (the save refuses that); the dialog's box sets the line's Amount to the payees' total
	line_note(frm, d, r) {
		const line = this.saved_expenses(frm).find((x) => x.name === d.get_value("expense_detail"));
		if (!line || !r.good) return "";
		const kes = (v) => format_currency(flt(v), "KES", 0);
		const total = this.line_total(frm, line, r);
		const limit = flt(line.sanctioned_amount) || flt(line.amount);
		if (Math.abs(total - limit) < 0.5) return "";
		const more = total > limit;
		d.set_df_property(
			"set_line_amount",
			"label",
			__("Set expense line #{0} to {1}, the payees' total (it is {2})", [line.idx, kes(total), kes(limit)])
		);
		d.set_df_property("set_line_amount", "hidden", 0);
		// never ticked for the user: a line raised to a wrong total would be approved as it stands
		d.set_value("set_line_amount", 0);
		return more
			? `<p style="color: var(--orange-600)">${__(
					"The payees of expense line #{0} come to {1}, more than its {2}: the claim cannot be saved like that. Check the amounts below; if they are right, tick the box below to set the line to {1}.",
					[line.idx, kes(total), kes(limit)]
			  )}</p>`
			: `<p class="text-muted">${__(
					"The payees of expense line #{0} come to {1} of its {2}: the other {3} is reimbursed to the claimant. Tick the box below to set the line to {1}.",
					[line.idx, kes(total), kes(limit), kes(limit - total)]
			  )}</p>`;
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

	// round 4.3: a phone M-Pesa paid before takes the name M-Pesa paid it under (Payee Name locked);
	// a new number keeps the typed name. The claim's save sets it again (claim_payees.clean_line).
	// What the claimant typed stays as Name Given when it is not M-Pesa's name (a wrong number, or one with a
	// new owner): they are told at once, the save warns again and the name check after the payout uses it.
	mpesa_name(cdt, cdn) {
		const row = locals[cdt][cdn];
		// the name filled in for the line's earlier number goes with it (what was typed comes back)
		const clear = (r) => ({
			mpesa_name: null,
			mpesa_name_from: null,
			typed_name: null,
			...(r.mpesa_name && r.payee_name === r.mpesa_name ? { payee_name: r.typed_name || "" } : {}),
		});
		if (row.payee_type !== MPSA_PAYEE_TYPE.phone || !row.phone) {
			if (row.mpesa_name) frappe.model.set_value(cdt, cdn, clear(row));
			return;
		}
		const phone = row.phone;
		frappe
			.xcall(`${MPSA_PAYEES_API}.mpesa_name`, { phone })
			.then((r) => {
				const now = locals[cdt] && locals[cdt][cdn];
				if (!now || now.phone !== phone) return; // changed again meanwhile
				if (!(r && r.name)) {
					if (now.mpesa_name) frappe.model.set_value(cdt, cdn, clear(now));
					return;
				}
				const typed = now.payee_name && now.payee_name !== now.mpesa_name ? now.payee_name : now.typed_name || "";
				const differs = typed && typed.trim().toUpperCase() !== r.name.trim().toUpperCase();
				frappe.model.set_value(cdt, cdn, {
					payee_name: r.name,
					mpesa_name: r.name,
					mpesa_name_from: r.from,
					typed_name: differs ? typed : null,
				});
				if (differs) {
					frappe.show_alert(
						{
							message: __("M-Pesa paid {0} as {1} before, not {2}: check the number.", [
								frappe.utils.escape_html(phone),
								frappe.utils.escape_html(r.name),
								frappe.utils.escape_html(typed),
							]),
							indicator: "orange",
						},
						10
					);
				}
			})
			.catch(() => null);
	},

	summary(frm) {
		const field = frm.fields_dict.mpesa_payees_summary;
		if (!field) return;
		const e = frappe.utils.escape_html;
		const s = this.state(frm);
		const t = s.totals || {};
		const kes = (v) => format_currency(flt(v), "KES", 0);
		const parts = [];
		if (frm.doc.docstatus === 0 && s.enabled && this.can_write(frm)) {
			// in plain sight where the payees are, not only under the M-Pesa menu
			parts.push(
				`<p><button class="btn btn-xs btn-primary mpsa-paste">${__("Paste Payees")}</button> <span class="text-muted">${__(
					"Many casuals at once: copy name, phone and amount from Excel and paste them, or upload the file."
				)}</span></p>`
			);
		}
		if ((s.lines || []).length) {
			parts.push(
				`<p><b>${__("Payees")}</b>: ${__("paid {0}", [kes(t["Paid"])])} &middot; ${__("in flight {0}", [
					kes(flt(t["In Flight"]) + flt(t["Queued"])),
				])} &middot; ${__("failed {0}", [kes(t["Failed"])])} &middot; ${__("left {0}", [kes(t["Draft"])])}</p>`
			);
		}
		if (s.payable_problem) parts.push(`<p style="color: var(--orange-600)">${e(s.payable_problem)}</p>`);
		// a name that does not match in red; one M-Pesa could not verify (a bank account, a till not named yet) in orange
		(s.lines || [])
			.filter((l) => l.status === "Paid" && l.name_note && this.verdict(l) !== "Matches")
			.forEach((l) => {
				const colour = this.verdict(l) === "Does Not Match" ? "--red-600" : "--orange-600";
				parts.push(`<p style="color: var(${colour})">${__("Row #{0}", [l.idx])}: ${e(l.name_note)}</p>`);
			});
		(s.lines || [])
			.filter((l) => l.staff_check === "Blocked")
			.forEach((l) =>
				parts.push(`<p style="color: var(--red-600)">${__("Row #{0}", [l.idx])}: ${e(l.staff_note || "")}</p>`)
			);
		(s.blocked || []).forEach((b) =>
			parts.push(
				`<p style="color: var(--red-600)">${__(
					"Row #{0}: blocked for claim payees, M-Pesa paid this destination under a staff member's name ({1}, payee row #{2}).",
					[b.idx, e(b.claim), b.flagged_idx]
				)}</p>`
			)
		);
		(s.suggestions || []).forEach((x, i) =>
			parts.push(
				`<p style="color: var(--blue-600)">${e(x.message)} <button class="btn btn-xs btn-default mpsa-suggest" data-i="${i}">${__(
					"Make Supplier Account"
				)}</button></p>`
			)
		);
		const flags = frappe.boot.mpsa || {};
		if (s.reimbursed && (flags.can_approve || flags.can_prepare || flags.can_pay)) {
			// nothing more is prepared or sent for the claim until it is repaired; the button is the managers'
			const amounts = [kes(s.reimbursed.stored), kes(s.reimbursed.booked)];
			const ask = flags.can_approve
				? __(
						"Total Amount Reimbursed shows {0}, but the entries booked against the claim add up to {1}: press Repair Reimbursed Total.",
						amounts
				  )
				: __(
						"Total Amount Reimbursed shows {0}, but the entries booked against the claim add up to {1}: ask an M-Pesa Manager to repair it. Nothing more is paid on this claim until then.",
						amounts
				  );
			parts.push(`<p style="color: var(--orange-600)">${ask}</p>`);
		}
		field.$wrapper.html(parts.join(""));
		field.$wrapper.find(".mpsa-paste").on("click", () => this.paste(frm));
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
		if (line && line.staff_check && line.staff_note) notes.push(e(line.staff_note));
		if (line && line.status === "Cancelled" && line.error_message) notes.push(e(line.error_message));
		field.$wrapper.html(notes.map((n) => `<p class="text-muted">${n}</p>`).join(""));
	},

	buttons(frm) {
		const flags = frappe.boot.mpsa || {};
		const s = this.state(frm);
		const lines = s.lines || [];
		if (frm.doc.docstatus === 0 && s.enabled && this.can_write(frm)) {
			frm.add_custom_button(__("Paste Payees"), () => this.paste(frm), __("M-Pesa"));
		}
		if (frm.doc.docstatus !== 1) return;
		if (flags.can_prepare && s.enabled && !s.payable_problem && !s.reimbursed) {
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
		if (flags.can_approve && this.flagged(frm).length) {
			frm.add_custom_button(__("Clear Staff Flag"), () => this.clear_staff(frm), __("M-Pesa"));
		}
	},

	// what the name check said: older lines have only Name Matches
	verdict(l) {
		return l.name_verdict || (l.name_matches ? "Matches" : "Does Not Match");
	},

	// the lines flagged as staff or self payments, and those whose destination another claim's flag blocks
	flagged(frm) {
		const s = this.state(frm);
		const names = (s.lines || []).filter((l) => l.staff_check === "Blocked").map((l) => l.name);
		return [...new Set(names.concat((s.blocked || []).map((b) => b.line)))];
	},

	// a named M-Pesa Manager who was not involved checked that the destination is no staff member's
	// (claim_payees.clear_staff_flag): it is paid to claim payees again
	clear_staff(frm) {
		const rows = Object.fromEntries((frm.doc.mpsa_payees || []).map((r) => [r.name, r]));
		const flagged = this.flagged(frm);
		const options = flagged.map((name) => {
			const r = rows[name] || {};
			return { value: name, label: `#${r.idx} ${r.payee_name || ""} ${r.destination || ""}` };
		});
		frappe.prompt(
			[
				{
					fieldtype: "Select",
					fieldname: "line",
					label: __("Payee Row"),
					reqd: 1,
					options,
					default: flagged.length === 1 ? flagged[0] : undefined,
				},
				{
					fieldtype: "Small Text",
					fieldname: "reason",
					label: __("How You Checked"),
					reqd: 1,
					description: __("Who you asked and what they showed: this payee is no staff member."),
				},
			],
			(values) =>
				frappe.xcall(`${MPSA_PAYEES_API}.clear_staff_flag`, { claim: frm.doc.name, ...values }).then(() => {
					frappe.show_alert({ message: __("Staff flag cleared"), indicator: "green" });
					frm.reload_doc();
				}),
			__("Clear Staff Flag"),
			__("Clear")
		);
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
					"Draft payouts for {0} payees ({1}): {2} merchants by B2B, one request each, and {3} phones in one B2C batch.",
					[rows.length, format_currency(total, "KES", 0), merchants.length, phones.length]
				)} ${this.who_sends()}</p>`,
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
				`<p>${this.who_sends()}</p>`,
		});
	},

	// with maker-checker on (M-Pesa Suite Settings), whoever prepared the payouts does not send them
	who_sends() {
		return (frappe.boot.mpsa || {}).maker_checker
			? __("Nothing is sent now: an M-Pesa Payer who did not prepare them sends each with Send via M-Pesa.")
			: __("Nothing is sent now: an M-Pesa Payer sends each with Send via M-Pesa.");
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

	// Paste Payees, many casuals at once: the claim is saved first (a new claim, or one with changes), as each
	// payee is paid for a saved expense line. The dialog checks the rows on the server, shows what the pasted
	// total does to the expense line, and adds the good rows; the claim's save checks them again.
	paste(frm, after_save) {
		if (!(frm.doc.expenses || []).some((r) => r.expense_type)) {
			frappe.msgprint({
				title: __("Paste Payees"),
				message: __(
					"Add the expense line first: its Expense Type (casual labour, say) and Amount. Then press Paste Payees again."
				),
			});
			return;
		}
		if (frm.is_new() || frm.is_dirty() || !this.saved_expenses(frm).length) {
			// a failed save resolves too (Frappe shows why): the dialog opens only on a saved claim
			if (!after_save) frm.save().then(() => this.paste(frm, true));
			return;
		}
		// which expense types allow payees, as the saved claim has its lines (the refresh after a save may
		// not have brought them yet): the line preselected is one of them
		frappe.xcall(`${MPSA_PAYEES_API}.claim_state`, { claim: frm.doc.name }).then((r) => {
			frm.mpsa_payees = r || {};
			this.paste_dialog(frm);
		});
	},

	paste_dialog(frm) {
		const e = frappe.utils.escape_html;
		const saved = this.saved_expenses(frm);
		const allowed = this.state(frm).allowed_types || [];
		const payable = saved.filter((r) => allowed.includes(r.expense_type));
		// one line whose type allows payees; else the claim's only line (the save says when its type does not)
		const preset = payable.length === 1 ? payable[0] : !payable.length && saved.length === 1 ? saved[0] : null;
		let checked = null;
		const d = new frappe.ui.Dialog({
			title: __("Paste Payees"),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${__(
						"Copy the payees from Excel (name, phone, amount) and paste them below, or upload the file. Title rows, the column names and a TOTAL row are left out, and a first column of row numbers is passed over. When the rows have other numbers too (days, a rate), copy the column names with them (Name, Phone, Amount), so the amount is never guessed. Rows marked Paid are not added again. Each phone is paid by B2C, so it must be an M-Pesa (Safaricom) number, and never an employee's."
					)}</p>`,
				},
				{
					fieldtype: "Select",
					fieldname: "expense_detail",
					label: __("Expense Line"),
					reqd: 1,
					options: saved.map((r) => ({ value: r.name, label: this.expense_label(frm, r) })),
					default: preset ? preset.name : undefined,
				},
				{ fieldtype: "Small Text", fieldname: "text", label: __("Rows") },
				{
					fieldtype: "Attach",
					fieldname: "file_url",
					label: __("Or Upload a CSV or XLSX File"),
					change: () => reset(),
				},
				{ fieldtype: "HTML", fieldname: "result" },
				{ fieldtype: "Check", fieldname: "set_line_amount", hidden: 1 },
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
					`<tr${x.errors.length ? ' style="color: var(--red-600)"' : ""}><td>${x.row}</td><td>${e(x.payee_name)}` +
					(x.mpesa_name
						? `<div class="text-muted small">${e(__("Name on M-Pesa"))}` +
						  (x.typed_name && x.typed_name !== x.mpesa_name ? ` (${e(__("typed: {0}", [x.typed_name]))})` : "") +
						  "</div>"
						: "") +
					`</td>` +
					`<td>${e(x.phone)}</td><td>${x.amount === null ? "" : format_currency(x.amount, "KES", 0)}</td>` +
					`<td>${e(x.errors.join("; "))}</td></tr>`
			);
			const skipped = r.left_out || [];
			const left_out = skipped.length
				? `<p class="text-muted">${
						skipped.length === 1
							? __("Left out as no payee row: row {0} (a title, the column names or notes above the list, or a total).", [
									skipped[0],
							  ])
							: __("Left out as no payee rows: rows {0} (titles, the column names or notes above the list, totals).", [
									skipped.join(", "),
							  ])
				  }</p>`
				: "";
			d.fields_dict.result.$wrapper.html(
				`<p>${__("{0} rows can be added ({1}); {2} have problems and are left out.", [
					r.good,
					format_currency(flt(r.good_total), "KES", 0),
					r.bad,
				])}</p>` +
					left_out +
					this.line_note(frm, d, r) +
					`<table class="table table-bordered table-condensed"><thead><tr><th>${__("Row")}</th><th>${__("Name")}</th>` +
					`<th>${__("Phone")}</th><th>${__("Amount")}</th><th>${__("Problem")}</th></tr></thead>` +
					`<tbody>${rows.join("")}</tbody></table>`
			);
			if (!r.good) return;
			checked = r;
			d.get_primary_btn().html(__("Add {0} Payees and Save", [r.good]));
		};
		const add = (values) => {
			const line = saved.find((x) => x.name === values.expense_detail) || {};
			if (values.set_line_amount && line.name) {
				// HRMS copies the Amount to the Sanctioned Amount and adds up the claim
				frappe.model.set_value(line.doctype, line.name, "amount", this.line_total(frm, line, checked));
			}
			checked.rows
				.filter((r) => !r.errors.length)
				.forEach((r) =>
					frm.add_child("mpesa_payees", {
						payee_type: MPSA_PAYEE_TYPE.phone,
						payee_name: r.payee_name,
						mpesa_name: r.mpesa_name || null,
						mpesa_name_from: r.mpesa_name_from || null,
						// what was pasted, when it is not M-Pesa's name (the save keeps it only then)
						typed_name: (r.mpesa_name && r.typed_name && r.typed_name !== r.mpesa_name && r.typed_name) || null,
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
			frappe.show_alert({ message: __("{0} payees added.", [checked.good]), indicator: "green" });
			// the save checks them again (and names the numbers M-Pesa paid before); a refusal says why and
			// leaves the rows on the form to put right
			frm.save();
		};
		// a change to what is pasted needs a new check
		const reset = () => {
			d.set_df_property("set_line_amount", "hidden", 1);
			d.set_value("set_line_amount", 0);
			if (!checked) return;
			checked = null;
			d.get_primary_btn().html(__("Check"));
		};
		d.fields_dict.text.$input.on("input", reset);
		d.fields_dict.expense_detail.$input.on("change", reset);
		d.show();
	},
};
