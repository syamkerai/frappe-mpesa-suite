// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

// M-Pesa Bulk Payout (money_out.bulk_payout): casual wages and overtime paid by M-Pesa B2C in bulk. A preparer
// pastes the rows and saves the draft; a named M-Pesa Manager or Accounts Manager submits it (the approval,
// ticking that they checked the new and differing M-Pesa names); Pay by M-Pesa makes a draft batch an M-Pesa
// Payer sends with Send via M-Pesa. Buttons show only to who the server accepts; the server checks everything
// again.
const MPSA_BULK_API = "mpsa_suite.money_out.bulk_payout";
const MPSA_BULK_COLOURS = {
	Draft: "--gray-600",
	Queued: "--blue-600",
	"In Flight": "--blue-600",
	Paid: "--green-600",
	Failed: "--red-600",
	"On Hold": "--orange-600",
	Cancelled: "--gray-600",
};

frappe.ui.form.on("M-Pesa Bulk Payout", {
	setup(frm) {
		const company = () => ({ filters: { company: frm.doc.company, is_group: 0 } });
		frm.set_query("cost_center", company);
		frm.set_query("cost_center", "rows", company);
		frm.set_query("project", () => ({ filters: { company: frm.doc.company } }));
		frm.set_query("project", "rows", () => ({ filters: { company: frm.doc.company } }));
		frm.set_query("mpesa_settings", () => ({ filters: { use_b2c: 1, company: frm.doc.company } }));
	},

	onload(frm) {
		if (!frm.is_new()) return;
		// the Suite's default for "Pay new numbers KES 10 first" (read on the server: preparers cannot read the settings)
		frappe
			.xcall(`${MPSA_BULK_API}.sheet_state`, { sheet: null })
			.then((r) => {
				const d = (r && r.defaults) || {};
				if (frm.is_new()) frm.set_value("verify_new_numbers", d.verify_new_numbers ? 1 : 0);
			})
			.catch(() => null);
	},

	refresh(frm) {
		mpsa_bulk.refresh(frm);
	},

	// the approval: the approver ticks that they checked the new numbers and the names M-Pesa knows otherwise
	// (required again on the server at the submit)
	before_submit(frm) {
		return mpsa_bulk.confirm_names(frm);
	},
});

frappe.ui.form.on("M-Pesa Bulk Payout Row", {
	rows_add(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		["expense_type", "cost_center", "project"].forEach((f) => {
			if (!row[f] && frm.doc[f]) frappe.model.set_value(cdt, cdn, f, frm.doc[f]);
		});
	},
	phone(frm) {
		mpsa_bulk.badges(frm);
	},
	payee_name(frm) {
		mpsa_bulk.badges(frm);
	},
	rows_remove(frm) {
		mpsa_bulk.badges(frm);
	},
});

const mpsa_bulk = {
	state(frm) {
		return frm.mpsa_bulk || {};
	},

	can_write(frm) {
		return Boolean(frm.perm && frm.perm[0] && frm.perm[0].write);
	},

	kes(v) {
		return format_currency(flt(v), "KES", 0);
	},

	refresh(frm) {
		this.badges(frm);
		if (frm.is_new()) {
			this.summary(frm);
			this.buttons(frm);
			return;
		}
		frappe
			.xcall(`${MPSA_BULK_API}.sheet_state`, { sheet: frm.doc.name })
			.then((r) => {
				frm.mpsa_bulk = r || {};
				// a payout's result reached the rows since the form was loaded: show it
				if (frm.mpsa_bulk.changed && !frm.is_dirty()) {
					frm.reload_doc();
					return;
				}
				this.summary(frm);
				this.buttons(frm);
			})
			.catch(() => null);
	},

	// the name M-Pesa gave each number before (mpsa_known_names.js): green, orange (another name) or blue (new)
	badges(frm) {
		if (!window.mpsa_known_names || !frm.fields_dict.rows) return;
		const rows = (frm.doc.rows || [])
			.filter((r) => r.phone && r.status !== "Cancelled")
			.map((r) => ({
				id: r.name,
				label: `#${r.idx} ${r.payee_name || ""}`.trim(),
				type: "Phone",
				number: r.phone,
				typed_name: r.typed_name || r.payee_name || null,
			}));
		mpsa_known_names.later(frm, () => mpsa_known_names.show_rows(frm, "rows", rows));
	},

	summary(frm) {
		const field = frm.fields_dict.summary;
		if (!field) return;
		const e = frappe.utils.escape_html;
		const s = this.state(frm);
		const parts = [];
		if (frm.doc.docstatus === 0 && this.can_write(frm)) {
			parts.push(
				`<p><button class="btn btn-xs btn-primary mpsa-bulk-paste">${__("Paste Rows")}</button> ` +
					`<button class="btn btn-xs btn-default mpsa-bulk-template">${__("Download Template")}</button> ` +
					`<span class="text-muted">${__(
						"Many at once: copy name, phone and amount (and Expense Type, Project, Cost Center) from Excel, or upload the file."
					)}</span></p>`
			);
		}
		if (frm.doc.docstatus === 0 && s.names && s.names.header) {
			parts.push(`<p><b>${__("M-Pesa names")}</b>: ${e(s.names.header)}</p>`);
		}
		if (frm.doc.docstatus === 1) {
			parts.push(
				`<p><b>${e(frm.doc.progress || frm.doc.status || "")}</b>: ${__("paid {0}", [
					this.kes(frm.doc.paid_amount),
				])} &middot; ${__("failed {0}", [this.kes(frm.doc.failed_amount)])} &middot; ${__("left {0}", [
					this.kes(frm.doc.left_amount),
				])}</p>`
			);
			const projects = Object.entries(s.by_project || {});
			if (projects.length > 1 || (projects.length === 1 && projects[0][0])) {
				const lines = projects.map(
					([project, t]) =>
						`<tr><td>${e(project || __("No project"))}</td><td>${t.rows}</td><td>${this.kes(t.paid)}</td>` +
						`<td>${this.kes(t.failed)}</td><td>${this.kes(t.left)}</td></tr>`
				);
				parts.push(
					`<table class="table table-bordered table-condensed"><thead><tr><th>${__("Project")}</th><th>${__(
						"Rows"
					)}</th><th>${__("Paid")}</th><th>${__("Failed")}</th><th>${__("Left")}</th></tr></thead>` +
						`<tbody>${lines.join("")}</tbody></table>`
				);
			}
		}
		const failures = s.failures || {};
		if (failures.insufficient) {
			const f = failures.insufficient;
			parts.push(
				`<p style="color: var(--red-600)">${__(
					"{0} rows ({1}) failed for insufficient funds: top up the shortcode's Utility account, then press Retry Failed. Rows {2}.",
					[f.rows.length, this.kes(f.amount), f.rows.map((i) => `#${i}`).join(", ")]
				)}</p>`
			);
		}
		Object.entries(failures)
			.filter(([kind]) => kind !== "insufficient")
			.forEach(([, f]) =>
				parts.push(
					`<p style="color: var(--red-600)">${__("Rows {0} failed ({1}).", [
						f.rows.map((i) => `#${i}`).join(", "),
						this.kes(f.amount),
					])} ${e(f.hint || __("Press Retry Failed, or Settle Outside M-Pesa."))}</p>`
				)
			);
		(s.rows || [])
			.filter((r) => r.status === "Paid" && r.name_verdict === "Does Not Match")
			.forEach((r) => parts.push(`<p style="color: var(--red-600)">${__("Row #{0}", [r.idx])}: ${e(r.name_note || "")}</p>`));
		(s.rows || [])
			.filter((r) => r.status === "On Hold")
			.forEach((r) =>
				parts.push(`<p style="color: var(--orange-600)">${__("Row #{0}", [r.idx])}: ${e(r.name_note || r.progress || "")}</p>`)
			);
		const verified = (s.rows || []).filter((r) => r.status === "Draft" && ["Verified", "Released"].includes(r.verification));
		if (verified.length) {
			parts.push(
				`<p style="color: var(--blue-600)">${__(
					"{0} new numbers were paid KES 10 under the name given: press Pay by M-Pesa to pay the rest.",
					[verified.length]
				)}</p>`
			);
		}
		if ((s.holding || []).length) {
			parts.push(
				`<p class="text-muted">${__(
					"The Journal Entry is posted once no row waits for M-Pesa ({0} waiting; a row waiting {1} hours after its send no longer holds it).",
					[s.holding.length, s.book_after_hours]
				)}</p>`
			);
		}
		const unbooked = s.unbooked || {};
		if ((unbooked.rows || []).length && !(s.holding || []).length) {
			parts.push(
				`<p style="color: var(--orange-600)">${__("Paid but not booked yet: rows {0} ({1}).", [
					unbooked.rows.map((i) => `#${i}`).join(", "),
					this.kes(unbooked.amount),
				])} ${(unbooked.problems || []).map(e).join(" ")}</p>`
			);
		}
		if ((s.journals || []).length) {
			parts.push(
				`<p>${__("Journal Entries")}: ${s.journals
					.map((j) => frappe.utils.get_form_link("Journal Entry", j.name, true) + (j.docstatus === 2 ? ` (${__("cancelled")})` : ""))
					.join(", ")}</p>`
			);
		}
		if ((s.unreconciled || []).length) {
			parts.push(
				`<p class="text-muted">${__("Paid rows whose statement line is not reconciled yet: {0}.", [
					s.unreconciled.map((u) => `#${u.row}`).join(", "),
				])}</p>`
			);
		}
		const batches = [...new Set((s.rows || []).filter((r) => r.payout).map((r) => r.payout))];
		if (batches.length) {
			parts.push(
				`<p class="text-muted">${__("Batches")}: ${batches
					.map((b) => frappe.utils.get_form_link("B2C Payment Disbursement", b, true))
					.join(", ")}</p>`
			);
		}
		const lines = (s.rows || []).filter((r) => r.progress && r.status !== "Paid");
		if (lines.length) {
			parts.push(
				`<details><summary>${__("Where each row is")}</summary><ul>${lines
					.map((r) => {
						const colour = MPSA_BULK_COLOURS[r.status] || "--gray-600";
						return `<li><span style="color: var(${colour})">#${r.idx} ${e(__(r.status))}</span>: ${e(r.progress)}</li>`;
					})
					.join("")}</ul></details>`
			);
		}
		field.$wrapper.html(parts.join(""));
		field.$wrapper.find(".mpsa-bulk-paste").on("click", () => this.paste(frm));
		field.$wrapper.find(".mpsa-bulk-template").on("click", () => this.template());
	},

	buttons(frm) {
		const flags = frappe.boot.mpsa || {};
		const s = this.state(frm);
		const rows = s.rows || [];
		const has = (status) => rows.some((r) => r.status === status);
		const group = __("M-Pesa");
		if (frm.doc.docstatus === 0 && this.can_write(frm)) {
			frm.add_custom_button(__("Paste Rows"), () => this.paste(frm), group);
		}
		if (frm.doc.docstatus !== 1) return;
		if (flags.can_prepare && has("Draft")) {
			frm.add_custom_button(__("Pay by M-Pesa"), () => this.pay(frm, "pay", "Draft"), group);
		}
		if (flags.can_prepare && has("Failed")) {
			frm.add_custom_button(__("Retry Failed"), () => this.pay(frm, "retry_failed", "Failed"), group);
		}
		if (flags.can_pay && has("In Flight")) {
			frm.add_custom_button(__("Check with M-Pesa Now"), () => this.check_now(frm), group);
		}
		if (s.can_book && (s.unbooked || {}).rows && s.unbooked.rows.length) {
			frm.add_custom_button(__("Book Now"), () => this.book_now(frm), group);
		}
		if (flags.can_approve) {
			if (rows.some((r) => ["Draft", "Failed", "On Hold"].includes(r.status))) {
				frm.add_custom_button(__("Settle Outside M-Pesa"), () => this.manager(frm, "settle_row"), group);
			}
			if (has("Failed")) {
				frm.add_custom_button(__("Correct Phone and Retry"), () => this.manager(frm, "correct_phone"), group);
			}
			if (has("On Hold")) {
				frm.add_custom_button(__("Release Held Row"), () => this.manager(frm, "release_row"), group);
			}
			if (has("In Flight")) {
				frm.add_custom_button(__("Mark as Not Paid"), () => this.manager(frm, "mark_not_paid"), group);
			}
		}
	},

	confirm_names(frm) {
		return new Promise((resolve) => {
			frappe
				.xcall(`${MPSA_BULK_API}.sheet_state`, { sheet: frm.doc.name })
				.then((r) => {
					const names = (r && r.names) || {};
					if (!names.acknowledge) {
						resolve();
						return;
					}
					const e = frappe.utils.escape_html;
					const rows = (frm.doc.rows || []).map((row, i) => [row, (names.rows || [])[i]]);
					const listed = (verdict) =>
						rows
							// a partial match (one word of M-Pesa's name) is listed with the other names
							.filter(
								([, info]) =>
									info &&
									(info.verdict === verdict ||
										(verdict === "differs" && info.verdict === "partial") ||
										(verdict === "new" && info.status === "new"))
							)
							.map(([row, info]) => `<li>#${row.idx} ${e(row.typed_name || row.payee_name || "")} ${e(row.phone || "")}: ${mpsa_known_names.badge_html(info)}</li>`)
							.join("");
					let done = false;
					const d = new frappe.ui.Dialog({
						title: __("Check the M-Pesa names"),
						fields: [
							{
								fieldtype: "HTML",
								fieldname: "names",
								options:
									`<p>${e(names.header || "")}</p>` +
									`<details open><summary>${__("Known under another name, or only partly")}</summary><ul>${listed("differs")}</ul></details>` +
									`<details open><summary>${__("New numbers")}</summary><ul>${listed("new")}</ul></details>`,
							},
							{ fieldtype: "Check", fieldname: "checked", label: names.acknowledge },
						],
						primary_action_label: __("Approve"),
						primary_action: (values) => {
							if (!values.checked) {
								frappe.msgprint(__("Tick that you checked them first."));
								return;
							}
							frm.doc.names_checked = 1;
							done = true;
							d.hide();
							resolve();
						},
					});
					d.onhide = () => {
						if (!done) {
							frappe.validated = false;
							resolve();
						}
					};
					d.show();
				})
				.catch(() => {
					frappe.validated = false;
					resolve();
				});
		});
	},

	pay(frm, method, status) {
		const rows = (this.state(frm).rows || []).filter((r) => r.status === status);
		const amounts = Object.fromEntries((frm.doc.rows || []).map((r) => [r.name, flt(r.amount) - flt(r.paid_amount)]));
		const total = rows.reduce((sum, r) => sum + flt(amounts[r.name]), 0);
		const d = new frappe.ui.Dialog({
			title: status === "Failed" ? __("Retry Failed") : __("Pay by M-Pesa"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${__("One draft B2C batch for {0} rows ({1}).", [rows.length, this.kes(total)])} ${
						frm.doc.verify_new_numbers ? __("Numbers M-Pesa never paid get KES 10 first.") : ""
					} ${this.who_sends()}</p>`,
				},
				{
					fieldtype: "Link",
					fieldname: "b2c_settings",
					label: __("Pay From (B2C)"),
					options: "Mpesa Settings",
					default: frm.doc.mpesa_settings || undefined,
					description: __("Leave empty when the company has one B2C shortcode."),
					get_query: () => ({ filters: { use_b2c: 1, company: frm.doc.company } }),
				},
			],
			primary_action_label: __("Prepare Payout"),
			primary_action: (values) =>
				frappe.xcall(`${MPSA_BULK_API}.${method}`, { sheet: frm.doc.name, ...values }).then((r) => {
					d.hide();
					this.made(r);
					frm.reload_doc();
				}),
		});
		d.show();
	},

	made(r) {
		frappe.msgprint({
			title: __("M-Pesa Payout Prepared"),
			indicator: "green",
			message:
				`<p>${__("{0} rows ({1}) are ready to send in {2}.", [
					r.rows,
					this.kes(r.total),
					frappe.utils.get_form_link("B2C Payment Disbursement", r.b2c, true),
				])}</p>` +
				(r.tests ? `<p>${__("{0} new numbers get KES 10 first.", [r.tests])}</p>` : "") +
				`<p>${this.who_sends()}</p>`,
		});
	},

	who_sends() {
		return (frappe.boot.mpsa || {}).maker_checker
			? __("Nothing is sent now: an M-Pesa Payer who did not prepare it sends it with Send via M-Pesa.")
			: __("Nothing is sent now: an M-Pesa Payer sends it with Send via M-Pesa.");
	},

	check_now(frm) {
		frappe.xcall(`${MPSA_BULK_API}.check_now`, { sheet: frm.doc.name }).then((r) => {
			frappe.msgprint({
				title: __("Check with M-Pesa Now"),
				message:
					`<p>${__("Asked M-Pesa about {0} payouts; each answer settles its row.", [(r.asked || []).length])}</p>` +
					((r.skipped || []).length
						? `<ul>${r.skipped.map((x) => `<li>${frappe.utils.escape_html(x)}</li>`).join("")}</ul>`
						: ""),
			});
			frm.reload_doc();
		});
	},

	book_now(frm) {
		frappe.prompt(
			[
				{
					fieldtype: "Date",
					fieldname: "posting_date",
					label: __("Posting Date"),
					description: __("Empty: the day M-Pesa paid the rows. A later day when that period is closed."),
				},
			],
			(values) =>
				frappe.xcall(`${MPSA_BULK_API}.book_now`, { sheet: frm.doc.name, ...values }).then((r) => {
					frappe.show_alert({
						message: r.journal_entry ? __("Booked in {0}", [r.journal_entry]) : __("Nothing waits to be booked"),
						indicator: "green",
					});
					frm.reload_doc();
				}),
			__("Book Now"),
			__("Book")
		);
	},

	// a named M-Pesa Manager's action on one row, with why
	manager(frm, method) {
		const config = {
			settle_row: [["Draft", "Failed", "On Hold"], __("Settle Outside M-Pesa"), __("How It Was Paid"), __("Settle")],
			correct_phone: [["Failed"], __("Correct Phone and Retry"), __("Why (who gave the right number)"), __("Correct and Prepare")],
			release_row: [["On Hold"], __("Release Held Row"), __("How You Checked It With the Person"), __("Release")],
			mark_not_paid: [["In Flight"], __("Mark as Not Paid"), __("What the M-Pesa Statement Shows"), __("Mark Not Paid")],
		}[method];
		const by_name = Object.fromEntries((frm.doc.rows || []).map((r) => [r.name, r]));
		const open = (this.state(frm).rows || []).filter((r) => config[0].includes(r.status));
		const fields = [
			{
				fieldtype: "Select",
				fieldname: "row",
				label: __("Row"),
				reqd: 1,
				options: open.map((r) => {
					const row = by_name[r.name] || {};
					return { value: r.name, label: `#${r.idx} ${row.payee_name || ""} ${row.phone || ""} ${this.kes(row.amount)} (${__(r.status)})` };
				}),
				default: open.length === 1 ? open[0].name : undefined,
			},
		];
		if (method === "correct_phone") {
			fields.push({ fieldtype: "Data", fieldname: "phone", label: __("Right Phone"), reqd: 1 });
		}
		fields.push({ fieldtype: "Small Text", fieldname: "reason", label: config[2], reqd: 1 });
		frappe.prompt(
			fields,
			(values) =>
				frappe.xcall(`${MPSA_BULK_API}.${method}`, { sheet: frm.doc.name, ...values }).then((r) => {
					if (r && r.b2c) this.made(r);
					else frappe.show_alert({ message: __("Done"), indicator: "green" });
					frm.reload_doc();
				}),
			config[1],
			config[3]
		);
	},

	template() {
		const csv = "Name,Phone,Amount,Expense Type,Project,Cost Center\nJane Wanjiku,0711000333,1500,,,\n";
		const blob = new Blob([csv], { type: "text/csv" });
		const a = document.createElement("a");
		a.href = URL.createObjectURL(blob);
		a.download = "bulk_payout_template.csv";
		document.body.appendChild(a);
		a.click();
		setTimeout(() => {
			URL.revokeObjectURL(a.href);
			a.remove();
		}, 1000);
	},

	// Paste Rows: the sheet's defaults fill the rows' empty Expense Type, Project and Cost Center. The dialog checks
	// the rows on the server and adds the good ones; the sheet's save checks them again.
	paste(frm) {
		const e = frappe.utils.escape_html;
		let checked = null;
		// round 4.5: the amount column the user picked (null: the one the server reads as the amount), and the one shown
		let picked = null;
		let shown_amount = null;
		const d = new frappe.ui.Dialog({
			title: __("Paste Rows"),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${__(
						"Copy the rows from Excel (name, phone, amount; optional Expense Type, Project and Cost Center columns, with their column names) and paste them below, or upload the file. Title rows, the column names and a TOTAL row are left out. When the rows have other numbers too (days, a rate), copy the column names with them, so the amount is never guessed. Rows marked Paid are not added again. Each phone is paid by B2C, so it must be an M-Pesa (Safaricom) number. Employees are paid here like anyone, but never whoever prepares, approves or sends the sheet."
					)}</p>`,
				},
				{ fieldtype: "Small Text", fieldname: "text", label: __("Rows") },
				{
					fieldtype: "Attach",
					fieldname: "file_url",
					label: __("Or Upload a CSV or XLSX File"),
					change: () => fresh(),
				},
				{
					fieldtype: "Select",
					fieldname: "amount_column",
					label: __("Amount Column"),
					description: __("The column paid on each row. Pick another one when the amount was read from the wrong column."),
					hidden: 1,
					change: () => pick(),
				},
				{ fieldtype: "HTML", fieldname: "result" },
			],
			primary_action_label: __("Check"),
			primary_action: (values) => {
				if (checked) {
					add();
					return;
				}
				check(values);
			},
		});
		const check = (values) => {
			if (!values.text && !values.file_url) {
				frappe.msgprint(__("Paste the rows or upload a file."));
				return;
			}
			frappe
				.xcall(`${MPSA_BULK_API}.parse_rows`, {
					text: values.text || null,
					file_url: values.file_url || null,
					sheet: frm.is_new() ? null : frm.doc.name,
					company: frm.doc.company,
					existing: (frm.doc.rows || []).filter((r) => r.phone).map((r) => [r.phone, r.expense_type || frm.doc.expense_type || ""]),
					expense_type: frm.doc.expense_type || null,
					cost_center: frm.doc.cost_center || null,
					project: frm.doc.project || null,
					amount_column: picked,
				})
				.then((r) => show(r));
		};
		// which column each field came from ("Amount ← GRAND TOTAL"), and the amount column to pick
		const show_columns = (c) => {
			const named = c && c.header_row !== null && c.header_row !== undefined;
			const choices = named ? c.amount_choices || [] : [];
			shown_amount = named && c.amount_column !== null && c.amount_column !== undefined ? String(c.amount_column) : "";
			if (choices.length) {
				const options = choices.map((x) => ({ value: String(x.column), label: x.label }));
				if (!shown_amount) options.unshift({ value: "", label: __("Pick the amount column") });
				d.set_df_property("amount_column", "options", options);
				d.set_df_property("amount_column", "hidden", 0);
				d.set_value("amount_column", shown_amount);
			} else {
				d.set_df_property("amount_column", "hidden", 1);
			}
			if (!named) {
				return `<p class="text-muted">${__(
					"No column names: the amount is the one number after the phone (a row with more numbers is not added)."
				)}</p>`;
			}
			const f = c.fields || {};
			const part = (label, field) => `${e(label)} ← <b>${f[field] ? e(f[field]) : e(__("none"))}</b>`;
			const parts = [part(__("Name"), "payee_name"), part(__("Phone"), "phone"), part(__("Amount"), "amount")];
			for (const [field, label] of [["expense_type", __("Expense Type")], ["project", __("Project")], ["cost_center", __("Cost Center")]]) {
				if (f[field]) parts.push(part(label, field));
			}
			return `<p>${__("Columns (row {0})", [c.header_row])}: ${parts.join(" · ")}</p>`;
		};
		// another amount column picked: the rows are checked again with it
		const pick = () => {
			const value = d.get_value("amount_column");
			if (value === null || value === undefined || String(value) === shown_amount) return;
			picked = value === "" ? null : Number(value);
			reset();
			check(d.get_values(true) || {});
		};
		const show = (r) => {
			const rows = (r.rows || []).map(
				(x) =>
					`<tr${x.errors.length ? ' style="color: var(--red-600)"' : ""}><td>${x.row}</td><td>${e(x.payee_name)}` +
					(x.mpesa_name
						? `<div class="text-muted small">${e(__("Name on M-Pesa"))}` +
						  (x.typed_name ? ` (${e(__("given: {0}", [x.typed_name]))})` : "") +
						  "</div>"
						: "") +
					`</td><td>${e(x.phone)}</td><td>${x.amount === null ? "" : this.kes(x.amount)}</td>` +
					`<td>${e([x.expense_type, x.project, x.cost_center].filter(Boolean).join(" / "))}</td>` +
					`<td>${e(x.errors.concat(x.warnings || []).join("; "))}</td></tr>`
			);
			const skipped = r.left_out || [];
			d.fields_dict.result.$wrapper.html(
				show_columns(r.columns) +
					`<p>${__("{0} rows can be added ({1}); {2} have problems and are left out.", [r.good, this.kes(r.good_total), r.bad])}</p>` +
					(skipped.length ? `<p class="text-muted">${__("Left out as no payee rows: rows {0}.", [skipped.join(", ")])}</p>` : "") +
					`<table class="table table-bordered table-condensed"><thead><tr><th>${__("Row")}</th><th>${__("Name")}</th>` +
					`<th>${__("Phone")}</th><th>${__("Amount")}</th><th>${__("Accounting")}</th><th>${__("Problem")}</th></tr></thead>` +
					`<tbody>${rows.join("")}</tbody></table>`
			);
			if (!r.good) return;
			checked = r;
			d.get_primary_btn().html(__("Add {0} Rows and Save", [r.good]));
		};
		const add = () => {
			checked.rows
				.filter((r) => !r.errors.length)
				.forEach((r) =>
					frm.add_child("rows", {
						payee_name: r.payee_name,
						phone: r.phone,
						amount: r.amount,
						expense_type: r.expense_type || frm.doc.expense_type || null,
						project: r.project || frm.doc.project || null,
						cost_center: r.cost_center || frm.doc.cost_center || null,
						mpesa_name: r.mpesa_name || null,
						mpesa_name_from: r.mpesa_name_from || null,
						typed_name: r.typed_name || null,
					})
				);
			frm.refresh_field("rows");
			frm.dirty();
			d.hide();
			frappe.show_alert({ message: __("{0} rows added.", [checked.good]), indicator: "green" });
			frm.save();
		};
		const reset = () => {
			if (!checked) return;
			checked = null;
			d.get_primary_btn().html(__("Check"));
		};
		// other rows: their columns are read afresh
		const fresh = () => {
			picked = null;
			shown_amount = null;
			d.set_df_property("amount_column", "hidden", 1);
			reset();
		};
		d.fields_dict.text.$input.on("input", fresh);
		d.show();
	},
};
