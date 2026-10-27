// Employee Advance list (HRMS): M-Pesa list actions and indicators (WP3: pay the
// ticked advances as one batch).
//
// Frappe loads this after the DocType's own list script (HRMS ships none today), which would set
// frappe.listview_settings["Employee Advance"]. Extend those settings, never replace them:
//
//   const key = "Employee Advance";
//   const settings = (frappe.listview_settings[key] = frappe.listview_settings[key] || {});
//   settings.add_fields = [...new Set([...(settings.add_fields || []), "company"])];
//   const hrms_onload = settings.onload;
//   settings.onload = function (listview) {
//   	if (hrms_onload) hrms_onload.apply(this, arguments);
//   	// M-Pesa actions here, shown from frappe.boot.mpsa (can_prepare, can_pay)
//   };
//   // the same for get_indicator, formatters and button: call HRMS's first, then add.
