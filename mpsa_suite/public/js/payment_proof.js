// Send Payment Proof on a paid M-Pesa payout (money_out.payment_print.proof_recipients): Frappe's own
// email composer, addressed to the ONE party the proof is about, with the suite's 80 mm proof of
// payment attached. A batch of several parties is never sent whole: the user picks one paid row and
// that row's payout is proved to its party alone, composed on that payout's own form (Frappe's
// composer attaches a document's print only from its form). Not for a salary payout, nor for who may
// not send email on what is attached. Nothing is sent until the user presses Send. Loaded with each
// payout form (hooks.py doctype_js); the handlers are set once, whichever form loads it first.
frappe.provide("mpsa_payment_proof");

mpsa_payment_proof.API = "mpsa_suite.money_out.payment_print.proof_recipients";
mpsa_payment_proof.BATCH = "B2C Payment Disbursement";
mpsa_payment_proof.REQUEST = "B2C Disbursement Request";
mpsa_payment_proof.DOCTYPES = ["Mpesa B2B Payment Request", mpsa_payment_proof.BATCH, mpsa_payment_proof.REQUEST];
// what an employee is paid for that is not a salary (salary_access.EMPLOYEE_DOCUMENTS)
mpsa_payment_proof.NOT_SALARY = ["Expense Claim", "Employee Advance"];
mpsa_payment_proof.WAIT = 120000; // a row's proof opens on its request's form within this (ms)

mpsa_payment_proof.salary_line = function (line) {
	return (
		line.reference_doctype === "Salary Slip" ||
		(line.party_type === "Employee" && !mpsa_payment_proof.NOT_SALARY.includes(line.reference_doctype))
	);
};

// a salary as the suite reads one (salary_access.has_salaries); the server checks again
mpsa_payment_proof.salary = function (frm) {
	const doc = frm.doc;
	if (frm.doctype === "Mpesa B2B Payment Request") {
		return Boolean(doc.salary_slip || (doc.employee && !doc.expense_claim && !doc.employee_advance));
	}
	if (frm.doctype === mpsa_payment_proof.REQUEST) return mpsa_payment_proof.salary_line(doc);
	return (
		doc.transaction_to_pay_against === "Salary Slip" ||
		(doc.party_type === "Employee" && !mpsa_payment_proof.NOT_SALARY.includes(doc.transaction_to_pay_against)) ||
		(doc.references || []).some((r) => mpsa_payment_proof.salary_line(r))
	);
};

// a batch of several parties is proved row by row, each on its own B2C Disbursement Request
mpsa_payment_proof.by_row = function (frm) {
	if (frm.doctype !== mpsa_payment_proof.BATCH) return false;
	const parties = (frm.doc.references || []).map((r) => `${r.party_type || frm.doc.party_type || ""}|${r.party || ""}`);
	return new Set(parties).size > 1;
};

mpsa_payment_proof.offered = function (frm) {
	const can_email = frappe.model.can_email ? frappe.model.can_email(frm.doctype, frm) : false;
	return (
		frm.doc.docstatus === 1 &&
		["Paid", "Partially Paid"].includes(frm.doc.status) &&
		can_email &&
		(!mpsa_payment_proof.by_row(frm) || frappe.model.can_email(mpsa_payment_proof.REQUEST)) &&
		!mpsa_payment_proof.salary(frm)
	);
};

mpsa_payment_proof.open = function (frm, r) {
	const composer = new frappe.views.CommunicationComposer({
		doc: frm.doc,
		frm,
		subject: r.subject,
		recipients: (r.recipients || []).join(", "),
		attach_document_print: true,
	});
	const field = composer.dialog && composer.dialog.fields_dict.select_print_format;
	if (field) composer.dialog.set_value("select_print_format", r.print_format);
};

mpsa_payment_proof.compose = function (frm, row) {
	frappe.xcall(mpsa_payment_proof.API, { doctype: frm.doctype, name: frm.doc.name, row: row || null }).then((r) => {
		if (r.rows) {
			mpsa_payment_proof.pick_row(frm, r.rows);
			return;
		}
		if (r.doctype === frm.doctype && r.name === frm.doc.name) {
			mpsa_payment_proof.open(frm, r);
			return;
		}
		// one row's payout of a batch: composed on that payout's own form, which attaches its proof
		mpsa_payment_proof.pending = Object.assign({ at: Date.now() }, r);
		frappe.set_route("Form", r.doctype, r.name);
	});
};

// a row's proof asked for on the batch: the composer opens once that payout's form is shown
mpsa_payment_proof.take_pending = function (frm) {
	const r = mpsa_payment_proof.pending;
	if (!r || r.doctype !== frm.doctype || r.name !== frm.doc.name) return;
	mpsa_payment_proof.pending = null;
	if (Date.now() - r.at <= mpsa_payment_proof.WAIT) mpsa_payment_proof.open(frm, r);
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
				description: __(
					"This batch pays several parties: each gets the proof of their own payout only. The email opens on that payout's own form."
				),
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
				mpsa_payment_proof.take_pending(frm);
				if (!mpsa_payment_proof.offered(frm)) return;
				frm.add_custom_button(__("Send Payment Proof"), () => mpsa_payment_proof.compose(frm));
			},
		})
	);
}
