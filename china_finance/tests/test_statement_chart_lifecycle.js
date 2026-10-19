// Run with: node china_finance/tests/test_statement_chart_lifecycle.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

let routeChanged;
const report = {raw_data: {chart: {type: "bar", data: {labels: ["A"]}}}};
const context = {
	frappe: {
		query_reports: {}, query_report: report,
		router: {on: (_event, fn) => { routeChanged = fn; }},
		get_route: () => ["query-report", "General Ledger"],
	},
	$: () => ({remove() {}}),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,
	"../china_finance/report/china_financial_statements/china_financial_statements.js"), "utf8"), context);
const settings = context.frappe.query_reports["China Financial Statements"];
let disposed = 0;
const makeChart = () => ({destroy() { disposed++; }});

// The framework asks for chart data before emptying the previous chart's DOM.
report.chart = makeChart();
assert.equal(settings.get_chart_data(), report.raw_data.chart);
assert.equal(disposed, 1);
assert.equal(report.chart, null);
settings.get_chart_data();
assert.equal(disposed, 1, "a chart must only be destroyed once");

// An empty result skips get_chart_data; after_refresh must still disconnect it.
report.chart = report.__china_statement_chart = makeChart();
report.data = [];
report.get_filter_value = () => "Cash Flow";
settings.after_refresh(report);
assert.equal(disposed, 2);
assert.equal(report.chart, null);

// Route cleanup must only dispose the chart owned by this report.
const chart = makeChart();
report.__china_statement_chart = chart;
report.chart = makeChart();
routeChanged();
assert.equal(disposed, 3);
assert.notEqual(report.chart, chart);
routeChanged();
assert.equal(disposed, 3);
console.log("PASS: statement chart replacement, empty results and route cleanup");
