// "Reverse Payment" on Mpesa C2B Payment Register: drafts an Mpesa Reversal Request.
frappe.ui.form.on("Mpesa C2B Payment Register", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.docstatus === 2 || !frm.doc.transid) return;
		frm.add_custom_button(
			__("Reverse Payment"),
			() =>
				frappe.prompt(
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
					({ reason }) =>
						frappe.call({
							method: "mpsa_suite.money_out.api.make_reversal",
							args: { c2b_register: frm.doc.name, reason },
							callback: (r) => {
								if (!r.exc) frappe.set_route("Form", "Mpesa Reversal Request", r.message);
							},
						}),
					__("Reverse {0}", [frm.doc.transid]),
					__("Create Draft")
				),
			__("Actions")
		);
	},
});
