const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const context = { frappe: { pages: { "china-statement-mapping": {} } }, __: (text) => "translated:" + text };
vm.createContext(context);
const source = fs.readFileSync(path.join(__dirname, "../china_finance/page/china_statement_mapping/china_statement_mapping.js"), "utf8");
vm.runInContext(source + "\nthis.Mapping = ChinaStatementMapping;", context);
const field = (value) => ({ get_value: () => value });
function harness() {
	const mapping = Object.create(context.Mapping.prototype);
	Object.assign(mapping, { company: field("Test"), statement_type: field("Balance Sheet"), accounting_standard: field("跟随公司设置") });
	for (const key of ["selected_accounts", "selected_rows", "collapsed_rows", "collapsed_accounts", "expanded_aggregates"]) mapping[key] = new Set(["old"]);
	for (const key of ["$summary", "$reclassifications", "$accounts", "$rows"]) mapping[key] = { content: "old", empty() { this.content = ""; }, html(value) { this.content = value; } };
	mapping.render_all = () => { mapping.rendered = true; };
	return mapping;
}
(async () => {
	const failed = harness(); failed.data = { stale: true };
	context.frappe.xcall = () => Promise.reject();
	await assert.doesNotReject(() => failed.refresh());
	assert.equal(failed.data, null);
	assert.match(failed.$rows.content, /translated:Unable to load statement mapping/);
	assert.equal(failed.$summary.content, "");
	assert.equal(failed.$reclassifications.content, "");
	assert.equal(failed.$accounts.content, "");
	assert.equal(failed.rendered, undefined);

	const success = harness(), data = { rows: ["unchanged"] };
	context.frappe.xcall = async () => data;
	await success.refresh();
	assert.equal(success.data, data);
	assert.equal(success.rendered, true);

	const stale = harness(); let rejectOld;
	context.frappe.xcall = () => new Promise((_resolve, reject) => { rejectOld = reject; });
	const oldRequest = stale.refresh();
	stale.refresh_request = {};
	stale.data = data; stale.$rows.content = "new company result";
	rejectOld(); await oldRequest;
	assert.equal(stale.data, data);
	assert.equal(stale.$rows.content, "new company result");
	console.log("PASS: localized failure state, successful response and stale request isolation");
})().catch((error) => { console.error(error); process.exitCode = 1; });
