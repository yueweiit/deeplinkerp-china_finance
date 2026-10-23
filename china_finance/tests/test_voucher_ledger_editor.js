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

test("voucher report retains source-document access and China Finance report roles", () => {
	const metadata = JSON.parse(
		fs.readFileSync(
			path.join(root, "china_finance/report/china_voucher_ledger/china_voucher_ledger.json"),
			"utf8"
		)
	);
	assert.equal(metadata.ref_doctype, "Journal Entry");
	const roles = new Set(metadata.roles.map((row) => row.role));
	for (const role of [
		"China Finance Manager",
		"China Finance User",
		"China Voucher Maker",
		"China Voucher Reviewer",
		"China Voucher Poster",
	])
		assert.ok(roles.has(role), `missing report role: ${role}`);
});

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
			throw(message) {
				throw new Error(message);
			},
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

test("one add-row action appears on the voucher header even if only a later row is eligible", () => {
	const h = harness();
	const render = vm.runInContext("render_inline_voucher_actions", h.context);
	const first = {
		editable_add: false,
		edit_source_doctype: "Journal Entry",
		edit_source_name: "JV-1",
		edit_voucher_key: "Journal Entry:JV-1",
		edit_key: "je:first-row",
		is_voucher_first_row: true,
	};
	const second = {
		...first,
		editable_add: true,
		edit_key: "je:second-row",
		is_voucher_first_row: false,
	};
	const current = report();
	current.data = [first, second, { ...second, edit_key: "je:third-row" }];
	current._china_inline_edit_state = {
		voucher_key: first.edit_voucher_key,
		changes: new Map([["summary", "changed"]]),
	};
	assert.match(render(current, first), /china-inline-add/);
	assert.match(render(current, first), /新增一行/);
	assert.match(render(current, first), /china-inline-save/);
	assert.doesNotMatch(
		render(current, second),
		/china-inline-add|china-inline-save|china-inline-open-source/
	);
	current.data = [first];
	assert.doesNotMatch(render(current, first), /china-inline-add/);
});

function actionHarness() {
	const h = harness();
	class Element {
		constructor() {
			this.dataset = { voucherKey: "Journal Entry:JV-1" };
			this.classList = { contains: (name) => name === "china-inline-add" };
		}
		closest(selector) {
			return selector.includes("china-inline-add") ? this : null;
		}
	}
	h.context.Element = Element;
	const listeners = new Map();
	const wrapper = {
		contains: () => true,
		addEventListener: (name, callback) => listeners.set(name, callback),
		removeEventListener(name, callback) {
			if (listeners.get(name) === callback) listeners.delete(name);
		},
	};
	const current = {
		...report(),
		page: { wrapper },
		data: [
			{
				edit_voucher_key: "Journal Entry:JV-1",
				edit_key: "je:eligible",
				editable_add: true,
			},
		],
	};
	const messages = [];
	h.context.frappe.msgprint = (message) => messages.push(message);
	const bind = vm.runInContext("bind_inline_voucher_actions", h.context);
	const event = { target: new Element(), preventDefault() {}, stopPropagation() {} };
	return { ...h, current, listeners, bind, messages, click: () => listeners.get("click")(event) };
}

test("add-row click loads the module and rebind does not duplicate handlers", async () => {
	const h = actionHarness();
	const opened = [];
	let loads = 0;
	h.context.frappe.require = async () => {
		loads++;
		h.context.china_finance.voucher_entry_add = {
			open: async (...args) => opened.push(args),
		};
	};
	h.bind(h.current);
	h.bind(h.current);
	assert.equal(h.listeners.size, 3);
	await h.click();
	assert.equal(loads, 1);
	assert.equal(opened.length, 1);
	assert.equal(opened[0][0], h.current);
	assert.equal(opened[0][1].edit_key, "je:eligible");
});

test("missing rows, failed asset loads and rejected dialog opens show errors", async () => {
	for (const failure of ["missing", "asset", "dialog"]) {
		const h = actionHarness();
		if (failure === "missing") h.current.data = [];
		h.context.frappe.require = async () => {
			if (failure === "dialog")
				h.context.china_finance.voucher_entry_add = {
					open: async () => {
						throw Error("dialog failed");
					},
				};
		};
		h.bind(h.current);
		await h.click();
		assert.equal(h.messages.length, 1);
		assert.match(
			h.messages[0],
			failure === "missing"
				? /没有可新增行/
				: failure === "asset"
				? /加载失败/
				: /dialog failed/
		);
	}
});

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
	].map((row) => ({
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
		async refresh() {
			this.refreshCount++;
		},
		datatable: {
			cellmanager: { deactivateEditing() {} },
			datamanager: {
				getColumns: () => ["debit", "credit", "base_total_amount"].map((id) => ({ id })),
			},
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
	assert.equal(
		control.$input.value,
		125,
		"formatted currency labels must not be parsed as input"
	);
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
	assert.deepEqual(
		r.data.map((row) => [row.debit, row.credit, row.base_total_amount]),
		[
			[125, 0, 125],
			[0, 125, 125],
		]
	);
	assert.equal(r.data[0]._inline_credit_dirty, undefined);
});

test("changing an amount back clears dirty state and another voucher cannot steal pending edits", () => {
	const h = harness();
	const r = amountReport();
	h.stageAmount(r, r.data[0], "debit", 100);
	assert.equal(
		h.amountEditor(
			r,
			125,
			{},
			{ ...r.data[1], edit_voucher_key: "Journal Entry:JV-2" },
			"credit"
		),
		false
	);
	assert.equal(r._china_inline_edit_state.voucher_key, "Journal Entry:JV-1");
	h.stageAmount(r, r.data[0], "debit", 125);
	assert.equal(r._china_inline_edit_state, null);
	assert.equal(r.data[0]._inline_debit_dirty, false);
});

test("posted amount changes wait for confirmation then submit both fields in one request", async () => {
	const h = harness();
	const r = amountReport();
	const calls = [];
	h.context.frappe.call = async (args) => {
		calls.push(args);
		return {
			message: args.method.includes("preview_")
				? { requires_confirmation: true }
				: { message: "done" },
		};
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
	h.context.frappe.call = async () => {
		calls++;
		throw new Error("unbalanced");
	};
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
	h.context.frappe.call = async (args) => {
		calls.push(args);
		return { message: {} };
	};
	await h.save(r);
	assert.equal(calls[0].args.changes.length, 2);
	assert.equal(calls[1].args.changes.length, 2);
});

test("delete marks a row, keeps staged balancing changes and cancellation restores both", () => {
	const h = harness();
	h.context.frappe.msgprint = (message) => h.notices.push(message);
	const r = amountReport();
	r.data[0].debit = 75;
	r.data.push({
		...r.data[0],
		debit: 50,
		amount_edit_key: "je:extra",
		is_voucher_first_row: false,
	});
	r.data.forEach((row, index) =>
		Object.assign(row, {
			edit_key: row.amount_edit_key,
			editable_delete: true,
			inline_entry_count: 3,
			summary_edit_key: `summary:${index}`,
		})
	);
	const stage = vm.runInContext("stage_inline_entry_delete", h.context);
	assert.equal(stage(r, r.data[2]), true);
	assert.equal(r.data[2]._inline_deleted, true);
	assert.equal(r._china_inline_edit_state.changes.get("delete:je:extra").value, true);
	assert.equal(h.amountEditor(r, 50, {}, r.data[2], "debit"), false);
	h.stageAmount(r, r.data[0], "debit", 125);
	assert.equal(r._china_inline_edit_state.changes.size, 2);
	const render = vm.runInContext("render_inline_voucher_actions", h.context);
	assert.match(render(r, { ...r.data[0], is_voucher_first_row: true }), /china-inline-delete/);
	assert.doesNotMatch(render(r, r.data[2]), /china-inline-delete/);
	h.cancel(r);
	assert.equal(r._china_inline_edit_state, null);
	assert.equal(r.data[0].debit, 75);
	assert.equal(r.data[2]._inline_deleted, undefined);
});

test("delete refuses conflicting edits, other vouchers and fewer than two remaining rows", () => {
	const h = harness();
	const messages = [];
	h.context.frappe.msgprint = (message) => messages.push(message);
	const stage = vm.runInContext("stage_inline_entry_delete", h.context);
	const r = amountReport();
	r.data.forEach((row) =>
		Object.assign(row, {
			edit_key: row.amount_edit_key,
			editable_delete: true,
			inline_entry_count: 3,
		})
	);
	h.stageAmount(r, r.data[0], "debit", 100);
	assert.equal(stage(r, r.data[0]), false);
	assert.match(messages.pop(), /待删除分录/);
	assert.equal(stage(r, { ...r.data[1], edit_voucher_key: "other" }), false);
	assert.match(messages.pop(), /另一张凭证/);
	h.cancel(r);
	assert.equal(stage(r, r.data[0]), true);
	assert.equal(stage(r, r.data[1]), false);
	assert.match(messages.pop(), /至少保留两条/);
});

test("pending-delete cells escape summary and account text while retaining currency formatting", () => {
	const h = harness();
	h.context.frappe.utils.escape_html = (value) =>
		String(value)
			.replaceAll("&", "&amp;")
			.replaceAll("<", "&lt;")
			.replaceAll(">", "&gt;")
			.replaceAll('"', "&quot;");
	const format = h.context.frappe.query_reports["China Voucher Ledger"].formatter;
	const value = "<b>费用</b><img src=x onerror=alert(1)>";
	const row = { _inline_deleted: true, account_label: value };
	for (const fieldname of ["remarks", "account"]) {
		const html = format(value, {}, { fieldname }, row, (value) => value);
		assert.doesNotMatch(html, /<b>|<img/);
		assert.match(html, /&lt;b&gt;/);
		assert.match(html, /china-inline-deleted/);
	}
	const amount = format(125, {}, { fieldname: "debit" }, row, () => "<div>CNY 125.00</div>");
	assert.match(amount, /<div>CNY 125\.00<\/div>/);
});

test("independent additions keep the original amount and can be cancelled or removed before saving", () => {
	const h = harness();
	const r = amountReport();
	r.render_datatable = () => {
		r.renderCount = (r.renderCount || 0) + 1;
	};
	r.data.forEach((row) =>
		Object.assign(row, {
			edit_key: row.amount_edit_key,
			editable_add: true,
			inline_entry_count: 2,
		})
	);
	const stage = vm.runInContext("stage_inline_entry_add", h.context);
	const remove = vm.runInContext("stage_inline_entry_delete", h.context);
	r.data[0].remarks = "Updated reference summary";
	const value = {
		source_row: r.data[0].edit_key,
		account: "Income - 测试公司",
		side: "credit",
		amount: 118811.88,
		summary: "Income",
	};
	assert.equal(stage(r, r.data[0], value), true);
	assert.equal(stage(r, r.data[0], { ...value, side: "debit", summary: "" }), true);
	assert.equal(r.data.length, 4);
	assert.equal(r.data[0].debit, 125);
	assert.equal(r.data[1].credit, 125);
	assert.equal(r.data[2].credit, 118811.88);
	assert.equal(r.data[3].debit, 118811.88);
	assert.equal(r._china_inline_edit_state.changes.size, 2);
	assert.equal(r._china_inline_edit_state.changes.get("add:new:1").value.summary, "Income");
	assert.equal(
		r._china_inline_edit_state.changes.get("add:new:2").value.summary,
		"Updated reference summary"
	);
	assert.equal(remove(r, r.data[2]), true);
	assert.equal(r.data.length, 3);
	h.cancel(r);
	assert.equal(r.data.length, 2);
	assert.equal(r._china_inline_edit_state, null);
});

test("posted sources missing a snapshot display an explicit pending badge", () => {
 const h = harness(), format = h.context.frappe.query_reports["China Voucher Ledger"].formatter;
 const pending = format(1, {}, {fieldname: "voucher_status"}, {voucher_status: 1, snapshot_pending: true}, value => value);
 assert.match(pending, /已记账，待补齐/); assert.match(pending, /orange/);
 const posted = format(1, {}, {fieldname: "voucher_status"}, {voucher_status: 1}, value => value);
 assert.match(posted, /已记账/); assert.doesNotMatch(posted, /待补齐/);
});

test('initial voucher filters await company validation before the first refresh', async () => {
 const h = harness();
 h.context.frappe.defaults = { get_user_default: () => 'Company A' };
 h.context.frappe.datetime = { get_today: () => '2026-10-22', month_start: () => '2026-10-01', month_end: () => '2026-10-31' };
 const values = {};
 const report = {
  _no_refresh: false,
  get_filter_value: key => values[key],
  get_filter: key => ({async set_value(value) {
   assert.equal(report._no_refresh, true);
   await new Promise(resolve => setTimeout(resolve, 1));
   if (key === 'company') assert.ok(values.from_date && values.to_date);
   values[key] = value;
  }}),
 };
 const pending = vm.runInContext('set_initial_voucher_filters', h.context)(report);
 assert.equal(values.company, undefined);
 await pending;
 assert.equal(values.company, 'Company A');
 assert.equal(values.from_date, '2026-10-01');
 assert.equal(values.to_date, '2026-10-31');
 assert.equal(report._no_refresh, false);
});

test('existing voucher date filters are retained on load', async () => {
 const h = harness();
 const values = {company: 'Company A', from_date: '2026-08-01', to_date: '2026-08-31'};
 await vm.runInContext('set_initial_voucher_filters', h.context)({
  get_filter_value: key => values[key],
  get_filter() { assert.fail('complete route filters must not be overwritten'); },
 });
});
