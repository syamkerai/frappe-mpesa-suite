// Send Payment Proof on a paid M-Pesa payout (money_out.payment_print.proof_recipients): Frappe's own
// email composer, addressed to the ONE party the proof is about, with the suite's 80 mm proof of
// payment attached. A batch of several parties is never sent whole: the user picks one paid row and
// that row's payout is proved to its party alone. Not for a salary payout, nor for who may not send
// email on the payout. Nothing is sent until the user presses Send. Loaded with each payout form
// (hooks.py doctype_js); the handlers are set once, whichever form loads it first.
frappe.provide("mpsa_payment_proof");

mpsa_payment_proof.API = "mpsa_suite.money_out.payment_print.proof_recipients";
mpsa_payment_proof.DOCTYPES = ["Mpesa B2B Payment Request", "B2C Payment Disbursement", "B2C Disbursement Request"];

mpsa_payment_proof.salary = function (frm) {
	const doc = frm.doc;
	return Boolean(
		doc.salary_slip ||
			doc.reference_doctype === "Salary Slip" ||
			doc.transaction_to_pay_against === "Salary Slip" ||
			(doc.references || []).some((r) => r.reference_doctype === "Salary Slip")
	);
};

mpsa_payment_proof.offered = function (frm) {
	const can_email = frappe.model.can_email ? frappe.model.can_email(frm.doctype, frm) : false;
	return (
		frm.doc.docstatus === 1 &&
		["Paid", "Partially Paid"].includes(frm.doc.status) &&
		can_email &&
		!mpsa_payment_proof.salary(frm)
	);
};

mpsa_payment_proof.compose = function (frm, row) {
	frappe.xcall(mpsa_payment_proof.API, { doctype: frm.doctype, name: frm.doc.name, row: row || null }).then((r) => {
		if (r.rows) {
			mpsa_payment_proof.pick_row(frm, r.rows);
			return;
		}
		const open = (doc) => {
			const composer = new frappe.views.CommunicationComposer({
				doc,
				frm: doc.name === frm.doc.name ? frm : null,
				subject: r.subject,
				recipients: (r.recipients || []).join(", "),
				attach_document_print: true,
			});
			const field = composer.dialog && composer.dialog.fields_dict.select_print_format;
			if (field) composer.dialog.set_value("select_print_format", r.print_format);
		};
		if (r.doctype === frm.doctype && r.name === frm.doc.name) {
			open(frm.doc);
		} else {
			// one row's payout of a batch: its own document and proof
			frappe.model.with_doctype(r.doctype, () => frappe.model.with_doc(r.doctype, r.name).then(open));
		}
	});
};

mpsa_payment_proof.pick_row = function (frm, rows) {
	if (!rows.length) {
		frappe.msgprint(__("No row of this batch is paid yet."));
		return;
	}
	frappe.prompt(
		[
			{
				fieldtype: "Select",
				fieldname: "row",
				label: __("Payout"),
				reqd: 1,
				options: rows.map((r) => ({ value: r.row, label: r.label })),
				description: __("This batch pays several parties: each gets the proof of their own payout only."),
			},
		],
		(values) => mpsa_payment_proof.compose(frm, values.row),
		__("Send Payment Proof"),
		__("Next")
	);
};

if (!mpsa_payment_proof.ready) {
	mpsa_payment_proof.ready = true;
	mpsa_payment_proof.DOCTYPES.forEach((doctype) =>
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				if (!mpsa_payment_proof.offered(frm)) return;
				frm.add_custom_button(__("Send Payment Proof"), () => mpsa_payment_proof.compose(frm));
			},
		})
	);
}
