frappe.ui.form.on("Payment Request", {
  refresh: function (frm) {
    sync_payment_fields(frm);
    mpesa_prompt_actions(frm);
  },

  mode_of_payment: function (frm) {
    if (frm.doc.mode_of_payment && frm.doc.company) {
      frappe.call({
        method:
          "mpsa_suite.mpsa_suite.api.sales_invoice.get_payment_gateway_from_mop",
        args: {
          mode_of_payment: frm.doc.mode_of_payment,
          company: frm.doc.company,
        },
        callback: function (r) {
          if (!r.exc && r.message) {
            frm.set_value("payment_gateway", r.message);
          }
        },
      });
    }
  },

  payment_gateway: function (frm) {
    if (frm.doc.payment_gateway && frm.doc.company) {
      frappe.call({
        method:
          "mpsa_suite.mpsa_suite.api.sales_invoice.get_mop_from_payment_gateway",
        args: {
          payment_gateway: frm.doc.payment_gateway,
          company: frm.doc.company,
        },
        callback: function (r) {
          if (!r.exc && r.message) {
            frm.set_value("mode_of_payment", r.message);
          }
        },
      });
    }
  },
});

function sync_payment_fields(frm) {
  if (frm.doc.mode_of_payment && !frm.doc.payment_gateway && frm.doc.company) {
    frappe.call({
      method:
        "mpsa_suite.mpsa_suite.api.sales_invoice.get_payment_gateway_from_mop",
      args: {
        mode_of_payment: frm.doc.mode_of_payment,
        company: frm.doc.company,
      },
      callback: function (r) {
        if (!r.exc && r.message) {
          frm.set_value("payment_gateway", r.message);
        }
      },
    });
  } else if (
    frm.doc.payment_gateway &&
    !frm.doc.mode_of_payment &&
    frm.doc.company
  ) {
    frappe.call({
      method:
        "mpsa_suite.mpsa_suite.api.sales_invoice.get_mop_from_payment_gateway",
      args: {
        payment_gateway: frm.doc.payment_gateway,
        company: frm.doc.company,
      },
      callback: function (r) {
        if (!r.exc && r.message) {
          frm.set_value("mode_of_payment", r.message);
        }
      },
    });
  }
}

// Resend M-Pesa Prompt / Check M-Pesa Status on a submitted M-Pesa Payment Request, through the
// prompt's own server functions: www.mpesa.stkpush.retry_stkpush sends the last prompt again (it
// refuses a paid one, or one just sent); create_new_request makes a new one when the last was
// paid and booked, or none was made (none while a paid prompt waits for its booking:
// money_in.payment_request.prompts_being_booked);
// m_pesa_api.check_transaction_status asks Safaricom. COLLECT_ROLES only.
function mpesa_prompt_actions(frm) {
  const doc = frm.doc;
  const gateway = (doc.payment_gateway || doc.payment_gateway_account || "").toLowerCase();
  if (doc.docstatus !== 1 || !gateway.startsWith("mpesa") || !frappe.boot.mpsa?.can_collect) return;
  frappe.db
    .get_list("Mpesa Express Request", {
      filters: { reference_doctype: "Payment Request", reference_name: doc.name, docstatus: 1 },
      fields: [
        "name", "status", "phone_number", "checkout_request_id", "transaction_id", "verification_status",
        "is_reconciled",
      ],
      order_by: "creation desc",
      limit: 20,
    })
    .then((rows) => {
      const last = (rows || [])[0];
      if (last && last.checkout_request_id && last.status !== "Completed") {
        frm.add_custom_button(__("Check M-Pesa Status"), () => mpesa_check_prompt(frm, last.name), __("M-Pesa"));
      }
      // A prompt the customer paid whose booking (its Payment Entry) is not made yet: the Payment
      // Request still owes that money until then, so a new prompt would ask the customer to pay
      // again. The server refuses one too (MpesaExpressRequest.validate_payment_request_amount).
      // Which prompts those are is the server's rule (payment_request.unbooked_prompts: one booked
      // through its C2B register, or whose money a reversal returned, is not), asked only when a
      // prompt looks paid and not reconciled.
      const maybe = (rows || []).some((r) => (r.status === "Completed" || r.transaction_id) && !cint(r.is_reconciled));
      const booking = maybe
        ? frappe.xcall("mpsa_suite.money_in.payment_request.prompts_being_booked", { payment_request: doc.name })
        : Promise.resolve([]);
      return booking.then((prompts) => {
        const paid = (prompts || [])[0];
        if (paid) {
          frm.set_intro(
            __("M-Pesa prompt {0} was paid{1} and is being booked: no new prompt is sent until it is.", [
              paid.name,
              paid.transaction_id ? ` (${paid.transaction_id})` : "",
            ]),
            "orange"
          );
          return;
        }
        // a prompt whose result is still being settled, or waits for a person, is left alone
        const undecided = last && ["Pending", "Mismatch", "Unverified"].includes(last.verification_status);
        if (
          ["Requested", "Initiated", "Partially Paid"].includes(doc.status) &&
          !undecided &&
          frappe.model.can_create("Mpesa Express Request")
        ) {
          frm.add_custom_button(__("Resend M-Pesa Prompt"), () => mpesa_resend_prompt(frm, last), __("M-Pesa"));
        }
      });
    });
}

function mpesa_resend_prompt(frm, last) {
  const again = last && last.status !== "Completed" && !last.transaction_id;
  frappe.prompt(
    {
      fieldname: "phone_number",
      fieldtype: "Data",
      label: __("Phone Number"),
      reqd: 1,
      default: (last && last.phone_number) || frm.doc.phone_number,
      description: __("Format: 0712345678 / 0112345678, or 254712345678 / 254112345678"),
    },
    (values) => {
      const phone_number = mpsa_collect.msisdn(values.phone_number);
      const call = again
        ? {
            method: "mpsa_suite.www.mpesa.stkpush.retry_stkpush",
            args: { name: last.name, phone_number },
          }
        : {
            method: "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.create_new_request",
            args: {
              phone_number,
              payment_gateway: frm.doc.payment_gateway,
              reference_type: "Payment Request",
              reference_id: frm.doc.name,
              // the request asks at most what the Payment Request still owes
              amount: flt(frm.doc.outstanding_amount) || flt(frm.doc.grand_total),
              currency: frm.doc.currency,
            },
          };
      frappe.call({
        ...call,
        freeze: true,
        freeze_message: __("Sending the prompt..."),
        callback: (r) => {
          if (r.exc) return;
          frappe.show_alert(
            { message: __("Prompt sent to {0}.", [frappe.utils.escape_html(phone_number)]), indicator: "green" },
            5
          );
          frm.reload_doc();
        },
      });
    },
    __("Resend M-Pesa Prompt"),
    __("Send")
  );
}

function mpesa_check_prompt(frm, name) {
  const e = frappe.utils.escape_html;
  frappe.call({
    method: "mpsa_suite.mpsa_suite.api.m_pesa_api.check_transaction_status",
    args: { name },
    freeze: true,
    freeze_message: __("Checking the prompt with M-Pesa..."),
    callback: (r) => {
      if (r.exc) return;
      const res = r.message || {};
      if (res.ResultDesc || res.ResponseDescription) {
        frappe.msgprint({
          title: __("M-Pesa Status"),
          indicator: String(res.ResultCode) === "0" ? "green" : "orange",
          message: [res.ResponseDescription, res.ResultDesc].filter(Boolean).map(e).join("<br>"),
        });
      } else if (res.errorMessage) {
        frappe.msgprint({ title: __("M-Pesa Status"), indicator: "red", message: e(res.errorMessage) });
      } else {
        frappe.msgprint(__("No answer from M-Pesa yet: check again in a minute."));
      }
      frm.reload_doc();
    },
  });
}
