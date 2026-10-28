// Employee Advance list (HRMS): Actions > Pay by M-Pesa on the ticked advances prepares one draft
// B2C Payment Disbursement for the employees paid by phone and a draft B2B request per advance of
// an employee paid to a bank account, paybill or till; each is then sent on its own with Send via
// M-Pesa (employee_pay.js, loaded when the action is used; the server checks everything again).
//
// Frappe loads this after the DocType's own list script (HRMS ships none today): its
// frappe.listview_settings are extended, never replaced. HRMS 15 has no Partially Paid: an advance
// it shows Unpaid that was paid in part shows Partly Paid; HRMS 16 says Partially Paid itself.
(function () {
	const key = "Employee Advance";
	const all = frappe.listview_settings;
	if (!all[key]) all[key] = {}; // only when no list script made them
	const settings = all[key];
	settings.add_fields = [...new Set([...(settings.add_fields || []), "paid_amount", "advance_amount"])];
	// Frappe's frappe.get_indicator answers from the DocType's own states (HRMS 15 lists "Unpaid") before
	// it asks listview_settings.get_indicator, so the suite's answer goes first, for this DocType only
	frappe.provide("mpsa_suite_indicators");
	mpsa_suite_indicators[key] = (doc) => {
		const paid = flt(doc.paid_amount);
		if (doc.docstatus === 1 && doc.status === "Unpaid" && paid > 0 && paid < flt(doc.advance_amount)) {
			return [__("Partly Paid"), "orange", "status,=,Unpaid"];
		}
		return null;
	};
	// Frappe answers first for an unsaved form ("Not Saved") and where it shows the workflow state (a
	// Workflow that overrides the status, or a Workflow State column), as frappe.get_indicator decides
	mpsa_suite_indicators.frappes_own = function (doc, doctype, show_workflow_state) {
		if (doc.__unsaved) return true;
		const workflow = frappe.workflow || {};
		const field = workflow.get_state_fieldname ? workflow.get_state_fieldname(doctype) : null;
		if (!field || !doc[field]) return false;
		const flow = (workflow.workflows || {})[doctype];
		const without_workflow = flow ? flow.override_status : true;
		const avoid = ((workflow.avoid_status_override || {})[doctype] || []).includes(doc[field]);
		return (!without_workflow || Boolean(show_workflow_state)) && !avoid;
	};
	if (!(frappe.get_indicator && frappe.get_indicator.mpsa_suite)) {
		const frappe_indicator = frappe.get_indicator;
		frappe.get_indicator = function (doc, doctype, show_workflow_state) {
			const dt = doctype || (doc && doc.doctype);
			const own = mpsa_suite_indicators[dt];
			const mine =
				own && doc && !mpsa_suite_indicators.frappes_own(doc, dt, show_workflow_state) ? own(doc) : null;
			return mine || frappe_indicator.apply(this, arguments);
		};
		frappe.get_indicator.mpsa_suite = true;
	}
	const hrms_onload = settings.onload;
	settings.onload = function (listview) {
		if (hrms_onload) hrms_onload.apply(this, arguments);
		if (!(frappe.boot.mpsa || {}).can_prepare) return;
		listview.page.add_action_item(__("Pay by M-Pesa"), () =>
			frappe.require("/assets/mpsa_suite/js/employee_pay.js", () => mpsa_employee_pay.bulk(listview, key))
		);
	};
})();
