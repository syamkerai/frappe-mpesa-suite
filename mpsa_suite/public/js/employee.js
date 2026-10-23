// Shows the employee's M-Pesa accounts (phone for B2C, bank paybill for B2B) and whether each can
// be paid now.
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
					const where =
						r.account_type === "Phone (B2C)"
							? __("Phone {0}", [e(r.phone || "")])
							: __("Bank paybill {0}, account {1}", [e(r.shortcode || ""), e(r.account_number || "")]);
					let state = r.problem ? e(r.problem) : __("Approved, can be paid");
					if (r.docstatus === 0) state = __("Waiting for approval");
					if (r.name_mismatch) state += " · " + __("last payment name did not match");
					const colour = r.name_mismatch ? "red" : r.problem ? "orange" : "green";
					return `<div><span class="indicator ${colour}"></span>
						<a href="/app/employee-m-pesa-account/${encodeURIComponent(r.name)}">${where}</a>
						${r.is_default ? "(" + __("default") + ")" : ""} — ${e(r.expected_name || "")}: ${state}</div>`;
				});
				frm.dashboard.add_section(lines.join(""), __("M-Pesa Accounts"));
			});
	},
});
