// Run with: node --test china_finance/tests/test_cash_flow_assignment_list.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const script = path.resolve(__dirname, "../public/js/china_cash_flow_assignment_list.js");
const api = fs.existsSync(script) ? require(script) : {};
const copy = (value) => JSON.parse(JSON.stringify(value));

function document(overrides = {}) {
	return {
		name: "CFA-1", modified: "2026-10-19 12:00:00", company: "公司甲", currency: "CNY",
		status: "Draft", docstatus: 0, can_write: true, can_confirm: true, blockers: [], remarks: "",
		options: [{ value: "IN", label: "销售收款", direction: "收款" },
			{ value: "OUT", label: "采购付款", direction: "付款" },
			{ value: "INTERNAL_TRANSFER", label: "内部划转", direction: "all" }],
		items: [{ name: "ITEM-1", gl_entry: "GL-1", cash_direction: "收款", cash_amount: 0.30,
			assigned_amount: 0.30, suggested_row_code: "IN", cash_flow_row_code: "", remarks: "" }],
		...overrides,
	};
}

test("the native cash flow list exposes its tested editor model", () => {
	assert.equal(typeof api.Session, "function", "the list editor session is missing");
});

test("money and split validation use integer cents", () => {
	assert.equal(api.to_minor("1000000.01"), 100000001);
	assert.equal(api.to_minor("0.10"), 10);
	assert.equal(api.to_minor("1.001"), null);
	assert.equal(api.to_minor("not money"), null);
	const doc = document();
	doc.items[0].cash_flow_row_code = "IN";
	doc.items[0].assigned_amount = "0.10";
	doc.items.push({ ...doc.items[0], name: "ITEM-2", assigned_amount: "0.20" });
	assert.deepEqual(api.confirmation_issues(doc), []);
	assert.equal(api.cash_groups(doc)[0].cash_minor, 30, "split cash amount must only be counted once");
	doc.items[1].assigned_amount = "0.19";
	assert.match(api.confirmation_issues(doc).join(" "), /0.01/);
});

test("suggestions fill only blank unsplit compatible rows and preserve manual allocations", () => {
	const doc = document();
	doc.items.push({ ...doc.items[0], name: "MANUAL", gl_entry: "GL-2", cash_flow_row_code: "INTERNAL_TRANSFER" });
	doc.items.push({ ...doc.items[0], name: "SPLIT-1", gl_entry: "GL-3" });
	doc.items.push({ ...doc.items[0], name: "SPLIT-2", gl_entry: "GL-3" });
	doc.items.push({ ...doc.items[0], name: "WRONG", gl_entry: "GL-4", suggested_row_code: "OUT" });
	assert.equal(api.fill_suggestions(doc), 1);
	assert.equal(doc.items[0].cash_flow_row_code, "IN");
	assert.equal(doc.items[1].cash_flow_row_code, "INTERNAL_TRANSFER");
	assert.equal(doc.items[2].cash_flow_row_code, "");
	assert.equal(doc.items[4].cash_flow_row_code, "");
	assert.equal(api.fill_suggestions(document({ can_write: false })), 0);
	assert.equal(api.fill_suggestions(document({ status: "Confirmed", docstatus: 1 })), 0);
});

test("unified assignment defaults to preserving existing values and direction", () => {
	const doc = document();
	doc.items.push({ ...doc.items[0], name: "ITEM-2", gl_entry: "GL-2", cash_flow_row_code: "INTERNAL_TRANSFER" });
	doc.items.push({ ...doc.items[0], name: "ITEM-3", gl_entry: "GL-3", cash_direction: "付款" });
	assert.equal(api.assign_project(doc, "IN"), 1);
	assert.equal(doc.items[1].cash_flow_row_code, "INTERNAL_TRANSFER");
	assert.equal(doc.items[2].cash_flow_row_code, "");
	assert.equal(api.assign_project(doc, "IN", false), 1);
	assert.equal(doc.items[1].cash_flow_row_code, "IN");
});

test("splits retain source identity and removing one retains the remaining allocation", () => {
	const doc = document();
	const row = api.split_item(doc, 0);
	assert.equal(row.gl_entry, "GL-1");
	assert.equal(row.name, "");
	assert.equal(row.cash_flow_row_code, "");
	assert.equal(row.assigned_amount, "0.00");
	assert.equal(api.remove_item(doc, 0), true);
	assert.equal(doc.items.length, 1);
	assert.equal(api.remove_item(doc, 0), false, "the final allocation cannot be removed");
});

test("confirmation preflight combines document permission, authoritative blockers, projects and amounts", () => {
	const doc = document({ can_confirm: false, blockers: ["职责分离：指定人与确认人不能相同"] });
	doc.items[0].cash_flow_row_code = "OUT";
	const issues = api.confirmation_issues(doc).join(" ");
	assert.match(issues, /职责分离/);
	assert.match(issues, /确认权限/);
	assert.match(issues, /方向/);
	assert.match(api.confirmation_issues(document({ status: "Cancelled", docstatus: 2 })).join(" "), /只读/);
});

test("edited rows replace old item blockers while authoritative document blockers remain", () => {
	const doc = document({ blockers: ["旧项目为空", "来源凭证已取消"], blocker_details: [
		{ message: "旧项目为空", scope: "items" }, { message: "来源凭证已取消", scope: "document" },
	] });
	doc.items[0].cash_flow_row_code = "IN";
	assert.deepEqual(api.confirmation_issues(doc), ["来源凭证已取消"]);
});

test("payload includes the entire document with server version and no invented source fields", () => {
	const doc = document({ remarks: "人工说明" });
	api.split_item(doc, 0);
	const payload = api.payload(doc);
	assert.equal(payload.modified, doc.modified);
	assert.equal(payload.remarks, "人工说明");
	assert.equal(payload.items.length, 2);
	assert.deepEqual(Object.keys(payload.items[0]).sort(), ["name", "gl_entry", "cash_flow_row_code", "assigned_amount", "remarks"].sort());
});

test("list responses cannot overwrite newer filters or edits made while loading", async () => {
	const requests = [];
	const session = new api.Session({ call: (_method, args) => new Promise((resolve) => requests.push({ args, resolve })) });
	session.filters.company = "公司甲";
	const first = session.load();
	session.filters.company = "公司乙";
	const second = session.load();
	requests[1].resolve({ assignments: [document({ name: "B", company: "公司乙" })], total: 1 });
	await second;
	requests[0].resolve({ assignments: [document()], total: 1 });
	await first;
	assert.equal(session.documents[0].name, "B");
	const third = session.load();
	session.edit("B", (doc) => { doc.remarks = "加载时输入"; });
	requests[2].resolve({ assignments: [document({ name: "B", remarks: "旧值" })], total: 1 });
	await third;
	assert.equal(session.documents[0].remarks, "加载时输入");
	assert.equal(session.dirty.has("B"), true);
});

test("a late loading failure cannot rerender an editor that changed after the request", async () => {
	let reject;
	const events = [];
	const session = new api.Session({ call: () => new Promise((_resolve, fail) => { reject = fail; }), onchange: (event) => events.push(event) });
	session.filters.company = "公司甲"; session.documents = [document()];
	const pending = session.load();
	session.edit("CFA-1", (doc) => { doc.remarks = "继续输入"; });
	reject(new Error("过期网络错误")); await pending;
	assert.equal(events.includes("error"), false, "a stale error must not request a full editor render");
	assert.equal(session.doc("CFA-1").remarks, "继续输入");
});

for (const rejection of [undefined, null]) {
	test(`loading handles a ${String(rejection)} rejection without a second exception`, async () => {
		const session = new api.Session({ call: () => Promise.reject(rejection) });
		session.filters.company = "公司甲";
		assert.equal(await session.load(), false);
		assert.match(session.error, /加载失败/);
		assert.equal(session.loading, false);
		assert.equal(session.documents.length, 0);
	});

	test(`processing handles a ${String(rejection)} rejection and preserves every failed edit`, async () => {
		const session = new api.Session({ call: () => Promise.reject(rejection) });
		session.documents = [document(), document({ name: "CFA-2" })];
		session.edit("CFA-1", (doc) => { doc.remarks = "保留修改甲"; });
		session.edit("CFA-2", (doc) => { doc.remarks = "保留修改乙"; });
		const results = await session.process(["CFA-1", "CFA-2"], "save");
		assert.deepEqual(results.map((result) => result.name), ["CFA-1", "CFA-2"]);
		assert.ok(results.every((result) => result.ok === false && /请求失败.*编辑内容已保留/.test(result.error)));
		assert.deepEqual([...session.dirty], ["CFA-1", "CFA-2"]);
		assert.equal(session.doc("CFA-1").remarks, "保留修改甲");
		assert.equal(session.doc("CFA-2").remarks, "保留修改乙");
		assert.equal(session.busy, false);
	});
}

test("dirty data blocks automatic refresh and cancelled filter changes preserve edits", async () => {
	let calls = 0;
	const session = new api.Session({ call: async () => { calls++; return { assignments: [document()], total: 1 }; } });
	session.filters.company = "公司甲";
	await session.load();
	session.edit("CFA-1", (doc) => { doc.remarks = "未保存"; });
	assert.equal(await session.load(), false);
	assert.equal(calls, 1);
	assert.equal(await session.change_filters({ status: "Confirmed" }, async () => false), false);
	assert.equal(session.filters.status, "Draft");
	assert.equal(session.documents[0].remarks, "未保存");
	await session.change_filters({ status: "Confirmed" }, async () => true);
	assert.equal(session.filters.status, "Confirmed");
	assert.equal(session.dirty.size, 0);
	assert.equal(calls, 2);
});

test("a repeated filter event neither discards edits nor starts another request", async () => {
	let calls = 0, prompts = 0;
	const session = new api.Session({ call: async () => { calls++; return { assignments: [document()], total: 1 }; } });
	session.filters.company = "公司甲";
	await session.load();
	session.edit("CFA-1", (doc) => { doc.remarks = "不能丢失"; });
	await session.change_filters({ status: "Draft" }, async () => { prompts++; return true; });
	assert.equal(prompts, 0);
	assert.equal(calls, 1);
	assert.equal(session.dirty.has("CFA-1"), true);
});

test("accepted company changes clear the old company's rows even if the new request fails", async () => {
	const requests = [];
	const session = new api.Session({ call: () => new Promise((resolve, reject) => requests.push({ resolve, reject })) });
	session.filters.company = "公司甲";
	session.documents = [document()]; session.total = 1; session.error = "旧错误";
	const stale = session.load();
	const switched = session.change_filters({ company: "公司乙" }, async () => true);
	assert.equal(session.documents.length, 0, "rows from the previous company cannot remain under the new heading");
	assert.equal(session.total, 0);
	assert.equal(session.error, "");
	requests[1].reject(new Error("公司乙读取失败"));
	await switched;
	requests[0].resolve({ assignments: [document()], total: 1 });
	await stale;
	assert.equal(session.filters.company, "公司乙");
	assert.equal(session.documents.length, 0);
	assert.match(session.error, /公司乙读取失败/);
});

test("batch processing is busy guarded and adopts only successful authoritative documents", async () => {
	let resolve, calls = 0, sent;
	const session = new api.Session({ call: (_method, args) => { calls++; sent = args; return new Promise((done) => { resolve = done; }); } });
	session.documents = [document(), document({ name: "CFA-2" })];
	session.edit("CFA-1", (doc) => { doc.remarks = "ok"; });
	session.edit("CFA-2", (doc) => { doc.remarks = "保留失败编辑"; });
	const pending = session.process(["CFA-1", "CFA-2"], "confirm");
	assert.equal(session.busy, true);
	assert.equal(session.edit("CFA-2", (doc) => { doc.remarks = "不应修改"; }), false);
	assert.deepEqual(await session.process(["CFA-1"], "save"), []);
	assert.equal(calls, 1);
	assert.equal(sent.action, "confirm");
	assert.equal(JSON.parse(sent.assignments)[1].remarks, "保留失败编辑");
	resolve({ results: [
		{ name: "CFA-1", ok: true, document: document({ status: "Confirmed", docstatus: 1, modified: "new" }) },
		{ name: "CFA-2", ok: false, error: "职责分离校验未通过" },
	] });
	await pending;
	assert.equal(session.documents.length, 1);
	assert.equal(session.documents[0].name, "CFA-2");
	assert.equal(session.results[0].document.status, "Confirmed");
	assert.equal(session.results[0].document.modified, "new");
	assert.equal(session.dirty.has("CFA-1"), false);
	assert.equal(session.dirty.has("CFA-2"), true);
	assert.equal(session.documents[0].remarks, "保留失败编辑");
	assert.match(session.results[1].error, /职责分离/);
	assert.equal(session.busy, false);
});

test("confirmation removes a successful draft from the draft page but retains its receipt and failed edits", async () => {
	const session = new api.Session({ call: async () => ({ results: [
		{ name: "CFA-1", ok: true, document: document({ status: "Confirmed", docstatus: 1 }) },
		{ name: "CFA-2", ok: false, error: "版本已改变" },
	] }) });
	session.documents = [document(), document({ name: "CFA-2" })]; session.total = 2;
	session.counts = { Draft: 2, Confirmed: 0, All: 2 };
	session.edit("CFA-2", (doc) => { doc.remarks = "保留"; });
	await session.process(["CFA-1", "CFA-2"], "confirm");
	assert.deepEqual(session.documents.map((doc) => doc.name), ["CFA-2"]);
	assert.equal(session.total, 1);
	assert.equal(session.counts.Draft, 1);
	assert.equal(session.counts.Confirmed, 1);
	assert.equal(session.counts.All, 2);
	assert.equal(session.results[0].document.status, "Confirmed");
	assert.equal(session.dirty.has("CFA-2"), true);
});

test("a missing batch result remains failed and dirty", async () => {
	const session = new api.Session({ call: async () => ({ results: [] }) });
	session.documents = [document()];
	session.dirty.add("CFA-1");
	await session.process(["CFA-1"], "save");
	assert.equal(session.dirty.has("CFA-1"), true);
	assert.equal(session.results[0].ok, false);
});

test("native mounting replaces only this list instance and never performs a second native fetch", async () => {
	const calls = [], events = {};
	const element = { length: 1, hide() { return this; }, show() { return this; }, children() { return this; },
		appendTo() { return this; }, on() { return this; }, html() { return this; }, find() { return this; },
		prop() { return this; }, attr() { return this; }, text() { return this; }, toggleClass() { return this; },
		addClass() { return this; }, filter() { return this; }, get() { return {}; }, };
	let route = ["List", "China Cash Flow Assignment", "List"];
	const root = {
		$: () => Object.create(element), document: { addEventListener() {} },
		addEventListener() {}, confirm: () => false,
		china_finance: { company_context: { get_company: () => "公司甲" } },
		frappe: {
			xcall: async (method, args) => { calls.push({ method, args }); return { assignments: [document()], total: 1 }; },
			get_route: () => route, router: { on: (name, fn) => { events[name] = fn; } },
			set_route() {}, route_options: null, realtime: { on() {} },
			utils: { get_form_link: (dt, name) => `/desk/${dt}/${name}` },
		},
	};
	let native_fetches = 0;
	const list = { $frappe_list: root.$(), $result: root.$(), page: { page_form: root.$(),
		set_primary_action() {}, set_secondary_action() {}, btn_primary: root.$(), wrapper: root.$() },
		refresh() { native_fetches++; }, render_list() { native_fetches++; }, before_refresh() {}, process_document_refreshes() {},
	};
	const editor = api.mount(list, root);
	assert.ok(editor?.session, "mount should install one native-list workbench");
	assert.equal(api.mount(list, root), editor, "cached navigation must not mount twice");
	await list.refresh();
	assert.equal(calls.length, 1);
	assert.equal(calls[0].args.company, "公司甲");
	list.render_list();
	assert.equal(native_fetches, 0);
	editor.session.edit("CFA-1", (doc) => { doc.remarks = "保留"; });
	await list.process_document_refreshes();
	assert.equal(calls.length, 1);
	assert.equal(editor.session.doc("CFA-1").remarks, "保留");
	route = ["List", "Customer", "List"];
	await list.refresh();
	assert.equal(calls.length, 1, "inactive cached lists cannot fetch");
});

function editor_harness(doc = document()) {
	const handlers = [], writes = [], dialogs = [];
	const element = (kind) => ({ kind, length: 1,
		hide() { return this; }, show() { return this; }, children() { return this; }, appendTo() { return this; },
		on(event, selector, handler) { if (kind === "root") handlers.push({ event, selector, handler }); return this; },
		html(value) { if (value !== undefined) writes.push({ kind, value }); return this; },
		find(selector) { return element(selector); }, filter() { return this; }, prop() { return this; },
		attr() { return this; }, text() { return this; }, toggleClass() { return this; }, addClass() { return this; },
	});
	const root = { $: (markup) => element(String(markup).startsWith("<section") ? "root" : "other"),
		addEventListener() {}, document: { addEventListener() {} },
		china_finance: { company_context: { get_company: () => "公司甲" } },
		frappe: { get_route: () => ["List", "China Cash Flow Assignment", "List"], set_route() {},
			router: { on() {} }, realtime: { on() {} }, utils: { get_form_link: () => "/desk/test" },
			xcall: async () => ({ assignments: [copy(doc)], total: 1 }), ui: { Dialog: class {
				constructor(options) { this.options = options; this.$wrapper = element("dialog"); dialogs.push(this); }
				set_primary_action() {} show() {} get_primary_btn() { return element("dialog-button"); }
			} },
		},
	};
	const list = { $frappe_list: element("list"), page: { page_form: element("filters"), wrapper: element("page"),
		btn_primary: element("save"), set_primary_action() {}, set_secondary_action() {},
	} };
	const editor = api.mount(list, root);
	return { editor, list, writes, dialogs,
		async dispatch(event, target) {
			const matches = (selector) => selector === "[data-edit]" ? !!target.dataset.edit
				: selector === "select[data-edit]" ? !!target.dataset.edit && target.tagName === "SELECT"
				: selector === "button[data-action]" ? !!target.dataset.action
				: selector === "[data-select]" ? !!target.dataset.select : false;
			for (const entry of handlers) if (entry.event === event && matches(entry.selector)) await entry.handler({ currentTarget: target });
		},
	};
}

test("voucher summaries are escaped and visible in both the list and details", async () => {
	const h = editor_harness(document({ voucher_summary: "销售回款 <客户A>" }));
	await h.list.refresh();
	const html = h.writes.filter((write) => write.kind === "root").at(-1).value;
	assert.match(html, /销售回款 &lt;客户A&gt;/);
	assert.match(html, /搜索摘要 \/ 凭证 \/ 科目/);
	await h.dispatch("click", { dataset: { action: "details", name: "CFA-1" } });
	assert.match(h.dialogs[0].options.fields[0].options, /凭证摘要/);
	assert.match(h.dialogs[0].options.fields[0].options, /销售回款 &lt;客户A&gt;/);
});

test("inline input and blur never replace edit controls or toolbar before the next click", async () => {
	const h = editor_harness();
	await h.list.refresh();
	const before = h.writes.filter((write) => ["root", "[data-toolbar]"].includes(write.kind)).length;
	const amount = { dataset: { edit: "assigned_amount", name: "CFA-1", index: "0" }, value: "0.10", tagName: "INPUT" };
	await h.dispatch("input", amount);
	await h.dispatch("change", amount);
	assert.equal(h.editor.session.doc("CFA-1").items[0].assigned_amount, "0.10");
	assert.equal(h.writes.filter((write) => ["root", "[data-toolbar]"].includes(write.kind)).length, before,
		"blur/input must retain both the next focus target and an upcoming action button");
	await h.dispatch("click", { dataset: { action: "split", name: "CFA-1", index: "0" } });
	assert.equal(h.editor.session.doc("CFA-1").items.length, 2, "the first following split click must apply once");
});
