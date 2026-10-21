const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

let handlers, pending, message;
const context = {
	__: (value) => value,
	frappe: {
		ui: {form: {on: (_doctype, events) => { handlers = events; }}},
		call: () => new Promise((resolve) => { pending = resolve; }),
		msgprint: (value) => { message = value; },
	},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,
	"../china_finance/doctype/china_statement_opening_balance/china_statement_opening_balance.js"), "utf8"), context);

function form(doc) {
	return {
		doc, buttons: [], set_intro() {}, refresh_field() {},
		add_custom_button(_label, action) { this.buttons.push(action); },
		async set_value(values) { Object.assign(this.doc, values); },
		add_child(_field, row) { this.doc.rows.push(row); },
	};
}
const definition = {template: "BS", opening_date: "2026-01-01", source_period_end: "2025-12-31",
	rows: [{row_code: "FIXED_ASSETS_NET", report_label: "Net fixed assets"}, {row_code: "TOTAL_ASSETS", report_label: "Total assets"}]};

(async () => {
	const current = form({company: "A", opening_date: "2026-01-01", docstatus: 0, template: "BS", currency: "CNY",
		rows: [{row_code: "FIXED_ASSETS_NET", provided: 1, source_label: "Fixed assets", amount: 3510.91}]});
	handlers.refresh(current);
	const fill = current.buttons[0]();
	pending({message: definition});
	await fill;
	assert.equal(current.doc.rows.length, 2);
	assert.equal(current.doc.rows[0].amount, 3510.91, "prefill must preserve direct source amounts");
	assert.equal(current.doc.rows[1].provided, 0, "prefilled rows must not imply verified zero amounts");
	assert.equal(current.doc.rows[1].amount, undefined, "no amount may be inferred from account balances");

	const stale = form({company: "A", opening_date: "2026-01-01", docstatus: 0, rows: []});
	handlers.refresh(stale);
	const old = stale.buttons[0]();
	stale.doc.company = "B";
	pending({message: definition});
	await old;
	assert.equal(stale.doc.rows.length, 0);
	assert.equal(stale.doc.template, undefined);

	const mismatch = form({company: "A", opening_date: "2026-01-01", docstatus: 0, template: "Other", rows: [{row_code: "A"}]});
	handlers.refresh(mismatch);
	const request = mismatch.buttons[0]();
	pending({message: definition});
	await request;
	assert.equal(mismatch.doc.template, "Other");
	assert.ok(message);
	const submitted = form({docstatus: 1});
	handlers.refresh(submitted);
	assert.equal(submitted.buttons.length, 0);
	console.log("PASS: direct amounts preserved, prefill unconfirmed, stale context ignored, template mismatch rejected and submitted forms read-only");
})().catch((error) => { console.error(error); process.exitCode = 1; });
