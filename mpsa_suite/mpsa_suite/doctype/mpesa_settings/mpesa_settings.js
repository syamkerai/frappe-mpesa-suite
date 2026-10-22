// Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Settings", {
	onload_post_render: function (frm) {
		frm.events.setup_account_balance_html(frm);
	},

	refresh: function (frm) {
		frappe.realtime.on("refresh_form", function () {
			frm.reload_doc();
		});
		frappe.realtime.on("refresh_mpesa_dashboard", function () {
			frm.reload_doc();
			frm.events.setup_account_balance_html(frm);
		});
		frm.fields_dict["exchange_rates"].grid.get_field("currency").get_query = function () {
			return {
				filters: {
					name: ["!=", "KES"],
				},
			};
		};

		// Register the transaction status listener once per form load,
		// removing any prior instance first so clicks never stack listeners.
		if (frm._mpesa_status_handler) {
			frappe.realtime.off("mpesa_transaction_status_update", frm._mpesa_status_handler);
		}
		frm._mpesa_status_handler = (data) => {
			frappe.hide_progress();
			if (frm._mpesa_status_timeout) {
				clearTimeout(frm._mpesa_status_timeout);
				frm._mpesa_status_timeout = null;
			}
			frappe.msgprint({
				message: __(data.message),
				title: __(data.title),
				indicator:
					data.status === "error"
						? "red"
						: data.status === "warning"
						? "orange"
						: "green",
			});
			if (data.document_name) {
				frappe.show_alert({
					message: __("View transaction: {0}", [data.document_name]),
					indicator: "green",
				});
			}
		};
		frappe.realtime.on("mpesa_transaction_status_update", frm._mpesa_status_handler);

		// Pull transaction completion listener — same off/on pattern
		if (frm._mpesa_pull_handler) {
			frappe.realtime.off("mpesa_pull_transaction_complete", frm._mpesa_pull_handler);
		}
		frm._mpesa_pull_handler = (data) => {
			frappe.hide_progress();
			if (frm._mpesa_pull_timeout) {
				clearTimeout(frm._mpesa_pull_timeout);
				frm._mpesa_pull_timeout = null;
			}
			frappe.msgprint({
				message: __(data.message),
				title: __(data.title),
				indicator:
					data.status === "error"
						? "red"
						: data.status === "warning"
						? "orange"
						: "green",
			});
		};
		frappe.realtime.on("mpesa_pull_transaction_complete", frm._mpesa_pull_handler);

		frm.add_custom_button(
			__("Register Pull Transaction"),
			() => frm.events.register_pull_transaction_action(frm),
			__("Pull Transaction")
		);
		frm.add_custom_button(
			__("Pull Transactions"),
			() => frm.events.pull_transactions_action(frm),
			__("Pull Transaction")
		);
	},

	get_account_balance: function (frm) {
		if (!frm.doc.initiator_name && !frm.doc.security_credential) {
			frappe.throw(__("Please set the initiator name and the security credential"));
		}
		frappe.call({
			method: "get_account_balance_info",
			doc: frm.doc,
		});
	},

	setup_account_balance_html: function (frm) {
		if (!frm.doc.account_balance) return;

		let text = frm.doc.account_balance;
		let rows = text.split("&");

		let data = {};

		rows.forEach((row) => {
			let parts = row.split("|");
			if (parts.length >= 6) {
				let [
					account_type,
					currency,
					current_balance,
					available_balance,
					reserved_balance,
					uncleared_balance,
				] = parts;

				data[account_type] = {
					currency,
					current_balance,
					available_balance,
					reserved_balance,
					uncleared_balance,
				};
			}
		});

		$("div").remove(".form-dashboard-section.custom");

		frm.dashboard.add_section(frappe.render_template("account_balance", { data: data }));

		frm.dashboard.show();
	},

	check_transaction_status: function (frm) {
		if (!frm.doc.initiator_name && !frm.doc.security_credential) {
			frappe.throw(__("Please set the initiator name and the security credential"));
		}
		frappe.clear_cache;
		frappe.prompt(
			[
				{
					label: "Transaction ID",
					fieldname: "transaction_id",
					fieldtype: "Data",
					reqd: 1,
				},
				{
					label: "Remarks",
					fieldname: "remarks",
					fieldtype: "Small Text",
				},
			],
			(values) => {
				frappe.call({
					method: "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status",
					args: {
						mpesa_settings: frm.doc.name,
						transaction_id: values.transaction_id,
						remarks: values.remarks,
					},
					callback: (r) => {
						if (r.message) {
							if (r.message.status === "error") {
								frappe.hide_progress();
								frappe.msgprint({
									message: __(r.message.message),
									title: __("Error"),
									indicator: "red",
								});
							} else {
								frappe.show_progress(
									__("Processing"),
									50,
									100,
									__("Waiting for M-Pesa callback...")
								);
								// Auto-cancel progress if Safaricom never calls back
								frm._mpesa_status_timeout = setTimeout(() => {
									frappe.hide_progress();
									frappe.show_alert({
										message: __(
											"No callback received from M-Pesa. Check Error Logs."
										),
										indicator: "orange",
									});
								}, 60000);
							}
						}
					},
					error: (err) => {
						frappe.msgprint({
							message: __("An error occurred: {0}", [err.message]),
							title: "Error",
							indicator: "red",
						});
					},
				});
			},
			__("Transaction Status Query"),
			__("Submit")
		);
	},

	register_pull_transaction_action: function (frm) {
		if (!frm.doc.pull_transaction_nominated_number) {
			frappe.throw(__("Please set the Pull Transaction Nominated Number first."));
			return;
		}
		frappe.confirm(
			__("Register nominated number {0} with Safaricom for Pull Transaction?", [
				frm.doc.pull_transaction_nominated_number,
			]),
			() => {
				frappe.call({
					method: "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.register_pull_transaction",
					args: { mpesa_settings: frm.doc.name },
					freeze: true,
					freeze_message: __("Registering with Safaricom..."),
					callback: (r) => {
						if (r.message && r.message.status === "success") {
							frappe.show_alert({
								message: __("Pull Transaction registered successfully."),
								indicator: "green",
							});
						} else {
							frappe.msgprint({
								message: __(
									r.message?.message ||
										__("Registration failed. Check Error Logs.")
								),
								title: __("Registration Error"),
								indicator: "red",
							});
						}
					},
					error: (err) => {
						frappe.msgprint({
							message: __("An error occurred: {0}", [
								err.message || "Unknown error",
							]),
							title: __("Error"),
							indicator: "red",
						});
					},
				});
			}
		);
	},

	pull_transactions_action: function (frm) {
		if (!frm.doc.pull_transaction_nominated_number) {
			frappe.throw(
				__("Please set the Pull Transaction Nominated Number and register before pulling.")
			);
			return;
		}

		const DATE_FORMAT = "YYYY-MM-DD HH:mm:ss";
		const PULL_WINDOW_HOURS = 48;

		const render_date_range_display = () => {
			const end_value = d.get_value("end_date") || frappe.datetime.now_datetime();
			const end_moment = moment(end_value, DATE_FORMAT);
			const start_moment = end_moment.clone().subtract(PULL_WINDOW_HOURS, "hours");
			d.fields_dict.date_range_display.$wrapper.html(
				`<p>${__("Pulling transactions from {0} to {1}", [
					`<b>${start_moment.format(DATE_FORMAT)}</b>`,
					`<b>${end_moment.format(DATE_FORMAT)}</b>`,
				])}</p>`
			);
		};

		const d = new frappe.ui.Dialog({
			title: __("Pull Transactions"),
			fields: [
				{
					label: __("End Date"),
					fieldname: "end_date",
					fieldtype: "Datetime",
					reqd: 1,
					default: frappe.datetime.now_datetime(),
					onchange: () => render_date_range_display(),
				},
				{
					fieldname: "date_range_display",
					fieldtype: "HTML",
				},
			],
			primary_action_label: __("Pull"),
			primary_action: (values) => {
				const end_moment = moment(values.end_date, DATE_FORMAT);
				const start_date = end_moment
					.clone()
					.subtract(PULL_WINDOW_HOURS, "hours")
					.format(DATE_FORMAT);

				frappe.call({
					method: "mpsa_suite.mpsa_suite.api.m_pesa_api.pull_transactions",
					args: {
						mpesa_settings: frm.doc.name,
						start_date: start_date,
						end_date: values.end_date,
					},
					freeze: true,
					freeze_message: __("Pulling transactions from M-Pesa..."),
					callback: (r) => {
						if (r.message) {
							if (r.message.status === "error") {
								frappe.msgprint({
									message: __(r.message.message),
									title: __("Error"),
									indicator: "red",
								});
							} else {
								frappe.show_progress(
									__("Processing"),
									50,
									100,
									__("Importing transactions...")
								);
								frm._mpesa_pull_timeout = setTimeout(() => {
									frappe.hide_progress();
									frappe.show_alert({
										message: __(
											"Import is taking longer than expected. Check Error Logs."
										),
										indicator: "orange",
									});
								}, 60000);
							}
						}
					},
					error: (err) => {
						frappe.msgprint({
							message: __("An error occurred: {0}", [
								err.message || "Unknown error",
							]),
							title: __("Error"),
							indicator: "red",
						});
					},
				});

				d.hide();
			},
		});

		d.show();
		render_date_range_display();
	},

	update_match_field_options: function (frm, cdt, cdn) {
		const row = frappe.get_doc(cdt, cdn);
		const target_doctype = row.target_doctype;

		if (!target_doctype) {
			frappe.utils.filter_dict(
				frm.fields_dict["reconciliation_order"].grid.grid_rows_by_docname[cdn].docfields,
				{ fieldname: "match_field" }
			)[0].options = [];
			frm.refresh();
			return;
		}

		frappe.call({
			method: "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.get_doctype_fields",
			args: { doctype: target_doctype },
			callback: function (r) {
				if (r && r.message) {
					let fields = ["", "name", ...r.message];

					frappe.utils.filter_dict(
						frm.fields_dict["reconciliation_order"].grid.grid_rows_by_docname[cdn]
							.docfields,
						{ fieldname: "match_field" }
					)[0].options = fields;

					frm.refresh();
				} else {
					frappe.msgprint(
						__("Could not load field information for DocType: {0}", [target_doctype])
					);
				}
			},
		});
	},
});

frappe.ui.form.on("Mpesa Reconciliation Priority", {
	target_doctype: function (frm, cdt, cdn) {
		frm.events.update_match_field_options(frm, cdt, cdn);
	},

	refresh: function (frm, cdt, cdn) {
		frm.events.update_match_field_options(frm, cdt, cdn);
	},
});

frappe.ui.form.on("Mpesa Conversion Rate", {
	currency: function (frm, cdt, cdn) {
		validate_kes_currency(frm, cdt, cdn);
	},

	exchange_rates_add: function (frm, cdt, cdn) {
		validate_kes_currency(frm, cdt, cdn);
	},
});

function validate_kes_currency(frm, cdt, cdn) {
	const row = locals[cdt][cdn];

	if (row.currency === "KES") {
		row.currency = null;
		frm.refresh_field("exchange_rates");
	}
}

// M-Pesa Suite: rebuild the B2C/B2B/status SecurityCredential on demand.
frappe.ui.form.on("Mpesa Settings", {
	refresh(frm) {
		if (frm.is_new() || !frm.doc.initiator_name) return;
		frm.add_custom_button(
			__("Regenerate Security Credential"),
			() => frm.call("regenerate_security_credential").then((r) => {
				frappe.show_alert({ message: r.message, indicator: "green" });
				frm.reload_doc();
			}),
			__("Security")
		);
	},
});
