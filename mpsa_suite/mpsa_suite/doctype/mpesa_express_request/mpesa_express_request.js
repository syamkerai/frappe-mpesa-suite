// Copyright (c) 2025, Navari Limited and contributors
// For license information, please see license.txt

const HAS_ERPNEXT = frappe.boot?.app_data?.some((app) => app.app_name === "erpnext");

frappe.ui.form.on("Mpesa Express Request", {
	refresh(frm) {
		// one listener per form: refresh runs on every reload, and each on() added another
		if (frm._mpesa_refresh_handler) {
			frappe.realtime.off("refresh_form", frm._mpesa_refresh_handler);
		}
		frm._mpesa_refresh_handler = () => frm.reload_doc();
		frappe.realtime.on("refresh_form", frm._mpesa_refresh_handler);
		// the Mpesa Actions call endpoints for COLLECT_ROLES (Reconcile: ADMIN_ROLES) only
		const can_collect = Boolean(frappe.boot.mpsa?.can_collect);

		autofill_gateway_settings(frm);

		if (frm.doc.route) {
			frm.add_web_link(`${frm.doc.route}`, "View STK Push Page");
		}

		convertAmountToKES(frm);

		if (frm.doc.status !== "Completed" && frm.doc.docstatus == 1 && can_collect) {
			frm.add_custom_button(
				__("Check Transaction Status"),
				function () {
					frappe.call({
						method: "mpsa_suite.mpsa_suite.api.m_pesa_api.check_transaction_status",
						args: {
							name: frm.doc.name,
						},
						freeze: true,
						freeze_message: __("Checking Transaction Status..."),
						callback: function (r) {
							if (r && r.message) {
								const res = r.message;

								if (res.ResultDesc && res.ResponseDescription) {
									const fullMessage = `
                  <b>Response:</b> ${res.ResponseDescription}<br>
                  <b>Result:</b> ${res.ResultDesc}
                `;

									const indicator = res.ResultCode === "0" ? "green" : "red";

									frappe.msgprint({
										message: fullMessage,
										indicator: indicator,
										title: __("Transaction Status"),
									});

									frm.reload_doc();
								} else if (res.errorMessage) {
									frappe.msgprint({
										message: res.errorMessage,
										indicator: "red",
										title: __("Error"),
									});
								} else {
									frappe.msgprint(
										__(
											"Missing ResultDesc or ResponseDescription in response.",
										),
									);
								}
							} else {
								frappe.msgprint(__("No response received."));
							}
						},
					});
				},
				__("Mpesa Actions"),
			);
		}

		if (frm.doc.docstatus == 1 && can_prompt_again(frm) && can_collect) {
			frm.add_custom_button(
				__("Initiate STK Push"),
				function () {
					frappe.call({
						method: "initiate_request",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Initiating STK Push..."),
						// args: {
						// 	document_name: frm.doc.name,
						// 	doctype: frm.doc.doctype,
						// 	payment_gateway: frm.doc.payment_gateway,
						// 	phone_number: frm.doc.phone_number,
						// 	request_amount: frm.doc.amount,
						// },
						// initiate_request returns nothing: no error means the prompt went out
						callback: function (r) {
							if (r.exc) return;
							frappe.msgprint({
								message: __("STK Push sent to {0}. The request updates when the customer answers.", [
									frappe.utils.escape_html(frm.doc.phone_number || ""),
								]),
								indicator: "green",
								title: __("STK Push Initiated"),
							});
							frm.reload_doc();
						},
					});
				},
				__("Mpesa Actions"),
			);
		}

		if (
			!frm.doc.is_reconciled &&
			frm.doc.status === "Completed" &&
			frm.doc.docstatus == 1 &&
			frappe.boot.mpsa?.can_approve
		) {
			frm.add_custom_button(
				__("Reconcile Payment"),
				function () {
					frappe.call({
						method: "reconcile_payment",
						doc: frm.doc,
						freeze: true,
						freeze_message: __("Reconciling Payment..."),
						callback: function (r) {
							console.log(r);
						},
					});
				},
				__("Mpesa Actions"),
			);
		}
	},

	settings: function (frm) {
		autofill_gateway_settings(frm);
	},
	payment_gateway: function (frm) {
		autofill_gateway_settings(frm);
	},

	base_amount: function (frm) {
		convertAmountToKES(frm);
	},

	currency: function (frm) {
		convertAmountToKES(frm);
	},
});

// The server refuses these too (m_pesa_api._check_can_prompt_again): a paid request, or one
// whose last callback is still being settled or waits for a person.
function can_prompt_again(frm) {
	return (
		frm.doc.status !== "Completed" &&
		!frm.doc.transaction_id &&
		!["Pending", "Mismatch", "Unverified"].includes(frm.doc.verification_status)
	);
}

function autofill_gateway_settings(frm) {
	if (frm.doc.settings && !frm.doc.payment_gateway) {
		frappe.db
			.get_value("Payment Gateway", { gateway_controller: frm.doc.settings }, "name")
			.then((r) => {
				if (r.message) {
					frm.set_value("payment_gateway", r.message.name);
				}
			});
	} else if (frm.doc.payment_gateway && !frm.doc.settings) {
		frappe.db
			.get_value("Payment Gateway", { name: frm.doc.payment_gateway }, "gateway_controller")
			.then((r) => {
				if (r.message) {
					frm.set_value("settings", r.message.gateway_controller);
				}
			});
	}
}

function convertAmountToKES(frm) {
	if (frm.doc.currency && frm.doc.currency !== "KES" && frm.doc.base_amount) {
		frappe.call({
			method: "mpsa_suite.utils.utils.convert_amount_to_kes",
			args: {
				amount: frm.doc.base_amount,
				currency: frm.doc.currency,
			},
			callback: function (r) {
				if (r.message) {
					if (r.message !== frm.doc.amount) {
						frm.set_value("amount", r.message);
					}
				}
			},
		});
	} else {
		frm.set_value("amount", frm.doc.amount || 0);
	}
}
