frappe.listview_settings["Mpesa C2B Payment Register"] = {
	// A receipt is unmatched, matched to a document (or only to a customer) and waiting, or booked.
	add_fields: ["docstatus", "payment_entry", "sales_invoice", "pos_invoice", "matched_reference_name", "customer"],
	has_indicator_for_draft: true,
	get_indicator(doc) {
		if (doc.docstatus === 2) return [__("Cancelled"), "red", "docstatus,=,2"];
		if (doc.docstatus === 1) {
			if (doc.payment_entry) return [__("Booked"), "green", "payment_entry,is,set"];
			if (doc.sales_invoice || doc.pos_invoice) return [__("On Invoice"), "green", "docstatus,=,1"];
			return [__("Submitted"), "blue", "docstatus,=,1"];
		}
		if (doc.matched_reference_name) return [__("Matched"), "blue", "matched_reference_name,is,set"];
		if (doc.customer) return [__("Customer Matched"), "yellow", "customer,is,set"];
		return [__("Unmatched"), "orange", "customer,is,not set"];
	},
	onload: function (listview) {
		// Always remove the previous listener before re-registering.
		// The guard-on-instance approach fails on navigation because each
		// page load creates a new listview object while old socket listeners
		// from the previous instance persist on the global event bus.
		if (window._mpesa_c2b_status_handler) {
			frappe.realtime.off(
				"mpesa_transaction_status_update",
				window._mpesa_c2b_status_handler
			);
		}
		window._mpesa_c2b_status_handler = (data) => {
			frappe.hide_progress();
			if (window._mpesa_c2b_status_timeout) {
				clearTimeout(window._mpesa_c2b_status_timeout);
				window._mpesa_c2b_status_timeout = null;
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

			listview.refresh();
		};
		frappe.realtime.on("mpesa_transaction_status_update", window._mpesa_c2b_status_handler);
		// Add a custom button to the page actions (top bar)
		listview.page.add_inner_button(__("Check Transaction Status"), function () {
			frappe.prompt(
				[
					{
						label: "Mpesa Settings",
						fieldname: "mpesa_settings",
						fieldtype: "Link",
						options: "Mpesa Settings",
						reqd: 1,
					},
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
						default: "OK",
						hidden: 1,
					},
				],
				(values) => {
					// the server checks the initiator: only M-Pesa admins can read those fields
					frappe.call({
						method: "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings.trigger_transaction_status",
						args: {
							mpesa_settings: values.mpesa_settings,
							transaction_id: values.transaction_id,
							remarks: values.remarks || "OK",
						},
						freeze: true,
						freeze_message: __("Checking transaction status..."),
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
									window._mpesa_c2b_status_timeout = setTimeout(() => {
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
							frappe.hide_progress();
							frappe.msgprint({
								message: __("An error occurred: {0}", [
									err.message || "Unknown error",
								]),
								title: "Error",
								indicator: "red",
							});
						},
					});
				},
				__("Transaction Status Query"),
				__("Submit")
			);
		});
	},
};
