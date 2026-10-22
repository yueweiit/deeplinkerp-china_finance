// Run with: node china_finance/tests/test_fixed_asset_presentation.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const context = {frappe: {query_reports: {}, router: {on() {}}}, __: (text) => text};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,
	"../china_finance/report/china_financial_statements/china_financial_statements.js"), "utf8"), context);
const settings = context.frappe.query_reports["China Financial Statements"];
for (const type of ["Account Activity and Balance", "Trial Balance", "Balance Sheet", "Profit and Loss", "Cash Flow"]) {
	let visibility;
	const report = {
		data: [{}], get_filter: () => ({}), get_filter_value: () => type,
		toggle_filter_display(field, visible) { visibility = {field, visible}; },
		$report: {hide() {}}, $tree_footer: {hide() {}},
	};
	settings.after_refresh(report);
	assert.deepEqual(visibility, {
		field: "show_fixed_asset_details", visible: type === "Account Activity and Balance",
	});
}
assert.doesNotThrow(() => context.sync_fixed_asset_detail_filter({get_filter: () => null}));
assert.doesNotThrow(() => context.sync_fixed_asset_detail_filter({}));
console.log("PASS: fixed asset detail option is scoped to activity/balance and tolerates older filter metadata");
