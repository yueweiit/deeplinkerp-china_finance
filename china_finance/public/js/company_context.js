frappe.provide("china_finance.company_context");

(() => {
	const context = china_finance.company_context;
	const workspace_path = "/desk/china-finance";
	const adapters = new Map();
	const dependent_names = new Set(["account", "bank_account", "cost_center", "project", "finance_book", "warehouse", "party", "party_name"]);
	const dependent_types = new Set(["Account", "Bank Account", "Cost Center", "Project", "Finance Book", "Warehouse", "Accounting Period"]);
	let selected_company, selection, ensuring, revision = 0, binding = false, committing = false, transition = null;
	const key = () => `china_finance:company:${frappe.session.user}`;
	const recent_key = () => `${key()}:recent`;
	const read = (storage, name, fallback = "") => { try { return storage?.getItem(name) || fallback; } catch { return fallback; } };
	const write = (storage, name, value) => { try { storage?.setItem(name, value); } catch { /* The in-memory choice remains available. */ } };
	const storage = name => { try { return window[name]; } catch { return null; } };
	function recent_names() { try { return JSON.parse(read(storage("localStorage"), recent_key(), "[]")).filter(name => typeof name === "string").slice(0, 5); } catch { return []; } }
	context.get_company = () => {
		if (selected_company === undefined) selected_company = read(storage("sessionStorage"), key(), read(storage("localStorage"), `${key()}:last`));
		return selected_company;
	};
	const working_company = () => transition?.company || context.get_company();
	context.is_switching = () => !!transition;
	context.default_company = () => working_company() || frappe.defaults.get_user_default("Company");
	function remember(company) {
		selected_company = company;
		write(storage("sessionStorage"), key(), company);
		write(storage("localStorage"), `${key()}:last`, company);
		if (company) write(storage("localStorage"), recent_key(), JSON.stringify([company, ...recent_names().filter(name => name !== company)].slice(0, 5)));
		update_bar();
	}
	function items() {
		return (frappe.boot.workspace_sidebar_item?.["china finance"]?.items || []).map(item =>
			item.link_type === "URL" && item.url?.split("?")[0] === "/desk/account/view/tree"
				? {...item, link_type: "Tree", link_to: "Account"} : item);
	}
	function is_workspace() { const route = frappe.get_route() || []; return route[0] === "Workspaces" && route[1] === "China Finance"; }
	function active_adapter() { const route = frappe.get_route() || []; return adapters.get(route.slice(0, 2).join("/")) || adapters.get(route[0]); }
	context.register_page = (name, adapter) => { adapters.set(name, adapter); update_bar(); };
	function is_finance() {
		const route = frappe.get_route() || [];
		if (is_workspace() || active_adapter()) return true;
		return items().some(item => item.type === "Link" && (
			(item.link_type === "Report" && route[0] === "query-report" && route[1] === item.link_to) ||
			(item.link_type === "Tree" && route[0] === "Tree" && route[1] === item.link_to) ||
			(item.link_type === "DocType" && ["List", "Tree", "Form"].includes(route[0]) && route[1] === item.link_to) ||
			(item.link_type === "Page" && route[0] === item.link_to)
		));
	}
	function update_bar() {
		if (!document.createElement || !frappe.container?.page) return;
		document.querySelectorAll(".cf-company-bar").forEach(element => element.remove());
		if (!is_finance()) return;
		const host = frappe.container.page;
		const bar = document.createElement("div"); bar.className = "cf-company-bar";
		const button = document.createElement("button"); button.type = "button"; button.className = "btn btn-default btn-sm";
		button.textContent = context.get_company() ? __("当前公司：{0}", [context.get_company()]) : __("选择公司");
		button.disabled = !!transition;
		if (transition) button.textContent = __("正在切换到：{0}", [transition.company]);
		button.title = __("切换公司"); button.onclick = () => context.select_company(); bar.appendChild(button);
		const route = frappe.get_route() || [];
		if (route[0] === "Form" && window.cur_frm?.doc?.company && window.cur_frm.doc.company !== context.get_company()) {
			const hint = document.createElement("span"); hint.textContent = __("当前单据属于：{0}", [window.cur_frm.doc.company]); bar.appendChild(hint);
		}
		if (public_page()) {
			const hint = document.createElement("span"); hint.textContent = __("公共配置，不按公司切换"); bar.appendChild(hint);
		}
		host.prepend(bar);
	}
	context.update_bar = update_bar;
	async function validate(company) {
		if (!company) return null;
		const response = await frappe.db.get_value("Company", company, ["name", "is_group"]);
		return response.message?.name && !response.message.is_group ? response.message.name : null;
	}
	async function may_switch() {
		const adapter = active_adapter();
		if (adapter?.before_switch) return adapter.before_switch();
		const route = frappe.get_route() || [];
		if (route[0] === "Form" && window.cur_frm?.__china_closing_action_running) {
			frappe.msgprint(__("月末结账操作正在执行，请等待处理完成后再切换公司")); return false;
		}
		if (route[0] === "Form" && window.cur_frm?.is_dirty?.()) {
			frappe.msgprint(__("请先保存或放弃当前单据的修改，再切换公司")); return false;
		}
		const state = route[0] === "query-report" && frappe.query_report?._china_inline_edit_state;
		if (state?.pending || state?.changes?.size) { frappe.msgprint(__("请先保存或取消凭证分录修改，再切换公司")); return false; }
		return true;
	}
	function dependent(field, name) { return dependent_names.has(name) || dependent_types.has(field?.options); }
	function active_tree() {
		const route = frappe.get_route() || [];
		return route[0] === "Tree" ? frappe.views?.trees?.[route[1]] : null;
	}
	async function silent_value(filter, value) {
		const change = filter.df.change, onchange = filter.df.onchange;
		filter.df.change = filter.df.onchange = undefined;
		try { await filter.set_value(value); }
		finally { filter.df.change = change; filter.df.onchange = onchange; }
	}
	async function clear_report_filters(report) {
		for (const filter of report.filters || []) if (dependent(filter.df, filter.df.fieldname)) await silent_value(filter, "");
	}
	function public_page() {
		const route = frappe.get_route() || [];
		return ["Form", "List"].includes(route[0]) && !!route[1] && !frappe.meta.get_docfield(route[1], "company");
	}
	const copy = value => {
		if (value instanceof Set) return new Set([...value].map(copy));
		if (value instanceof Map) return new Map([...value].map(([key, item]) => [key, copy(item)]));
		if (Array.isArray(value)) return value.map(copy);
		if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, copy(item)]));
		return value;
	};
	context.capture_session = session => {
		const snapshot = Object.fromEntries(Object.entries(session).filter(([, value]) => typeof value !== "function").map(([key, value]) => [key, copy(value)]));
		return () => {
			const request = session.request + 1, revision = session.revision + 1;
			Object.assign(session, copy(snapshot), {request, revision, loading: false});
			session.onchange("restored");
		};
	};
	async function refresh_report(report) {
		const result = report.refresh(true);
		// Frappe QueryReport's outer Promise only resolves on success; observe the
		// underlying request too so HTTP errors cannot leave company switching stuck.
		if (!report.last_ajax?.fail) return result;
		return Promise.race([result, new Promise((_, reject) => report.last_ajax.fail(reject))]);
	}
	function watch_tree_load(tree) {
		const widget = tree.tree;
		if (!widget?.get_nodes || !widget.root_node || widget.root_node.loaded) return;
		let ready, failed;
		tree._cf_ready = new Promise((resolve, reject) => { ready = resolve; failed = reject; });
		tree._cf_ready.catch(() => {}); // Initial native renders have no awaiting caller.
		const render = widget.on_node_render, get_nodes = widget.get_nodes;
		widget.on_node_render = function (node, ...args) {
			if (node.is_root && node.loaded) ready(true);
			return render?.call(this, node, ...args);
		};
		widget.get_nodes = function (value, is_root) {
			if (!is_root) return get_nodes.call(this, value, is_root);
			// Native Tree wraps its request in a resolve-only Promise. Observe the
			// request directly so a rejected root query also rejects the switch.
			return Promise.resolve().then(() => frappe.call({method: this.method, timeout: 30000,
				args: {...this.args, parent: value, is_root}})).then(response => {
				if (response.exc || response.message == null) throw new Error(__("公司数据加载失败"));
				this.on_get_node?.(response.message); return response.message;
			}).catch(error => { failed(error); return []; });
		};
	}
	function block_current_page(message = __("公司页面加载失败，请刷新页面后重试")) {
		const host = frappe.container?.page;
		if (!host || !document.createElement) return;
		host.classList.add("cf-company-recovery-required");
		if (host.querySelector(".cf-company-recovery")) return;
		const panel = document.createElement("div"); panel.className = "cf-company-recovery"; panel.setAttribute("role", "alert");
		const text = document.createElement("p"); text.textContent = message; panel.appendChild(text);
		const button = document.createElement("button"); button.className = "btn btn-primary"; button.textContent = __("刷新页面");
		button.onclick = () => window.location.reload(); panel.appendChild(button); host.appendChild(panel);
	}
	async function refresh_list(list) {
		list.last_args = null;
		// The native instance method is throttled and can return the previous
		// request's Promise. A company change must await its own new query.
		const Native = frappe.views?.ListView;
		return Native && list instanceof Native ? Native.prototype.refresh.call(list) : list.refresh();
	}
	async function set_list_company(list, company) {
		const filters = list.filter_area.get().filter(filter => filter[1] !== "company" && !dependent(frappe.meta.get_docfield(list.doctype, filter[1]), filter[1]));
		filters.push([list.doctype, "company", "=", company]);
		await list.filter_area.clear(false); await list.filter_area.set(filters); list.start = 0;
		return refresh_list(list);
	}
	function prepare_current(previous) {
		const adapter = active_adapter();
		if (adapter?.switch_company) {
			const snapshot = adapter.capture?.(previous);
			return {apply: company => adapter.switch_company(company), restore: () => adapter.restore ? adapter.restore(snapshot) : adapter.switch_company(previous)};
		}
		const route = [...(frappe.get_route() || [])];
		if (public_page() || is_workspace()) return {apply: () => true, restore: () => {}};
		if (route[0] === "Form") {
			const options = frappe.route_options;
			return {
				apply: async company => {
					frappe.route_options = {company: ["=", company]};
					transition.route = JSON.stringify(["List", route[1], "List"]);
					await frappe.set_route("List", route[1], "List");
					if (frappe.get_route()?.[0] !== "List" || frappe.get_route()?.[1] !== route[1]) return false;
					const list = window.cur_list;
					if (list?.doctype !== route[1]) return false;
					await list.init_promise;
					if (list.cash_flow_workbench) {
						const loaded = await list.cash_flow_workbench.refresh(false, company);
						return !!loaded && list.cash_flow_workbench.session.filters.company === company;
					}
					await set_list_company(list, company); return true;
				},
				restore: async () => { transition.route = JSON.stringify(route); frappe.route_options = options; await frappe.set_route(...route); },
			};
		}
		if (route[0] === "Tree" && active_tree()?.page?.fields_dict?.company) {
			const tree = active_tree();
			return {apply: company => context.sync_tree(tree, company), restore: () => context.sync_tree(tree, previous)};
		}
		if (route[0] === "query-report" && frappe.query_report?.get_filter("company", false)) {
			const report = frappe.query_report;
			const snapshot = report.filters.map(filter => [filter, copy(filter.df.fieldname === "company" ? (report._cf_working_company || previous) : filter.get_value())]);
			const prepared = Object.fromEntries(["prepared_report_name", "prepared_report_document", "prepared_report"].map(key => [key, report[key]]));
			const header = copy(report.statutory_header);
			return {
				apply: async company => {
					await clear_report_filters(report);
					report.prepared_report_name = report.prepared_report_document = null; report.prepared_report = false;
					report.previous_filters = null;
					const no_refresh = report._no_refresh; report._no_refresh = true;
					try { await report.get_filter("company").set_value(company, true); }
					finally { report._no_refresh = no_refresh; }
					await refresh_report(report); return true;
				},
				restore: async () => {
					for (const [filter, value] of snapshot) await silent_value(filter, value);
					Object.assign(report, prepared);
					report.previous_filters = null;
					report.statutory_header = header;
					await refresh_report(report);
				},
			};
		}
		if (route[0] === "List" && window.cur_list?.doctype === route[1] && window.cur_list?.filter_area) {
			const list = window.cur_list;
			const snapshot = copy(list.filter_area.get()).map(filter => filter[1] === "company" && filter[2] === "=" ? [...filter.slice(0, 3), list._cf_working_company || previous] : filter);
			const start = list.start;
			return {apply: async company => {await set_list_company(list, company); return true;}, restore: async () => {
				await list.filter_area.clear(false); await list.filter_area.set(snapshot); list.start = start; await refresh_list(list);
			}};
		}
		throw new Error(__("当前页面尚未准备好，请稍后再切换公司"));
	}
	context.change_company = async (company, { apply = true, guard = true, is_current = () => true, on_apply = () => {}, force = false } = {}) => {
		if (committing) return null;
		const token = ++revision, route = JSON.stringify(frappe.get_route());
		const verified = await validate(company);
		const current = () => token === revision && route === JSON.stringify(frappe.get_route()) && is_current();
		if (!current()) return null;
		if (!verified) { frappe.msgprint(__("请选择有权访问的非集团公司")); return null; }
		if (verified === context.get_company() && !force) return verified;
		if (guard && !(await may_switch())) return null;
		if (!current()) return null;
		const previous = context.get_company(), page = apply && is_finance() ? prepare_current(previous) : null;
		committing = binding = true; transition = {company: verified, route, cancelled: false};
		const view = current_view();
		if (page && view) view._cf_company_stale = true;
		let frozen = false;
		// The native router hides dialogs during the authorized Form -> List
		// transition. Dialog visibility only guards validation, not this phase.
		const still_current = () => !transition.cancelled && transition.route === JSON.stringify(frappe.get_route()) && token === revision;
		try {
			on_apply(); update_bar();
			if (apply && frappe.dom?.freeze) { frappe.dom.freeze(__("正在切换公司，请稍候…")); frozen = true; }
			const loaded = page ? await page.apply(verified) : true;
			if (!still_current()) return null;
			if (loaded === false) throw new Error(__("公司切换失败，已保留原公司"));
			remember(verified); mark_current(verified); return verified;
		} catch (error) {
			// Navigation cancels this application. Never restore into a different
			// report instance or navigate the user back to the old document.
			if (!still_current()) return null;
			transition.company = previous;
			try {
				if ((await page?.restore()) === false) throw new Error("restore failed");
				if (still_current() && view) view._cf_company_stale = false;
			}
			catch { if (still_current()) {block_current_page(__("原公司页面恢复失败，请刷新页面后重试")); frappe.msgprint(__("原公司页面恢复失败，请刷新页面后重试"));} }
			throw error;
		} finally {
			const cancelled = !still_current();
			committing = binding = false; transition = null; update_bar();
			if (frozen) frappe.dom.unfreeze();
			if (cancelled) context.bind_current();
		}
	};
	async function choices() { return (await frappe.call({ method: "china_finance.services.company_context.get_company_choices", args: {recent: JSON.stringify(recent_names())} })).message; }
	context.ensure_company = () => {
		if (ensuring) return ensuring;
		ensuring = (async () => {
			const route = JSON.stringify(frappe.get_route());
			const stored = context.get_company();
			if (stored) {
				const verified = await validate(stored);
				if (route !== JSON.stringify(frappe.get_route())) return null;
				if (verified) { remember(verified); return verified; }
				remember("");
			}
			const data = await choices();
			if (route !== JSON.stringify(frappe.get_route())) return null;
			if (!data.companies.length) { frappe.msgprint(__("没有可用的非集团公司，请联系管理员检查公司权限或公司设置")); return null; }
			if (data.companies.length === 1 && !data.has_more) { remember(data.companies[0].name); return data.companies[0].name; }
			return context.select_company(data);
		})().finally(() => { ensuring = null; });
		return ensuring;
	};
	context.select_company = (initial) => {
		if (selection) return selection;
		selection = new Promise(resolve => {
			let chosen = null, saving = false, applying = false, closed = false;
			const dialog = new frappe.ui.Dialog({ title: __("选择公司"), size: "small", fields: [
				{fieldname: "recent", fieldtype: "HTML"},
				{fieldname: "company", label: __("公司"), fieldtype: "Link", options: "Company", reqd: 1, default: context.default_company() || "", get_query: () => ({query: "china_finance.services.company_context.search_companies"})},
				{fieldname: "choices", fieldtype: "HTML"},
			] });
			const render_choices = (field, rows, heading) => {
				const wrapper = dialog.fields_dict?.[field]?.$wrapper?.get(0); if (!wrapper || closed) return;
				wrapper.replaceChildren(); if (!rows?.length) return;
				const label = document.createElement("p"); label.textContent = heading; wrapper.appendChild(label);
				for (const row of rows) {
					const button = document.createElement("button"); button.type = "button"; button.className = "btn btn-default btn-sm cf-company-choice";
					button.textContent = `${row.abbr ? row.abbr + " · " : ""}${row.name} · ${row.finance_enabled ? __("已启用中国财务") : __("尚未启用中国财务")}`;
					button.onclick = () => dialog.set_value("company", row.name); wrapper.appendChild(button);
				}
			};
			dialog.set_primary_action(__("确定"), async () => {
				const company = dialog.get_values()?.company; if (!company || saving) return;
				saving = true; dialog.get_primary_btn().prop("disabled", true);
				try {
					const result = await context.change_company(company, {is_current: () => dialog.display, on_apply: () => { applying = true; }});
					if (result) chosen = result;
					if (dialog.display && result) dialog.hide();
				} catch { if (dialog.display) frappe.msgprint(__("无法确认公司，请检查权限或网络后重试")); }
				finally { saving = false; applying = false; dialog.get_primary_btn().prop("disabled", false); if (!dialog.display) { selection = null; resolve(chosen); } }
			});
			dialog.onhide = () => { closed = true; if (applying) return; revision++; selection = null; resolve(chosen); };
			dialog.show();
			Promise.resolve(initial || choices()).then(data => {
				render_choices("recent", data.recent, __("最近使用")); render_choices("choices", data.companies, __("可用公司（也可输入名称或简称搜索）"));
			}).catch(() => { if (dialog.display) frappe.msgprint(__("无法读取公司列表，请稍后重试")); });
		});
		return selection;
	};
	function navigation_target(url) {
		let path; try { path = decodeURIComponent(url.pathname).replace(/\/$/, ""); } catch { return null; }
		return items().find(item => item.type === "Link" && (
			(item.link_type === "Report" && path === `/desk/query-report/${item.link_to}`) ||
			(item.link_type === "Tree" && path === `/desk/${frappe.router.slug(item.link_to)}/view/tree`) ||
			(item.link_type === "Page" && path === `/desk/${item.link_to}`) ||
			(item.link_type === "DocType" && [ `/desk/${frappe.router.slug(item.link_to)}`, `/desk/${frappe.router.slug(item.link_to)}/view/list` ].includes(path))
		));
	}
	context.route_for_company = async href => {
		const url = new URL(href, window.location.origin), company = context.get_company();
		if (!company || url.origin !== window.location.origin) return null;
		const target = navigation_target(url); if (!target) return null;
		if (target.link_type === "DocType") { await frappe.model.with_doctype(target.link_to); if (!frappe.meta.get_docfield(target.link_to, "company")) return null; }
		let old_company = url.searchParams.get("company");
		try { const parsed = JSON.parse(old_company); if (Array.isArray(parsed)) old_company = parsed[1]; } catch { /* Plain report company value. */ }
		if (old_company && old_company !== company) for (const name of [...url.searchParams.keys()]) if (dependent_names.has(name)) url.searchParams.delete(name);
		url.searchParams.set("company", target.link_type === "DocType" ? JSON.stringify(["=", company]) : company);
		url.searchParams.delete("sidebar"); return url.pathname + url.search + url.hash;
	};
	function navigate(href) {
		const url = new URL(href, window.location.origin);
		frappe.route_options = Object.fromEntries([...url.searchParams].map(([key, value]) => { try { return [key, JSON.parse(value)]; } catch { return [key, value]; } }));
		frappe.route_hash = url.hash; return frappe.set_route(url.pathname);
	}
	function current_view() {
		const route = frappe.get_route() || [];
		if (route[0] === "query-report" && frappe.query_report?.report_name === route[1]) return frappe.query_report;
		if (route[0] === "List" && window.cur_list?.doctype === route[1]) return window.cur_list;
	}
	function mark_current(company) {
		const view = current_view(); if (view) {view._cf_working_company = company; view._cf_company_stale = false;}
		frappe.container?.page?.classList?.remove("cf-company-recovery-required");
		frappe.container?.page?.querySelector?.(".cf-company-recovery")?.remove();
	}
	context.bind_current = () => {
		update_bar(); if (!is_finance()) return;
		const route = frappe.get_route() || [], view = current_view();
		if (transition) {if (view) view._cf_company_stale = true; return;}
		if (route[0] === "query-report" && (!frappe.query_report?.report_name || frappe.query_report.report_name === route[1])) context.bind_report(frappe.query_report);
		if (route[0] === "List" && window.cur_list?.doctype === route[1]) context.bind_list(window.cur_list);
		if (route[0] === "Tree") context.bind_tree(active_tree());
		if (!view?._cf_working_company || view._cf_company_initializing || (!view._cf_company_stale && view._cf_working_company === context.get_company()) || view._cf_resync || view.cash_flow_workbench) return;
		view._cf_resync = true;
		const state = view._china_inline_edit_state;
		const dirty = state?.pending || state?.changes?.size;
		const task = dirty
			? context.change_company(view._cf_working_company, {apply: false, guard: false})
			: context.change_company(context.get_company(), {force: true});
		const owns_page = () => current_view() === view && JSON.stringify(frappe.get_route()) === JSON.stringify(route);
		task.then(result => {if (!result && owns_page()) block_current_page();})
			.catch(() => {if (owns_page()) block_current_page();}).finally(() => {view._cf_resync = false;});
	};
	function tree_company(tree) {
		const route = frappe.get_route() || [];
		if (route[0] !== "Tree" || route[1] !== tree.doctype) return null;
		// A cached TreeFactory ignores route_options; consume its company ourselves.
		const option = frappe.route_options?.company;
		if (option) {
			delete frappe.route_options.company;
			if (typeof option === "string") return option;
			if (Array.isArray(option) && option[0] === "=") return option[1];
		}
		return tree._cf_company_target || working_company() || tree.page.fields_dict.company.get_value();
	}
	context.sync_tree = (tree, company = tree_company(tree)) => {
		const field = tree?.page?.fields_dict?.company;
		if (!field || !company) return Promise.resolve(false);
		if (tree._cf_company_sync && tree._cf_company_target === company) return tree._cf_company_sync;
		const request = tree._cf_company_request = (tree._cf_company_request || 0) + 1;
		tree._cf_company_target = company;
		const pending = (async () => {
			if (company !== working_company()) {
				try {
					if (!(await context.change_company(company, {apply: false}))) company = working_company();
				} catch {
					frappe.msgprint(__("无法确认公司，请检查权限或网络后重试"));
					company = working_company();
				}
			}
			const route = frappe.get_route() || [];
			if (!company || request !== tree._cf_company_request || route[0] !== "Tree" || route[1] !== tree.doctype) return false;
			tree._cf_company_target = company;
			if (field.get_value() !== company || tree.args.company !== company) {
				await field.set_value(company, tree.args.company !== company);
			}
			await tree._cf_ready;
			return field.get_value() === company && tree.args.company === company;
		})().catch(() => { frappe.msgprint(__("无法确认公司，请检查权限或网络后重试")); return false; });
		tree._cf_company_sync = pending;
		pending.finally(() => {
			if (request === tree._cf_company_request) { tree._cf_company_sync = null; tree._cf_company_target = null; }
		});
		return pending;
	};
	context.bind_tree = tree => {
		const field = tree?.page?.fields_dict?.company;
		if (!field || field._cf_company_bound) return;
		field._cf_company_bound = true;
		const render = tree.make_tree;
		tree.make_tree = function (...args) {
			const company = tree_company(tree);
			if (company && (company !== working_company() || field.get_value() !== company || tree.args.company !== company)) {
				return context.sync_tree(tree, company);
			}
			const result = render.apply(this, args);
			watch_tree_load(tree); return result;
		};
		const original = field.df.change;
		field.df.change = async function (...args) {
			const request = field._cf_company_request = (field._cf_company_request || 0) + 1;
			if (field.get_value() === working_company()) return original?.apply(this, args);
			if (tree._cf_company_sync) {
				// Native initial change events must wait for the same pending choice.
				if (field.get_value() === tree._cf_company_target) return;
				tree._cf_company_request++;
				tree._cf_company_sync = null; tree._cf_company_target = null;
			}
			if (binding) { await silent_value(field, working_company()); return; }
			try {
				const accepted = await context.change_company(field.get_value());
				if (request !== field._cf_company_request) return;
				if (accepted) return;
			}
			catch { frappe.msgprint(__("无法确认公司，请检查权限或网络后重试")); }
			if (request !== field._cf_company_request) return;
			await silent_value(field, working_company());
		};
		context.sync_tree(tree);
	};
	context.bind_report = report => {
		const filter = report?.get_filter("company", false); if (!filter || filter._cf_company_bound) return;
		filter._cf_company_bound = true;
		report._cf_working_company = filter.get_value() || context.get_company();
		const handler = filter.df.change || filter.df.onchange, hook = filter.df.change ? "change" : "onchange";
		filter.df[hook] = async function (...args) {
			const request = filter._cf_company_request = (filter._cf_company_request || 0) + 1;
			if (binding) {
				if (filter.get_value() === working_company()) return handler?.apply(this, args);
				await silent_value(filter, working_company()); return;
			}
			const previous = working_company(), value = filter.get_value();
			if (value === previous) return handler?.apply(this, args);
			let accepted = false;
			try { accepted = !!(value && await context.change_company(value)); }
			catch { frappe.msgprint(__("无法确认公司，请检查权限或网络后重试")); }
			if (request !== filter._cf_company_request) return;
			if (!accepted) { await silent_value(filter, working_company()); return; }
			return;
		};
		// A direct link's visible company is authoritative.
		if (filter.get_value() && filter.get_value() !== working_company()) {
			const pending = context.change_company(filter.get_value(), {apply: false})
				.catch(() => frappe.msgprint(__("无法确认公司，请检查权限或网络后重试")))
				.finally(() => {if (report._cf_company_initializing === pending) report._cf_company_initializing = null;});
			report._cf_company_initializing = pending;
		}
	};
	context.bind_list = list => {
		if (!list?.filter_area || list.cash_flow_workbench || list._cf_company_bound || !frappe.meta.get_docfield(list.doctype, "company")) return;
		list._cf_company_bound = true;
		list._cf_working_company = context.get_company();
		const area = list.filter_area, original = area.refresh_list_view;
		area.refresh_list_view = async function (...args) {
			if (!this.trigger_refresh) return original?.apply(this, args);
			const request = this._cf_company_request = (this._cf_company_request || 0) + 1;
			const matches = this.get().filter(filter => filter[1] === "company");
			if (matches.length !== 1 || matches[0][2] !== "=" || matches[0][3] === working_company()) return original?.apply(this, args);
			try { if (await context.change_company(matches[0][3])) return list.on_filter_change?.(); }
			catch { frappe.msgprint(__("无法确认公司，请检查权限或网络后重试")); }
			if (request !== this._cf_company_request) return;
			const filters = this.get().filter(filter => filter[1] !== "company");
			if (working_company()) filters.push([list.doctype, "company", "=", working_company()]);
			await this.clear(false); await this.set(filters);
		};
		if (frappe.utils?.debounce) area.debounced_refresh_list_view = frappe.utils.debounce(area.refresh_list_view.bind(area), 300);
		const matches = area.get().filter(filter => filter[1] === "company");
		if (matches.length === 1 && matches[0][2] === "=" && matches[0][3] !== working_company()) {
			context.change_company(matches[0][3], {apply: false}).catch(() => frappe.msgprint(__("无法确认公司，请检查权限或网络后重试")));
		}
	};

	document.addEventListener("focusin", () => {
		if (is_finance() && frappe.get_route()?.[0] === "query-report") context.bind_report(frappe.query_report);
	}, true);
	document.addEventListener("click", async event => {
		if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
		const link = event.target.closest?.("a[href]"); if (!link || link.hasAttribute("download")) return;
		const url = new URL(link.href, window.location.origin); if (url.origin !== window.location.origin || link.target === "_blank") return;
		try {
			if (url.pathname.replace(/\/$/, "") === workspace_path) {
				event.preventDefault(); event.stopImmediatePropagation();
				if (await context.ensure_company()) { await navigate(workspace_path); frappe.after_ajax(context.bind_current); } return;
			}
			if (link.target === "_blank" || !context.get_company() || !navigation_target(url)) return;
			if (!is_workspace() && !link.closest('.body-sidebar[data-title="China Finance"]')) return;
			event.preventDefault(); event.stopImmediatePropagation();
			const route = await context.route_for_company(link.href); await navigate(route || url.pathname + url.search + url.hash);
		} catch { frappe.msgprint(__("无法确认公司，请检查权限或网络后重试")); }
	}, true);
	function on_route() {
		if (transition && transition.route !== JSON.stringify(frappe.get_route())) transition.cancelled = true;
		frappe.after_ajax(async () => {
			context.bind_current();
			if (is_workspace() && !transition) { try { await context.ensure_company(); update_bar(); } catch { frappe.msgprint(__("无法读取公司列表，请稍后重试")); } }
		});
	}
	$(document).on("page-change", () => frappe.after_ajax(context.bind_current));
	$(document).on("app_ready", () => { frappe.router.on("change", on_route); on_route(); });
})();
