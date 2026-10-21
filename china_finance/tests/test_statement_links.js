// Run with: node china_finance/tests/test_statement_links.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

let handler, route, alert;
const escape_html = (s) => String(s).replaceAll("&", "&amp;").replaceAll('"', "&quot;")
	.replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll("'", "&#39;");
const filters = {company: "Test", statement_type: "Profit and Loss", from_date: "2026-01-01", to_date: "2026-12-31"};
const context = {
	frappe: {query_reports: {}, router: {on() {}}, utils: {escape_html}, query_report: {get_filter_value: (key) => filters[key]},
		set_route: (...args) => { route = args; }, show_alert: (value) => { alert = value; }},
	__: (s) => s, document: {},
	$: (element) => element === context.document ? {on: (_event, _selector, fn) => { handler = fn; }} : element,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../china_finance/report/china_financial_statements/china_financial_statements.js"), "utf8"), context);
const accounts = ["660303 - 手续费", "660302 - 利息"];
const link = context.source_account_link("财务费用", [...accounts, accounts[0]]);
assert.ok(link.includes('data-accounts="' + escape_html(JSON.stringify(accounts)) + '"'));
assert.ok(!link.includes("data-account="));
const unsafe = context.source_account_link("label", ['x\"><script>bad</script>']);
assert.ok(!unsafe.includes("<script>"));
context.bind_source_account_links();
let prevented = false;
handler.call({attr: (key) => key === "data-accounts" ? JSON.stringify(accounts) : undefined},
	{preventDefault: () => { prevented = true; }});
assert.ok(prevented);
assert.equal(route[1], "General Ledger");
assert.deepEqual(Array.from(route[2].account), accounts);
assert.equal(route[2].from_date, filters.from_date);
assert.ok(alert.message.includes("结转"));
const report = {get_filter_value: () => "Account Activity and Balance"};
context.frappe.query_reports["China Financial Statements"].after_refresh(report);
report.get_filter_value = () => "Profit and Loss";
context.frappe.query_reports["China Financial Statements"].after_refresh(report);
console.log("PASS: all-account links, escaping, route filters, closing explanation and refresh without closing control");

vm.runInContext(fs.readFileSync(path.join(__dirname, "../china_finance/report/china_purchase_reconciliation/china_purchase_reconciliation.js"), "utf8"), context);
assert.equal(context.frappe.query_reports["China Purchase Reconciliation"].filters, undefined,
	"Report JSON filters must not be replaced by an empty JavaScript array");

// A heading has no balance; a fully depreciated asset still has a real zero net balance.
let tree_options;
context.window = {DataTable: class {
	constructor(_body, options) {
		tree_options = options;
		this.rowmanager = {setTreeDepth() {}};
	}
}};
context.frappe.boot = {lang: "zh"};
context.frappe.utils.datatable = {get_translations: () => ({})};
context.frappe.utils.is_rtl = () => false;
context.format_currency = (amount, currency) => `${currency} ${amount.toFixed(2)}`;
const panel = {body: {}, $expand: {on() {}}, $collapse: {on() {}}};
const heading = {label: "Fixed assets", row_type: "Heading", opening_amount: null, amount: null};
const net_asset = {label: "Net fixed assets", row_type: "Formula", opening_amount: 3510.91, amount: 0};
context.render_balance_sheet_tree(panel, [heading, net_asset], "CNY");
for (const column of tree_options.columns.filter((column) => column.id !== "label")) {
	assert.equal(column.format(heading[column.id], 0, column, heading), "");
	assert.equal(column.format(net_asset[column.id], 1, column, net_asset),
		context.format_currency(net_asset[column.id], "CNY"));
}
const financial_settings = context.frappe.query_reports["China Financial Statements"];
assert.equal(financial_settings.formatter(null, 0,
	{fieldtype: "Currency", fieldname: "asset_amount"}, {asset_row_type: "Heading"},
	() => { throw new Error("Heading amount must remain blank"); }), "");
assert.equal(financial_settings.formatter(0, 1,
	{fieldtype: "Currency", fieldname: "asset_amount"}, {asset_row_type: "Formula"},
	() => "CNY 0.00"), "CNY 0.00");
console.log("PASS: balance-sheet heading amounts are blank and zero net assets remain visible");
