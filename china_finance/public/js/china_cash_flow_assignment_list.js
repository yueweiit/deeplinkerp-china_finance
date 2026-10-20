/* Cash-flow editing stays on the native DocType List route. */
(function (root, factory) {
	const api = factory(root);
	if (typeof module === "object" && module.exports) module.exports = api;
	if (!root.frappe) return;
	root.frappe.provide("china_finance.cash_flow_assignment_list");
	Object.assign(root.china_finance.cash_flow_assignment_list, api);
	root.frappe.listview_settings["China Cash Flow Assignment"] = {
		hide_name_filter: true,
		onload(listview) { api.mount(listview, root); },
	};
})(typeof window === "undefined" ? globalThis : window, function (root) {
	"use strict";
	// Desk supplies __; the fallback keeps the editor model usable in Node tests.
	const __ = (text, args = []) => typeof root.__ === "function" ? root.__(text, args)
		: text.replace(/\{(\d+)\}/g, (match, index) => args[index] ?? match);
	const METHOD = "china_finance.services.cash_flow_assignment.";
	const clone = (value) => JSON.parse(JSON.stringify(value));
	const editable = (doc) => doc.status === "Draft" && Number(doc.docstatus) === 0 && !!doc.can_write;
	const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
	function to_minor(value) {
		const match = String(value ?? "").trim().match(/^(-?)(\d+)(?:\.(\d{1,2}))?$/);
		if (!match) return null;
		const result = Number(match[2]) * 100 + Number((match[3] || "").padEnd(2, "0"));
		return Number.isSafeInteger(result) ? (match[1] ? -result : result) : null;
	}
	const amount = (minor) => (minor / 100).toFixed(2);
	const money = (value) => {
		const minor = to_minor(value);
		if (minor === null) return "—";
		if (typeof root.format_number === "function") return root.format_number(minor / 100, null, 2);
		return (minor / 100).toLocaleString(root.frappe?.boot?.lang?.replace(/_/g, "-") || undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	};
	function cash_groups(doc) {
		const groups = new Map();
		(doc.items || []).forEach((row, index) => {
			if (!groups.has(row.gl_entry)) groups.set(row.gl_entry, {
				gl_entry: row.gl_entry, cash_minor: to_minor(row.cash_amount), direction: row.cash_direction, rows: [],
			});
			groups.get(row.gl_entry).rows.push({ row, index });
		});
		return [...groups.values()];
	}
	function valid_option(doc, code, direction) {
		return (doc.options || []).find((option) => option.value === code && (option.direction === "all" || option.direction === direction));
	}
	function fill_suggestions(doc) {
		if (!editable(doc)) return 0;
		let count = 0;
		cash_groups(doc).forEach((group) => {
			const row = group.rows[0].row;
			if (group.rows.length !== 1 || row.cash_flow_row_code || !valid_option(doc, row.suggested_row_code, row.cash_direction)) return;
			row.cash_flow_row_code = row.suggested_row_code;
			row.cash_flow_row_label = valid_option(doc, row.suggested_row_code, row.cash_direction).label;
			count++;
		});
		return count;
	}
	function assign_project(doc, code, only_empty = true) {
		if (!editable(doc)) return 0;
		let count = 0;
		cash_groups(doc).forEach((group) => {
			const row = group.rows[0].row;
			const option = valid_option(doc, code, row.cash_direction);
			if (group.rows.length !== 1 || (only_empty && row.cash_flow_row_code) || !option || row.cash_flow_row_code === code) return;
			row.cash_flow_row_code = code;
			row.cash_flow_row_label = option.label;
			count++;
		});
		return count;
	}
	function split_item(doc, index) {
		if (!editable(doc) || !doc.items[index]) return null;
		const source = doc.items[index];
		const group = cash_groups(doc).find((entry) => entry.gl_entry === source.gl_entry);
		const used = group.rows.reduce((total, entry) => total + (to_minor(entry.row.assigned_amount) || 0), 0);
		const row = { ...source, name: "", cash_flow_row_code: "", cash_flow_row_label: "", remarks: "", assigned_amount: amount(Math.max(0, (group.cash_minor || 0) - used)) };
		doc.items.splice(index + 1, 0, row);
		return row;
	}
	function remove_item(doc, index) {
		if (!editable(doc) || !doc.items[index]) return false;
		const group = cash_groups(doc).find((entry) => entry.gl_entry === doc.items[index].gl_entry);
		if (group.rows.length === 1) return false;
		doc.items.splice(index, 1);
		return true;
	}
	function confirmation_issues(doc) {
		const issues = Array.isArray(doc.blocker_details)
			? doc.blocker_details.filter((entry) => entry.scope !== "items").map((entry) => entry.message)
			: [...(doc.blockers || [])];
		if (doc.status !== "Draft" || Number(doc.docstatus) !== 0) issues.push(__("已确认或已取消的单据只读"));
		if (!doc.can_confirm) issues.push(__("没有整单确认权限"));
		if (!(doc.items || []).length) issues.push(__("没有现金明细"));
		cash_groups(doc).forEach((group, number) => {
			let total = 0, invalid = false;
			group.rows.forEach(({ row }) => {
				if (!row.cash_flow_row_code) issues.push(__("第 {0} 笔尚未指定项目", [number + 1]));
				else if (!valid_option(doc, row.cash_flow_row_code, row.cash_direction)) issues.push(__("第 {0} 笔项目无效或收付方向不匹配", [number + 1]));
				const minor = to_minor(row.assigned_amount);
				if (minor === null || minor <= 0) { issues.push(__("第 {0} 笔指定金额须为正数且最多两位小数", [number + 1])); invalid = true; }
				else total += minor;
			});
			if (!invalid && total !== group.cash_minor) issues.push(__("第 {0} 笔指定合计与发生额相差 {1}", [number + 1, money(amount(Math.abs(total - (group.cash_minor || 0))))]));
		});
		return [...new Set(issues)];
	}
	function payload(doc) {
		return { name: doc.name, modified: doc.modified, remarks: doc.remarks || "", items: (doc.items || []).map((row) => ({
			name: row.name || "", gl_entry: row.gl_entry, cash_flow_row_code: row.cash_flow_row_code || "",
			assigned_amount: row.assigned_amount, remarks: row.remarks || "",
		})) };
	}
	class Session {
		constructor({ call, onchange = () => {} }) {
			this.call = call; this.onchange = onchange;
			this.filters = { company: "", from_date: "", to_date: "", status: "Draft", search: "", direction: "", start: 0, page_length: 20 };
			this.documents = []; this.dirty = new Set(); this.selected = new Set(); this.counts = {};
			this.results = []; this.total = 0; this.revision = 0; this.request = 0; this.busy = false; this.loading = false;
		}
		doc(name) { return this.documents.find((doc) => doc.name === name); }
		edit(name, change) {
			const doc = this.doc(name);
			if (this.busy || !doc || !editable(doc)) return false;
			const before = JSON.stringify(doc);
			change(doc);
			if (before === JSON.stringify(doc)) return false;
			this.dirty.add(name); this.revision++; this.onchange("edit", name); return true;
		}
		async load() {
			if (this.busy || this.dirty.size || !this.filters.company) return false;
			const request = ++this.request, revision = this.revision, filters = JSON.stringify(this.filters);
			this.loading = true; this.onchange("loading");
			try {
				const response = await this.call(METHOD + "get_cash_flow_assignment_list", clone(this.filters));
				if (request !== this.request || revision !== this.revision || filters !== JSON.stringify(this.filters)) return false;
				this.documents = clone(response.assignments || []); this.total = response.total || 0; this.counts = response.counts || {};
				this.selected.clear(); this.results = []; this.error = ""; this.onchange("loaded"); return true;
			} catch (error) {
				if (request === this.request && revision === this.revision && filters === JSON.stringify(this.filters)) {
					this.error = error?.message || __("加载失败，请重试"); this.onchange("error");
				}
				return false;
			} finally {
				if (request === this.request) { this.loading = false; this.onchange("loading"); }
			}
		}
		async change_filters(changes, confirm_discard) {
			if (this.busy) return false;
			if (Object.entries(changes).every(([key, value]) => this.filters[key] === value)) return false;
			if (this.dirty.size && !(await confirm_discard())) return false;
			this.dirty.clear(); this.selected.clear(); this.revision++; this.request++;
			Object.assign(this.filters, { start: 0 }, changes);
			// A failed new query must never present old-company rows under its heading.
			this.documents = []; this.total = 0; this.counts = {}; this.error = ""; this.results = [];
			this.onchange("filters");
			return this.load();
		}
		async process(names, action) {
			if (this.busy || !names.length) return [];
			const docs = names.map((name) => this.doc(name)).filter(Boolean);
			if (!docs.length) return [];
			this.busy = true; this.request++; this.loading = false; this.onchange("busy");
			try {
				const response = await this.call(METHOD + "process_cash_flow_assignments", { assignments: JSON.stringify(docs.map(payload)), action });
				this.results = docs.map((doc) => (response.results || []).find((result) => result.name === doc.name) || { name: doc.name, ok: false, error: __("服务器未返回此单处理结果，请刷新核对") });
				this.results.forEach((result) => {
					if (!result.ok || !result.document) return;
					const index = this.documents.findIndex((doc) => doc.name === result.name);
					const old_status = this.documents[index].status, new_status = result.document.status;
					if (old_status !== new_status) {
						if (Number.isFinite(this.counts[old_status])) this.counts[old_status]--;
						if (Number.isFinite(this.counts[new_status])) this.counts[new_status]++;
					}
					if (this.filters.status !== "All" && result.document.status !== this.filters.status) {
						this.documents.splice(index, 1); this.total = Math.max(0, this.total - 1);
					} else this.documents[index] = clone(result.document);
					this.dirty.delete(result.name); this.selected.delete(result.name);
				});
				return this.results;
			} catch (error) {
				this.results = docs.map((doc) => ({ name: doc.name, ok: false, error: error?.message || __("请求失败，编辑内容已保留，请核对服务器状态后重试") }));
				return this.results;
			} finally { this.busy = false; this.revision++; this.onchange("processed"); }
		}
	}
	return { Session, to_minor, cash_groups, fill_suggestions, assign_project, split_item, remove_item, confirmation_issues, payload, editable, escape, money, mount };
	function mount(listview, root) {
		if (listview.cash_flow_workbench) return listview.cash_flow_workbench;
		const frappe = root.frappe, $ = root.$;
		const context = root.china_finance?.company_context;
		let context_company = context?.get_company() || "", expanded = new Set(), month = "", notice = "";
		const own_route = ["List", "China Cash Flow Assignment", "List"];
		const active = () => { const route = frappe.get_route() || []; return route[0] === "List" && route[1] === own_route[1]; };
		const session = new Session({ call: (method, args) => frappe.xcall(method, args), onchange: (event, name) => {
			if (event === "edit") update_edit_feedback(name);
			else if (event === "loading") update_loading_feedback();
			else render();
		} });
		session.filters.company = context_company;
		// setup_view calls onload before BaseList.show performs its first fetch.
		// Patch this instance, so native refreshes never fetch/render a second list.
		listview.$frappe_list.children().hide();
		listview.page.page_form.hide();
		const $root = $(`<section class="cf-assignment-list" aria-label="${escape(__("现金流量指定工作区"))}"></section>`).appendTo(listview.$frappe_list);
		const editor = { session, refresh, render, discard, active };
		let view_scroll = { left: 0, top: 0 };
		listview.cash_flow_workbench = editor;
		listview.before_refresh = () => Promise.resolve();
		listview.refresh = () => refresh(false);
		listview.render_list = () => render();
		listview.process_document_refreshes = () => { listview.pending_document_refreshes = []; return refresh(false); };
		listview.page.set_primary_action(__("保存更改"), () => save(), "save");
		listview.page.set_secondary_action(__("刷新"), () => refresh(true), "refresh");
		listview.page.wrapper?.find(".actions-btn-group").hide();
		$root.on("click", "button[data-action]", click);
		$root.on("change", "[data-filter]", filter_change);
		$root.on("keydown", "[data-filter='search']", (event) => { if (event.key === "Enter") { event.preventDefault(); filter_change(event); } });
		$root.on("change", "[data-select]", selection_change);
		$root.on("input", "[data-edit]", field_change);
		$root.on("change", "[data-edit]", field_change);
		$root.on("input", "[data-horizontal-scroll]", horizontal_input);
		$root.on("keydown", "[data-horizontal-scroll]", horizontal_keydown);
		root.addEventListener("resize", () => { if (active()) layout_viewport(); });
		if (root.ResizeObserver && $root.get?.(0)) {
			const resize = new root.ResizeObserver(() => { if (active()) layout_viewport(); });
			resize.observe($root.get(0));
		}
		install_navigation_guard();
		frappe.realtime?.on("list_update", (data) => {
			if (data?.doctype !== own_route[1] || !active()) return;
			if (session.dirty.size || session.busy) { notice = __("服务器数据已更新；本地编辑已保留，保存后请刷新核对。"); render_toolbar(); return; }
			refresh(false);
		});
		render();
		return editor;

		function discard() {
			if (session.busy) { frappe.msgprint?.(__("正在处理整单，请等待结果后再离开。")); return Promise.resolve(false); }
			if (!session.dirty.size) return Promise.resolve(true);
			return new Promise((resolve) => {
				if (frappe.confirm) frappe.confirm(__("有尚未保存的指定内容。放弃修改并继续？"), () => resolve(true), () => resolve(false));
				else resolve(root.confirm(__("有尚未保存的指定内容。放弃修改并继续？")));
			});
		}
		function clear_edits() { session.dirty.clear(); session.selected.clear(); session.request++; session.revision++; }
		function option_company(value) {
			if (Array.isArray(value)) return value[0] === "=" ? value[1] : "";
			if (typeof value !== "string") return "";
			if (value.startsWith("[")) { try { return option_company(JSON.parse(value)); } catch { return ""; } }
			return value;
		}
		async function refresh(explicit) {
			if (!active() || session.busy) return false;
			const options = frappe.route_options || {};
			const selected_context = context?.get_company() || "";
			const company = option_company(options.company) || (selected_context !== context_company ? selected_context : session.filters.company);
			if (options.company) frappe.route_options = null;
			if (company !== session.filters.company) {
				const loaded = await session.change_filters({ company }, discard);
				if (session.filters.company === company) context_company = selected_context;
				return loaded;
			} else if (session.dirty.size) {
				if (!explicit) { notice = __("有未保存更改，自动刷新已暂停。"); render_toolbar(); return false; }
				if (!(await discard())) return false;
				clear_edits();
			}
			context_company = selected_context;
			notice = "";
			if (!session.filters.company) { render(); return false; }
			return session.load();
		}
		function disabled(value) { return value ? " disabled" : ""; }
		function button(action, label, extra = "", off = false, style = "btn-default") {
			return `<button type="button" class="btn ${style} btn-sm" data-action="${action}" ${extra}${disabled(off || session.busy)}>${label}</button>`;
		}
		function input(field, value, label, type = "text", extra = "") {
			return `<label class="cf-filter"><span>${label}</span><input class="form-control input-sm" type="${type}" data-filter="${field}" value="${escape(value)}" ${extra}${disabled(session.busy)}></label>`;
		}
		function render_toolbar() {
			const selected = [...session.selected].map((name) => session.doc(name)).filter(Boolean);
			$root.find("[data-toolbar]").html(`<div class="cf-toolbar"><div class="cf-tabs" role="tablist" aria-label="${escape(__("指定单状态"))}">${[["Draft", __("待处理")], ["Confirmed", __("已确认")], ["All", __("全部")]].map(([value, label]) => `<button type="button" class="cf-tab ${session.filters.status === value ? "active" : ""}" role="tab" aria-selected="${session.filters.status === value}" data-action="tab" data-status="${value}"${disabled(session.busy)}>${escape(label)}<span>${session.counts[value] ?? "—"}</span></button>`).join("")}</div>
			<span class="cf-selection-status">${selection_status()}</span><div class="cf-tools">${button("suggest", escape(__("填入系统建议")), "", !selected.some(editable))}${button("bulk", escape(__("统一指定项目")), "", !selected.some(editable))}${button("save", escape(save_label()), "", !session.dirty.size)}${button("confirm", escape(selected.length ? __("确认所选（{0}）", [selected.length]) : __("确认所选")), "", !selected.length, "btn-primary")}</div></div>
			<div class="cf-notice" role="status"${notice_text() ? "" : " hidden"}>${escape(notice_text())}</div>`);
			update_save_button();
			if (session.busy) $root.find("button, input, select, textarea").prop("disabled", true);
			else {
				$root.find("[data-readonly='1']").prop("disabled", true);
				$root.find("[data-filter]").prop("disabled", session.loading);
			}
			layout_viewport();
		}
		function selection_status() { return `${escape(__("已选 {0} 张", [session.selected.size]))}${session.dirty.size ? ` · <strong>${escape(__("{0} 张未保存", [session.dirty.size]))}</strong>` : ""}`; }
		function notice_text() { return session.loading ? __("正在加载指定单…") : session.busy ? __("正在逐单处理，请等待结果…") : notice; }
		function update_loading_feedback() {
			const text = notice_text();
			$root.find(".cf-notice").text(text).prop("hidden", !text);
			$root.find("[data-filter]").prop("disabled", session.loading || session.busy);
			$root.find("[data-action='previous']").prop("disabled", session.loading || session.busy || !session.filters.start);
			$root.find("[data-action='next']").prop("disabled", session.loading || session.busy || session.filters.start + session.filters.page_length >= session.total);
			layout_viewport();
		}
		function update_save_button() {
			listview.page.btn_primary?.prop("disabled", session.busy || !session.dirty.size).text(save_label());
			$root.find("[data-action='save']").prop("disabled", session.busy || !session.dirty.size).text(save_label());
		}
		function save_label() { return session.busy ? __("处理中…") : session.dirty.size ? __("保存更改（{0}）", [session.dirty.size]) : __("保存更改"); }
		function document_state(doc) {
			return session.dirty.has(doc.name) ? __("未保存") : doc.status === "Draft" ? (doc.assigned_by ? __("已保存 · 待确认") : __("待指定")) : status_label(doc.status);
		}
		function update_edit_feedback(name) {
			// Never replace inputs or buttons during input/blur/change: the next click
			// or Tab must still target the same attached element.
			$root.find(".cf-selection-status").html(selection_status()); update_save_button();
			const doc = session.doc(name); if (!doc) return;
			const $doc = $root.find(".cf-document").filter((_, element) => element.dataset.document === name);
			$doc.toggleClass("is-dirty", session.dirty.has(name));
			$doc.find("[data-document-state]").text(document_state(doc));
			$doc.find("[data-document-feedback]").html(document_feedback(doc));
			cash_groups(doc).forEach((group, index) => {
				$doc.find(`[data-summary-group='${index}']`).html(group_summary(group));
				$doc.find(`[data-origin-group='${index}']`).text(group_origin(group));
			});
		}
		function viewport_node() { return $root.find(".cf-table-scroll").get?.(0); }
		function capture_scroll() {
			const viewport = viewport_node();
			if (viewport) view_scroll = { left: Number(viewport.scrollLeft) || 0, top: Number(viewport.scrollTop) || 0 };
		}
		function update_horizontal_control() {
			const viewport = viewport_node(), control = $root.find("[data-horizontal-scroll]").get?.(0);
			if (!viewport || !control) return;
			const max = Math.max(0, (viewport.scrollWidth || 0) - (viewport.clientWidth || 0));
			viewport.scrollLeft = Math.min(max, Math.max(0, viewport.scrollLeft || 0));
			control.max = String(max); control.value = String(viewport.scrollLeft); control.disabled = max === 0;
			control.setAttribute?.("aria-valuetext", `${Math.round(viewport.scrollLeft)} / ${Math.round(max)}`);
			const width = control.clientWidth || viewport.clientWidth || 0;
			control.style?.setProperty("--cf-scroll-thumb-width", `${Math.min(width, Math.max(42, Math.round(width * (viewport.clientWidth || 0) / Math.max(1, viewport.scrollWidth || 0))))}px`);
			view_scroll = { left: viewport.scrollLeft, top: viewport.scrollTop || 0 };
		}
		function layout_viewport() {
			const viewport = viewport_node(); if (!viewport) return;
			const top = viewport.getBoundingClientRect?.().top;
			if (Number.isFinite(top) && root.innerHeight) {
				const height = (selector) => $root.find(selector).get?.(0)?.getBoundingClientRect?.().height || 0;
				const reserved = height(".cf-scroll-dock") + height(".cf-footer") + height(".cf-results") + 12;
				viewport.style?.setProperty("height", `${Math.max(140, Math.floor(root.innerHeight - top - reserved))}px`);
			}
			update_horizontal_control();
		}
		function horizontal_input(event) {
			const viewport = viewport_node(); if (!viewport) return;
			viewport.scrollLeft = Number(event.currentTarget.value) || 0; update_horizontal_control();
		}
		function horizontal_keydown(event) {
			const viewport = viewport_node(); if (!viewport) return;
			const keys = { ArrowRight: (viewport.scrollLeft || 0) + 80, ArrowLeft: (viewport.scrollLeft || 0) - 80, Home: 0, End: viewport.scrollWidth || 0 };
			if (!(event.key in keys)) return;
			event.preventDefault(); viewport.scrollLeft = keys[event.key]; update_horizontal_control();
		}
		function restore_viewport() {
			const viewport = viewport_node(); if (!viewport) return;
			const position = { ...view_scroll };
			// Set the final height before restoring scrollTop. The temporary CSS
			// height can otherwise clamp a deep position while replacing the table.
			layout_viewport();
			viewport.scrollLeft = position.left; viewport.scrollTop = position.top;
			viewport.addEventListener?.("scroll", update_horizontal_control, { passive: true });
			update_horizontal_control();
			// Font/layout and native sidebar changes can settle after this render.
			root.requestAnimationFrame?.(() => { if (active()) layout_viewport(); });
		}
		function render() {
			capture_scroll();
			const position = { ...view_scroll };
			const filters = session.filters;
			const drafts = session.documents.filter((doc) => doc.status === "Draft" && (doc.can_write || doc.can_confirm));
			$root.html(`<div data-toolbar></div><div class="cf-filters"><div class="cf-company" title="${escape(filters.company)}">${button("company", escape(filters.company || __("选择公司")), `title="${escape(__("切换公司"))}"`, !context?.select_company)}</div>${input("month", month, escape(__("记账月份")), "month")}${input("from_date", filters.from_date, escape(__("开始日期")), "date")}${input("to_date", filters.to_date, escape(__("结束日期")), "date")}${input("search", filters.search, escape(__("搜索摘要 / 凭证 / 科目")), "search", `placeholder="${escape(__("搜索摘要 / 凭证 / 科目"))}"`)}
			<label class="cf-filter"><span>${escape(__("收付方向"))}</span><select class="form-control input-sm" data-filter="direction" aria-label="${escape(__("收付方向"))}"${disabled(session.busy)}>${[["", __("全部方向")], ["收款", __("收款")], ["付款", __("付款")]].map(([value, label]) => `<option value="${value}"${filters.direction === value ? " selected" : ""}>${escape(label)}</option>`).join("")}</select></label></div>${session.error ? `<div class="cf-error" role="alert">${escape(session.error)}</div>` : ""}
			<div class="cf-table-scroll" tabindex="0" aria-label="${escape(__("现金流量指定明细，可横向滚动"))}"><table class="cf-cash-table"><colgroup><col style="width:32px"><col style="width:180px"><col style="width:96px"><col style="width:240px"><col style="width:58px"><col style="width:128px"><col style="width:280px"><col style="width:126px"><col style="width:224px"></colgroup><thead><tr><th class="cf-checkbox-cell"><input type="checkbox" data-select="all" aria-label="${escape(__("选择本页可处理指定单"))}"${drafts.length && drafts.every((doc) => session.selected.has(doc.name)) ? " checked" : ""}${disabled(!drafts.length || session.busy)}></th><th class="cf-identity">${escape(__("凭证 / 指定状态"))}</th><th>${escape(__("记账日期"))}</th><th>${escape(__("现金 / 银行科目"))}</th><th>${escape(__("方向"))}</th><th class="cf-number">${escape(__("现金发生额"))}</th><th>${escape(__("现金流量项目"))}</th><th class="cf-number">${escape(__("指定金额"))}</th><th>${escape(__("操作"))}</th></tr></thead>${session.documents.length ? session.documents.map(render_document).join("") : `<tbody><tr><td colspan="9" class="cf-empty">${escape(!filters.company ? __("选择公司后查看现金流量指定单。") : session.loading ? __("正在加载…") : __("当前条件下没有指定单"))}</td></tr></tbody>`}</table></div>
			<div class="cf-scroll-dock"><input type="range" data-horizontal-scroll="1" class="cf-scroll-range" min="0" max="0" value="0" step="1" aria-label="${escape(__("横向滚动指定明细"))}"></div>
			<div class="cf-footer"><span>${escape(__("第 {0}–{1} 张，共 {2} 张 · 每页 {3} 张", [session.total ? filters.start + 1 : 0, Math.min(filters.start + session.documents.length, session.total), session.total, filters.page_length]))}</span><div class="cf-tools">${button("previous", escape(__("上一页")), "", !filters.start || session.loading)}${button("next", escape(__("下一页")), "", filters.start + filters.page_length >= session.total || session.loading)}</div></div>
			<div class="cf-results" aria-live="polite"${session.results.length ? "" : " hidden"}>${session.results.length ? `<strong>${escape(__("逐单处理结果"))}</strong>${session.results.map((result) => `<div class="cf-result ${result.ok && result.document ? "success" : "failure"}"><span>${escape(result.name)}</span><span>${escape(result.ok && result.document ? (result.document.status === "Draft" ? __("已保存 · 待确认") : status_label(result.document.status)) : result.error || __("未收到有效单据，请刷新核对"))}</span></div>`).join("")}` : ""}</div>`);
			render_toolbar(); view_scroll = position; restore_viewport();
		}
		function status_label(status) { return ({ Draft: __("草稿"), Confirmed: __("已确认"), Cancelled: __("已取消") })[status] || status; }
		function render_document(doc) {
			const groups = cash_groups(doc), locked = !editable(doc), open = expanded.has(doc.name);
			const span = Math.max(1, doc.items?.length || 0) + (open ? 1 : 0);
			const identity = render_identity(doc, span);
			let first = true;
			const rows = groups.map((group, group_index) => group.rows.map(({ row, index }, allocation_index) => {
				const is_first = first; first = false;
				return `<tr class="cf-allocation-row" data-allocation-index="${index}">
					${is_first ? identity : ""}${allocation_index === 0 ? render_cash_leg(doc, group, group_index) : ""}
					${render_allocation(doc, row, index, locked, group)}<td>${render_row_actions(doc, group, index, locked, is_first, open)}</td></tr>`;
			}).join("")).join("");
			return `<tbody class="cf-document${session.dirty.has(doc.name) ? " is-dirty" : ""}${session.selected.has(doc.name) ? " is-selected" : ""}" data-document="${escape(doc.name)}">
				${rows || `<tr>${identity}<td colspan="7">${escape(__("没有现金明细"))}</td></tr>`}${open ? render_expanded_document(doc, groups, locked) : ""}</tbody>`;
		}
		function render_identity(doc, span) {
			const selectable = doc.status === "Draft" && (doc.can_write || doc.can_confirm);
			return `<td class="cf-checkbox-cell" rowspan="${span}"><input type="checkbox" data-select="${escape(doc.name)}" aria-label="${escape(__("选择指定单 {0}", [doc.name]))}"${session.selected.has(doc.name) ? " checked" : ""}${disabled(!selectable || session.busy)} data-readonly="${selectable ? "0" : "1"}"></td>
				<td class="cf-identity" rowspan="${span}" title="${escape(doc.name)} · ${escape(doc.voucher_summary || "")} · ${escape(__("修订 {0}", [doc.revision || 1]))}">${button("details", escape(doc.china_accounting_voucher || doc.name), `data-name="${escape(doc.name)}"`, false, "cf-voucher-link")}<span class="cf-document-state" data-document-state>${escape(document_state(doc))}</span></td>`;
		}
		function render_cash_leg(doc, group, group_index) {
			const row = group.rows[0].row, span = group.rows.length;
			return `<td rowspan="${span}">${escape(doc.posting_date || "—")}</td><td class="cf-account" rowspan="${span}">${escape(row.cash_account || "—")}</td>
				<td class="cf-direction" rowspan="${span}">${escape(group.direction === "收款" ? __("收款") : group.direction === "付款" ? __("付款") : group.direction)}</td><td class="cf-number" rowspan="${span}">${escape(money(row.cash_amount))}<span data-summary-group="${group_index}">${group_summary(group)}</span></td>`;
		}
		function render_row_actions(doc, group, index, locked, first, open) {
			const extra = `data-name="${escape(doc.name)}"`;
			return `<div class="cf-row-actions">
				${!locked ? button("split", escape(__("拆分")), `${extra} data-index="${index}"`, false, "cf-text-action") : ""}
				${!locked && group.rows.length > 1 ? button("remove", escape(__("移除")), `${extra} data-index="${index}"`, false, "cf-text-action") : ""}
				${first ? button("toggle", escape(open ? __("收起") : __("展开")), `${extra} aria-expanded="${open}"`, false, "cf-text-action") : ""}
				${first ? button("confirm-one", escape(__("确认整单")), `${extra} data-readonly="${doc.status === "Draft" && doc.can_confirm ? "0" : "1"}"`, doc.status !== "Draft" || !doc.can_confirm, "cf-text-action") : ""}</div>`;
		}
		function render_expanded_document(doc, groups, locked) {
			return `<tr class="cf-expanded"><td colspan="7"><div class="cf-expanded-summary"><span>${escape(__("凭证摘要：{0}", [doc.voucher_summary || "—"]))}</span><span data-document-feedback>${document_feedback(doc)}</span></div>
				${groups.map((group, group_index) => `<div class="cf-expanded-group"><span class="cf-muted">${escape(__("对方科目：{0}", [group.rows[0].row.counterpart_accounts || "—"]))} · ${escape(group.gl_entry)}</span><span class="cf-muted" data-origin-group="${group_index}">${escape(group_origin(group))}</span>
					${group.rows.map(({ row, index }) => `<label class="cf-note-label">${escape(__("第 {0} 项说明", [index + 1]))}<textarea rows="2" class="form-control input-sm cf-row-note" data-edit="remarks" data-name="${escape(doc.name)}" data-index="${index}" data-readonly="${locked ? "1" : "0"}" aria-label="${escape(__("{0} 第 {1} 项指定说明", [doc.name, index + 1]))}" placeholder="${escape(__("指定依据或拆分说明"))}"${disabled(locked || session.busy)}>${escape(row.remarks)}</textarea></label>`).join("")}</div>`).join("")}</td></tr>`;
		}
		function document_feedback(doc) {
			const issues = doc.status === "Draft" ? confirmation_issues(doc) : [];
			return `${escape(doc.name)} · ${escape(__("修订 {0}", [doc.revision || 1]))} · ${escape(document_state(doc))}${issues.length ? ` · <span class="cf-warning">${escape(issues.join("; "))}</span>` : ` · ${escape(!editable(doc) ? __("只读") : __("项目与金额已配平"))}`}`;
		}
		function group_summary(group) {
			const total = group.rows.reduce((sum, entry) => sum + (to_minor(entry.row.assigned_amount) || 0), 0);
			return total !== group.cash_minor ? `<span class="cf-warning cf-difference">${escape(__("差额 {0}", [money(amount((group.cash_minor || 0) - total))]))}</span>` : "";
		}
		function group_origin(group) {
			const row = group.rows[0].row;
			return group.rows.length > 1 ? __("拆分指定") : row.cash_flow_row_code ? __("已有指定") : row.suggested_row_code ? __("系统建议：{0}", [row.suggested_row_label || row.suggested_row_code]) : __("无系统建议");
		}
		function render_allocation(doc, row, index, locked, group) {
			const attrs = `data-name="${escape(doc.name)}" data-index="${index}" data-readonly="${locked ? "1" : "0"}"`;
			const options = (doc.options || []).filter((option) => option.direction === "all" || option.direction === row.cash_direction);
			const known = options.some((option) => option.value === row.cash_flow_row_code);
			return `<td>${locked ? `<span class="cf-readonly">${escape(row.cash_flow_row_label || row.cash_flow_row_code || __("未指定"))}</span>` : `<select class="form-control input-sm cf-project${row.cash_flow_row_code ? "" : " is-empty"}" data-edit="cash_flow_row_code" ${attrs} aria-label="${escape(__("{0} 第 {1} 项现金流量项目", [doc.name, index + 1]))}" title="${escape(group_origin(group))}"${disabled(session.busy)}><option value="">${escape(__("请选择现金流量项目"))}</option>${!known && row.cash_flow_row_code ? `<option selected value="${escape(row.cash_flow_row_code)}">${escape(__("{0}（请重新核对）", [row.cash_flow_row_label || row.cash_flow_row_code]))}</option>` : ""}${options.map((option) => `<option value="${escape(option.value)}"${option.value === row.cash_flow_row_code ? " selected" : ""}>${escape(option.label)}</option>`).join("")}</select>`}</td><td class="cf-number"><input type="number" class="form-control input-sm cf-assigned-amount" min="0.01" step="0.01" data-edit="assigned_amount" ${attrs} aria-label="${escape(__("{0} 第 {1} 项指定金额", [doc.name, index + 1]))}" value="${escape(row.assigned_amount)}"${disabled(locked || session.busy)}></td>`;
		}
		function selection_change(event) {
			if (session.busy) return;
			const target = event.currentTarget, name = target.dataset.select;
			const names = name === "all" ? session.documents.filter((doc) => doc.status === "Draft" && (doc.can_write || doc.can_confirm)).map((doc) => doc.name) : [name];
			names.forEach((id) => target.checked ? session.selected.add(id) : session.selected.delete(id)); render();
		}
		function field_change(event) {
			const target = event.currentTarget;
			session.edit(target.dataset.name, (doc) => {
				const row = doc.items[Number(target.dataset.index)];
				row[target.dataset.edit] = target.value;
				if (target.dataset.edit === "cash_flow_row_code") {
					row.cash_flow_row_label = valid_option(doc, target.value, row.cash_direction)?.label || "";
					$(target).toggleClass("is-empty", !target.value);
				}
			});
		}
		async function filter_change(event) {
			const field = event.currentTarget.dataset.filter, value = event.currentTarget.value;
			let changes = { [field]: value };
			if (field === "month") {
				const [year, number] = value.split("-").map(Number);
				changes = value ? { from_date: `${value}-01`, to_date: `${value}-${new Date(year, number, 0).getDate()}` } : { from_date: "", to_date: "" };
			}
			if (changes.from_date && (changes.to_date || session.filters.to_date) && changes.from_date > (changes.to_date || session.filters.to_date)) { frappe.msgprint(__("开始日期不能晚于结束日期")); render(); return; }
			if (field === "to_date" && value && session.filters.from_date > value) { frappe.msgprint(__("结束日期不能早于开始日期")); render(); return; }
			if (await session.change_filters(changes, discard)) month = field === "month" ? value : ["from_date", "to_date"].includes(field) ? "" : month;
			render();
		}
		async function click(event) {
			const target = event.currentTarget, action = target.dataset.action, name = target.dataset.name;
			if (target.disabled || session.busy) return;
			if (action === "company") {
				if (!(await discard())) return;
				const company = await context.select_company();
				if (company) { await session.change_filters({ company }, async () => true); context_company = company; }
			} else if (action === "tab") await session.change_filters({ status: target.dataset.status }, discard);
			else if (action === "previous" || action === "next") await session.change_filters({ start: session.filters.start + (action === "next" ? 1 : -1) * session.filters.page_length }, discard);
			else if (action === "toggle") { expanded.has(name) ? expanded.delete(name) : expanded.add(name); render(); }
			else if (action === "split" || action === "remove") { session.edit(name, (doc) => (action === "split" ? split_item : remove_item)(doc, Number(target.dataset.index))); render(); }
			else if (action === "suggest") {
				let count = 0;
				session.selected.forEach((id) => session.edit(id, (doc) => { count += fill_suggestions(doc); }));
				notice = __("已填入 {0} 笔空白且未拆分行的系统建议，请核对后保存。", [count]); render();
			} else if (action === "save") await save();
			else if (action === "bulk") show_bulk();
			else if (action === "details") show_details(name);
			else if (action === "confirm" || action === "confirm-one") show_confirmation(action === "confirm-one" ? [name] : [...session.selected]);
		}
		async function save() { if (!session.busy && session.dirty.size) await session.process([...session.dirty], "save"); }
		function show_bulk() {
			const docs = [...session.selected].map((name) => session.doc(name)).filter(editable);
			const options = [...new Map(docs.flatMap((doc) => doc.options || []).map((option) => [option.value, option])).values()];
			const dialog = new frappe.ui.Dialog({ title: __("统一指定现金流量项目"), fields: [
				{ fieldname: "project", label: __("现金流量项目"), fieldtype: "Select", options: [{ value: "", label: __("请选择") }, ...options], reqd: 1 },
				{ fieldname: "only_empty", label: __("仅填空白行，保留已有指定"), fieldtype: "Check", default: 1 },
				{ fieldtype: "HTML", options: `<div class="text-muted">${escape(__("仅应用于方向匹配且尚未拆分的现金行。更改后需保存。"))}</div>` },
			] });
			dialog.set_primary_action(__("应用到列表"), () => {
				const values = dialog.get_values(); if (!values?.project || session.busy) return;
				let count = 0;
				docs.forEach((doc) => session.edit(doc.name, (current) => { count += assign_project(current, values.project, !!values.only_empty); }));
				notice = __("已指定 {0} 笔现金明细；方向不匹配、拆分及保留的已有值已跳过。", [count]); dialog.hide(); render();
			}); dialog.show();
		}
		function show_confirmation(names) {
			const docs = names.map((name) => session.doc(name)).filter(Boolean);
			const checks = docs.map((doc) => ({ doc, issues: confirmation_issues(doc) }));
			const ready = checks.filter((check) => !check.issues.length);
			const dialog = new frappe.ui.Dialog({ title: __("确认现金流量指定"), size: "large", fields: [{ fieldtype: "HTML", options: `
				<p>${escape(__("按整张单据确认，包含单据内全部现金明细。{0} 张可处理，{1} 张需完善。", [ready.length, checks.length - ready.length]))}</p><div class="cf-confirm-list">${checks.map(({ doc, issues }) => `<div class="cf-confirm-row"><strong>${escape(doc.name)}</strong><span class="${issues.length ? "text-danger" : "text-success"}">${escape(issues.length ? issues.join("; ") : __("项目与金额校验通过"))}</span></div>`).join("")}</div><p class="text-muted">${escape(__("本次修改随整单确认提交。职责分离、权限及最新版本由服务器复核；确认后明细只读并纳入现金流量统计。"))}</p>` }] });
			dialog.set_primary_action(__("确认可处理的 {0} 张", [ready.length]), async () => { if (session.busy || !ready.length) return; dialog.hide(); await session.process(ready.map((check) => check.doc.name), "confirm"); });
			dialog.show(); dialog.get_primary_btn().prop("disabled", !ready.length);
		}
		function link(doctype, name) { return name ? `<a href="${escape(frappe.utils.get_form_link(doctype, name))}">${escape(name)}</a>` : "—"; }
		function show_details(name) {
			const doc = session.doc(name); if (!doc) return;
			const kv = (label, value) => `<div class="cf-detail-kv"><span>${label}</span><span>${value || "—"}</span></div>`;
			const dialog = new frappe.ui.Dialog({ title: __("凭证与指定详情"), fields: [{ fieldtype: "HTML", options: `<div class="cf-detail-body">
				${kv(escape(__("指定单")), escape(doc.name))}${kv(escape(__("公司")), escape(doc.company))}${kv(escape(__("状态 / 修订")), `${escape(status_label(doc.status))} / ${escape(doc.revision || 1)}`)}${kv(escape(__("记账日期")), escape(doc.posting_date))}${kv(escape(__("凭证摘要")), escape(doc.voucher_summary))}${kv(escape(__("中国会计凭证")), link("China Accounting Voucher", doc.china_accounting_voucher))}${kv(escape(__("来源单据")), doc.source_doctype ? link(doc.source_doctype, doc.source_name) : "—")}
				<hr>${kv(escape(__("指定人")), escape(doc.assigned_by))}${kv(escape(__("指定时间")), escape(doc.assigned_on))}${kv(escape(__("确认人")), escape(doc.confirmed_by))}${kv(escape(__("确认时间")), escape(doc.confirmed_on))}${kv(escape(__("取消人")), escape(doc.cancelled_by))}${kv(escape(__("取消时间")), escape(doc.cancelled_on))}${kv(escape(__("取消原因")), escape(doc.cancellation_reason))}
				<hr><label>${escape(__("整单指定说明"))}<textarea class="form-control" rows="4" data-document-note${disabled(!editable(doc))}>${escape(doc.remarks)}</textarea></label><p class="text-muted">${escape(editable(doc) ? __("说明输入后暂存于列表，请点击保存更改。") : __("此单据只读。"))}</p>${doc.blockers?.length ? `<div class="text-danger">${doc.blockers.map(escape).join("<br>")}</div>` : ""}</div>` }] });
			dialog.$wrapper.addClass("cf-list-details");
			dialog.$wrapper.on("input", "[data-document-note]", (event) => session.edit(name, (current) => { current.remarks = event.currentTarget.value; }));
			dialog.set_primary_action(__("打开原始指定单"), async () => { if (!(await discard())) return; clear_edits(); dialog.hide(); frappe.set_route("Form", own_route[1], name); });
			dialog.show();
		}
		function install_navigation_guard() {
			const original_route = frappe.set_route;
			let bypass = false, navigating = false;
			frappe.set_route = function (...args) {
				if (bypass || !active() || (!session.dirty.size && !session.busy)) return original_route.apply(this, args);
				if (navigating) return Promise.resolve();
				navigating = true;
				return discard().then((ok) => { if (!ok) return; clear_edits(); bypass = true; try { return original_route.apply(frappe, args); } finally { bypass = false; } }).finally(() => { navigating = false; });
			};
			root.addEventListener("beforeunload", (event) => { if (active() && (session.dirty.size || session.busy)) { event.preventDefault(); event.returnValue = ""; } });
			// History back/forward bypasses set_route. Restore this cached list while
			// asking, then repeat the requested route only after an explicit discard.
			frappe.router?.on("change", () => {
				if (active() || navigating || (!session.dirty.size && !session.busy)) return;
				const requested = [...(frappe.get_route() || [])]; navigating = true;
				bypass = true; original_route.apply(frappe, [own_route]); bypass = false;
				discard().then((ok) => { if (ok) { clear_edits(); bypass = true; original_route.apply(frappe, [requested]); bypass = false; } }).finally(() => { navigating = false; });
			});
		}
	}
});
