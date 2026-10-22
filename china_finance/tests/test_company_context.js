// Run with: node --test china_finance/tests/test_company_context.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const root = path.resolve(__dirname, "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");
const sidebar = JSON.parse(read("workspace_sidebar/china_finance.json"));
const tick = () => new Promise((resolve) => setImmediate(resolve));

function harness(shared = new Map(), tab = new Map()) {
	let route = ["desktop"];
	const events = {}, store = tab, dialogs = [], navigations = [], messages = [];
	const context = vm.createContext({
		URL, Promise, setTimeout: (fn) => setImmediate(fn),
		__: (text, values = []) => text.replace(/\{(\d+)\}/g, (_, i) => values[i]),
		document: { addEventListener: (event, fn) => { events[event] = fn; } },
		sessionStorage: { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key) },
		localStorage: {getItem: key => shared.get(key) ?? null, setItem: (key, value) => shared.set(key, value), removeItem: key => shared.delete(key)},
		$: () => ({ on: (event, fn) => { events[event] = fn; } }),
		china_finance: { company_context: {} },
		frappe: {
			provide() {}, session: { user: "test@example.com" },
			boot: { workspace_sidebar_item: { "china finance": sidebar } },
			defaults: { get_user_default: () => "默认公司" },
			get_route: () => route,
			router: { slug: (name) => name.toLowerCase().replaceAll(" ", "-"), on: (event, fn) => { events[event] = fn; } },
			after_ajax: (fn) => fn(),
			workspace: { is_read_only: true, page: { set_secondary_action: (label, fn) => { events.switch = fn; events.label = label; } } },
			model: { with_doctype: async () => {} },
			meta: { get_docfield: (dt, fieldname) => dt !== "China Financial Statement Template" ? {fieldname} : null },
			call: async () => ({message: {companies: [{name: "公司甲"}, {name: "公司乙"}], recent: [], has_more: false}}),
			db: { get_value: async (_dt, name) => ({ message: { name, is_group: 0 } }) },
			msgprint: (message) => messages.push(message),
			ui: { Dialog: class {
				constructor(options) { this.options = options; this.value = options.fields.find(field => field.fieldname === "company").default; dialogs.push(this); }
				set_primary_action(_label, fn) { this.submit = fn; }
				get_values() { return { company: this.value }; }
				set_value(_name, value) { this.value = value; }
				get_primary_btn() { return { prop: (_key, value) => { this.disabled = value; } }; }
				show() { this.display = true; }
				hide() { this.display = false; this.onhide(); }
			} },
		},
	});
	context.window = context;
	context.location = { origin: "https://erp.example.com" };
	const frappe = context.frappe;
	frappe.set_route = async (pathname, ...parts) => {
		if (parts.length) {
			route = [pathname, ...parts];
			navigations.push({pathname: `/desk/${frappe.router.slug(parts[0])}`, filters: {...frappe.route_options}});
			events.change?.(); return;
		}
		navigations.push({ pathname, filters: { ...frappe.route_options } });
		route = pathname === "/desk/china-finance" ? ["Workspaces", "China Finance"] : [pathname];
		events.change?.();
	};
	vm.runInContext(read("public/js/company_context.js"), context);
	events.app_ready();
	return {
		context, frappe, events, dialogs, navigations, messages, store,
		company: context.china_finance.company_context,
		async select(value) {
			const done = this.company.select_company();
			dialogs.at(-1).value = value;
			await dialogs.at(-1).submit();
			return done;
		},
		async visit(value) { route = value; events.change(); await tick(); },
		click(href, { inSidebar = true, target = "", ...flags } = {}) {
			const link = { href, target, hasAttribute: () => false, closest: () => inSidebar };
			const event = { button: 0, target: { closest: () => link }, preventDefault() { this.prevented = true; }, stopImmediatePropagation() {}, ...flags };
			return { event, done: events.click(event) };
		},
	};
}

test("workspace entrance selects company and opens the workspace exactly once", async () => {
	const h = harness();
	const { event, done } = h.click("/desk/china-finance");
	assert.ok(event.prevented);
	assert.equal(h.navigations.length, 0);
	await tick();
	assert.equal(h.dialogs.length, 1);
	h.dialogs[0].value = "悦为智能（东莞）有限公司";
	await h.dialogs[0].submit();
	await done;
	await tick();
	assert.equal(h.navigations[0].pathname, "/desk/china-finance");
	assert.equal(h.dialogs.length, 1, "route change must not open another dialog");
	assert.equal(h.company.get_company(), "悦为智能（东莞）有限公司");
	assert.equal(h.frappe.defaults.get_user_default("Company"), "默认公司");
});

test("cancel keeps the previous company and does not navigate", async () => {
	const h = harness();
	await h.select("公司甲");
	const done = h.company.select_company();
	h.dialogs.at(-1).hide();
	await done;
	assert.equal(h.company.get_company(), "公司甲");
	assert.equal(h.navigations.length, 0);
});

test("new-tab links keep native browser behavior", async () => {
	const h = harness();
	const { done, event } = h.click("/desk/china-finance", {target: "_blank"});
	await done;
	assert.equal(event.prevented, undefined);
	assert.equal(h.dialogs.length, 0);
});

test("app startup tolerates a route that is not initialized yet", async () => {
	const h = harness();
	await h.visit(null);
	assert.equal(h.dialogs.length, 0);
	await h.visit(["Workspaces", "China Finance"]);
	assert.equal(h.dialogs.length, 1);
	h.dialogs[0].hide();
});

test("permission failure or group company cannot replace the selection", async () => {
	const h = harness();
	await h.select("公司甲");
	const pending = h.company.select_company();
	const dialog = h.dialogs.at(-1);
	dialog.value = "无权访问公司";
	h.frappe.db.get_value = async () => { throw new Error("Forbidden"); };
	await dialog.submit();
	assert.equal(h.company.get_company(), "公司甲");
	assert.equal(dialog.disabled, false);
	assert.ok(dialog.display);
	h.frappe.db.get_value = async () => ({ message: { name: "集团", is_group: 1 } });
	await dialog.submit();
	assert.equal(h.company.get_company(), "公司甲");
	assert.equal(h.messages.length, 2);
	dialog.hide();
	assert.equal(await pending, null);
});

test("closing a dialog during validation ignores its late response", async () => {
	const h = harness();
	await h.select("公司甲");
	let resolve;
	h.frappe.db.get_value = () => new Promise((done) => { resolve = done; });
	const pending = h.company.select_company();
	const dialog = h.dialogs.at(-1);
	dialog.value = "公司乙";
	const submitted = dialog.submit();
	dialog.hide();
	resolve({ message: { name: "公司乙" } });
	await submitted;
	assert.equal(await pending, null);
	assert.equal(h.company.get_company(), "公司甲");
});

test("switching company replaces old report/list/page filters before navigation", async () => {
	const h = harness();
	await h.select("公司甲");
	await h.click("/desk/journal-entry?company=old&docstatus=0").done;
	assert.deepEqual(Array.from(h.navigations.at(-1).filters.company), ["=", "公司甲"]);
	assert.equal(h.navigations.at(-1).filters.docstatus, 0);
	await h.select("公司乙 & 公司丙");
	for (const href of ["/desk/query-report/China%20Voucher%20Ledger?company=old", "/desk/journal-entry/view/list", "/desk/china-statement-mapping", "/desk/china-banking"]) {
		await h.click(href).done;
		const company = h.navigations.at(-1).filters.company;
		assert.equal(Array.isArray(company) ? company[1] : company, "公司乙 & 公司丙");
		assert.equal(h.navigations.at(-1).filters.sidebar, undefined);
		assert.ok(!h.navigations.at(-1).pathname.includes("?"));
	}
	assert.equal(h.dialogs.length, 2, "opening 查凭证 must not prompt again");
});

test("existing documents, foreign sites and other modules are not intercepted", async () => {
	const h = harness();
	await h.select("公司甲");
	for (const href of ["/desk/journal-entry/ACC-JV-0001", "https://other.example/desk/journal-entry", "/desk/customer"]) {
		assert.equal(await h.company.route_for_company(href), null);
		assert.equal(h.click(href).event.prevented, undefined);
	}
	assert.equal(h.click("/desk/journal-entry", { inSidebar: false }).event.prevented, undefined);
	assert.equal(await h.company.route_for_company("/desk/china-financial-statement-template"), null);
	assert.equal(h.click("/desk/china-finance", { ctrlKey: true }).event.prevented, undefined);
});

test("direct workspace entry prompts, and re-entering uses the previous choice", async () => {
	const h = harness();
	await h.visit(["Workspaces", "China Finance"]);
	assert.equal(h.dialogs.length, 1);
	h.dialogs[0].value = "公司甲";
	await h.dialogs[0].submit();
	await h.visit(["List", "Journal Entry"]);
	await h.visit(["Workspaces", "China Finance"]);
	assert.equal(h.dialogs.length, 1, "returning to the workspace does not ask again");
});

test("cached banking page restores chosen company and reloads its iframe", () => {
	const h = harness();
	const storage = new Map();
	h.context.sessionStorage = { getItem: (k) => storage.get(k), setItem: (k, v) => storage.set(k, v) };
	h.frappe.pages = { "china-banking": {} };
	vm.runInContext(read("china_finance/page/china_banking/china_banking.js"), h.context);
	const page = vm.runInContext("Object.create(ChinaBankingPage.prototype)", h.context);
	let reloads = 0;
	page.$iframe = { attr(_key, value) { if (value) reloads++; return "/banking?embedded=1"; } };
	h.frappe.route_options = { company: "公司甲" };
	page.sync_company();
	page.sync_company();
	assert.equal(reloads, 1);
	storage.set(`china_finance:company:test@example.com:bank`, JSON.stringify("公司乙"));
	page.sync_company();
	assert.equal(reloads, 2);
	assert.equal(JSON.parse(storage.get(`china_finance:company:test@example.com:bank`)), "公司甲");
});

test("mapping console discards the previous company's slower response", async () => {
	const h = harness();
	h.frappe.pages = { "china-statement-mapping": {} };
	const requests = [];
	h.frappe.xcall = (_method, args) => new Promise((resolve) => requests.push({ args, resolve }));
	vm.runInContext(read("china_finance/page/china_statement_mapping/china_statement_mapping.js"), h.context);
	const page = vm.runInContext("Object.create(ChinaStatementMapping.prototype)", h.context);
	let company = "公司甲", renders = 0;
	Object.assign(page, {
		company: { get_value: () => company }, statement_type: { get_value: () => "Balance Sheet" },
		accounting_standard: { get_value: () => "跟随公司设置" },
		selected_accounts: new Set(), selected_rows: new Set(), collapsed_rows: new Set(),
		collapsed_accounts: new Set(), expanded_aggregates: new Set(),
		$rows: { html() {} }, $accounts: { empty() {} }, render_all: () => renders++,
	});
	const first = page.refresh();
	company = "公司乙";
	const second = page.refresh();
	requests[1].resolve({ company: "公司乙" });
	await second;
	requests[0].resolve({ company: "公司甲" });
	await first;
	assert.equal(page.data.company, "公司乙");
	assert.equal(renders, 1);
});

function report_fixture(h, company = "公司甲") {
 let refreshes = 0;
 const filter = (fieldname, value, options) => ({df: {fieldname, options, onchange() {if (this.on_change) return this.on_change(); if (!report._no_refresh) refreshes++;}}, value,
  get_value() {return this.value;}, async set_value(value) {this.value = value; await this.df.onchange?.call(this);}});
 const filters = [filter("company", company, "Company"), filter("account", "甲科目", "Account"), filter("from_date", "2026-01-01", "Date")];
 const report = {report_name: "China Voucher Ledger", filters, get_filter: name => filters.find(f => f.df.fieldname === name), refresh: async () => {refreshes++;}};
 h.frappe.query_report = report;
 return {report, filters, refreshes: () => refreshes};
}

test("single authorized company enters automatically; zero companies gives a useful message", async () => {
 const h = harness();
 h.frappe.call = async () => ({message: {companies: [{name: "唯一公司"}], recent: [], has_more: false}});
 assert.equal(await h.company.ensure_company(), "唯一公司");
 assert.equal(h.dialogs.length, 0);
 const empty = harness();
 empty.frappe.call = async () => ({message: {companies: [], recent: [], has_more: false}});
 assert.equal(await empty.company.ensure_company(), null);
 assert.match(empty.messages.at(-1), /没有可用/);
});

test("reload restores this tab; other tabs keep their company while recent choices are shared", async () => {
 const shared = new Map(), tabA = new Map(), tabB = new Map();
 const a = harness(shared, tabA); await a.select("公司甲");
 const b = harness(shared, tabB); assert.equal(await b.company.ensure_company(), "公司甲");
 await b.select("公司乙");
 assert.equal(a.company.get_company(), "公司甲");
 assert.equal(harness(shared, tabA).company.get_company(), "公司甲");
 assert.equal(harness(shared).company.get_company(), "公司乙");
 assert.deepEqual(JSON.parse(shared.get("china_finance:company:test@example.com:recent")), ["公司乙", "公司甲"]);
});

test("revoked or grouped remembered companies are not silently reused", async () => {
 const tab = new Map([["china_finance:company:test@example.com", "旧公司"]]);
 const h = harness(new Map(), tab);
 h.frappe.db.get_value = async () => ({message: {name: "旧公司", is_group: 1}});
 const pending = h.company.ensure_company(); await tick();
 assert.equal(h.company.get_company(), "");
 assert.equal(h.dialogs.length, 1); h.dialogs[0].hide(); await pending;
});

test("saved documents preserve their company and unsaved or active closing work blocks switching", async () => {
 const h = harness(); await h.select("公司甲");
 h.context.cur_frm = {doc: {company: "公司甲"}, is_dirty: () => true};
 await h.visit(["Form", "Journal Entry", "JE1"]);
 assert.equal(await h.company.change_company("公司乙"), null);
 h.context.cur_frm.is_dirty = () => false;
 h.context.cur_frm.__china_closing_action_running = true;
 assert.equal(await h.company.change_company("公司乙"), null);
 h.context.cur_frm.__china_closing_action_running = false;
 list_fixture(h);
 assert.equal(await h.company.change_company("公司乙"), "公司乙");
 assert.equal(h.context.cur_frm.doc.company, "公司甲");
});

test("report switch clears dependent filters once and preserves dates", async () => {
 const h = harness(); await h.select("公司甲");
 const f = report_fixture(h); await h.visit(["query-report", "China Voucher Ledger"]);
 await h.company.change_company("公司乙");
 assert.equal(f.filters[0].value, "公司乙"); assert.equal(f.filters[1].value, "");
 assert.equal(f.filters[2].value, "2026-01-01"); assert.equal(f.refreshes(), 1);
});

test("manual report changes sync context; dirty edits or permission failures restore without refreshing", async () => {
 const h = harness(); await h.select("公司甲"); const f = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]);
 f.report._china_inline_edit_state = {changes: new Map([["row1", {}]])};
 await f.filters[0].set_value("公司乙");
 assert.equal(f.filters[0].value, "公司甲"); assert.equal(f.refreshes(), 0);
 assert.equal(f.filters[1].value, "甲科目");
 f.report._china_inline_edit_state.changes.clear();
 h.frappe.db.get_value = async () => {throw new Error("Network");};
 await f.filters[0].set_value("公司乙");
 assert.equal(f.filters[0].value, "公司甲"); assert.equal(f.refreshes(), 0);
 h.frappe.db.get_value = async (_dt, name) => ({message: {name, is_group: 0}});
 await f.filters[0].set_value("公司乙");
 assert.equal(h.company.get_company(), "公司乙"); assert.equal(f.filters[1].value, "");
 assert.equal(f.refreshes(), 1);
});

test("native list company changes clear account filters before querying and preserve status", async () => {
 const h = harness(); await h.select("公司甲");
 let filters = [["Journal Entry", "company", "=", "公司甲"], ["Journal Entry", "account", "=", "甲科目"], ["Journal Entry", "docstatus", "=", 0]];
 const queries = [];
 h.context.cur_list = {doctype: "Journal Entry", refresh: async () => queries.push(JSON.parse(JSON.stringify(filters))),
  filter_area: {trigger_refresh: true, get: () => filters, clear: async () => {filters = [];}, set: async values => {filters = values;}, refresh_list_view() {h.context.cur_list.refresh();}}};
 await h.visit(["List", "Journal Entry", "List"]);
 filters[0][3] = "公司乙";
 await h.context.cur_list.filter_area.refresh_list_view();
 assert.equal(queries.length, 1); assert.equal(queries[0].some(f => f[1] === "account"), false);
 assert.equal(queries[0].find(f => f[1] === "docstatus")[3], 0);
 assert.equal(h.company.get_company(), "公司乙");
});

test("same-company navigation preserves account drilldowns; replacing company removes stale accounts", async () => {
 const h = harness(); await h.select("公司甲");
 const same = new URL(await h.company.route_for_company("/desk/query-report/China%20Voucher%20Ledger?company=公司甲&account=A&from_date=2026-01-01"), h.context.location.origin);
 assert.equal(same.searchParams.get("account"), "A");
 const other = new URL(await h.company.route_for_company("/desk/query-report/China%20Voucher%20Ledger?company=公司乙&account=A&from_date=2026-01-01"), h.context.location.origin);
 assert.equal(other.searchParams.has("account"), false); assert.equal(other.searchParams.get("from_date"), "2026-01-01");
});

test("custom page adapters may reject switches without losing the previous company", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["china-opening-balances"]);
 let calls = 0;
 h.company.register_page("china-opening-balances", {before_switch: async () => false, switch_company: () => {calls++;}});
 assert.equal(await h.company.change_company("公司乙"), null); assert.equal(calls, 0);
 h.company.register_page("china-opening-balances", {switch_company: async () => false});
 await assert.rejects(h.company.change_company("公司乙")); assert.equal(h.company.get_company(), "公司甲");
});

test("late validation cannot update another route or overwrite a more recent switch", async () => {
 const h = harness(); await h.select("公司甲");
 const pending = [];
 h.frappe.db.get_value = (_dt, name) => new Promise(resolve => pending.push(() => resolve({message: {name}})));
 const first = h.company.change_company("公司乙"), second = h.company.change_company("公司丙");
 pending[1](); assert.equal(await second, "公司丙"); pending[0](); assert.equal(await first, null);
 const third = h.company.change_company("公司丁"); await h.visit(["Form", "Journal Entry", "JE1"]);
 pending[2](); assert.equal(await third, null); assert.equal(h.company.get_company(), "公司丙");
});

test("embedded Banking company storage is isolated per tab and synchronizes the parent selector", async () => {
 const h = harness(), other = harness();
 const key = "china_finance:company:test@example.com:bank";
 h.context.parent = h.context; other.context.parent = other.context;
 h.store.set(key, JSON.stringify("公司甲")); other.store.set(key, JSON.stringify("公司乙"));
 vm.runInContext(read("public/js/banking_company_context.js"), h.context);
 vm.runInContext(read("public/js/banking_company_context.js"), other.context);
 assert.equal(JSON.parse(h.context.localStorage.getItem("bank-rec-selected-company")), "公司甲");
 assert.equal(JSON.parse(other.context.localStorage.getItem("bank-rec-selected-company")), "公司乙");
 h.context.localStorage.setItem("bank-rec-selected-company", JSON.stringify("公司丙")); await tick();
 assert.equal(h.company.get_company(), "公司丙"); assert.equal(JSON.parse(other.store.get(key)), "公司乙");
 const notifications = [];
 h.context.localStorage.subscribe("bank-rec-selected-company", value => notifications.push(value));
 h.frappe.db.get_value = async () => ({message: null});
 h.context.localStorage.setItem("bank-rec-selected-company", JSON.stringify("无权访问")); await tick();
 assert.equal(JSON.parse(h.store.get(key)), "公司丙"); assert.equal(JSON.parse(notifications[0]), "公司丙");
});

test("account-tree company follows the finance selector in both directions", async () => {
 const h = harness(); await h.select("公司甲"); let reloads = 0;
 const field = {df: {change: () => reloads++}, value: "公司甲", get_value() {return this.value;}, async set_value(value) {this.value = value; await this.df.change?.();}};
 const tree = {doctype: "Account", args: {company: "公司甲"}, page: {fields_dict: {company: field}}, make_tree() {}};
 field.df.change = () => {tree.args.company = field.get_value(); reloads++;};
 h.frappe.views = {trees: {Account: tree}};
 h.context.cur_tree = {args: tree.args};
 await h.visit(["Tree", "Account"]);
 assert.match(await h.company.route_for_company('/desk/account/view/tree?sidebar=China%20Finance'), /company=/);
 await h.company.change_company("公司乙"); assert.equal(field.value, "公司乙");
 await field.set_value("公司丙"); assert.equal(h.company.get_company(), "公司丙");
 assert.equal(reloads, 2);
});

test("a slow rejected report selection cannot undo a later accepted selection", async () => {
 const h = harness(); await h.select("公司甲"); const f = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]);
 const pending = [];
 h.frappe.db.get_value = (_dt, name) => new Promise(resolve => pending.push(() => resolve({message: {name}})));
 const first = f.filters[0].set_value("公司乙"), second = f.filters[0].set_value("公司丙");
 pending[1](); await second; pending[0](); await first;
 assert.equal(f.filters[0].value, "公司丙"); assert.equal(h.company.get_company(), "公司丙");
 assert.equal(f.refreshes(), 1);
});

test("financial statements refresh company data and tax header while retaining the accounting period", async () => {
 const h = harness(); await h.select("公司甲");
 h.frappe.query_reports = {}; h.frappe.app = {};
 vm.runInContext(read("china_finance/report/china_financial_statements/china_financial_statements.js"), h.context);
 for (const fn of ["sync_fixed_asset_detail_filter", "add_china_finance_export_actions", "bind_source_account_links", "sync_accounting_period"]) h.context[fn] = () => {};
 h.frappe.db.get_value = async (_dt, name, fields) => ({message: Array.isArray(fields) ? {name, is_group: 0} : {tax_id: name + "税号", accounting_standard: "企业会计准则"}});
 const f = report_fixture(h), extras = new Map();
 f.report.report_name = "China Financial Statements";
 const accounting_period = {df: {fieldname: "accounting_period", fieldtype: "Select"}, value: "1月", get_value() {return this.value;}, async set_value(value) {this.value = value;}};
 f.filters.push(accounting_period);
 f.report.get_filter = name => f.filters.find(field => field.df.fieldname === name) || (extras.has(name) ? extras.get(name) : (extras.set(name, {}), extras.get(name)));
 f.report.get_filter_value = name => f.filters.find(field => field.df.fieldname === name)?.get_value() || (name === "fiscal_year" ? "2026" : "");
 h.frappe.query_reports["China Financial Statements"].onload(f.report);
 await h.visit(["query-report", "China Financial Statements"]);
 await h.company.change_company("公司乙"); await tick();
 assert.equal(f.refreshes(), 1); assert.equal(f.report.statutory_header.tax_id, "公司乙税号");
 assert.equal(accounting_period.value, "1月");
 await f.filters[0].set_value("公司丙"); await tick();
 assert.equal(f.refreshes(), 2); assert.equal(f.report.statutory_header.tax_id, "公司丙税号");
});

test("company edits during an in-flight report reload cannot desynchronize the heading", async () => {
 const h = harness(); await h.select("公司甲"); const f = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]);
 let loaded;
 f.report.refresh = () => new Promise(resolve => {loaded = resolve;});
 const switching = h.company.change_company("公司乙"); await tick();
 await f.filters[0].set_value("公司丙");
 assert.equal(f.filters[0].get_value(), "公司乙"); assert.equal(h.company.get_company(), "公司甲");
 assert.equal(h.company.is_switching(), true);
 loaded(); await switching;
 assert.equal(h.company.get_company(), "公司乙");
});

// Execute the actual Frappe TreeFactory / TreeView and both Account tree hooks.
// Only DOM drawing and server reads are replaced; cur_tree remains the raw widget.
function native_account_tree(h) {
 const renders = [], controls = new Map();
 const $ = selector => ({find: () => ({remove() {}}), trigger() {
  const name = typeof selector === "string" && selector.match(/data-fieldname='([^']+)'/)?.[1];
  const field = controls.get(name); if (field) Promise.resolve().then(() => field.df.change?.call(field));
 }});
 $.extend = Object.assign; $.each = (values, fn) => values.forEach((value, i) => fn(i, value));
 h.context.$ = $;
 h.context.erpnext = {utils: {get_tree_options: () => ["公司甲", "公司乙", "公司丙"], get_tree_default: () => "公司甲"}};
 h.frappe.flags = {}; h.frappe.utils = {escape_html: value => value};
 h.frappe.views = {Factory: class {}, trees: {}};
 h.frappe.treeview_settings = {};
 h.frappe.get_route_str = () => h.frappe.get_route().join("/");
 h.frappe.get_meta = () => ({fields: [{fieldname: "account_type", options: "Bank"}, {fieldname: "account_category", options: "Account Category"}]});
 h.frappe.model.with_doctype = (_dt, callback) => {callback?.(); return Promise.resolve();};
 h.frappe.ui.Tree = class {
  constructor(options) {Object.assign(this, options); this.nodes = {}; renders.push({company: options.args.company, label: options.label});}
 };
 vm.runInContext(fs.readFileSync(path.resolve(root, "../../frappe/frappe/public/js/frappe/views/treeview.js"), "utf8"), h.context);
 const prototype = h.frappe.views.TreeView.prototype;
 for (const method of ["get_permissions", "set_title", "set_menu_item", "set_primary_action"]) prototype[method] = () => {};
 prototype.make_page = function () {
  this.parent = {}; this.body = {};
  this.page = {fields_dict: {}, filters: {}, add_inner_button() {}, add_divider_to_button_group() {}, add_field: df => {
   const field = {df, value: df.default, $input: {on() {}}, get_value() {return this.value;}, async set_value(value, force = false) {
    if (this.value === value && !force) return;
    this.value = value; return this.df.change?.call(this);
   }};
   controls.set(df.fieldname, field); this.page.fields_dict[df.fieldname] = field; return field;
  }};
 };
 vm.runInContext(fs.readFileSync(path.resolve(root, "../../erpnext/erpnext/accounts/doctype/account/account_tree.js"), "utf8"), h.context);
 vm.runInContext(read("public/js/account_tree.js"), h.context);
 const factory = new h.frappe.views.TreeFactory();
 return {renders, controls, factory, make() {factory.make(["Tree", "Account"]); return h.frappe.views.trees.Account;}};
}

test("native Account factory starts with the selected company and exposes a separate raw cur_tree", async () => {
 const h = harness(); await h.select("公司乙"); await h.visit(["Tree", "Account"]);
 const native = native_account_tree(h), tree = native.make(); await tick();
 assert.equal(h.context.cur_tree.page, undefined, "the native widget has no company controls");
 assert.equal(h.context.cur_tree.doctype, undefined);
 assert.equal(native.controls.get("company").get_value(), "公司乙");
 assert.ok(native.renders.length > 0); assert.ok(native.renders.every(row => row.company === "公司乙" && row.label === "公司乙"));
 await h.company.change_company("公司丙");
 assert.equal(tree.args.company, "公司丙"); assert.equal(native.renders.at(-1).label, "公司丙");
 await native.controls.get("company").set_value("公司甲");
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(tree.args.company, "公司甲");
});

test("cached native TreeFactory re-entry uses the working company before rendering any old-company tree", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["Tree", "Account"]);
 const native = native_account_tree(h); native.make(); await tick();
 await h.visit(["Workspaces", "China Finance"]); await h.select("公司乙");
 const count = native.renders.length;
 await h.visit(["Tree", "Account"]); native.factory.on_show(); await tick();
 assert.equal(h.company.get_company(), "公司乙");
 assert.equal(native.controls.get("company").get_value(), "公司乙");
 assert.ok(native.renders.slice(count).length > 0);
 assert.ok(native.renders.slice(count).every(row => row.company === "公司乙" && row.label === "公司乙"));
});

test("an explicit company on a cached native tree updates its controls and context together", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["Tree", "Account"]);
 const native = native_account_tree(h), tree = native.make(); await tick();
 await h.visit(["Form", "Journal Entry", "JE1"]);
 h.frappe.route_options = {company: "公司乙", sidebar: "China Finance"};
 await h.visit(["Tree", "Account"]); native.factory.on_show(); await tick();
 assert.equal(tree.args.company, "公司乙"); assert.equal(h.company.get_company(), "公司乙");
 assert.equal(h.frappe.route_options.company, undefined); assert.equal(h.frappe.route_options.sidebar, "China Finance");
 assert.equal(native.renders.at(-1).label, "公司乙");
});

test("first native Account tree honors an explicit company over the remembered default", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["Tree", "Account"]);
 h.frappe.route_options = {company: "公司乙"};
 const native = native_account_tree(h), tree = native.make(); await tick();
 assert.equal(h.company.get_company(), "公司乙"); assert.equal(tree.args.company, "公司乙");
 assert.ok(native.renders.every(row => row.company === "公司乙"));
});

test("a rejected cached tree company falls back to the current company rather than displaying stale data", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["Tree", "Account"]);
 const native = native_account_tree(h), tree = native.make(); await tick();
 await h.visit(["Workspaces", "China Finance"]); await h.select("公司乙");
 h.frappe.db.get_value = async () => ({message: {name: "集团公司", is_group: 1}});
 h.frappe.route_options = {company: "集团公司"}; const count = native.renders.length;
 await h.visit(["Tree", "Account"]); native.factory.on_show(); await tick();
 assert.equal(h.company.get_company(), "公司乙"); assert.equal(tree.args.company, "公司乙");
 assert.equal(native.controls.get("company").get_value(), "公司乙");
 assert.ok(native.renders.slice(count).every(row => row.company === "公司乙"));
});

function list_fixture(h, company = "公司甲") {
 let filters = [["Journal Entry", "company", "=", company]], queries = [];
 const list = {doctype: "Journal Entry", start: 20, filter_area: {trigger_refresh: true,
  get: () => filters, clear: async () => {filters = [];}, set: async values => {filters = values;}, refresh_list_view() {}},
  refresh: async () => {queries.push(JSON.parse(JSON.stringify(filters)));}};
 h.context.cur_list = list;
 return {list, queries};
}

test("a failed report reload restores the company, filters and prior data without persisting the attempted choice", async () => {
 const h = harness(), sharedKey = "china_finance:company:test@example.com";
 await h.select("公司甲"); const f = report_fixture(h); await h.visit(["query-report", "China Voucher Ledger"]);
 f.report.statutory_header = {tax_id: "原税号"}; f.report.data = [{company: "公司甲"}];
 f.report.prepared_report_name = "PREP-A"; f.report.prepared_report_document = {company: "公司甲"}; f.report.prepared_report = true;
 f.report.previous_filters = {company: "公司乙"};
 const handler = f.filters[0].df.onchange;
 f.filters[0].df.onchange = function () {assert.equal(f.report.previous_filters, null); return handler?.call(this);};
 f.report.refresh = async () => {
  if (f.filters[0].get_value() === "公司乙") {f.report.statutory_header = {tax_id: "新税号"}; throw new Error("HTTP 417");}
  f.report.data = [{company: f.filters[0].get_value()}];
 };
 await assert.rejects(h.company.change_company("公司乙"), /417/);
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(h.store.get(sharedKey), "公司甲");
 assert.equal(f.filters[0].get_value(), "公司甲"); assert.equal(f.filters[1].get_value(), "甲科目");
 assert.equal(f.report.data[0].company, "公司甲"); assert.equal(f.report.statutory_header.tax_id, "原税号");
 assert.equal(f.report.prepared_report_name, "PREP-A"); assert.equal(f.report.prepared_report_document.company, "公司甲");
 assert.equal(f.report.previous_filters, null);
 assert.equal(h.company.is_switching(), false);
});

test("the committed company and history stay unchanged until the new report finishes loading", async () => {
 const shared = new Map(), h = harness(shared); await h.select("公司甲"); const f = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]); let finish;
 f.report.refresh = () => new Promise(resolve => {finish = resolve;});
 const pending = h.company.change_company("公司乙"); await tick();
 assert.equal(h.company.get_company(), "公司甲");
 assert.equal(shared.get("china_finance:company:test@example.com:last"), "公司甲");
 assert.deepEqual(JSON.parse(shared.get("china_finance:company:test@example.com:recent")), ["公司甲"]);
 assert.equal(h.company.is_switching(), true); finish(); await pending;
 assert.equal(h.company.get_company(), "公司乙");
 assert.deepEqual(JSON.parse(shared.get("china_finance:company:test@example.com:recent")), ["公司乙", "公司甲"]);
});

test("native QueryReport request failure rejects its resolve-only refresh and releases the switch", async () => {
 const h = harness(); await h.select("公司甲"); const f = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]); let fail;
 f.report.refresh = () => {
  if (f.filters[0].value === "公司乙") {f.report.last_ajax = {fail: fn => {fail = fn;}}; return new Promise(() => {});}
  f.report.last_ajax = null; return Promise.resolve();
 };
 const pending = h.company.change_company("公司乙"); await tick(); fail(new Error("Failed xhr"));
 await assert.rejects(pending, /Failed xhr/);
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(f.filters[0].value, "公司甲");
 assert.equal(h.company.is_switching(), false);
});

test("saved voucher details move to the new company's list after a successful switch", async () => {
 const h = harness(); await h.select("公司甲"); const f = list_fixture(h);
 h.context.cur_frm = {doc: {company: "公司甲", name: "JE1"}, is_dirty: () => false};
 await h.visit(["Form", "Journal Entry", "JE1"]);
 await h.company.change_company("公司乙");
 assert.equal(h.frappe.get_route()[0], "List"); assert.equal(h.frappe.get_route()[1], "Journal Entry");
 assert.equal(h.context.cur_frm.doc.company, "公司甲");
 assert.equal(f.queries.at(-1).find(filter => filter[1] === "company")[3], "公司乙");
 assert.equal(h.company.get_company(), "公司乙");
});

test("a failure opening the new company list returns to the original voucher and company", async () => {
 const h = harness(); await h.select("公司甲"); const f = list_fixture(h);
 f.list.refresh = async () => {throw new Error("Permission denied");};
 h.context.cur_frm = {doc: {company: "公司甲"}, is_dirty: () => false};
 await h.visit(["Form", "Journal Entry", "JE1"]);
 await assert.rejects(h.company.change_company("公司乙"), /Permission denied/);
 assert.deepEqual(Array.from(h.frappe.get_route()), ["Form", "Journal Entry", "JE1"]);
 assert.equal(h.company.get_company(), "公司甲");
});

test("public configuration stays open while changing the working company", async () => {
 const h = harness(); await h.select("公司甲");
 await h.visit(["Form", "China Financial Statement Template", "Template1"]);
 await h.company.change_company("公司乙");
 assert.deepEqual(Array.from(h.frappe.get_route()), ["Form", "China Financial Statement Template", "Template1"]);
 assert.equal(h.navigations.length, 0); assert.equal(h.company.get_company(), "公司乙");
});

test("an unready finance report fails explicitly instead of claiming that the company changed", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["query-report", "China Voucher Ledger"]);
 await assert.rejects(h.company.change_company("公司乙"), /尚未准备好/);
 assert.equal(h.company.get_company(), "公司甲");
});

test("custom editor snapshot restores unsaved rows and selections when switching fails", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["china-opening-balances"]);
 const session = vm.runInContext('({company:"公司甲", rows:[{amount:"10.25"}], dirty:true, selected:new Set(["row1"]), request:1, revision:2, onchange(){}})', h.context);
 h.company.register_page("china-opening-balances", {
  before_switch: async () => true, capture: () => h.company.capture_session(session), restore: restore => restore(),
  switch_company: async company => {session.company = company; session.rows = []; session.selected.clear(); session.dirty = false; return false;},
 });
 await assert.rejects(h.company.change_company("公司乙"));
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(session.company, "公司甲");
 assert.equal(session.rows[0].amount, "10.25"); assert.ok(session.selected.has("row1")); assert.equal(session.dirty, true);
 assert.ok(session.request > 1); assert.ok(session.revision > 2);
});

test("a cached report resynchronizes when returning after a company switch elsewhere", async () => {
 const h = harness(); await h.select("公司甲"); const f = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]); await h.visit(["Workspaces", "China Finance"]);
 await h.select("公司乙"); await h.visit(["query-report", "China Voucher Ledger"]); await tick();
 assert.equal(f.filters[0].get_value(), "公司乙"); assert.equal(f.filters[1].get_value(), "");
 assert.equal(h.company.get_company(), "公司乙"); assert.equal(f.report._cf_working_company, "公司乙");
});

test("returning to a cached report with unsaved edits preserves its company and changes", async () => {
 const h = harness(); await h.select("公司甲"); const f = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]);
 f.report._china_inline_edit_state = {changes: new Map([["row1", {}]])};
 await h.visit(["Workspaces", "China Finance"]); await h.select("公司乙");
 await h.visit(["query-report", "China Voucher Ledger"]); await tick();
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(f.filters[0].get_value(), "公司甲");
 assert.equal(f.report._china_inline_edit_state.changes.size, 1); assert.equal(f.refreshes(), 0);
});

test("bank reconciliation commits only after its iframe has loaded the app", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["china-banking"]);
 h.frappe.pages = {"china-banking": {}};
 vm.runInContext(read("china_finance/page/china_banking/china_banking.js"), h.context);
 h.context.setTimeout = () => 1; h.context.clearTimeout = () => {};
 const listeners = new Map();
 const frame = {contentDocument: {querySelector: () => ({childElementCount: 1})},
  addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name)};
 const page = vm.runInContext("Object.create(ChinaBankingPage.prototype)", h.context);
 page.company = "公司甲"; page.$iframe = {get: () => frame, attr: () => "/china_banking_embedded"};
 h.company.register_page("china-banking", {capture: previous => previous, restore: company => page.sync_company(company, {wait: true}), switch_company: company => page.sync_company(company, {wait: true})});
 const pending = h.company.change_company("公司乙"); await tick();
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(h.company.is_switching(), true);
 listeners.get("load")(); await pending;
 assert.equal(h.company.get_company(), "公司乙"); assert.equal(listeners.size, 0);
});

test("a failed bank iframe restores the old company before releasing the switch", async () => {
 const h = harness(); await h.select("公司甲"); await h.visit(["china-banking"]);
 h.frappe.pages = {"china-banking": {}};
 vm.runInContext(read("china_finance/page/china_banking/china_banking.js"), h.context);
 h.context.setTimeout = () => 1; h.context.clearTimeout = () => {};
 const listeners = new Map(); let root = null;
 const frame = {contentDocument: {querySelector: () => root}, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name)};
 const page = vm.runInContext("Object.create(ChinaBankingPage.prototype)", h.context);
 page.company = "公司甲"; page.$iframe = {get: () => frame, attr: () => "/china_banking_embedded"};
 h.company.register_page("china-banking", {capture: previous => previous, restore: company => page.sync_company(company, {wait: true}), switch_company: company => page.sync_company(company, {wait: true})});
 const pending = h.company.change_company("公司乙"); const failed = assert.rejects(pending, /加载失败/); await tick();
 listeners.get("load")(); await tick();
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(page.company, "公司甲"); assert.equal(h.company.is_switching(), true);
 root = {childElementCount: 1}; listeners.get("load")(); await failed;
 assert.equal(h.company.is_switching(), false); assert.equal(listeners.size, 0);
});

test("initial report adoption wins over cached-view synchronization, including repeated page events", async () => {
 const h = harness(); await h.select("公司甲"); const f = report_fixture(h, "公司乙");
 f.filters[1].value = "乙科目";
 let finish;
 h.frappe.db.get_value = (_dt, name) => new Promise(resolve => {finish = () => resolve({message: {name}});});
 await h.visit(["query-report", "China Voucher Ledger"]);
 h.company.bind_current(); h.company.bind_current();
 finish(); await tick();
 assert.equal(h.company.get_company(), "公司乙");
 assert.equal(f.filters[0].value, "公司乙"); assert.equal(f.filters[1].value, "乙科目");
 assert.equal(f.refreshes(), 0, "adopting a direct link must preserve its loaded filters and data");
});

test("cash-flow detail switching passes the target after native List initialization consumed route options", async () => {
 const h = harness(); await h.select("公司甲"); let target;
 const session = {filters: {company: "公司甲"}};
 h.context.cur_list = {doctype: "China Cash Flow Assignment", cash_flow_workbench: {session,
  async refresh(_explicit, company) {target = company; session.filters.company = company; return true;}}};
 h.context.cur_frm = {doc: {company: "公司甲"}, is_dirty: () => false};
 await h.visit(["Form", "China Cash Flow Assignment", "CFA-A"]);
 const route = h.frappe.set_route;
 h.frappe.set_route = async (...args) => {await route(...args); h.frappe.route_options = null;};
 assert.equal(await h.company.change_company("公司乙"), "公司乙");
 assert.equal(target, "公司乙"); assert.equal(session.filters.company, "公司乙");
 assert.equal(h.context.cur_frm.doc.company, "公司甲");
});

for (const failure of [false, true]) test(`navigation during report loading ignores late ${failure ? "failure" : "success"} and preserves the active report`, async () => {
 const shared = new Map(), h = harness(shared); await h.select("公司甲"); const original = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]);
 let finish;
 original.report.refresh = () => original.filters[0].value === "公司乙"
  ? new Promise((resolve, reject) => {finish = () => failure ? reject(new Error("HTTP 417")) : resolve();})
  : Promise.resolve();
 const pending = h.company.change_company("公司乙"); await tick();
 const next = report_fixture(h); next.report.report_name = "China Financial Statements";
 await h.visit(["query-report", "China Financial Statements"]);
 finish(); assert.equal(await pending, null); await tick();
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(next.filters[0].value, "公司甲");
 assert.equal(next.report._cf_working_company, "公司甲"); assert.equal(h.company.is_switching(), false);
 assert.deepEqual(JSON.parse(shared.get("china_finance:company:test@example.com:recent")), ["公司甲"]);
 h.frappe.query_report = original.report;
 await h.visit(["query-report", "China Voucher Ledger"]); await tick();
 assert.equal(original.filters[0].value, "公司甲", "the interrupted cached report must resynchronize before reuse");
 assert.equal(original.report._cf_company_stale, false);
});

test("leaving and returning to the same route still cancels the pending company application", async () => {
 const h = harness(); await h.select("公司甲"); const f = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]); let finish;
 f.report.refresh = () => f.filters[0].value === "公司乙" ? new Promise(resolve => {finish = resolve;}) : Promise.resolve();
 const pending = h.company.change_company("公司乙"); await tick();
 await h.visit(["Workspaces", "China Finance"]);
 await h.visit(["query-report", "China Voucher Ledger"]);
 finish(); assert.equal(await pending, null); await tick();
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(f.filters[0].value, "公司甲");
});

for (const success of [false, true]) test(`rapid embedded bank selections retain the last ${success ? "successful" : "confirmed"} company`, async () => {
 const h = harness(); await h.select("公司甲"); h.context.parent = h.context;
 const key = "china_finance:company:test@example.com:bank", pending = {};
 h.store.set(key, JSON.stringify("公司甲"));
 vm.runInContext(read("public/js/banking_company_context.js"), h.context);
 const notified = []; h.context.localStorage.subscribe("bank-rec-selected-company", value => notified.push(value));
 h.frappe.db.get_value = (_dt, name) => new Promise((resolve, reject) => {pending[name] = {resolve, reject};});
 h.context.localStorage.setItem("bank-rec-selected-company", JSON.stringify("公司乙"));
 h.context.localStorage.setItem("bank-rec-selected-company", JSON.stringify("公司丙"));
 if (success) pending["公司丙"].resolve({message: {name: "公司丙"}});
 else pending["公司丙"].reject(new Error("Network error"));
 await tick(); pending["公司乙"].resolve({message: {name: "公司乙"}}); await tick();
 const expected = success ? "公司丙" : "公司甲";
 assert.equal(h.company.get_company(), expected); assert.equal(JSON.parse(h.store.get(key)), expected);
 if (!success) assert.equal(JSON.parse(notified.at(-1)), "公司甲");
});

test("navigation while the target List initializes cancels without returning to the source document", async () => {
 const h = harness(); await h.select("公司甲"); const f = list_fixture(h); let finish;
 f.list.init_promise = new Promise(resolve => {finish = resolve;});
 h.context.cur_frm = {doc: {company: "公司甲"}, is_dirty: () => false};
 await h.visit(["Form", "Journal Entry", "JE1"]);
 const pending = h.company.change_company("公司乙"); await tick();
 const next = report_fixture(h);
 await h.visit(["query-report", "China Voucher Ledger"]);
 finish(); assert.equal(await pending, null); await tick();
 assert.deepEqual(Array.from(h.frappe.get_route()), ["query-report", "China Voucher Ledger"]);
 assert.equal(h.company.get_company(), "公司甲"); assert.equal(next.filters[0].value, "公司甲");
 await h.visit(["List", "Journal Entry", "List"]); await tick();
 assert.equal(f.list.filter_area.get().find(filter => filter[1] === "company")[3], "公司甲");
});

test("the native router hiding the company dialog does not cancel an authorized Form-to-List switch", async () => {
 const h = harness(); await h.select("公司甲"); const f = list_fixture(h);
 h.context.cur_frm = {doc: {company: "公司甲"}, is_dirty: () => false};
 await h.visit(["Form", "Journal Entry", "JE1"]);
 const route = h.frappe.set_route;
 h.frappe.set_route = async (...args) => {h.dialogs.at(-1).hide(); await route(...args);};
 assert.equal(await h.select("公司乙"), "公司乙");
 assert.equal(h.company.get_company(), "公司乙");
 assert.equal(f.list.filter_area.get().find(filter => filter[1] === "company")[3], "公司乙");
});
