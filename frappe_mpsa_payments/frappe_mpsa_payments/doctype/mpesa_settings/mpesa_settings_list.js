frappe.listview_settings["Mpesa Settings"] = {
	add_fields: ["pull_registration_status", "last_pull_status"],

	get_indicator: function (doc) {
		if (doc.last_pull_status === "Success") {
			return [__("Pull OK"), "green", "last_pull_status,=,Success"];
		}
		if (doc.last_pull_status === "No Data") {
			return [__("Pull: No Data"), "orange", "last_pull_status,=,No Data"];
		}
		if (doc.last_pull_status === "Error") {
			return [__("Pull Error"), "red", "last_pull_status,=,Error"];
		}
		return [__("Not Pulled"), "gray", "last_pull_status,=,"];
	},

	onload: function (listview) {
		listview.page.add_action_item(__("Register Pull Transactions"), () => {
			bulk_register(listview.get_checked_items(true) || []);
		});

		listview.page.add_action_item(__("Pull Transactions"), () => {
			bulk_pull(listview.get_checked_items(true) || []);
		});

		listview.page.add_menu_item(__("Register Pull Transactions (All)"), () => {
			bulk_register([]);
		});

		listview.page.add_menu_item(__("Pull Transactions (All Enabled)"), () => {
			bulk_pull([]);
		});
	},
};

function bulk_pull(selected) {
	// Matches the single-doc dialog: Safaricom's window is 48h, so only the
	// end is worth asking for. All pages within it are fetched automatically.
	const DATE_FORMAT = "YYYY-MM-DD HH:mm:ss";
	const PULL_WINDOW_HOURS = 48;

	const render_range = () => {
		const end_value = dialog.get_value("end_date") || frappe.datetime.now_datetime();
		const end_moment = moment(end_value, DATE_FORMAT);
		const start_moment = end_moment.clone().subtract(PULL_WINDOW_HOURS, "hours");
		dialog.fields_dict.date_range_display.$wrapper.html(
			`<p>${__("Pulling transactions from {0} to {1}", [
				`<b>${start_moment.format(DATE_FORMAT)}</b>`,
				`<b>${end_moment.format(DATE_FORMAT)}</b>`,
			])}</p>`
		);
	};

	const dialog = new frappe.ui.Dialog({
		title: selected.length
			? __("Pull Transactions for {0} shortcode(s)", [selected.length])
			: __("Pull Transactions for all hourly-enabled shortcodes"),
		fields: [
			{
				fieldname: "end_date",
				fieldtype: "Datetime",
				label: __("End Date"),
				reqd: 1,
				default: frappe.datetime.now_datetime(),
				onchange: () => render_range(),
			},
			{
				fieldname: "date_range_display",
				fieldtype: "HTML",
			},
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Runs in background. Check error log for combined result. Existing transactions get skipped automatically."
				)}</p>`,
			},
		],
		primary_action_label: __("Pull"),
		primary_action(values) {
			const start_date = moment(values.end_date, DATE_FORMAT)
				.subtract(PULL_WINDOW_HOURS, "hours")
				.format(DATE_FORMAT);
			dialog.hide();
			frappe.call({
				method: "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.bulk_pull_transactions",
				args: {
					settings_names: selected,
					start_date: start_date,
					end_date: values.end_date,
				},
				freeze: true,
				freeze_message: __("Queueing pull..."),
				callback: function (r) {
					if (r.message) {
						frappe.show_alert({
							message: r.message.message,
							indicator: r.message.status === "error" ? "red" : "blue",
						});
					}
				},
			});
		},
	});
	dialog.show();
	render_range();
}

function bulk_register(selected) {
	// Preview first. Registration cannot be undone from our side: Safaricom
	// rejects re-registration with "Shortcode already Registered!", so a wrong
	// CallBackURL baked in here is permanent. Show it before committing.
	frappe.call({
		method: "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.bulk_register_pull_transactions",
		args: { settings_names: selected, dry_run: 1 },
		callback: function (r) {
			if (!r.message || r.message.status === "error") {
				frappe.msgprint(r.message ? r.message.message : __("Nothing to register."));
				return;
			}

			const msg = r.message;
			frappe.confirm(
				__(
					"Register <b>{0}</b> shortcode(s) for Pull Transactions?<br><br>" +
						"Callback URL that will be sent to Safaricom:<br><code>{1}</code>" +
						"<br><br><b>This cannot be undone.</b> Safaricom rejects re-registration, " +
						"so make sure you are on the correct production domain before continuing.",
					[msg.count, frappe.utils.escape_html(msg.callback_url)]
				),
				() => {
					frappe.call({
						method: "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings.bulk_register_pull_transactions",
						args: { settings_names: selected, dry_run: 0 },
						freeze: true,
						freeze_message: __("Queueing registration..."),
						callback: function (res) {
							if (res.message) {
								frappe.show_alert({
									message: res.message.message,
									indicator: "blue",
								});
							}
						},
					});
				}
			);
		},
	});
}

frappe.realtime.on("mpesa_bulk_pull_registration_complete", function (data) {
	frappe.msgprint({
		title: data.title || __("Bulk Pull Registration Complete"),
		message: data.message,
		indicator: data.status === "success" ? "green" : "orange",
	});
});

frappe.realtime.on("mpesa_bulk_pull_complete", function (data) {
	frappe.msgprint({
		title: data.title || __("Bulk Pull Complete"),
		message: data.message,
		indicator: data.status === "success" ? "green" : "orange",
	});
	if (cur_list && cur_list.doctype === "Mpesa Settings") {
		cur_list.refresh();
	}
});
