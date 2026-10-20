// Run with: node --test china_finance/tests/test_cash_flow_assignment_list.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
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

function editor_harness(doc = document(), options = {}) {
	const handlers = [], writes = [], dialogs = [], calls = [], properties = [], nodes = new Map(), window_events = {};
	const node = (kind) => {
		if (!nodes.has(kind)) {
			const value = { scrollLeft: 0, clientWidth: 900, scrollWidth: 1500,
			clientHeight: 400, scrollHeight: 1200, value: "0", max: "0", disabled: false, dataset: {},
			style: { setProperty(key, next) { this[key] = next; if (key === "height") value.clientHeight = parseInt(next, 10); } }, listeners: {},
			addEventListener(event, handler) { this.listeners[event] = handler; },
			getBoundingClientRect: () => ({ top: options.viewport_top ?? 230, height: ({ ".cf-table-scroll": 400, ".cf-scroll-dock": 14, ".cf-footer": 38, ".cf-results": 0 })[kind] || 0 }),
			};
			let scroll_top = 0;
			Object.defineProperty(value, "scrollTop", { get: () => scroll_top, set: (next) => { scroll_top = Math.max(0, Math.min(next, value.scrollHeight - value.clientHeight)); } });
			nodes.set(kind, value);
		}
		return nodes.get(kind);
	};
	const element = (kind) => ({ kind, length: 1, get: () => node(kind),
		hide() { return this; }, show() { return this; }, children() { return this; }, appendTo() { return this; },
		on(event, selector, handler) { if (kind === "root") handlers.push({ event, selector, handler }); return this; },
		html(value) { if (value !== undefined) {
			writes.push({ kind, value });
			if (kind === "root") for (const selector of [".cf-table-scroll", "[data-horizontal-scroll]"]) {
				const old = nodes.get(selector); nodes.delete(selector);
				if (old) Object.assign(node(selector), { clientWidth: old.clientWidth, scrollWidth: old.scrollWidth, scrollHeight: old.scrollHeight });
			}
		} return this; },
		find(selector) { return element(selector); }, filter() { return this; },
		prop(key, value) { if (value !== undefined) { properties.push({ kind, key, value }); node(kind)[key] = value; } return this; },
		attr() { return this; }, text() { return this; }, toggleClass() { return this; }, addClass() { return this; },
	});
	const root = { $: (markup) => element(String(markup).startsWith("<section") ? "root" : "other"),
		addEventListener(event, handler) { window_events[event] = handler; }, document: { addEventListener() {} }, innerHeight: 900,
		confirm: () => !!options.discard,
		requestAnimationFrame: (callback) => callback(),
		china_finance: { company_context: { get_company: () => "公司甲" } },
		frappe: { get_route: () => ["List", "China Cash Flow Assignment", "List"], set_route() {},
			router: { on() {} }, realtime: { on() {} }, utils: { get_form_link: () => "/desk/test" },
			xcall: async (method, args) => { calls.push({ method, args }); return options.call ? options.call(method, args) : { assignments: [copy(doc)], total: 1 }; }, ui: { Dialog: class {
				constructor(options) { this.options = options; this.$wrapper = element("dialog"); dialogs.push(this); }
				set_primary_action(label, action) { this.primary = action; } show() {} hide() {} get_primary_btn() { return element("dialog-button"); }
			} },
		},
	};
	const list = { $frappe_list: element("list"), page: { page_form: element("filters"), wrapper: element("page"),
		btn_primary: element("save"), set_primary_action() {}, set_secondary_action() {},
	} };
	const editor = (options.api || api).mount(list, root);
	return { editor, list, writes, dialogs, calls, properties, nodes, node, window_events,
		async dispatch(event, target, extra = {}) {
			const matches = (selector) => selector === "[data-edit]" ? !!target.dataset.edit
				: selector === "select[data-edit]" ? !!target.dataset.edit && target.tagName === "SELECT"
				: selector === "button[data-action]" ? !!target.dataset.action
				: selector === "[data-select]" ? !!target.dataset.select
				: selector === "[data-horizontal-scroll]" ? !!target.dataset.horizontalScroll
				: selector === "[data-filter]" ? !!target.dataset.filter : false;
			for (const entry of handlers) if (entry.event === event && matches(entry.selector)) await entry.handler({ currentTarget: target, preventDefault() {}, ...extra });
		},
	};
}

for (const [language, labels] of Object.entries({
	en: { "保存更改": "Save Changes", "确认现金流量指定": "Confirm Cash Flow Assignments", "收款": "Receipt", "指定单": "Assignment" },
	es: { "保存更改": "Guardar cambios", "确认现金流量指定": "Confirmar asignaciones de flujo de efectivo", "收款": "Cobro", "指定单": "Asignación" },
})) {
	test(`${language} translates controls, confirmation and details while retaining API codes`, async () => {
		const translated = [];
		const sandbox = { module: { exports: {} }, __: (text, args = []) => {
			translated.push(text);
			return (labels[text] || text).replace(/\{(\d+)\}/g, (match, index) => args[index] ?? match);
		} };
		vm.runInNewContext(fs.readFileSync(script, "utf8"), sandbox);
		const localized = sandbox.module.exports;
		const doc = document(); doc.items[0].cash_flow_row_code = "IN";
		const h = editor_harness(doc, { api: localized });
		await h.list.refresh();
		const html = h.writes.filter((write) => write.kind === "root").at(-1).value;
		assert.ok(html.includes(labels["收款"]));
		assert.ok(html.includes('value="收款"'), "direction values must retain their stored API identity");
		assert.ok(h.writes.some((write) => write.kind === "[data-toolbar]" && write.value.includes(labels["保存更改"])));
		await h.dispatch("click", { dataset: { action: "confirm-one", name: "CFA-1" } });
		assert.equal(h.dialogs.at(-1).options.title, labels["确认现金流量指定"]);
		await h.dispatch("click", { dataset: { action: "details", name: "CFA-1" } });
		assert.ok(h.dialogs.at(-1).options.fields[0].options.includes(labels["指定单"]));
		h.editor.session.results = [{ name: doc.name, ok: false, error: "test error" }];
		h.editor.render();
		for (const key of ["收付方向", "指定金额", "第 {0} 项说明", "确认整单", "逐单处理结果", "没有整单确认权限"]) {
			if (key === "第 {0} 项说明") await h.dispatch("click", { dataset: { action: "toggle", name: "CFA-1" } });
			if (key === "没有整单确认权限") localized.confirmation_issues(document({ can_confirm: false }));
			assert.ok(translated.includes(key), key);
		}
		assert.equal(localized.payload(doc).items[0].cash_flow_row_code, "IN");
		assert.equal(localized.payload(doc).items[0].assigned_amount, 0.30);
	});
}

test("voucher summaries are escaped and accessible in expanded rows and details", async () => {
	const h = editor_harness(document({ voucher_summary: "销售回款 <客户A>" }));
	await h.list.refresh();
	await h.dispatch("click", { dataset: { action: "toggle", name: "CFA-1" } });
	const html = h.writes.filter((write) => write.kind === "root").at(-1).value;
	assert.match(html, /销售回款 &lt;客户A&gt;/);
	assert.match(html, /搜索摘要 \/ 凭证 \/ 科目/);
	await h.dispatch("click", { dataset: { action: "details", name: "CFA-1" } });
	assert.match(h.dialogs[0].options.fields[0].options, /凭证摘要/);
	assert.match(h.dialogs[0].options.fields[0].options, /销售回款 &lt;客户A&gt;/);
});

test("expanded allocation notes retain multiline audit explanations", async () => {
	const doc = document(); doc.items[0].remarks = "第一行依据\n第二行补充";
	const h = editor_harness(doc); await h.list.refresh();
	await h.dispatch("click", { dataset: { action: "toggle", name: "CFA-1" } });
	assert.match(h.writes.filter((write) => write.kind === "root").at(-1).value,
		/<textarea[^>]*data-edit="remarks"[^>]*>第一行依据\n第二行补充<\/textarea>/);
	await h.dispatch("input", { dataset: { edit: "remarks", name: "CFA-1", index: "0" }, value: "第一行依据\n第二行修改" });
	assert.equal(api.payload(h.editor.session.doc("CFA-1")).items[0].remarks, "第一行依据\n第二行修改");
});

test("one shared table groups whole documents and preserves interleaved allocation indices", async () => {
	const doc = document({ china_accounting_voucher: "CAV-1", items: [
		{ ...document().items[0], name: "ITEM-1", assigned_amount: "0.10", cash_account: "1002 - 银行存款 - 完整长科目" },
		{ ...document().items[0], name: "ITEM-2", gl_entry: "GL-2" },
		{ ...document().items[0], name: "ITEM-3", assigned_amount: "0.20" },
	] });
	const h = editor_harness(doc);
	await h.list.refresh();
	h.editor.session.documents.push(document({ name: "CFA-2" })); h.editor.render();
	const html = h.writes.filter((write) => write.kind === "root").at(-1).value;
	assert.equal((html.match(/<table\b/g) || []).length, 1);
	assert.equal((html.match(/<thead\b/g) || []).length, 1);
	assert.equal((html.match(/<tbody\b/g) || []).length, 2);
	assert.equal((html.match(/data-select="CFA-1"/g) || []).length, 1);
	assert.match(html, /class="cf-checkbox-cell" rowspan="3"/);
	assert.match(html, /class="cf-identity" rowspan="3"/);
	assert.ok(html.indexOf('data-allocation-index="2"') < html.indexOf('data-allocation-index="1"'), "cash legs stay grouped without renumbering their original payload indices");
	await h.dispatch("change", { dataset: { edit: "cash_flow_row_code", name: "CFA-1", index: "2" }, value: "IN" });
	assert.equal(h.editor.session.doc("CFA-1").items[2].cash_flow_row_code, "IN");
	assert.equal(h.editor.session.doc("CFA-1").items[1].cash_flow_row_code, "");
	await h.dispatch("click", { dataset: { action: "toggle", name: "CFA-1" } });
	assert.match(h.writes.filter((write) => write.kind === "root").at(-1).value, /class="cf-identity" rowspan="4"/);
});

test("split rerenders preserve both scroll positions and recalculate the horizontal range", async () => {
	const h = editor_harness(); await h.list.refresh();
	const viewport = h.node(".cf-table-scroll"); viewport.scrollLeft = 410; viewport.scrollTop = 156;
	await h.dispatch("click", { dataset: { action: "split", name: "CFA-1", index: "0" } });
	assert.notEqual(h.node(".cf-table-scroll"), viewport, "a full render creates a new real viewport node");
	assert.equal(h.node(".cf-table-scroll").scrollLeft, 410); assert.equal(h.node(".cf-table-scroll").scrollTop, 156);
	assert.equal(Number(h.node("[data-horizontal-scroll]").value), 410);
	assert.equal(Number(h.node("[data-horizontal-scroll]").max), 600);
	h.node(".cf-table-scroll").scrollWidth = 1000; h.editor.render();
	assert.equal(h.node(".cf-table-scroll").scrollLeft, 100, "scroll position must clamp to the resized content");
	assert.equal(Number(h.node("[data-horizontal-scroll]").value), 100);
});

test("restoring a deep vertical position waits for the native viewport height", async () => {
	const h = editor_harness(document(), { viewport_top: 700 }); await h.list.refresh();
	h.node(".cf-table-scroll").scrollTop = 950;
	assert.equal(h.node(".cf-table-scroll").scrollTop, 950);
	await h.dispatch("click", { dataset: { action: "split", name: "CFA-1", index: "0" } });
	assert.equal(h.node(".cf-table-scroll").scrollTop, 950, "the temporary CSS height must not clamp away a restorable position");
});

test("the thin horizontal control supports input, arrows, Home and End without rerendering", async () => {
	const h = editor_harness(); await h.list.refresh();
	const control = h.node("[data-horizontal-scroll]"); control.dataset.horizontalScroll = "1"; control.value = "210";
	const before = h.writes.filter((write) => write.kind === "root").length;
	await h.dispatch("input", control); assert.equal(h.node(".cf-table-scroll").scrollLeft, 210);
	await h.dispatch("keydown", control, { key: "ArrowRight" }); assert.equal(h.node(".cf-table-scroll").scrollLeft, 290);
	await h.dispatch("keydown", control, { key: "End" }); assert.equal(h.node(".cf-table-scroll").scrollLeft, 600);
	await h.dispatch("keydown", control, { key: "Home" }); assert.equal(h.node(".cf-table-scroll").scrollLeft, 0);
	assert.equal(h.writes.filter((write) => write.kind === "root").length, before);
	h.node(".cf-table-scroll").scrollLeft = 127;
	h.node(".cf-table-scroll").listeners.scroll();
	assert.equal(Number(control.value), 127, "native trackpad scrolling must move the independent thin control");
});

test("viewport height follows the actual native page position and resizes without losing edits", async () => {
	const h = editor_harness(); await h.list.refresh();
	const viewport = h.node(".cf-table-scroll");
	assert.equal(viewport.style.height, "606px");
	viewport.scrollLeft = 160;
	await h.dispatch("input", { dataset: { edit: "assigned_amount", name: "CFA-1", index: "0" }, value: "0.20" });
	viewport.getBoundingClientRect = () => ({ top: 310, height: 400 });
	h.window_events.resize();
	assert.equal(viewport.style.height, "526px");
	assert.equal(viewport.scrollLeft, 160);
	assert.equal(h.editor.session.doc("CFA-1").items[0].assigned_amount, "0.20");
});

test("in-list save persists full-document edits while selection alone never submits", async () => {
	const h = editor_harness(document(), { call: async (method, args) => {
		if (!method.endsWith("process_cash_flow_assignments")) return { assignments: [document()], total: 1 };
		const sent = JSON.parse(args.assignments)[0];
		return { results: [{ name: sent.name, ok: true, document: document({ ...sent, modified: "saved", assigned_by: "用户甲" }) }] };
	} });
	await h.list.refresh();
	await h.dispatch("change", { dataset: { select: "CFA-1" }, checked: true });
	await h.dispatch("change", { dataset: { edit: "cash_flow_row_code", name: "CFA-1", index: "0" }, value: "IN" });
	assert.equal(h.calls.length, 1, "selecting a project may only change the local draft");
	assert.ok(h.properties.some((entry) => entry.kind === "[data-action='save']" && entry.key === "disabled" && entry.value === false));
	await h.dispatch("click", { dataset: { action: "save" } });
	assert.equal(h.calls[1].args.action, "save");
	assert.equal(JSON.parse(h.calls[1].args.assignments)[0].items[0].cash_flow_row_code, "IN");
	assert.equal(h.editor.session.dirty.size, 0);
	assert.equal(h.editor.session.doc("CFA-1").status, "Draft");
	assert.match(h.writes.filter((write) => write.kind === "root").at(-1).value, /已保存[ ·]*待确认/);
});

test("per-document confirm uses the same full-document API only after explicit confirmation", async () => {
	const doc = document(); doc.items[0].cash_flow_row_code = "IN";
	const h = editor_harness(doc, { call: async (method, args) => method.endsWith("process_cash_flow_assignments")
		? { results: [{ name: "CFA-1", ok: true, document: document({ status: "Confirmed", docstatus: 1 }) }] }
		: { assignments: [doc], total: 1 } });
	await h.list.refresh();
	await h.dispatch("click", { dataset: { action: "confirm-one", name: "CFA-1" } });
	assert.equal(h.calls.length, 1); assert.equal(h.dialogs.length, 1);
	await h.dialogs[0].primary();
	assert.equal(h.calls[1].args.action, "confirm");
	assert.equal(JSON.parse(h.calls[1].args.assignments)[0].items.length, 1);
});

test("cancelled date and view filters retain local edits, selection, controls and scroll", async () => {
	const h = editor_harness(); await h.list.refresh();
	await h.dispatch("change", { dataset: { select: "CFA-1" }, checked: true });
	await h.dispatch("input", { dataset: { edit: "assigned_amount", name: "CFA-1", index: "0" }, value: "0.12" });
	h.node(".cf-table-scroll").scrollLeft = 350;
	await h.dispatch("change", { dataset: { filter: "from_date" }, value: "2026-09-01" });
	await h.dispatch("click", { dataset: { action: "tab", status: "Confirmed" } });
	assert.equal(h.calls.length, 1); assert.equal(h.editor.session.filters.from_date, "");
	assert.equal(h.editor.session.filters.status, "Draft");
	assert.equal(h.editor.session.doc("CFA-1").items[0].assigned_amount, "0.12");
	assert.equal(h.editor.session.selected.has("CFA-1"), true);
	assert.equal(h.node(".cf-table-scroll").scrollLeft, 350);
	assert.match(h.writes.filter((write) => write.kind === "root").at(-1).value, /data-select="CFA-1"[^>]* checked/);
});

test("a failed inline save restores editable controls and retains the entered project and note", async () => {
	let finish;
	const h = editor_harness(document(), { call: (method) => method.endsWith("process_cash_flow_assignments")
		? new Promise((resolve) => { finish = resolve; }) : Promise.resolve({ assignments: [document()], total: 1 }) });
	await h.list.refresh();
	await h.dispatch("click", { dataset: { action: "toggle", name: "CFA-1" } });
	await h.dispatch("change", { dataset: { edit: "cash_flow_row_code", name: "CFA-1", index: "0" }, value: "IN" });
	await h.dispatch("input", { dataset: { edit: "remarks", name: "CFA-1", index: "0" }, value: "人工依据 <A>" });
	const pending = h.dispatch("click", { dataset: { action: "save" } });
	assert.equal(h.editor.session.busy, true);
	assert.match(h.writes.filter((write) => write.kind === "root").at(-1).value, /data-edit="cash_flow_row_code"[^>]* disabled/);
	finish({ results: [{ name: "CFA-1", ok: false, error: "版本冲突，请核对" }] }); await pending;
	assert.equal(h.editor.session.busy, false); assert.equal(h.editor.session.dirty.has("CFA-1"), true);
	const html = h.writes.filter((write) => write.kind === "root").at(-1).value;
	assert.doesNotMatch(html, /data-edit="cash_flow_row_code"[^>]* disabled/);
	assert.match(html, /value="IN" selected/); assert.match(html, /人工依据 &lt;A&gt;/);
	assert.match(html, /版本冲突/);
});

for (const status of ["Confirmed", "Cancelled"]) {
	test(`${status} history stays read-only after another document is saved`, async () => {
		const historical = document({ name: "HISTORY", status, docstatus: status === "Confirmed" ? 1 : 2 });
		const h = editor_harness(document(), { call: async (method) => method.endsWith("process_cash_flow_assignments")
			? { results: [{ name: "CFA-1", ok: true, document: document({ assigned_by: "用户甲" }) }] }
			: { assignments: [document(), historical], total: 2 } });
		await h.list.refresh();
		await h.dispatch("input", { dataset: { edit: "assigned_amount", name: "CFA-1", index: "0" }, value: "0.20" });
		await h.dispatch("click", { dataset: { action: "save" } });
		const html = h.writes.filter((write) => write.kind === "root").at(-1).value;
		const body = html.match(/<tbody[^>]*data-document="HISTORY">([\s\S]*?)<\/tbody>/)[1];
		assert.doesNotMatch(body, /<select/); assert.match(body, /data-edit="assigned_amount"[^>]* disabled/);
		assert.match(body, /data-select="HISTORY"[^>]* disabled/);
		await h.dispatch("input", { dataset: { edit: "assigned_amount", name: "HISTORY", index: "0" }, value: "999" });
		assert.equal(h.editor.session.doc("HISTORY").items[0].assigned_amount, 0.30);
	});
}

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
