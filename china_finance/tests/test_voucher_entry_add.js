const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../public/js/voucher_entry_add.js"), "utf8");
function harness() {
	const dialogs = [],
		staged = [],
		messages = [];
	class Dialog {
		constructor(options) {
			this.options = options;
			this.doc = options.doc;
			this.values = {
				source_row: "je:bank",
				account: "Income",
				side: "credit",
				amount: 118811.88,
				summary: "",
			};
			dialogs.push(this);
		}
		show() {}
		hide() {
			this.options.onhide();
		}
		get_value(key) {
			return this.values[key];
		}
		set_df_property() {}
		async set_values(values) {
			Object.assign(this.values, values);
		}
	}
	const context = vm.createContext({
		china_finance: { voucher_entry_add: {} },
		__: (s, args = []) => s.replace(/\{(\d+)\}/g, (_, i) => args[i] ?? ""),
		frappe: {
			provide() {},
			ui: { Dialog },
			format: (v) => String(v),
			utils: {
				escape_html: (v) => String(v).replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
			},
			msgprint: (v) => messages.push(v),
			call() {
				throw Error("Opening an add dialog must not save a voucher");
			},
		},
	});
	vm.runInContext(source, context);
	const row = {
		editable_add: true,
		edit_voucher_key: "JE:1",
		edit_key: "je:bank",
		edit_source_doctype: "Journal Entry",
		edit_source_name: "JV-1",
		account: "Bank",
		debit: 100000,
		credit: 0,
		currency: "CNY",
		amount_precision: 2,
	};
	const report = {
		data: [
			row,
			{ ...row, edit_key: "je:second", account: "Expense", debit: 0, credit: 100000 },
		],
	};
	return {
		dialogs,
		staged,
		messages,
		row,
		report,
		open: () =>
			context.china_finance.voucher_entry_add.open(report, row, (row, value) => {
				staged.push({ row, value });
				return true;
			}),
	};
}
test("add accepts an amount above the original and an independent credit direction without saving", async () => {
	const h = harness();
	await h.open();
	const d = h.dialogs[0];
	d.options.primary_action(d.values);
	assert.equal(h.staged[0].value.amount, 118811.88);
	assert.equal(h.staged[0].value.side, "credit");
	assert.equal(h.staged[0].value.source_row, "je:bank");
	assert.equal(h.row.debit, 100000);
	assert.equal(h.report._china_add_dialog, null);
});
test("equal and signed amounts can be staged while zero and non-finite inputs are blocked", async () => {
	for (const amount of [100000, -118811.88, 0, NaN, Infinity]) {
		const h = harness();
		await h.open();
		const d = h.dialogs[0];
		d.values.amount = amount;
		d.options.primary_action(d.values);
		assert.equal(h.staged.length, Number.isFinite(amount) && amount !== 0 ? 1 : 0);
	}
});
test("same-voucher edits remain pending and a second dialog cannot open", async () => {
	const h = harness();
	h.report._china_inline_edit_state = { voucher_key: "JE:1", changes: new Map([["x", {}]]) };
	await h.open();
	await h.open();
	assert.equal(h.dialogs.length, 1);
	assert.equal(h.staged.length, 0);
});
test("other vouchers and pending saves cannot stage an addition", async () => {
	for (const state of [{ voucher_key: "other" }, { voucher_key: "JE:1", pending: true }]) {
		const h = harness();
		h.report._china_inline_edit_state = state;
		await h.open();
		assert.equal(h.dialogs.length, 0);
	}
});
test("reference selection excludes deleted and unsaved rows and updates the account query", async () => {
	const h = harness();
	h.report.data.push(
		{ ...h.row, edit_key: "deleted", _inline_deleted: true },
		{ ...h.row, edit_key: "new:1", _inline_added: true }
	);
	await h.open();
	const d = h.dialogs[0];
	const selector = d.options.fields.find((f) => f.fieldname === "source_row");
	assert.deepEqual(
		Array.from(selector.options, (o) => o.value),
		["je:bank", "je:second"]
	);
	d.values.source_row = "je:second";
	await selector.onchange();
	assert.equal(
		d.options.fields.find((f) => f.fieldname === "account").get_query().filters.edit_key,
		"je:second"
	);
	assert.equal(d.values.side, "credit");
	assert.equal(d.values.amount, null);
});
