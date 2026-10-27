// Expense Claim list (HRMS): M-Pesa list actions and indicators (WP3: pay the ticked
// claims as one batch).
//
// Frappe loads this after HRMS's own expense_claim_list.js, which sets
// frappe.listview_settings["Expense Claim"]. Extend those settings, never replace them:
//
//   const key = "Expense Claim";
//   const settings = (frappe.listview_settings[key] = frappe.listview_settings[key] || {});
//   settings.add_fields = [...new Set([...(settings.add_fields || []), "company"])];
//   const hrms_onload = settings.onload;
//   settings.onload = function (listview) {
//   	if (hrms_onload) hrms_onload.apply(this, arguments);
//   	// M-Pesa actions here, shown from frappe.boot.mpsa (can_prepare, can_pay)
//   };
//   // the same for get_indicator, formatters and button: call HRMS's first, then add.
