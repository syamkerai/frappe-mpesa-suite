// Send Payment Proof on a paid M-Pesa payout (money_out.payment_print.proof_recipients): Frappe's own
// email composer, filled in with who the payout concerns and the suite's 80 mm proof of payment
// attached. Nothing is sent until the user presses Send. Loaded with each payout form (hooks.py
// doctype_js); the handlers are set once, whichever form loads it first.
frappe.provide("mpsa_payment_proof");

mpsa_payment_proof.DOCTYPES = ["Mpesa B2B Payment Request", "B2C Payment Disbursement", "B2C Disbursement Request"];

mpsa_payment_proof.paid = function (frm) {
	return frm.doc.docstatus === 1 && ["Paid", "Partially Paid"].includes(frm.doc.status);
};

mpsa_payment_proof.compose = function (frm) {
	frappe
		.xcall("mpsa_suite.money_out.payment_print.proof_recipients", { doctype: frm.doctype, name: frm.doc.name })
		.then((r) => {
			const composer = new frappe.views.CommunicationComposer({
				doc: frm.doc,
				frm,
				subject: r.subject,
				recipients: (r.recipients || []).join(", "),
				attach_document_print: true,
			});
			const field = composer.dialog && composer.dialog.fields_dict.select_print_format;
			if (field) composer.dialog.set_value("select_print_format", r.print_format);
		});
};

if (!mpsa_payment_proof.ready) {
	mpsa_payment_proof.ready = true;
	mpsa_payment_proof.DOCTYPES.forEach((doctype) =>
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				if (!mpsa_payment_proof.paid(frm)) return;
				frm.add_custom_button(__("Send Payment Proof"), () => mpsa_payment_proof.compose(frm));
			},
		})
	);
}
