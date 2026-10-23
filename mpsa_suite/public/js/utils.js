/* global mpesa_qp */
frappe.provide("mpesa_qp");

mpesa_qp.active_dialogs = mpesa_qp.active_dialogs || {
	c2b: null,
	request: null,
};
mpesa_qp.pending_stk_request = mpesa_qp.pending_stk_request || null;

mpesa_qp.close_active_dialogs = function () {
	for (const key of ["request", "c2b"]) {
		const dialog = mpesa_qp.active_dialogs[key];
		if (!dialog) continue;

		try {
			dialog.hide?.();
		} catch (error) {
			console.error(error);
		}

		try {
			dialog.$wrapper?.modal("hide");
			dialog.$wrapper?.removeClass("in show").hide();
		} catch (error) {
			console.error(error);
		}

		mpesa_qp.active_dialogs[key] = null;
	}

	$(".modal[data-mpesa-dialog], .modal[data-mpesa-dialog] .modal").each(function () {
		const $modal = $(this).closest(".modal");
		try {
			$modal.modal("hide");
		} catch (error) {
			console.error(error);
		}
		$modal.removeClass("in show").hide().remove();
	});

	$("body").removeClass("modal-open");
	$(".modal-backdrop").remove();
};

/**
 * Show the Mpesa Quick Pay dialog.
 *
 * @param {object}   frm                - Frappe form object
 * @param {string}   page               - "invoice" (the POS page integration was removed)
 * @param {function} process_mpesa      - callback(frm, dialog) fired on "Add Payments"
 * @param {number}   outstanding        - amount still to pay, shown in the header
 * @param {object}   config             - { auto_save_invoice, auto_submit_invoice, merge_payments }
 * @param {array}    initial_selections - already-queued payments [ { name, amount, full_name } ]
 * @param {object}   mop_config         - POS page only: { mop, account, shortcode }
 *                                        Pass null on the invoice desk.
 */
mpesa_qp.show_dialog = function (
	frm,
	page,
	process_mpesa,
	outstanding,
	config,
	initial_selections = [],
	mop_config = null
) {
	if (outstanding <= 0 && !initial_selections.length) {
		frappe.msgprint(__("No outstanding amount to pay"));
		return;
	}

	const dialog = new frappe.ui.Dialog({
		title: mop_config ? __(`Mpesa C2B - ${mop_config.mop}`) : __("Mpesa C2B - Mpesa"),
		size: "large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "payment_summary",
				options: mpesa_summary_html(frm, outstanding, mop_config),
			},
			{ fieldtype: "Section Break", label: __("Select Mpesa Payments") },
			{ fieldtype: "HTML", fieldname: "mpesa_list" },
			{ fieldtype: "Section Break" },
			{ fieldtype: "HTML", fieldname: "mpesa_totals" },
			{
				fieldtype: "Section Break",
				fieldname: "payment_section",
				label: __("Payment Options"),
				hidden: 1,
			},
			{ fieldtype: "HTML", fieldname: "payment_options_html" },
		],
		primary_action_label: __("Add Payments"),
		primary_action: () => process_mpesa(frm, dialog),
		secondary_action_label: __("Request Payment"),
		secondary_action: () => mpesa_show_request_dialog(frm, outstanding, mop_config, dialog),
	});

	dialog.page = page;
	dialog.doctype = frm.doctype;
	dialog.company = frm.doc.company;
	dialog.pos_profile = frm.doc.pos_profile || null;
	dialog.currency = frm.doc.currency;
	dialog.outstanding = outstanding;
	dialog.auto_save = config.auto_save_invoice;
	dialog.auto_submit = config.auto_submit_invoice;
	dialog.merge_payments = config.merge_payments;
	dialog.mop_config = mop_config;
	dialog.search_term = "";
	dialog.selected = Array.isArray(initial_selections) ? [...initial_selections] : [];
	dialog.mpesa_data = { count: 0, payments: [] };
	mpesa_qp.active_dialogs.c2b = dialog;

	dialog.$wrapper.find(".modal-dialog").css("max-width", "800px");
	dialog.$wrapper.attr("data-mpesa-dialog", "c2b");
	dialog.$wrapper.on("hidden.bs.modal", () => {
		if (mpesa_qp.active_dialogs.c2b === dialog) {
			mpesa_qp.active_dialogs.c2b = null;
		}
		if (!mpesa_qp.active_dialogs.request) {
			mpesa_qp.pending_stk_request = null;
		}
	});
	dialog.show();
	mpesa_inject_dialog_styles();

	dialog.fields_dict.mpesa_list.$wrapper.html(
		`<div class="text-center text-muted p-4"><i class="fa fa-spinner fa-spin"></i> ${__(
			"Loading…"
		)}</div>`
	);
	mpesa_load_payments(dialog, "");
};

function mpesa_summary_html(frm, outstanding, mop_config) {
	const paid = flt(frm.doc.paid_amount || 0);
	const total = flt(frm.doc.grand_total || 0);
	const percent = total > 0 ? Math.round((paid / total) * 100) : 0;

	return `
		<div class="mpesa-pay-header">
			<div class="mpesa-header-icon"><i class="fa fa-mobile"></i></div>
			<div class="mpesa-header-info">
				${
					mop_config
						? `
				<div class="mpesa-row">
					<span>${__("Payment Mode")}</span>
					<strong>${frappe.utils.escape_html(mop_config.mop)}</strong>
				</div>`
						: ""
				}
				<div class="mpesa-row">
					<span>${__("Type")}</span>
					<strong>${frm.doctype}</strong>
				</div>
				<div class="mpesa-row">
					<span>${__("Customer")}</span>
					<strong>${frm.doc.customer_name || frm.doc.customer}</strong>
				</div>
				<div class="mpesa-row">
					<span>${__("Grand Total")}</span>
					<strong>${format_currency(total, frm.doc.currency)}</strong>
				</div>
				<div class="mpesa-row">
					<span>${__("Already Paid")}</span>
					<strong class="text-success">${format_currency(
						paid,
						frm.doc.currency
					)} <small>(${percent}%)</small></strong>
				</div>
				<div class="mpesa-row mpesa-outstanding">
					<span>${__("Outstanding")}</span>
					<strong>${format_currency(outstanding, frm.doc.currency)}</strong>
				</div>
			</div>
		</div>`;
}

function mpesa_load_payments(dialog, search) {
	// POS page  → mop_config is set  → pass mode_of_payment so the backend
	//             returns only payments for that MOP's shortcode.
	// Invoice desk → mop_config is null → pass pos_profile so the backend
	//             returns payments for all eligible MOPs on the profile.
	const args = {
		company: dialog.company,
		search: search || "",
	};

	if (dialog.mop_config) {
		args.mode_of_payment = dialog.mop_config.mop;
	} else if (dialog.pos_profile) {
		args.pos_profile = dialog.pos_profile;
	}

	frappe.call({
		method: "mpsa_suite.mpsa_suite.api.mpesa_quick_pay.get_mpesa_payments",
		args,
		callback(r) {
			dialog.mpesa_data = r.message || { count: 0, payments: [] };
			mpesa_render_list(dialog);
			mpesa_render_totals(dialog);
		},
	});
}

function mpesa_render_list(dialog) {
	const wrapper = dialog.fields_dict.mpesa_list.$wrapper;
	const count = dialog.mpesa_data.count || 0;
	const payments = dialog.mpesa_data.payments || [];

	if (count === 0) {
		wrapper.html(`
			<div class="mpesa-empty-state">
				<i class="fa fa-inbox fa-3x text-muted"></i>
				<p class="mt-3">${__("No pending Mpesa payments found")}</p>
				<small class="text-muted">${__("Payments will appear here once received")}</small>
			</div>`);
		return;
	}

	let html = `
		<div class="mpesa-search-section">
			<div class="mpesa-search-box">
				<i class="fa fa-search mpesa-search-icon"></i>
				<input type="text" class="form-control" id="mpesa-search"
					   placeholder="${__("Search name, phone, transaction ID, reference (min 3 chars)…")}"
					   value="${frappe.utils.escape_html(dialog.search_term || "")}">
			</div>
			<div class="mpesa-count-badge">
				<i class="fa fa-mobile"></i>
				<span><strong>${count}</strong> ${__("pending")}</span>
			</div>
		</div>
		<div class="mpesa-payments-list" id="mpesa-payments-list">`;

	if (payments.length === 0 && (dialog.search_term || "").length >= 3) {
		html += `<div class="mpesa-no-results"><i class="fa fa-search text-muted"></i><p>${__(
			"No payments match your search"
		)}</p></div>`;
	} else if (payments.length === 0) {
		html += `<div class="mpesa-search-prompt"><i class="fa fa-hand-o-up text-muted"></i><p>${__(
			"Enter a search term above to find payments"
		)}</p></div>`;
	} else {
		html += `
			<div class="mpesa-results-header">
				<label class="mpesa-checkbox-label mpesa-select-all-label">
					<input type="checkbox" id="mpesa-select-all">
					<span>${__("Select All")} (${payments.length} ${__("results")})</span>
				</label>
			</div>`;

		const today = frappe.datetime.get_today();
		for (const p of payments) {
			const amount = flt(p.transamount || 0);
			const is_selected = dialog.selected.some((x) => x.name === p.name);
			const is_exact = Math.abs(amount - dialog.outstanding) < 0.01;
			const is_over = amount > dialog.outstanding;
			const age_days = mpesa_age_days(p.posting_date || p.creation, today);

			let item_class = is_selected ? "selected" : "";
			if (is_exact) item_class += " exact-match";
			else if (is_over) item_class += " overpayment-warning";

			html += `
				<div class="mpesa-payment-item ${item_class}">
					<div class="mpesa-item-checkbox">
						<input type="checkbox" class="mpesa-item-check"
							   data-name="${frappe.utils.escape_html(p.name || "")}"
							   data-full-name="${frappe.utils.escape_html(p.full_name || "")}"
							   data-amount="${amount}"
							   ${is_selected ? "checked" : ""}>
					</div>
					<div class="mpesa-item-info">
						<div class="mpesa-item-primary">
							<span class="mpesa-sender-name">${frappe.utils.escape_html(p.full_name || __("Unknown"))}</span>
							<span class="mpesa-amount ${is_over && !is_exact ? "text-warning" : ""}">
								${format_currency(amount, dialog.currency)}
								${is_exact ? `<span class="mpesa-exact-badge">${__("EXACT")}</span>` : ""}
								${
									is_over && !is_exact
										? `<i class="fa fa-exclamation-triangle" title="${__(
												"Exceeds outstanding"
										  )}"></i>`
										: ""
								}
							</span>
						</div>
						<div class="mpesa-item-secondary">
							<span><i class="fa fa-phone"></i> ${frappe.utils.escape_html(p.msisdn || "")}</span>
							${p.billrefnumber ? `<span><i class="fa fa-hashtag"></i> ${frappe.utils.escape_html(p.billrefnumber)}</span>` : ""}
							<span><i class="fa fa-exchange"></i> ${frappe.utils.escape_html(p.transid || p.name || "")}</span>
						</div>
					</div>
					<div class="mpesa-age-indicator"
						 style="${mpesa_age_style(age_days)}"
						 title="${frappe.utils.escape_html(p.posting_date || p.creation || "")}">
						${mpesa_age_label(age_days)}
					</div>
				</div>`;
		}
	}

	html += "</div>";
	wrapper.html(html);
	mpesa_bind_list_events(dialog, wrapper);
}

function mpesa_bind_list_events(dialog, wrapper) {
	let timeout;
	const $search = wrapper.find("#mpesa-search");

	$search.on("input", function () {
		const val = $(this).val().trim();
		clearTimeout(timeout);
		if (val.length >= 3 || val.length === 0) {
			timeout = setTimeout(() => {
				dialog.search_term = val;
				mpesa_load_payments(dialog, val);
			}, 300);
		}
	});

	if ($search.length) {
		$search.focus();
		if (dialog.search_term) {
			const v = $search.val();
			$search.val("").val(v);
		}
	}

	wrapper.find("#mpesa-select-all").on("change", function () {
		wrapper
			.find(".mpesa-item-check")
			.prop("checked", $(this).is(":checked"))
			.trigger("change");
	});

	wrapper.find(".mpesa-item-check").on("change", function () {
		const name = $(this).data("name");
		const amount = flt($(this).data("amount"));
		const full_name = $(this).data("full-name");
		const checked = $(this).is(":checked");

		if (checked) {
			if (!dialog.selected.find((x) => x.name === name)) {
				dialog.selected.push({ name, amount, full_name });
			}
		} else {
			dialog.selected = dialog.selected.filter((x) => x.name !== name);
		}

		$(this).closest(".mpesa-payment-item").toggleClass("selected", checked);
		mpesa_render_totals(dialog);
	});
}

function mpesa_render_totals(dialog) {
	const wrapper = dialog.fields_dict.mpesa_totals.$wrapper;
	const total_sel = dialog.selected.reduce((s, p) => s + flt(p.amount), 0);
	const remaining = dialog.outstanding - total_sel;
	const overpayment = total_sel > dialog.outstanding;
	const excess = overpayment ? total_sel - dialog.outstanding : 0;

	if (total_sel > 0) {
		dialog.set_df_property("payment_section", "hidden", 0);
		invoice_payment_options(dialog);
	} else {
		dialog.set_df_property("payment_section", "hidden", 1);
	}

	wrapper.html(`
		<div class="mpesa-totals-bar ${overpayment ? "mpesa-overpayment" : ""}">
			<div class="mpesa-total-item">
				<span>${__("Selected")}</span>
				<strong>${dialog.selected.length} ${__("payment(s)")}</strong>
			</div>
			<div class="mpesa-total-item">
				<span>${__("Total Amount")}</span>
				<strong class="${total_sel >= dialog.outstanding ? "text-success" : ""}">
					${format_currency(total_sel, dialog.currency)}
				</strong>
			</div>
			<div class="mpesa-total-item">
				<span>${__("Remaining")}</span>
				<strong class="${remaining > 0 ? "text-warning" : "text-success"}">
					${format_currency(Math.max(0, remaining), dialog.currency)}
				</strong>
			</div>
			${
				overpayment
					? `
			<div class="mpesa-total-item mpesa-excess-warning">
				<span>${__("Excess")}</span>
				<strong class="text-warning">${format_currency(excess, dialog.currency)}</strong>
			</div>
			<div class="mpesa-total-item mpesa-overpay-note" style="width:100%">
				<span class="text-warning">
					<i class="fa fa-exclamation-triangle"></i>
					${__("Total exceeds outstanding. Excess will be recorded as change.")}
				</span>
			</div>`
					: ""
			}
		</div>`);
}

function invoice_payment_options(dialog) {
	const wrapper = dialog.fields_dict.payment_options_html.$wrapper;
	const fully_paid =
		dialog.selected.reduce((s, p) => s + flt(p.amount), 0) >= dialog.outstanding;

	wrapper.html(`
		<div class="mpesa-invoice-options">
			<label class="mpesa-checkbox-label">
				<input type="checkbox" id="mpesa-auto-save" ${dialog.auto_save ? "checked" : ""}>
				<span>${__("Auto-save invoice after adding payments")}</span>
			</label>
			${
				fully_paid
					? `
			<label class="mpesa-checkbox-label">
				<input type="checkbox" id="mpesa-auto-submit" ${dialog.auto_submit ? "checked" : ""}>
				<span>${__("Auto-submit invoice (if fully paid)")}</span>
			</label>`
					: ""
			}
			${
				dialog.selected.length > 1
					? `
			<label class="mpesa-checkbox-label">
				<input type="checkbox" id="mpesa-merge-payments" ${dialog.merge_payments ? "checked" : ""}>
				<span>${__("Merge selected payments into one payment")}</span>
			</label>`
					: ""
			}
		</div>`);

	wrapper.find("#mpesa-auto-save").on("change", function () {
		dialog.auto_save = $(this).is(":checked");
	});
	wrapper.find("#mpesa-auto-submit").on("change", function () {
		dialog.auto_submit = $(this).is(":checked");
	});
	wrapper.find("#mpesa-merge-payments").on("change", function () {
		dialog.merge_payments = $(this).is(":checked");
	});
}

function mpesa_show_request_dialog(frm, outstanding, mop_config, parent_dialog = null) {
	if (mpesa_qp.pending_stk_request?.invoice === frm.doc.name) {
		frappe.show_alert(
			{
				message: __("An STK push is already pending for this order."),
				indicator: "orange",
			},
			5
		);
		return;
	}

	frappe.db.get_value("Customer", frm.doc.customer, ["mobile_no"], function (value) {
		const phone = value.mobile_no || "";
		const req = new frappe.ui.Dialog({
			title: __("Request Mpesa Payment"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "request_info",
					options: `
						<div class="mpesa-request-info">
							${
								mop_config
									? `
							<div class="mpesa-row">
								<span>${__("Payment Mode")}</span>
								<strong>${frappe.utils.escape_html(mop_config.mop)}</strong>
							</div>`
									: ""
							}
							<div class="mpesa-req-row">
								<span>${__("Customer")}</span>
								<strong>${frm.doc.customer_name || frm.doc.customer}</strong>
							</div>
							<div class="mpesa-req-row">
								<span>${__("Amount to Request")}</span>
								<strong class="text-success">${format_currency(outstanding, frm.doc.currency)}</strong>
							</div>
						</div>`,
				},
				{
					fieldtype: "Data",
					fieldname: "phone_number",
					label: __("Phone Number"),
					reqd: 1,
					default: phone,
					description: __("Format: 0712345678 or 254712345678"),
				},
			],
			primary_action_label: __("Send Request"),
			primary_action(values) {
				if (!values.phone_number) {
					frappe.msgprint(__("Please enter a phone number"));
					return;
				}

				if (mpesa_qp.pending_stk_request?.invoice === frm.doc.name) {
					frappe.show_alert(
						{
							message: __("An STK push is already pending for this order."),
							indicator: "orange",
						},
						5
					);
					return;
				}

				frappe.call({
					method: "mpsa_suite.mpsa_suite.api.sales_invoice.initiate_invoice_stk_push",
					args: {
						invoice: frm.doc.name,
						phone_number: values.phone_number,
						amount: outstanding,
						currency: frm.doc.currency,
						mode_of_payment: mop_config.mop,
						company: frm.doc.company,
						type: frm.doc.doctype,
					},
					freeze: true,
					freeze_message: __("Processing Payment..."),
					callback(r) {
						if (r.message && r.message.status === "success") {
							mpesa_qp.pending_stk_request = {
								invoice: frm.doc.name,
								phone_number: values.phone_number,
							};

							if (parent_dialog?.get_secondary_btn) {
								const button = parent_dialog.get_secondary_btn();
								button?.prop("disabled", true).text(__("Waiting for Payment..."));
							}

							req.hide();

							frappe.show_alert(
								{
									message: __("STK Push sent to {0}. Processing Payment...", [
										values.phone_number,
									]),
									indicator: "blue",
								},
								5
							);

							const handler = (data) => {
								if (
									data.reference_doctype === frm.doc.doctype &&
									data.reference_name === frm.doc.name
								) {
									console.log("Debug Statement: ", data);
									frappe.realtime.off("mpesa_stk_payment_completed", handler);
									mpesa_qp.pending_stk_request = null;

									try {
										req.hide();
									} catch (e) {
										console.error(e);
									}

									mpesa_qp.close_active_dialogs();

									frappe.show_alert(
										{
											message: __("Payment Received! Transaction: {0}", [
												data.transaction_id,
											]),
											indicator: "green",
										},
										7
									);

									frm.refresh();
									console.log("Debug Statement: ", frm.doc);

									frappe.utils.play_sound("submit");
								}
							};
							frappe.realtime.on("mpesa_stk_payment_completed", handler);
						}
					},
					error(r) {
						mpesa_qp.pending_stk_request = null;
						if (parent_dialog?.get_secondary_btn) {
							const button = parent_dialog.get_secondary_btn();
							button?.prop("disabled", false).text(__("Request Payment"));
						}
						frappe.msgprint({
							title: __("Error"),
							message: r.message || __("Failed to send request"),
							indicator: "red",
						});
					},
				});
			},
		});
		mpesa_qp.active_dialogs.request = req;
		req.$wrapper.attr("data-mpesa-dialog", "request");
		req.$wrapper.on("hidden.bs.modal", () => {
			if (mpesa_qp.active_dialogs.request === req) {
				mpesa_qp.active_dialogs.request = null;
			}
		});
		req.show();
	});
}

function mpesa_age_days(date_str, today) {
	if (!date_str) return 0;
	return Math.max(
		0,
		Math.floor(
			(frappe.datetime.str_to_obj(today) - frappe.datetime.str_to_obj(date_str)) / 86400000
		)
	);
}

function mpesa_age_style(days) {
	if (days === 0) return "background:rgba(34,197,94,0.15);color:#16a34a;";
	if (days <= 3) return "background:rgba(234,179,8,0.15);color:#ca8a04;";
	if (days <= 7) return "background:rgba(249,115,22,0.15);color:#ea580c;";
	if (days <= 14) return "background:rgba(239,68,68,0.15);color:#dc2626;";
	return "background:rgba(185,28,28,0.2);color:#b91c1c;";
}

function mpesa_age_label(days) {
	if (days === 0) return __("Today");
	if (days === 1) return __("1 day");
	if (days < 7) return days + " " + __("days");
	if (days < 14) return __("1 week+");
	if (days < 30) return Math.floor(days / 7) + " " + __("weeks");
	return Math.floor(days / 30) + " " + __("month(s)");
}

mpesa_qp.inject_btn_styles = function () {
	if (document.getElementById("mpesa-pos-styles")) return;
	$(`<style id="mpesa-pos-styles">
		.mpesa-quick-pay-section { padding: 10px 15px; background: var(--bg-color); }
		.mpesa-quick-pay-btn {
			width: 100%; padding: 8px 16px;
			background: linear-gradient(135deg, #00a650 0%, #007a3d 100%);
			border: none; color: white; font-weight: 500; font-size: 14px; border-radius: 6px;
		}
		.mpesa-quick-pay-btn:hover {
			background: linear-gradient(135deg, #008f45 0%, #006632 100%);
			transform: translateY(-1px);
			box-shadow: 0 4px 8px rgba(0, 166, 80, 0.3);
		}
		.mpesa-summary-panel {
			margin-top: 8px; border: 1px solid rgba(0,166,80,.3);
			border-radius: 6px; overflow: hidden; font-size: 12px;
		}
		.mpesa-summary-header {
			display: flex; justify-content: space-between; align-items: center;
			padding: 6px 10px; background: rgba(0,166,80,.08); font-weight: 600; color: #007a3d;
		}
		.mpesa-summary-total { color: #007a3d; }
		.mpesa-summary-list { list-style: none; margin: 0; padding: 0; }
		.mpesa-summary-item {
			display: flex; align-items: center; gap: 6px;
			padding: 5px 10px; border-top: 1px solid rgba(0,166,80,.15);
		}
		.mpesa-summary-name { flex: 1; color: var(--text-color); }
		.mpesa-summary-amt { font-weight: 600; color: #00a650; white-space: nowrap; }
		.mpesa-summary-remove {
			background: none; border: none; color: var(--text-muted); cursor: pointer; padding: 0 2px; line-height: 1;
		}
		.mpesa-summary-remove:hover { color: var(--red-500); }
	</style>`).appendTo("head");
};

function mpesa_inject_dialog_styles() {
	if (document.getElementById("mpesa-form-styles")) return;
	$(`<style id="mpesa-form-styles">
		.mpesa-pay-header {
			background: linear-gradient(135deg,#00a650 0%,#007a3d 100%);
			border-radius:10px; padding:16px 20px; color:white;
			margin-bottom:12px; display:flex; align-items:center; gap:16px;
		}
		.mpesa-header-icon { font-size:2.5em; opacity:.9; }
		.mpesa-header-info { flex:1; }
		.mpesa-row { display:flex; justify-content:space-between; align-items:center; padding:4px 0; }
		.mpesa-row:not(:last-child) { border-bottom:1px solid rgba(255,255,255,.15); }
		.mpesa-row .text-success { color:#a3e635 !important; }
		.mpesa-outstanding { font-size:1.15em; padding-top:8px; }
		.mpesa-outstanding strong { color:#fbbf24; }

		.mpesa-search-section {
			display:flex; align-items:center; gap:12px;
			background:var(--bg-color); border:1px solid var(--border-color);
			border-radius:10px; padding:10px 14px; margin-bottom:12px;
		}
		.mpesa-search-box { flex:1; position:relative; }
		.mpesa-search-box input { padding-left:32px; height:36px; font-size:13px; border-radius:6px; width:100%; }
		.mpesa-search-icon { position:absolute; left:10px; top:50%; transform:translateY(-50%); color:var(--text-muted); font-size:12px; }
		.mpesa-count-badge {
			display:flex; align-items:center; gap:8px; padding:8px 12px;
			background:rgba(0,166,80,.1); border-radius:6px; color:#00a650; font-size:13px; white-space:nowrap;
		}

		.mpesa-payments-list { max-height:280px; overflow-y:auto; border:1px solid var(--border-color); border-radius:8px; }
		.mpesa-results-header {
			padding:8px 12px; background:var(--bg-color);
			border-bottom:1px solid var(--border-color); position:sticky; top:0; z-index:1;
		}
		.mpesa-select-all-label { font-weight:500; font-size:12px; }

		.mpesa-payment-item {
			display:flex; align-items:center; gap:10px;
			padding:10px 12px; border-bottom:1px solid var(--border-color);
			cursor:pointer; transition:background .15s;
		}
		.mpesa-payment-item:last-child { border-bottom:none; }
		.mpesa-payment-item:hover { background:var(--bg-color); }
		.mpesa-payment-item.selected { background:rgba(0,166,80,.08); }
		.mpesa-payment-item.exact-match { border-left:3px solid #00a650; background:rgba(0,166,80,.05); }
		.mpesa-exact-badge {
			display:inline-block; font-size:9px; background:#00a650; color:white;
			padding:1px 5px; border-radius:3px; margin-left:6px; vertical-align:middle;
		}
		.mpesa-payment-item.overpayment-warning { border-left:3px solid var(--yellow-500); }
		.mpesa-payment-item.overpayment-warning.selected { background:rgba(234,179,8,.08); }
		.mpesa-item-checkbox { flex-shrink:0; }
		.mpesa-item-checkbox input { width:16px; height:16px; accent-color:#00a650; }
		.mpesa-item-info { flex:1; min-width:0; }
		.mpesa-item-primary { display:flex; justify-content:space-between; align-items:center; margin-bottom:2px; }
		.mpesa-sender-name { font-weight:600; font-size:13px; }
		.mpesa-amount { font-weight:600; color:#00a650; font-size:13px; }
		.mpesa-amount.text-warning { color:#ca8a04; }
		.mpesa-item-secondary { display:flex; flex-wrap:wrap; gap:10px; font-size:11px; color:var(--text-muted); }
		.mpesa-item-secondary i { margin-right:3px; }

		.mpesa-age-indicator {
			flex-shrink:0; padding:3px 8px; border-radius:4px;
			font-size:10px; font-weight:600; min-width:60px; text-align:center;
		}

		.mpesa-empty-state, .mpesa-no-results, .mpesa-search-prompt {
			text-align:center; padding:30px 20px; color:var(--text-muted);
		}
		.mpesa-no-results i, .mpesa-search-prompt i { font-size:1.8em; margin-bottom:8px; display:block; }
		.mpesa-empty-state i { font-size:2.5em; margin-bottom:10px; }

		.mpesa-totals-bar {
			display:flex; gap:20px; padding:12px 14px; background:var(--bg-color);
			border-radius:8px; border:1px solid var(--border-color); flex-wrap:wrap; align-items:center;
		}
		.mpesa-totals-bar.mpesa-overpayment { border-color:var(--yellow-500); background:rgba(234,179,8,.05); }
		.mpesa-total-item { display:flex; flex-direction:column; }
		.mpesa-total-item span { font-size:10px; color:var(--text-muted); }
		.mpesa-total-item strong { font-size:1.1em; }
		.mpesa-excess-warning { padding:4px 10px; background:rgba(234,179,8,.1); border-radius:4px; }
		.mpesa-overpay-note { width:100%; margin-top:6px; padding-top:6px; border-top:1px dashed var(--yellow-500); font-size:12px; }

		.mpesa-checkbox-label { display:flex; align-items:center; gap:6px; cursor:pointer; font-weight:normal; font-size:12px; }
		.mpesa-checkbox-label input[type="checkbox"] { width:14px; height:14px; accent-color:#00a650; }
		.mpesa-invoice-options { display:flex; align-items:center; gap:20px; padding:6px 0; flex-wrap:wrap; }

		.mpesa-request-info {
			background:linear-gradient(135deg,#00a650 0%,#007a3d 100%);
			border-radius:8px; padding:12px 16px; color:white; margin-bottom:12px;
		}
		.mpesa-req-row { display:flex; justify-content:space-between; align-items:center; padding:4px 0; }
		.mpesa-req-row:not(:last-child) { border-bottom:1px solid rgba(255,255,255,.15); }
	</style>`).appendTo("head");
}
