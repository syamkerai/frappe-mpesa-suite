frappe.ui.form.on("Mpesa Webhook Delivery", {
	refresh(frm) {
		if (frm.doc.status !== "Delivered") {
			frm.add_custom_button(__("Send Now"), () => frm.call("retry_now").then(() => frm.reload_doc()));
		}
	},
});
