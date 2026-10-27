// Employee Advance list (HRMS): Actions > Pay by M-Pesa on the ticked advances prepares one draft
// B2C Payment Disbursement for the employees paid by phone and a draft B2B request per advance of
// an employee paid to a bank account, paybill or till; each is then sent on its own with Send via
// M-Pesa (employee_pay.js, loaded when the action is used; the server checks everything again).
//
// Frappe loads this after the DocType's own list script (HRMS ships none today): its
// frappe.listview_settings are extended, never replaced.
(function () {
	const key = "Employee Advance";
	const all = frappe.listview_settings;
	if (!all[key]) all[key] = {}; // only when no list script made them
	const settings = all[key];
	const hrms_onload = settings.onload;
	settings.onload = function (listview) {
		if (hrms_onload) hrms_onload.apply(this, arguments);
		if (!(frappe.boot.mpsa || {}).can_prepare) return;
		listview.page.add_action_item(__("Pay by M-Pesa"), () =>
			frappe.require("/assets/mpsa_suite/js/employee_pay.js", () => mpsa_employee_pay.bulk(listview, key))
		);
	};
})();
