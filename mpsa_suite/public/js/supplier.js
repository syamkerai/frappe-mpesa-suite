// Shows the supplier's M-Pesa accounts (paybill, till, bank account or phone), whether each can be
// paid now, and asks for a default when there are several and none is.
frappe.ui.form.on("Supplier", {
	refresh(frm) {
		if (frm.is_new() || !frappe.model.can_read("Supplier M-Pesa Account")) return;

		frm.add_custom_button(
			__("Add M-Pesa Account"),
			() => frappe.new_doc("Supplier M-Pesa Account", { supplier: frm.doc.name }),
			__("M-Pesa")
		);
		if (frappe.model.can_create("Mpesa B2B Payment Request")) {
			frm.add_custom_button(
				__("Pay via M-Pesa (B2B)"),
				() => frappe.new_doc("Mpesa B2B Payment Request", { supplier: frm.doc.name }),
				__("M-Pesa")
			);
		}

		frappe
			.xcall(
				"mpsa_suite.mpsa_suite.doctype.supplier_m_pesa_account.supplier_m_pesa_account.accounts_for_supplier",
				{ supplier: frm.doc.name }
			)
			.then((rows) => {
				if (!rows || !rows.length) return;
				const e = frappe.utils.escape_html;
				const lines = rows.map((r) => {
					let state = r.problem ? e(r.problem) : __("Approved, can be paid");
					if (r.docstatus === 0) state = __("Waiting for approval");
					if (r.name_mismatch) state += " · " + __("last payment name did not match");
					const colour = r.name_mismatch || r.needs_reapproval ? "red" : r.problem ? "orange" : "green";
					return `<div><span class="indicator ${colour}"></span>
						<a href="/app/supplier-m-pesa-account/${encodeURIComponent(r.name)}">${mpsa_supplier_where(r)}</a>
						${r.is_default ? "(" + __("default") + ")" : ""} — ${e(r.expected_name || "")}: ${state}</div>`;
				});
				frm.dashboard.add_section(lines.join(""), __("M-Pesa Accounts"));
				const approved = rows.filter((r) => r.docstatus === 1);
				if (approved.length >= 2 && !approved.some((r) => r.is_default)) {
					frm.dashboard.set_headline(
						__("{0} approved M-Pesa accounts and none is the default: open the one payments go to when no account is picked and press Make Default.", [
							approved.length,
						]),
						"red"
					);
				}
			});
	},
});

// "Paybill 888880, account TK-001", "Bank account 0123456789 at KCB (paybill 522522)", ...
function mpsa_supplier_where(r) {
	const e = frappe.utils.escape_html;
	if (r.account_type === "Phone (B2C)") return __("Phone {0}", [e(r.phone || "")]);
	if (r.account_type === "Till (Buy Goods)") return __("Till {0}", [e(r.shortcode || "")]);
	if (r.account_type === "Paybill") return __("Paybill {0}, account {1}", [e(r.shortcode || ""), e(r.account_number || "")]);
	return __("Bank account {0} at {1} (paybill {2})", [
		e(r.account_number || ""),
		e(r.bank_paybill || ""),
		e(r.shortcode || ""),
	]);
}
