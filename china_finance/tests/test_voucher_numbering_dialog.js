const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

function harness(allowed = true) {
	let dialog, open;
	const requests = [], notices = [];
	const result = {voucher_count: 2, history_count: 1, changed_count: 2,
		changes: [{posting_date: "2026-08-01", source_name: "JE-2", old_number: "记2", new_number: "记1"}],
		next_number: "记3", blockers: [], needs_update: true, can_apply: true, plan_hash: "preview-hash"};
	const report = {get_filter_value: field => ({company: "Test Company", from_date: "2026-08-01"})[field],
		page: {add_menu_item: (_label, action) => {open = action;}}, refresh: async () => {report.refreshed = true;}};
	const context = {china_finance: {}, __: (text, args = []) => text.replace(/\{(\d+)\}/g, (_, n) => args[n]), console: {error() {}}};
	context.frappe = {
		provide() {context.china_finance.voucher_numbering = {};},
		user: {has_role: () => allowed}, utils: {escape_html: text => text.replaceAll("<", "&lt;")},
		datetime: {get_today: () => "2026-10-19"}, msgprint: message => notices.push(message),
		call: async request => {requests.push(request); return {message: {...result}};},
		ui: {Dialog: class {
			constructor(options) {
				dialog = this;
				this.options = options;
				this.values = Object.fromEntries(options.fields.map(field => [field.fieldname, field.default]));
				this.fields_dict = Object.fromEntries(options.fields.map(field => [field.fieldname, {
					$wrapper: {empty() {}, html(value) {dialog.preview = value;}}, $input: {attr() {}},
				}]));
				this.primary = options.primary_action;
				// Frappe may invoke field handlers while constructing controls.
				options.fields[0].onchange();
			}
			show() {}
			hide() {this.hidden = true;}
			get_values() {return {...this.values};}
			set_value(field, value) {this.values[field] = value;}
			set_df_property() {}
			set_primary_action(label, action) {this.primary = action; this.label = label;}
			get_primary_btn() {return {prop() {}};}
		}},
	};
	vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../public/js/voucher_numbering.js"), "utf8"), context);
	context.china_finance.voucher_numbering.bind_report(report);
	return {report, requests, notices, result, context, get dialog() {return dialog;}, get open() {return open;}};
}

test("only finance managers see the action and pending voucher edits block opening", () => {
	assert.equal(harness(false).open, undefined);
	const h = harness();
	h.report._china_inline_edit_state = {changes: new Map([["debit", 100]])};
	h.open();
	assert.equal(h.dialog, undefined);
	assert.equal(h.notices.length, 1);
});

test("preview never applies; confirmation requires print and export acknowledgement", async () => {
	const h = harness();
	h.open();
	h.dialog.values.reason = "取消重复凭证";
	await h.dialog.primary();
	assert.equal(h.requests[0].args.apply, 0);
	assert.equal(h.dialog.label, "确认整理");
	assert.match(h.dialog.preview, /JE-2/);
	await h.dialog.primary();
	assert.equal(h.requests.length, 1);
	h.dialog.values.acknowledge = 1;
	h.result.applied = true;
	h.result.batch_id = "VNO-test";
	await h.dialog.primary();
	assert.equal(h.requests[1].args.apply, 1);
	assert.equal(h.requests[1].args.expected_plan_hash, "preview-hash");
	assert.equal(h.requests[1].type, "POST");
	assert.equal(h.dialog.hidden, true);
	assert.equal(h.report.refreshed, true);
});

test("changing company or month invalidates the plan and clears acknowledgement", async () => {
	const h = harness();
	h.open();
	await h.dialog.primary();
	h.dialog.values.acknowledge = 1;
	h.dialog.values.accounting_period = "2026-09";
	h.dialog.options.fields.find(field => field.fieldname === "accounting_period").onchange();
	assert.equal(h.dialog.label, "预览编号");
	assert.equal(h.dialog.values.acknowledge, 0);
	await h.dialog.primary();
	assert.equal(h.requests[1].args.apply, 0);
	assert.equal(h.requests[1].args.accounting_period, "2026-09");
});

test("blocked preview cannot apply and server failures require a new preview", async () => {
	const h = harness();
	h.open();
	h.result.blockers = ["<archived>"];
	h.result.can_apply = false;
	await h.dialog.primary();
	assert.match(h.dialog.preview, /&lt;archived>/);
	assert.notEqual(h.dialog.label, "确认整理");
	h.result.blockers = [];
	h.result.can_apply = true;
	await h.dialog.primary();
	h.dialog.values.acknowledge = 1;
	h.context.frappe.call = async () => {throw Error("stale preview");};
	await h.dialog.primary();
	assert.equal(h.dialog.label, "预览编号");
	assert.equal(h.dialog.values.acknowledge, 0);
	assert.equal(h.report.refreshed, undefined);
});


test("preview shows whole-month missing snapshots and drafts without enabling blocked apply", async () => {
 const h = harness();
 h.result.missing_snapshot_count = 2;
 h.result.draft_count = 3;
 h.result.blockers = ["Missing snapshots must be repaired"];
 h.result.can_apply = false;
 h.open();
 await h.dialog.primary();
 assert.match(h.dialog.preview, /待补齐正式凭证 2 张/);
 assert.match(h.dialog.preview, /未提交草稿 3 张/);
 assert.match(h.dialog.preview, /Missing snapshots must be repaired/);
 await h.dialog.primary();
 assert.equal(h.requests.at(-1).args.apply, 0);
});
