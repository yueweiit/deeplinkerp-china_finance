const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname,
	"../china_finance/report/china_voucher_ledger/china_voucher_ledger.js"), "utf8");
const expectedStatus = ["全部有效凭证", "未记账", "待记账", "已记账", "已冲销"];
const expectedPeriods = ["", "本月凭证", "上月凭证", "近三个月", "本年凭证"];

for (const language of ["zh", "en", "es"]) {
	const context = {
		frappe: { query_reports: {} },
		__: (text) => `${language}:${text}`,
	};
	vm.createContext(context);
	vm.runInContext(source, context);
	const filters = context.frappe.query_reports["China Voucher Ledger"].filters;
	const status = filters.find((filter) => filter.fieldname === "voucher_status");
	const period = filters.find((filter) => filter.fieldname === "period_preset");
	assert.deepEqual(Array.from(status.options), expectedStatus);
	assert.deepEqual(Array.from(period.options), expectedPeriods);
	assert.equal(status.default, expectedStatus[0]);
	assert.equal(period.default, expectedPeriods[1]);
	assert.equal(status.label, `${language}:状态`);
	assert.equal(period.label, `${language}:凭证日期`);
}
console.log("PASS: voucher filter values remain compatible with backend status and period codes in all languages");
