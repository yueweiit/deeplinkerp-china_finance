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
	assert.equal(await failed.refresh(), false);
	assert.equal(failed.loading, false);
	assert.equal(failed.data, null);
	assert.match(failed.$rows.content, /translated:Unable to load statement mapping/);
	assert.equal(failed.$summary.content, "");
	assert.equal(failed.$reclassifications.content, "");
	assert.equal(failed.$accounts.content, "");
	assert.equal(failed.rendered, undefined);

	const success = harness(), data = { rows: ["unchanged"] };
	context.frappe.xcall = async () => data;
	assert.equal(await success.refresh(), true);
	assert.equal(success.loading, false);
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
	let adapter, notice;
	context.window = {china_finance: {company_context: {default_company: () => "Company A", register_page: (_name, value) => {adapter = value;}}}};
	context.frappe.defaults = {get_user_default: () => "Company A"};
	context.frappe.user = {has_role: () => false}; context.frappe.breadcrumbs = {add() {}};
	context.frappe.msgprint = value => {notice = value;};
	context.frappe.ui = {make_app_page: () => ({set_primary_action() {}, add_field: df => ({df, value: df.default,
		get_value() {return this.value;}, async set_value(value) {this.value = value; return this.df.change?.();}})})};
	context.Mapping.prototype.setup_content = () => {};
	const page = new context.Mapping({}); page.data = data; page.selected_accounts.add("old account");
	const saved = adapter.capture("Company A");
	page.refresh = async () => {page.selected_accounts.clear(); page.data = null; return false;};
	assert.equal(await adapter.switch_company("Company B"), false, "failed loads must reach the company transaction");
	page.render_all = () => {};
	await adapter.restore(saved);
	assert.equal(page.company.get_value(), "Company A"); assert.equal(page.data, data);
	assert.ok(page.selected_accounts.has("old account"));
	page.loading = true; assert.equal(adapter.before_switch(), false); assert.match(notice, /尚未准备好/);
	console.log("PASS: load outcomes, mapping rollback, preserved selections, loading guard and stale request isolation");
})().catch((error) => { console.error(error); process.exitCode = 1; });
