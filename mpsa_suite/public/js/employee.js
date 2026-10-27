// Shows the employee's M-Pesa accounts (a phone for B2C; a bank account, paybill or till for B2B),
// whether each can be paid now, and asks for a default when there are several and none is.
frappe.ui.form.on("Employee", {
	refresh(frm) {
		if (frm.is_new() || !frappe.model.can_read("Employee M-Pesa Account")) return;

		if (frappe.model.can_create("Employee M-Pesa Account")) {
			frm.add_custom_button(
				__("Add M-Pesa Account"),
				() =>
					frappe.new_doc("Employee M-Pesa Account", {
						employee: frm.doc.name,
						phone: frm.doc.cell_number || "",
						account_number: frm.doc.bank_ac_no || "",
					}),
				__("M-Pesa")
			);
		}

		frappe
			.xcall(
				"mpsa_suite.mpsa_suite.doctype.employee_m_pesa_account.employee_m_pesa_account.accounts_for_employee",
				{ employee: frm.doc.name }
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
						<a href="/app/employee-m-pesa-account/${encodeURIComponent(r.name)}">${mpsa_employee_where(r)}</a>
						${r.is_default ? "(" + __("default") + ")" : ""} — ${e(r.expected_name || "")}: ${state}</div>`;
				});
				frm.dashboard.add_section(lines.join(""), __("M-Pesa Accounts"));
				const approved = rows.filter((r) => r.docstatus === 1);
				if (approved.length >= 2 && !approved.some((r) => r.is_default)) {
					frm.dashboard.set_headline(
						__("{0} approved M-Pesa accounts and none is the default: salaries are not planned until one is. Open the one salaries go to and press Make Default.", [
							approved.length,
						]),
						"red"
					);
				}
			});
	},
});

// "Phone 2547...", "Bank account 0123456789 at KCB (paybill 522522)", "Till 835811", ...
function mpsa_employee_where(r) {
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
