// Run with: node --test china_finance/tests/test_closing_month.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

function harness(doc) {
	let handlers;
	const context = vm.createContext({
		frappe: { ui: { form: { on: (_doctype, events) => { handlers = events; } } } },
		__: (text) => text,
	});
	const source = path.resolve(__dirname, "../china_finance/doctype/china_closing_run/china_closing_run.js");
	vm.runInContext(fs.readFileSync(source, "utf8"), context);
	const changes = [];
	const indicators = [];
	const frm = {
		doc: { docstatus: 0, ...doc },
		fields_dict: {},
		page: {set_indicator: (...args) => indicators.push(args)},
		is_new: () => false,
		get_field: () => null,
		refresh_field() {},
		set_value(field, value) {
			changes.push([field, value]);
			this.doc[field] = value;
		},
	};
	return { handlers, frm, changes, context, indicators };
}

test("refresh preserves year-end dates with an empty or leftover closing month", async () => {
	for (const closing_month of [null, "2026-08"]) {
		const { handlers, frm, changes } = harness({
			closing_type: "Year End", closing_month,
			from_date: "2026-01-01", to_date: "2026-12-31",
		});
		await handlers.refresh(frm);
		assert.equal(frm.doc.from_date, "2026-01-01");
		assert.equal(frm.doc.to_date, "2026-12-31");
		assert.deepEqual(changes, []);
	}
});

test("selecting a month derives the whole calendar month including leap years", () => {
	for (const [closing_month, to_date] of [["2026-02", "2026-02-28"], ["2024-02", "2024-02-29"]]) {
		const { handlers, frm } = harness({ closing_type: "Monthly", closing_month });
		handlers.closing_month(frm);
		assert.equal(frm.doc.from_date, `${closing_month}-01`);
		assert.equal(frm.doc.to_date, to_date);
	}
});

test("clearing a monthly selection clears its derived dates", () => {
	const { handlers, frm } = harness({
		closing_type: "Monthly", closing_month: "",
		from_date: "2026-08-01", to_date: "2026-08-31",
	});
	handlers.closing_month(frm);
	assert.equal(frm.doc.from_date, null);
	assert.equal(frm.doc.to_date, null);
});

test("refresh infers the month on legacy monthly records without changing their dates", async () => {
	const { handlers, frm, changes } = harness({
		closing_type: "Monthly", closing_month: null,
		from_date: "2026-08-01", to_date: "2026-08-31",
	});
	await handlers.refresh(frm);
	assert.equal(frm.doc.closing_month, "2026-08");
	assert.deepEqual(changes, []);
});

function listIndicator(doc) {
	const context = vm.createContext({frappe: {listview_settings: {}}, __: text => text});
	vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../china_finance/doctype/china_closing_run/china_closing_run_list.js'), 'utf8'), context);
	const settings = context.frappe.listview_settings['China Closing Run'];
	assert.equal(settings.has_indicator_for_draft, true);
	return Array.from(settings.get_indicator(doc));
}

test('submitted closing and reopened history have distinct business indicators', () => {
	assert.deepEqual(listIndicator({docstatus: 1, status: 'Closed'}), ['已结账', 'green', 'status,=,Closed']);
	assert.deepEqual(listIndicator({docstatus: 1, status: 'Reopened'}), ['已反结账（历史）', 'grey', 'status,=,Reopened']);
});

test('unfinished and failed reversal cannot appear as completed', () => {
	assert.equal(listIndicator({docstatus: 1, status: 'Reopening'})[0], '反结账处理中');
	assert.equal(listIndicator({docstatus: 1, status: 'Reopen Failed'})[1], 'red');
});

test('drafts expose preparation progress rather than generic submitted status', () => {
	for (const [phase, label] of Object.entries({Posting: '凭证记账中', Checking: '结账检查中', Closing: '损益结转中', Ready: '待确认结账', Paused: '已暂停', Failed: '处理失败'})) {
		assert.equal(listIndicator({docstatus: 0, status: 'Draft', preparation_state: phase})[0], label);
	}
	assert.equal(listIndicator({docstatus: 0, status: 'Draft'})[0], '待结账');
});


test('reopening form offers progress and resume actions without a reclose action', async () => {
	for (const status of ['Reopening', 'Reopen Failed']) {
		const {handlers, frm} = harness({docstatus: 1, status});
		const buttons = [];
		frm.set_intro = () => {};
		frm.page.set_primary_action = label => buttons.push(label);
		frm.add_custom_button = label => buttons.push(label);
		await handlers.refresh(frm);
		assert.deepEqual(buttons, ['检查反结账进度', '继续撤销总账']);
	}
});

test('reclosing opens the server-selected active record rather than creating another client draft', async () => {
	const {handlers, frm, context} = harness({docstatus: 1, status: 'Reopened', name: 'history'});
	const buttons = {};
	let route;
	frm.set_intro = () => {};
	frm.page.set_primary_action = () => {};
	frm.add_custom_button = (label, callback) => { buttons[label] = callback; };
	context.frappe.call = async options => {
		assert.equal(options.method, 'china_finance.services.closing_lifecycle.get_or_create_reclosing');
		assert.equal(options.type, 'POST');
		assert.equal(options.args.name, 'history');
		return {message: {name: 'existing-run', created: false}};
	};
	context.frappe.set_route = (...args) => {route = args;};
	await handlers.refresh(frm);
	await buttons['重新结账']();
	assert.deepEqual(route, ['Form', 'China Closing Run', 'existing-run']);
});

test('ready closing keeps a pause action for legacy duplicate recovery', async () => {
	const calls = [];
	const context = vm.createContext({
		china_finance: {month_end: {}},
		__: text => text,
		frappe: {
			provide() {}, utils: {escape_html: value => value}, model: {can_write: () => false},
			call: async options => { calls.push(options); return {message: {}}; },
		},
	});
	vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../public/js/month_end.js'), 'utf8'), context);
	const buttons = {};
	let reloaded = 0;
	const frm = {
		doc: {name: 'duplicate-ready', docstatus: 0, preparation_state: 'Ready', preparation_data: '{}'},
		toggle_display() {}, set_df_property() {}, set_intro() {},
		fields_dict: {preparation_preview: {$wrapper: {html() {}}}},
		page: {set_primary_action() {}},
		add_custom_button: (label, callback) => { buttons[label] = callback; },
		reload_doc: async () => { reloaded++; },
	};
	context.china_finance.month_end.render(frm);
	assert.equal(typeof buttons['暂停'], 'function');
	await buttons['暂停']();
	assert.equal(calls[0].method, 'china_finance.services.month_end.pause');
	assert.equal(calls[0].args.name, 'duplicate-ready');
	assert.equal(calls[0].type, 'POST');
	assert.equal(frm.__month_stop, true);
	assert.equal(reloaded, 1);
});
