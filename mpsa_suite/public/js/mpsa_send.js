// Send via M-Pesa / Approve (money_out.send_confirm): the one deliberate step that sends a payout or
// approves a payee account. On a saved draft it replaces the form's Submit; the server refuses
// every other submit (the list's, the API's, a script's), so this only offers the button to the
// people the server will accept (frappe.boot.mpsa, boot.py) and shows what is sent before Confirm.
/* global mpsa_send */
frappe.provide("mpsa_send");

mpsa_send.METHOD = "mpsa_suite.money_out.send_confirm";
mpsa_send.PAYOUTS = [
	"Mpesa B2B Payment Request",
	"B2C Payment Disbursement",
	"B2C Disbursement Request",
	"Mpesa Reversal Request",
];
mpsa_send.ACCOUNTS = ["Supplier M-Pesa Account", "Employee M-Pesa Account"];

mpsa_send.is_account = (doctype) => mpsa_send.ACCOUNTS.includes(doctype);

// Approving needs ADMIN_ROLES, sending PAYOUT_ROLES, both a named login (not Administrator).
mpsa_send.allowed = (doctype) => {
	const flags = frappe.boot.mpsa || {};
	return Boolean(mpsa_send.is_account(doctype) ? flags.can_approve : flags.can_pay);
};

mpsa_send.label = (doctype) => (mpsa_send.is_account(doctype) ? __("Approve") : __("Send via M-Pesa"));

mpsa_send.kes = (value) => format_currency(value || 0, "KES", 2);

// A saved draft whose toolbar offers Submit gets Send via M-Pesa (Approve) instead, and someone
// the server would refuse gets no button at all.
mpsa_send.refresh = (frm) => {
	if (frm.is_new() || frm.doc.docstatus !== 0 || frm.is_dirty()) return;
	if (!frm.toolbar || frm.toolbar.current_status !== "Submit") return; // no submit right, or a Workflow
	if (!mpsa_send.allowed(frm.doctype)) {
		frm.page.clear_primary_action();
		return;
	}
	frm.page.set_primary_action(mpsa_send.label(frm.doctype), () => mpsa_send.open(frm));
};

for (const doctype of [...mpsa_send.PAYOUTS, ...mpsa_send.ACCOUNTS]) {
	frappe.ui.form.on(doctype, { refresh: mpsa_send.refresh });
}

mpsa_send.open = async (frm) => {
	const r = await frappe.xcall(`${mpsa_send.METHOD}.prepare_send`, {
		doctype: frm.doctype,
		name: frm.doc.name,
		modified: frm.doc.modified,
	});
	mpsa_send.confirm(frm, r);
};

mpsa_send.summary_html = (r) => {
	const e = frappe.utils.escape_html;
	const kes = mpsa_send.kes;
	const account = mpsa_send.is_account(r.doctype);
	let html = `<p class="text-muted">${
		account
			? __("Check the account against its proof. Payouts can use it once you confirm and its hold period is over.")
			: __("Read this before you confirm. Nothing is sent until you press Confirm.")
	}</p>`;
	html += `<table class="table table-bordered table-sm"><thead><tr><th>${__("Receiver")}</th>
		<th>${account ? __("Account") : __("Pays To")}</th>`;
	if (!account) html += `<th class="text-right">${__("Amount")}</th><th class="text-right">${__("Charge (estimate)")}</th>`;
	html += `<th>${account ? __("Type") : __("For")}</th></tr></thead><tbody>`;
	for (const row of r.rows || []) {
		let who = e(row.receiver || "");
		if (row.party && row.party !== row.receiver) who += ` <span class="text-muted">(${e(row.party)})</span>`;
		if (row.expected_name) {
			who += `<br><span class="text-muted small">${__("Name on the approved account: {0}", [e(row.expected_name)])}</span>`;
		}
		let where = `<b>${e(row.destination || "")}</b>`;
		// a bank account: whose it is (the bank's name above is the paybill's, not the holder's)
		if (row.account_holder) {
			where += `<br><span class="text-muted small">${__("Account holder: {0}", [e(row.account_holder)])}</span>`;
		}
		html += `<tr><td>${who}</td><td>${where}</td>`;
		if (!account) {
			const charge = row.charge === null || row.charge === undefined ? "" : kes(row.charge);
			html += `<td class="text-right">${kes(row.amount)}</td><td class="text-right">${charge}</td>`;
		}
		html += `<td>${e(row.document || "")}</td></tr>`;
	}
	html += "</tbody></table>";
	if (!account) {
		let total = __("Total: {0}", [kes(r.total)]);
		if (r.charge !== null && r.charge !== undefined) total += " " + __("(charge about {0})", [kes(r.charge)]);
		html += `<h3 class="text-right">${total}</h3>`;
		if (r.charge_note) html += `<p class="text-muted text-right small">${e(r.charge_note)}</p>`;
		if (r.pay_from) {
			const from = e(r.pay_from) + (r.shortcode ? ` (${e(r.shortcode)})` : "");
			html += `<p>${__("Paid from: {0}", [from])}</p>`;
		}
	}
	html += `<p class="text-muted small">${__("Prepared by {0}; last changed by {1}.", [
		e(r.prepared_by || ""),
		e(r.changed_by || ""),
	])}`;
	if (account && r.entered_by) html += " " + __("Details entered by {0}.", [e(r.entered_by)]);
	html += "</p>";
	if (account && r.proof) {
		html += `<p><a href="${encodeURI(r.proof)}" target="_blank" rel="noopener">${__("Open the proof")}</a></p>`;
	}
	if ((r.problems || []).length) {
		html += `<h5>${__("Cannot be confirmed")}</h5><ul>`;
		html += r.problems.map((p) => `<li style="color: var(--red-600)">${e(p)}</li>`).join("");
		html += "</ul>";
	}
	return html;
};

mpsa_send.confirm = (frm, r) => {
	const account = mpsa_send.is_account(r.doctype);
	const d = new frappe.ui.Dialog({
		title: account ? __("Approve {0}", [r.name]) : __("Send {0} via M-Pesa", [r.name]),
		size: "large",
		fields: [{ fieldtype: "HTML", fieldname: "summary" }],
	});
	d.fields_dict.summary.$wrapper.html(mpsa_send.summary_html(r));
	// each press builds a new dialog: drop the old one once closed
	const drop = () => setTimeout(() => d.$wrapper.remove(), 500);
	if (!r.token) {
		d.set_primary_action(__("Close"), () => d.hide());
		d.onhide = drop;
		d.show();
		return;
	}
	let left = r.delay || 5;
	const action = account ? __("Confirm and Approve") : __("Confirm and Send {0}", [mpsa_send.kes(r.total)]);
	const label = () => (left > 0 ? `${action} (${left})` : action);
	let sending = false;
	d.set_primary_action(label(), async () => {
		if (left > 0 || sending) return;
		sending = true;
		d.get_primary_btn().prop("disabled", true);
		try {
			await frappe.xcall(`${mpsa_send.METHOD}.confirm_send`, {
				doctype: r.doctype,
				name: r.name,
				token: r.token,
			});
			d.hide();
			frappe.show_alert({ message: account ? __("Approved") : __("Sent to M-Pesa"), indicator: "green" });
			frm.reload_doc();
		} catch (err) {
			sending = false;
			d.get_primary_btn().prop("disabled", false);
		}
	});
	const btn = d.get_primary_btn();
	btn.prop("disabled", true).text(label());
	// Forced reading time: Confirm stays disabled, with a visible countdown (the server refuses earlier).
	const timer = setInterval(() => {
		left -= 1;
		btn.text(label());
		if (left <= 0) {
			clearInterval(timer);
			btn.prop("disabled", false);
		}
	}, 1000);
	d.onhide = () => {
		clearInterval(timer);
		drop();
	};
	d.show();
};

// The list's Actions > Submit would submit (send or approve) many documents at once. The server
// refuses it for these DocTypes (send_confirm.assert_deliberate), so the list does not offer it.
// The DocTypes' own list settings stay untouched: the action is taken out of Frappe's list menu.
mpsa_send.hide_bulk_submit = () => {
	const ListView = frappe.views && frappe.views.ListView;
	const get_items = ListView && ListView.prototype.get_actions_menu_items;
	if (typeof get_items !== "function" || ListView.prototype.mpsa_send_hides_submit) return;
	ListView.prototype.get_actions_menu_items = function (...args) {
		const items = get_items.apply(this, args);
		if (![...mpsa_send.PAYOUTS, ...mpsa_send.ACCOUNTS].includes(this.doctype)) return items;
		const submit = __("Submit", null, "Button in list view actions menu");
		return items.filter((item) => item.label !== submit);
	};
	ListView.prototype.mpsa_send_hides_submit = true;
};

mpsa_send.hide_bulk_submit();
