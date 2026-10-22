// Copyright (c) 2026, Astral Industries Limited and contributors
// For license information, please see license.txt

const LAB = "mpsa_suite.sandbox_lab.api.";

frappe.ui.form.on("Mpesa Sandbox Test Run", {
	setup(frm) {
		["c2b_settings", "stk_settings", "b2c_settings", "b2b_settings"].forEach((f) =>
			frm.set_query(f, () => ({ filters: { sandbox: 1 } }))
		);
		frm.set_query("test_sales_invoice", () => ({
			filters: { docstatus: 1, outstanding_amount: [">", 0], ...(frm.doc.company ? { company: frm.doc.company } : {}) },
		}));
		frm.set_query("test_purchase_invoice", () => ({
			filters: { docstatus: 1, outstanding_amount: [">", 0], ...(frm.doc.test_supplier ? { supplier: frm.doc.test_supplier } : {}) },
		}));
		frm.set_query("test_b2c_disbursement", () => ({ filters: { docstatus: ["<", 2] } }));
	},

	refresh(frm) {
		render_health(frm);
		if (frm.is_new()) return;

		frm.add_custom_button(__("Health Check"), () =>
			lab_call(frm, "run_health_check", __("Checking the sandbox setup..."))
		);
		frm.add_custom_button(__("Run Ready Tests"), () =>
			lab_call(frm, "run_ready", __("Starting the automatic tests..."), (started) => {
				const n = Object.keys(started || {}).length;
				frappe.show_alert(n ? __("{0} tests started", [n]) : __("Nothing ready: run guided tests or their prerequisites first"));
			})
		);
		frm.add_custom_button(__("Check Now"), () => lab_call(frm, "check_now", __("Checking results...")));
		frm.add_custom_button(__("Add New Scenarios"), () => lab_call(frm, "add_scenarios"), __("Tools"));
		frm.add_custom_button(__("Simulate a C2B Payment"), () => simulate_dialog(frm), __("Tools"));
		frm.add_custom_button(__("Replay a Callback"), () => replay_dialog(frm), __("Tools"));

		frm.dashboard.clear_headline();
		if (frm.doc.status === "Failed") {
			frm.dashboard.set_headline(__("{0} test(s) failed: open the row for the reason and its reference document.", [frm.doc.failed]), "red");
		} else if (frm.doc.waiting) {
			frm.dashboard.set_headline(__("{0} test(s) waiting for Safaricom. This page refreshes itself.", [frm.doc.waiting]), "blue");
		}
		auto_refresh(frm);
	},

	onload_post_render(frm) {
		auto_refresh(frm);
	},
});

frappe.ui.form.on("Mpesa Sandbox Test Case", {
	run(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the test run first."));
			return;
		}
		const go = () =>
			lab_call(frm, "run_case", __("Starting {0}...", [row.title]), null, { case: row.name });
		if (row.mode === "Guided") {
			frappe.confirm(
				`<b>${frappe.utils.escape_html(row.title)}</b><br>${frappe.utils.escape_html(row.instructions || "")}<br><br>${__("Have your phone ready. Start?")}`,
				go
			);
		} else {
			go();
		}
	},
	reset(frm, cdt, cdn) {
		lab_call(frm, "reset_case", null, null, { case: cdn });
	},
});

function lab_call(frm, method, freeze_message, then, extra) {
	return frappe
		.call({
			method: LAB + method,
			args: { run: frm.doc.name, ...(extra || {}) },
			freeze: !!freeze_message,
			freeze_message,
		})
		.then((r) => {
			if (then) then(r.message);
			frm.reload_doc();
		});
}

function auto_refresh(frm) {
	if (frm._lab_timer) clearInterval(frm._lab_timer);
	if (!frm.doc.waiting) return;
	frm._lab_timer = setInterval(() => {
		if (cur_frm !== frm || frm.is_dirty()) return clearInterval(frm._lab_timer);
		frm.reload_doc();
	}, 20000);
}

function render_health(frm) {
	const field = frm.get_field("health_html");
	if (!field) return;
	let items = [];
	try {
		items = JSON.parse(frm.doc.health_report || "[]");
	} catch (e) {
		items = [];
	}
	if (!items.length) {
		field.$wrapper.html(`<p class="text-muted small">${__("Run Health Check to see the setup status.")}</p>`);
		return;
	}
	const colour = { ok: "green", warning: "orange", error: "red" };
	field.$wrapper.html(
		items
			.map(
				(i) =>
					`<div><span class="indicator ${colour[i.level] || "gray"}"></span><b>${frappe.utils.escape_html(i.label)}</b>: ${frappe.utils.escape_html(i.detail)}</div>`
			)
			.join("")
	);
}

function simulate_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Simulate a C2B Payment (sandbox)"),
		fields: [
			{ fieldname: "mpesa_settings", fieldtype: "Link", options: "Mpesa Settings", label: __("Mpesa Settings"), reqd: 1, default: frm.doc.c2b_settings, get_query: () => ({ filters: { sandbox: 1 } }) },
			{ fieldname: "shortcode", fieldtype: "Data", label: __("Shortcode"), reqd: 1, default: frm.doc.c2b_shortcode },
			{ fieldname: "paybill", fieldtype: "Check", label: __("Pay Bill (untick for Buy Goods)"), default: (frm.doc.c2b_type || "Pay Bill") === "Pay Bill" ? 1 : 0 },
			{ fieldname: "amount", fieldtype: "Int", label: __("Amount (KES)"), reqd: 1, default: frm.doc.amount || 10 },
			{ fieldname: "bill_ref", fieldtype: "Data", label: __("Account Number (BillRefNumber)"), default: frm.doc.test_sales_invoice },
			{ fieldname: "msisdn", fieldtype: "Data", label: __("Payer MSISDN"), default: frm.doc.test_msisdn || "254708374149" },
		],
		primary_action_label: __("Send"),
		primary_action(values) {
			frappe
				.call({ method: LAB + "simulate_c2b", args: values, freeze: true })
				.then((r) => {
					d.hide();
					frappe.msgprint({
						title: __("Safaricom accepted the simulation"),
						message: `<pre>${frappe.utils.escape_html(JSON.stringify(r.message, null, 2))}</pre>${__("The confirmation should reach Mpesa C2B Payment Register within a minute or two.")}`,
					});
				});
		},
	});
	d.show();
}

function replay_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Replay a Callback"),
		fields: [
			{ fieldname: "doctype", fieldtype: "Select", label: __("Document Type"), reqd: 1, options: ["Mpesa Express Request", "Mpesa C2B Payment Register", "B2C Disbursement Request", "Mpesa B2B Payment Request", "Mpesa Reversal Request"].join("\n") },
			{ fieldname: "name", fieldtype: "Dynamic Link", options: "doctype", label: __("Document"), reqd: 1 },
			{ fieldname: "with_token", fieldtype: "Check", label: __("Include the callback token (untick to see what a forger gets)"), default: 1 },
		],
		primary_action_label: __("Replay"),
		primary_action(values) {
			frappe.call({ method: LAB + "replay_callback", args: values, freeze: true }).then((r) => {
				d.hide();
				frappe.msgprint({
					title: __("Our endpoint answered"),
					message: `<pre>${frappe.utils.escape_html(JSON.stringify(r.message, null, 2))}</pre>${__("Check the document: a replay must never book twice.")}`,
				});
			});
		},
	});
	d.show();
}
