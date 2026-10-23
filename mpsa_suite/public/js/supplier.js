// Shows the supplier's M-Pesa paybill / till accounts and whether each can be paid now.
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
					const where =
						r.account_type === "Paybill"
							? __("Paybill {0}, account {1}", [e(r.shortcode), e(r.account_number || "")])
							: __("Till {0}", [e(r.shortcode)]);
					let state = r.problem ? e(r.problem) : __("Approved, can be paid");
					if (r.docstatus === 0) state = __("Waiting for approval");
					if (r.name_mismatch) state += " · " + __("last payment name did not match");
					const colour = r.name_mismatch ? "red" : r.problem ? "orange" : "green";
					return `<div><span class="indicator ${colour}"></span>
						<a href="/app/supplier-m-pesa-account/${encodeURIComponent(r.name)}">${where}</a>
						${r.is_default ? "(" + __("default") + ")" : ""} — ${e(r.expected_name || "")}: ${state}</div>`;
				});
				frm.dashboard.add_section(lines.join(""), __("M-Pesa Accounts"));
			});
	},
});
