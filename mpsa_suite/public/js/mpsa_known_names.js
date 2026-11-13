// The name M-Pesa gave each phone, till and paybill it paid before (money_out.known_names, round 4.5),
// shown as a badge wherever a payout is typed or approved. Daraja names no one before the money leaves,
// so the name M-Pesa gave the number last time is the check that it is the right number:
// - green "M-Pesa: NAME · paid N times, last 28 Oct": the typed (or expected) name matches;
// - orange "M-Pesa knows this number as NAME": it does not;
// - blue "New number: never paid; check it with the person";
// - gray "M-Pesa shows the bank's name only" (a bank's paybill), or "Paid before" (a name seen on
//   salaries only, to who may not see salaries).
// The server builds each badge (for_destinations: read on M-Pesa Known Name, salary names hidden);
// this module asks for them and shows them under a field, or as a list under a table. The Send via
// M-Pesa dialog (mpsa_send.js) shows the same names from its own summary.
//
// For later features (the bulk payout sheet, the petty cash phone, Pay via Staff):
//   mpsa_known_names.show(frm, fieldname, destination)       a badge under a field
//   mpsa_known_names.show_rows(frm, table, rows)             a list under a table: rows are destinations
//                                                            with an id and a label ("#3 Jane Doe")
//   mpsa_known_names.fetch([destination, ...])               -> Promise of {id: info} (the server's answer)
//   mpsa_known_names.badge_html(info)                        one badge
//   mpsa_known_names.counts(infos)                           "3 known and matching · 1 under another name · 2 new"
// A destination: {id, key} or {id, type, number}: type Phone, Till or Paybill (or any payee type, schedule
// route or B2B command), number (or phone, shortcode, a schedule line's destination); and the name to
// compare: typed_name, else account_doctype + account (an approved account's Expected Name), claim_payee
// or party_type + party, which the server reads where the user may.
/* global mpsa_known_names */
frappe.provide("mpsa_known_names");

mpsa_known_names.METHOD = "mpsa_suite.money_out.known_names.for_destinations";
mpsa_known_names.DOCTYPE = "M-Pesa Known Name";
// the same question within this time is answered from the browser (a form refreshes often)
mpsa_known_names.CACHE_MS = 60 * 1000;
mpsa_known_names.COLOURS = {
	green: "--green-600",
	orange: "--orange-600",
	blue: "--blue-600",
	gray: "--gray-600",
};
mpsa_known_names._cache = {};

// the server answers who may read the list (the roles that prepare, approve or send payouts)
mpsa_known_names.allowed = () =>
	Boolean(frappe.model && frappe.model.can_read && frappe.model.can_read(mpsa_known_names.DOCTYPE));

// a destination worth asking about: something that names a number
mpsa_known_names.wanted = (d) =>
	Boolean(d && (d.key || d.number || d.phone || d.shortcode || d.destination || d.account));

mpsa_known_names.badge_html = (info) => {
	const badge = info && info.badge;
	if (!badge || !badge.text) return "";
	const e = frappe.utils.escape_html;
	const colour = mpsa_known_names.COLOURS[badge.colour] || mpsa_known_names.COLOURS.gray;
	let html = `<span class="mpsa-known-name" data-verdict="${e(info.verdict || info.status || "")}" style="color: var(${colour})">${e(
		badge.text
	)}</span>`;
	if (info.verdict === "differs" && info.typed_name) {
		html += ` <span class="text-muted">${__("(given: {0})", [e(info.typed_name)])}</span>`;
	}
	return html;
};

// "3 known and matching · 1 under another name · 2 new" for a list of the server's answers
mpsa_known_names.counts = (infos) => {
	const n = { match: 0, differs: 0, new: 0, bank: 0, hidden: 0 };
	for (const info of infos || []) {
		if (!info || !info.badge) continue;
		if (info.bank) n.bank += 1;
		else if (info.status === "hidden") n.hidden += 1;
		else if (info.status === "new" || info.verdict === "new") n.new += 1;
		else if (info.verdict === "differs") n.differs += 1;
		else n.match += 1;
	}
	const parts = [];
	if (n.match) parts.push(__("{0} known and matching", [n.match]));
	if (n.differs) parts.push(__("{0} under another name", [n.differs]));
	if (n.new) parts.push(__("{0} new", [n.new]));
	if (n.bank) parts.push(__("{0} bank (M-Pesa shows the bank's name only)", [n.bank]));
	if (n.hidden) parts.push(__("{0} paid before", [n.hidden]));
	return parts.join(" · ");
};

mpsa_known_names.fetch = (destinations) => {
	const list = (destinations || []).filter((d) => mpsa_known_names.wanted(d));
	if (!list.length || !mpsa_known_names.allowed()) return Promise.resolve({});
	const key = JSON.stringify(list);
	const now = Date.now();
	const hit = mpsa_known_names._cache[key];
	if (hit && now - hit.at < mpsa_known_names.CACHE_MS) return hit.answer;
	if (Object.keys(mpsa_known_names._cache).length > 50) mpsa_known_names._cache = {};
	const answer = frappe
		.xcall(mpsa_known_names.METHOD, { destinations: list })
		.then((r) => r || {})
		.catch(() => ({}));
	mpsa_known_names._cache[key] = { at: now, answer };
	return answer;
};

// the box the badges go in, at the end of a field's own wrapper (hidden with the field)
mpsa_known_names._box = (frm, fieldname, css) => {
	const field = frm && frm.fields_dict && frm.fields_dict[fieldname];
	if (!field || !field.$wrapper) return null;
	let box = field.$wrapper.children(`.${css}`);
	if (!box.length) {
		box = $(`<div class="${css} small" style="margin-top: 4px"></div>`);
		field.$wrapper.append(box);
	}
	return box;
};

mpsa_known_names.clear = (frm, fieldname) => {
	const box = mpsa_known_names._box(frm, fieldname, "mpsa-known-name-box");
	if (box) box.empty();
};

// a badge under ``fieldname`` for one destination (cleared when there is nothing to ask about)
mpsa_known_names.show = async (frm, fieldname, destination) => {
	const box = mpsa_known_names._box(frm, fieldname, "mpsa-known-name-box");
	if (!box) return;
	if (!mpsa_known_names.wanted(destination) || !mpsa_known_names.allowed()) {
		box.empty();
		return;
	}
	const asked = JSON.stringify(destination);
	box.attr("data-asked", asked);
	const answer = await mpsa_known_names.fetch([Object.assign({}, destination, { id: "one" })]);
	if (box.attr("data-asked") !== asked) return; // changed meanwhile
	box.html(mpsa_known_names.badge_html(answer.one));
};

// a list under the table ``table``: one line per row with a number ("#3 Jane Doe: badge"), and the counts
mpsa_known_names.show_rows = async (frm, table, rows) => {
	const box = mpsa_known_names._box(frm, table, "mpsa-known-names");
	if (!box) return;
	const wanted = (rows || []).filter((r) => mpsa_known_names.wanted(r));
	if (!wanted.length || !mpsa_known_names.allowed()) {
		box.empty();
		return;
	}
	const ask = wanted.map((r, i) => {
		const d = Object.assign({}, r, { id: String(r.id || i) });
		delete d.label;
		return d;
	});
	const asked = JSON.stringify(ask);
	box.attr("data-asked", asked);
	const answer = await mpsa_known_names.fetch(ask);
	if (box.attr("data-asked") !== asked) return;
	const e = frappe.utils.escape_html;
	const infos = ask.map((d) => answer[d.id]);
	const lines = wanted
		.map((r, i) => {
			const html = mpsa_known_names.badge_html(infos[i]);
			return html ? `<div>${e(r.label || "")}${r.label ? ": " : ""}${html}</div>` : "";
		})
		.filter(Boolean);
	if (!lines.length) {
		box.empty();
		return;
	}
	box.html(
		`<div class="text-muted">${__("Names M-Pesa gave these numbers before")}: ${e(
			mpsa_known_names.counts(infos)
		)}</div>${lines.join("")}`
	);
};

// ---------------------------------------------------------------- the forms
const MPSA_KN_PHONE = "Phone (B2C)";

// Supplier and Employee M-Pesa Account: at entry and at approval, under the phone or the till/paybill
mpsa_known_names.account = (frm) => {
	const d = frm.doc;
	const phone = d.account_type === MPSA_KN_PHONE;
	mpsa_known_names.clear(frm, phone ? "shortcode" : "phone");
	mpsa_known_names.show(frm, phone ? "phone" : "shortcode", {
		type: d.account_type,
		number: phone ? d.phone : d.shortcode,
		typed_name: d.expected_name || null,
	});
};

// the B2B request form: its till or paybill, against the approved account's expected name (else the party's)
mpsa_known_names.b2b = (frm) => {
	const d = frm.doc;
	const account = d.employee_mpesa_account || d.supplier_mpesa_account || null;
	mpsa_known_names.show(frm, "receiver_shortcode", {
		type: d.command_id,
		number: d.receiver_shortcode,
		account_doctype: d.employee_mpesa_account
			? "Employee M-Pesa Account"
			: d.supplier_mpesa_account
			? "Supplier M-Pesa Account"
			: null,
		account,
		claim_payee: d.claim_payee || null,
		party_type: d.supplier ? "Supplier" : d.employee ? "Employee" : null,
		party: d.supplier || d.employee || null,
	});
};

// M-Pesa Payment Schedule lines (Pay Due Lines pays them): each line's account
mpsa_known_names.schedule = (frm) => {
	const rows = (frm.doc.lines || [])
		.filter((l) => l.status !== "Cancelled")
		.map((l) => ({
			id: l.name,
			label: `#${l.idx} ${l.payee_name || l.party || ""}`.trim(),
			route: l.route,
			destination: l.destination,
			account_doctype: l.payee_account_doctype || null,
			account: l.payee_account || null,
			party_type: l.party_type || null,
			party: l.party || null,
		}));
	mpsa_known_names.show_rows(frm, "lines", rows);
};

// Expense Claim payee lines (round 4.3 fills a known phone's name; this shows every line's, tills too)
mpsa_known_names.claim = (frm) => {
	const rows = (frm.doc.mpesa_payees || [])
		.filter((r) => r.status !== "Cancelled")
		.map((r) => ({
			id: r.name,
			label: `#${r.idx} ${r.payee_name || ""}`.trim(),
			type: r.payee_type,
			number: r.payee_type === MPSA_KN_PHONE ? r.phone : r.shortcode,
			typed_name: r.typed_name || r.payee_name || null,
		}));
	mpsa_known_names.show_rows(frm, "mpesa_payees", rows);
};

// B2C Payment Disbursement rows: each phone, against its approved account, claim payee or party
mpsa_known_names.disbursement = (frm) => {
	const rows = (frm.doc.references || []).map((r) => ({
		id: r.name,
		label: `#${r.idx} ${r.party || ""}`.trim(),
		type: "Phone",
		number: r.partyb,
		account_doctype: r.payee_account_doctype || null,
		account: r.payee_account || null,
		claim_payee: r.claim_payee || null,
		party_type: r.party_type || frm.doc.party_type || null,
		party: r.party || null,
	}));
	mpsa_known_names.show_rows(frm, "references", rows);
};

// each form: what draws its badges, the fields that redraw them, and its table's row fields
mpsa_known_names.FORMS = {
	"Supplier M-Pesa Account": {
		render: mpsa_known_names.account,
		fields: ["account_type", "phone", "shortcode", "expected_name"],
	},
	"Employee M-Pesa Account": {
		render: mpsa_known_names.account,
		fields: ["account_type", "phone", "shortcode", "expected_name"],
	},
	"Mpesa B2B Payment Request": {
		render: mpsa_known_names.b2b,
		fields: ["command_id", "receiver_shortcode", "supplier_mpesa_account", "employee_mpesa_account", "supplier", "employee"],
	},
	"M-Pesa Payment Schedule": {
		render: mpsa_known_names.schedule,
		table: "lines",
		child: "M-Pesa Payment Schedule Line",
		child_fields: ["route", "destination", "payee_account", "party", "payee_name"],
	},
	"Expense Claim": {
		render: mpsa_known_names.claim,
		table: "mpesa_payees",
		child: "M-Pesa Claim Payee",
		child_fields: ["payee_type", "phone", "shortcode", "payee_name", "typed_name"],
	},
	"B2C Payment Disbursement": {
		render: mpsa_known_names.disbursement,
		table: "references",
		child: "B2C Payment Disbursement Reference",
		child_fields: ["partyb", "party", "payee_account", "claim_payee"],
	},
};

// one redraw per burst of changes (a paste adds many rows at once)
mpsa_known_names.later = (frm, render) => {
	clearTimeout(frm.mpsa_known_names_timer);
	frm.mpsa_known_names_timer = setTimeout(() => render(frm), 300);
};

mpsa_known_names.register = () => {
	for (const [doctype, form] of Object.entries(mpsa_known_names.FORMS)) {
		const redraw = (frm) => mpsa_known_names.later(frm, form.render);
		const handlers = { refresh: redraw };
		for (const field of form.fields || []) handlers[field] = redraw;
		frappe.ui.form.on(doctype, handlers);
		if (!form.child) continue;
		const child = { [`${form.table}_remove`]: redraw };
		for (const field of form.child_fields || []) child[field] = redraw;
		frappe.ui.form.on(form.child, child);
	}
};

mpsa_known_names.register();
