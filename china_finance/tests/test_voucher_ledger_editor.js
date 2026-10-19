// Run with: node --test china_finance/tests/test_voucher_ledger_editor.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(
	path.join(root, "china_finance/report/china_voucher_ledger/china_voucher_ledger.js"),
	"utf8"
);

function inputHarness() {
	const handlers = {};
	return {
		handlers,
		value: "",
		on(event, handler) {
			handlers[event] = handler;
			return this;
		},
		attr() {
			return this;
		},
		select() {
			this.selected = true;
			return this;
		},
		val(value) {
			if (value === undefined) return this.value;
			this.value = value;
			return this;
		},
	};
}

function harness() {
	const controls = [];
	const timers = [];
	const notices = [];
	const context = vm.createContext({
		window: { setTimeout: (callback) => timers.push(callback) },
		document: {},
		china_finance: {},
		__: (text) => text,
		frappe: {
			query_reports: {},
			throw(message) { throw new Error(message); },
			show_alert() {},
			utils: {
				escape_html: (value) => String(value),
				icon: () => "",
			},
			ui: {
				form: {
					make_control({ df }) {
						const $input = inputHarness();
						const control = {
							df,
							$input,
							input: {},
							awesomplete: {},
							toggle_label() {},
							toggle_description() {},
							set_input_value(value) {
								$input.val(value);
							},
							set_focus() {
								this.focused = true;
							},
							get_value() {
								return $input.val();
							},
							set_value() {
								this.setValueCalls = (this.setValueCalls || 0) + 1;
							},
							on_input() {
								this.inputQueries = (this.inputQueries || 0) + 1;
							},
						};
						controls.push(control);
						return control;
					},
				},
			},
		},
	});
	context.window.window = context.window;
	vm.runInContext(source, context);
	context.recordNotice = (...args) => notices.push(args);
	vm.runInContext("show_inline_notice = (...args) => recordNotice(...args)", context);
	return {
		context,
		controls,
		timers,
		notices,
		accountEditor: vm.runInContext("create_inline_account_editor", context),
		summaryEditor: vm.runInContext("create_inline_summary_editor", context),
		amountEditor: vm.runInContext("create_inline_amount_editor", context),
		stageAmount: vm.runInContext("stage_inline_amount_change", context),
		cancel: vm.runInContext("cancel_inline_voucher_changes", context),
		save: vm.runInContext("save_inline_voucher_changes", context),
	};
}

function report() {
	return {
		get_filter_value: () => "测试公司",
		datatable: { cellmanager: { deactivateEditing() {} } },
	};
}

test("account editor focuses immediately without validating the existing account", () => {
	const h = harness();
	const editor = h.accountEditor(
		report(),
		"1001 - 库存现金 - 测试公司",
		{},
		{
			editable_account: true,
			edit_voucher_key: "Journal Entry:JV-1",
			edit_source_doctype: "Journal Entry",
			edit_source_name: "JV-1",
			edit_key: "je:row-1",
		}
	);
	editor.initValue("1001 - 库存现金 - 测试公司");
	const control = h.controls[0];
	assert.equal(control.focused, true);
	assert.equal(control.$input.value, "1001 - 库存现金 - 测试公司");
	assert.equal(control.setValueCalls, undefined);
	assert.equal(control.inputQueries, 1);
	control.df.change();
	assert.equal(h.timers.length, 1, "selecting a new account must finish cell editing");
});

test("summary editor stays active while typing and keeps space inside the input", () => {
	const h = harness();
	const editor = h.summaryEditor(
		report(),
		"原摘要",
		{},
		{
			editable_summary: true,
			edit_voucher_key: "Journal Entry:JV-1",
			summary_edit_key: "je-summary:row-1",
		}
	);
	editor.initValue("原摘要");
	const control = h.controls[0];
	assert.equal(control.focused, true);
	assert.equal(control.df.change, undefined, "typing must not deactivate the DataTable editor");
	assert.equal(
		typeof control.change,
		"function",
		"idle input must not rewrite the temporary value"
	);

	let propagationStopped = false;
	let defaultPrevented = false;
	control.$input.handlers["keydown.china_inline_voucher_editor"]({
		originalEvent: { key: " ", code: "Space", keyCode: 32 },
		stopPropagation() {
			propagationStopped = true;
		},
		preventDefault() {
			defaultPrevented = true;
		},
	});
	assert.equal(propagationStopped, true);
	assert.equal(defaultPrevented, false, "space must still be inserted into the input");
});

function amountReport() {
	const rows = [
		{ amount_edit_key: "je:expense", debit: 125, credit: 0 },
		{ amount_edit_key: "je:bank", debit: 0, credit: 125 },
	].map(row => ({
		...row,
		base_total_amount: 125,
		editable_amount: true,
		amount_precision: 2,
		currency: "CNY",
		edit_voucher_key: "Journal Entry:JV-1",
		edit_source_doctype: "Journal Entry",
		edit_source_name: "JV-1",
		edit_source_modified: "version-1",
	}));
	return {
		...report(),
		data: rows,
		refreshCount: 0,
		async refresh() { this.refreshCount++; },
		datatable: {
			cellmanager: { deactivateEditing() {} },
			datamanager: { getColumns: () => ["debit", "credit", "base_total_amount"].map(id => ({ id })) },
			refreshRow() {},
		},
	};
}

test("amount editor uses row numbers, keeps focus, allows signed values and rejects invalid input", () => {
	const h = harness();
	const r = amountReport();
	const editor = h.amountEditor(r, "CNY 125.00", {}, r.data[0], "debit");
	editor.initValue("CNY 125.00");
	const control = h.controls[0];
	assert.equal(control.df.fieldtype, "Currency");
	assert.equal(control.df.precision, 2);
	assert.equal(control.$input.value, 125, "formatted currency labels must not be parsed as input");
	assert.equal(control.focused, true);
	assert.equal(typeof control.change, "function");
	control.$input.val("-25.10");
	control.get_value = () => -25.1;
	assert.equal(editor.getValue(), -25.1);
	control.$input.val("");
	assert.equal(editor.getValue(), 0);
	control.$input.val("invalid");
	control.get_value = () => null;
	assert.throws(() => editor.getValue(), /请输入有效金额/);
});

test("both amounts stage in one voucher and cancellation restores zero values and totals", () => {
	const h = harness();
	const r = amountReport();
	h.stageAmount(r, r.data[0], "debit", 100);
	h.stageAmount(r, r.data[1], "credit", 100);
	assert.equal(r._china_inline_edit_state.changes.size, 2);
	assert.equal(r.data[0].base_total_amount, 100);
	assert.equal(r.data[1]._inline_credit_dirty, true);
	h.stageAmount(r, r.data[0], "credit", -5);
	h.cancel(r);
	assert.equal(r._china_inline_edit_state, null);
	assert.deepEqual(r.data.map(row => [row.debit, row.credit, row.base_total_amount]), [[125, 0, 125], [0, 125, 125]]);
	assert.equal(r.data[0]._inline_credit_dirty, undefined);
});

test("changing an amount back clears dirty state and another voucher cannot steal pending edits", () => {
	const h = harness();
	const r = amountReport();
	h.stageAmount(r, r.data[0], "debit", 100);
	assert.equal(h.amountEditor(r, 125, {}, {...r.data[1], edit_voucher_key: "Journal Entry:JV-2"}, "credit"), false);
	assert.equal(r._china_inline_edit_state.voucher_key, "Journal Entry:JV-1");
	h.stageAmount(r, r.data[0], "debit", 125);
	assert.equal(r._china_inline_edit_state, null);
	assert.equal(r.data[0]._inline_debit_dirty, false);
});

test("posted amount changes wait for confirmation then submit both fields in one request", async () => {
	const h = harness();
	const r = amountReport();
	const calls = [];
	h.context.frappe.call = async args => {
		calls.push(args);
		return { message: args.method.includes("preview_") ? { requires_confirmation: true } : { message: "done" } };
	};
	h.stageAmount(r, r.data[0], "debit", 100);
	h.stageAmount(r, r.data[1], "credit", 100);
	await h.save(r);
	assert.equal(calls.length, 1);
	assert.equal(r._china_inline_edit_state.confirming, true);
	assert.equal(r.refreshCount, 0);
	await h.save(r, true);
	assert.equal(calls.length, 2);
	assert.equal(calls[1].type, "POST");
	assert.equal(calls[1].args.changes.length, 2);
	assert.equal(r._china_inline_edit_state, null);
	assert.equal(r.refreshCount, 1);
});

test("failed amount preflight preserves staged values for correction and sends no apply request", async () => {
	const h = harness();
	const r = amountReport();
	let calls = 0;
	h.context.frappe.call = async () => { calls++; throw new Error("unbalanced"); };
	h.stageAmount(r, r.data[0], "debit", 100);
	await h.save(r);
	assert.equal(calls, 1);
	assert.equal(r.data[0].debit, 100);
	assert.equal(r._china_inline_edit_state.pending, false);
	assert.equal(r._china_inline_edit_state.changes.size, 1);
	assert.equal(r.refreshCount, 0);
});

test("save waits for the last active amount cell before building its request", async () => {
	const h = harness();
	const r = amountReport();
	const calls = [];
	h.stageAmount(r, r.data[0], "debit", 100);
	r.datatable.cellmanager.submitEditing = async () => {
		await Promise.resolve();
		h.stageAmount(r, r.data[1], "credit", 100);
	};
	h.context.frappe.call = async args => { calls.push(args); return { message: {} }; };
	await h.save(r);
	assert.equal(calls[0].args.changes.length, 2);
	assert.equal(calls[1].args.changes.length, 2);
});
