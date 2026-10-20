/* global china_finance */

frappe.query_reports["China Voucher Ledger"] = {
	filters: [
		{
			fieldname: "receipt_import",
			label: __("回单批次"),
			fieldtype: "Link",
			options: "China Bank Receipt Import",
		},
		{
			fieldname: "company",
			label: __("公司"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
		},
		{
			fieldname: "period_preset",
			label: __("凭证日期"),
			fieldtype: "Select",
			options: ["", "本月凭证", "上月凭证", "近三个月", "本年凭证"],
			default: "本月凭证",
			on_change: (report) => {
				const preset = report.get_filter_value("period_preset");
				if (preset) set_quick_period(report, preset);
			},
		},
		{ fieldname: "from_date", label: __("起始日期"), fieldtype: "Date", reqd: 1 },
		{ fieldname: "to_date", label: __("截止日期"), fieldtype: "Date", reqd: 1 },
		{ fieldname: "voucher_word", label: __("凭证字号"), fieldtype: "Data" },
		{
			fieldname: "voucher_status",
			label: __("状态"),
			fieldtype: "Select",
			options: ["全部有效凭证", "未记账", "待记账", "已记账", "已冲销"],
			default: "全部有效凭证",
		},
		{ fieldname: "accounting_period", label: __("会计期间"), fieldtype: "Data" },
		{
			fieldname: "source_doctype",
			label: __("来源类型"),
			fieldtype: "Select",
			options: ["", "Journal Entry", "Payment Entry", "Period Closing Voucher"],
		},
		{ fieldname: "source_name", label: __("来源单据"), fieldtype: "Data" },
		{ fieldname: "voucher_number", label: __("凭证编号"), fieldtype: "Data" },
		{ fieldname: "account", label: __("科目"), fieldtype: "Link", options: "Account" },
		{
			fieldname: "party_type",
			label: __("往来类型"),
			fieldtype: "Select",
			options: ["", "Customer", "Supplier", "Employee", "Shareholder"],
		},
		{ fieldname: "party", label: __("往来单位"), fieldtype: "Data" },
		{ fieldname: "search_text", label: __("关键词"), fieldtype: "Data" },
	],
	tree: false,
	get_datatable_options(datatable_options) {
		// Keep one fixed width for each column so every row stays aligned.
		datatable_options.layout = "fixed";
		datatable_options.checkboxColumn = true;
		datatable_options.getEditor = (...args) =>
			create_inline_voucher_editor(frappe.query_report, ...args);
		return datatable_options;
	},
	after_datatable_render(datatable) {
		china_finance.datatable_layout.bind_full_width(frappe.query_report, datatable);
	},
	formatter(value, row, column, data, default_formatter) {
		const formatted = default_formatter(value, row, column, data);
		if (
			column.fieldname === "voucher_status" &&
			data?.voucher_status !== undefined &&
			data?.voucher_status !== null
		) {
			const status = {
				0: [__("未记账"), "orange"],
				3: [__("待记账"), "blue"],
				1: [__("已记账"), "green"],
				2: [__("已冲销"), "red"],
			}[data.voucher_status];
			if (!status) return "";
			return `<span class="indicator-pill no-indicator-dot china-voucher-status-text ${status[1]}">${status[0]}</span>`;
		}
		if (column.fieldname === "statutory_number" && data?.source_doctype && data?.source_name) {
			const route = frappe.utils.get_form_link(data.source_doctype, data.source_name);
			return `<a href="${route}" class="china-voucher-link" data-source-doctype="${encodeURIComponent(
				data.source_doctype
			)}" data-source-name="${encodeURIComponent(data.source_name)}">${formatted}</a>`;
		}
		if (column.fieldname === "account") {
			const label = frappe.utils.escape_html(data?.account_label || value || "");
			const title = frappe.utils.escape_html(
				data?.editable_account
					? __("点击选择兼容科目")
					: data?.inline_edit_reason || __("该行不能直接修改")
			);
			const dirty = data?._inline_account_dirty ? " is-dirty" : "";
			const editable = data?.editable_account ? " is-editable" : " is-read-only";
			return `<span class="china-inline-account${editable}${dirty}" title="${title}">${label}${
				data?.editable_account ? '<span class="china-inline-account__arrow">▾</span>' : ""
			}</span>`;
		}
		if (column.fieldname === "remarks") {
			const label = frappe.utils.escape_html(value || "");
			const empty_label = data?.editable_summary
				? `<span class="text-muted">${__("点击填写摘要")}</span>`
				: "";
			const title = frappe.utils.escape_html(
				data?.editable_summary
					? data?.inline_summary_edit_hint || __("点击修改摘要")
					: data?.inline_summary_edit_reason || __("该行不能直接修改摘要")
			);
			const dirty = data?._inline_summary_dirty ? " is-dirty" : "";
			const editable = data?.editable_summary ? " is-editable" : " is-read-only";
			const edit_icon = data?.editable_summary
				? `<span class="china-inline-summary__edit">${frappe.utils.icon(
						"edit",
						"xs"
				  )}</span>`
				: "";
			return `<span class="china-inline-summary${editable}${dirty}" title="${title}">${
				label || empty_label
			}${edit_icon}</span>`;
		}
		if (["debit", "credit"].includes(column.fieldname) && data?.edit_source_name) {
			const editable = data.editable_amount ? " is-editable" : " is-read-only";
			const dirty = data[`_inline_${column.fieldname}_dirty`] ? " is-dirty" : "";
			const title = frappe.utils.escape_html(
				data.editable_amount
					? __("点击修改金额")
					: data.inline_amount_edit_reason || __("该行不能直接修改金额")
			);
			return `<span class="china-inline-amount${editable}${dirty}" title="${title}">${formatted}</span>`;
		}
		if (
			column.fieldname === "source_action" &&
			data?.is_voucher_first_row &&
			data?.edit_source_name
		) {
			return render_inline_voucher_actions(frappe.query_report, data);
		}
		if (
			!data ||
			!["posting_date", "statutory_number", "accounting_period"].includes(column.fieldname)
		) {
			return formatted;
		}
		const index = Number.isInteger(row?._index)
			? row._index
			: frappe.query_report.data?.indexOf(data);
		const previous = index > 0 ? frappe.query_report.data?.[index - 1] : null;
		if (previous && previous.voucher_snapshot === data.voucher_snapshot) return "";
		return formatted;
	},
	onload(report) {
		frappe.require("/assets/china_finance/js/voucher_numbering.js", () =>
			china_finance.voucher_numbering.bind_report(report)
		);
		frappe.require("/assets/china_finance/js/voucher_preparation.js", () =>
			china_finance.preparation.bind_report(report)
		);
		report.page.wrapper.addClass("china-voucher-ledger-report");
		ensure_voucher_ledger_styles();
		report.page.wrapper.on("click", ".china-voucher-link", (event) => {
			event.preventDefault();
			const source_doctype = decodeURIComponent(event.currentTarget.dataset.sourceDoctype);
			const source_name = decodeURIComponent(event.currentTarget.dataset.sourceName);
			frappe.set_route("Form", source_doctype, source_name);
		});
		bind_inline_voucher_actions(report);
		wrap_report_refresh_for_inline_edits(report);
		report.page.wrapper.on("click", ".china-voucher-snapshot-link", (event) => {
			event.preventDefault();
			const snapshot_name = decodeURIComponent(event.currentTarget.dataset.snapshotName);
			frappe.set_route("Form", "China Accounting Voucher", snapshot_name);
		});
		if (!report.get_filter_value("company")) {
			report.set_filter_value(
				"company",
				window.china_finance?.company_context?.default_company() ||
					frappe.defaults.get_user_default("Company")
			);
		}
		if (!report.get_filter_value("from_date") || !report.get_filter_value("to_date")) {
			const today = frappe.datetime.get_today();
			report.set_filter_value({
				period_preset: "本月凭证",
				from_date: frappe.datetime.month_start(today),
				to_date: frappe.datetime.month_end(today),
			});
		}
	},
};

function get_inline_edit_state(report) {
	return report?._china_inline_edit_state || null;
}

function render_inline_voucher_actions(report, data) {
	const state = get_inline_edit_state(report);
	const active = state?.voucher_key === data.edit_voucher_key;
	const disabled = active && state.pending ? " disabled" : "";
	const source_doctype = encodeURIComponent(data.edit_source_doctype);
	const source_name = encodeURIComponent(data.edit_source_name);
	const common = `data-source-doctype="${source_doctype}" data-source-name="${source_name}" data-voucher-key="${frappe.utils.escape_html(
		data.edit_voucher_key
	)}"`;
	const buttons = [
		`<button type="button" class="btn btn-xs btn-default china-inline-open-source" ${common}>${__(
			"打开原单"
		)}</button>`,
	];
	if (active && state.changes.size) {
		buttons.push(
			`<button type="button" class="btn btn-xs btn-primary china-inline-save" ${common}${
				state.confirming ? " hidden" : ""
			}${disabled}>${__("保存修改")}</button>`
		);
		buttons.push(
			`<button type="button" class="btn btn-xs btn-danger china-inline-confirm" ${common}${
				state.confirming ? "" : " hidden"
			}${disabled}>${__("确认更正")}</button>`
		);
		buttons.push(
			`<button type="button" class="btn btn-xs btn-default china-inline-cancel" ${common}${disabled}>${__(
				"取消"
			)}</button>`
		);
	}
	return `<div class="china-inline-actions">${buttons.join("")}</div>`;
}

function create_inline_voucher_editor(
	report,
	col_index,
	row_index,
	value,
	parent,
	column,
	row,
	data
) {
	if (column?.id === "account") {
		return create_inline_account_editor(report, value, parent, data);
	}
	if (column?.id === "remarks") {
		return create_inline_summary_editor(report, value, parent, data);
	}
	if (["debit", "credit"].includes(column?.id)) {
		return create_inline_amount_editor(report, value, parent, data, column.id);
	}
	return false;
}

function create_inline_account_editor(report, value, parent, data) {
	if (!data?.editable_account) {
		show_inline_notice(
			report,
			data?.inline_edit_reason || __("该行不能直接修改科目"),
			"orange",
			data?.inline_bank_transaction
		);
		return false;
	}

	const state = get_inline_edit_state(report);
	if (state?.voucher_key && state.voucher_key !== data.edit_voucher_key) {
		show_inline_notice(report, __("请先保存或取消当前凭证的修改，再编辑另一张凭证"), "orange");
		return false;
	}

	let initializing = true;
	const control = frappe.ui.form.make_control({
		df: {
			fieldname: "inline_account",
			fieldtype: "Link",
			options: "Account",
			label: __("科目"),
			only_select: 1,
			get_query: () => ({
				query: "china_finance.services.source_voucher_edit.get_compatible_accounts",
				filters: {
					source_doctype: data.edit_source_doctype,
					source_name: data.edit_source_name,
					edit_key: data.edit_key,
				},
			}),
		},
		parent,
		render_input: true,
	});
	control.toggle_label(false);
	control.toggle_description(false);
	// The report already constrains the query to the editable source field. Do
	// not append Frappe's technical "Filtered by" row to the account choices.
	control.get_filter_description = async () => null;
	const destroy_dropdown = bind_inline_account_dropdown(control, parent);
	keep_inline_editor_space_local(control);
	control.df.change = () => {
		if (!initializing)
			window.setTimeout(() => report.datatable?.cellmanager?.deactivateEditing(), 0);
	};

	return {
		initValue(initial_value) {
			initializing = true;
			initialize_inline_control(control, initial_value);
			initializing = false;
			control.$input?.select();
			control.on_input({ target: { value: "" } });
		},
		getValue() {
			destroy_dropdown();
			return control.get_value();
		},
		setValue(new_value) {
			control.set_value(new_value);
			stage_inline_account_change(report, data, new_value);
		},
	};
}

function create_inline_summary_editor(report, value, parent, data) {
	if (!data?.editable_summary) {
		show_inline_notice(
			report,
			data?.inline_summary_edit_reason || __("该行不能直接修改摘要"),
			"orange",
			data?.inline_bank_transaction
		);
		return false;
	}

	const state = get_inline_edit_state(report);
	if (state?.voucher_key && state.voucher_key !== data.edit_voucher_key) {
		show_inline_notice(report, __("请先保存或取消当前凭证的修改，再编辑另一张凭证"), "orange");
		return false;
	}

	const control = frappe.ui.form.make_control({
		df: {
			fieldname: "inline_summary",
			fieldtype: "Data",
			label: __("摘要"),
			length: 500,
		},
		parent,
		render_input: true,
	});
	control.toggle_label(false);
	control.toggle_description(false);
	control.$input?.attr("maxlength", 500);
	// Data controls normally write their value back after 500 ms of idle input.
	// DataTable owns this temporary value, so commit only when editing ends.
	control.change = () => {};
	keep_inline_editor_space_local(control);

	return {
		initValue(initial_value) {
			initialize_inline_control(control, initial_value || "");
			control.$input?.select();
		},
		getValue() {
			return control.get_value();
		},
		setValue(new_value) {
			control.set_value(new_value);
			stage_inline_summary_change(report, data, new_value);
		},
	};
}

function create_inline_amount_editor(report, value, parent, data, field) {
	if (!data?.editable_amount) {
		show_inline_notice(
			report,
			data?.inline_amount_edit_reason || __("该行不能直接修改金额"),
			"orange",
			data?.inline_bank_transaction
		);
		return false;
	}
	const state = get_inline_edit_state(report);
	if (state?.voucher_key && state.voucher_key !== data.edit_voucher_key) {
		show_inline_notice(report, __("请先保存或取消当前凭证的修改，再编辑另一张凭证"), "orange");
		return false;
	}
	const control = frappe.ui.form.make_control({
		df: {
			fieldname: `inline_${field}`,
			fieldtype: "Currency",
			label: field === "debit" ? __("借方") : __("贷方"),
			options: "currency",
			precision: data.amount_precision,
		},
		parent,
		render_input: true,
	});
	control.get_doc = () => ({ currency: data.currency });
	control.toggle_label(false);
	control.toggle_description(false);
	control.change = () => {};
	keep_inline_editor_space_local(control);
	return {
		initValue() {
			initialize_inline_control(control, Number(data[field] || 0));
			control.$input?.select();
		},
		getValue() {
			if (!String(control.$input.val()).trim()) return 0;
			const amount = control.get_value();
			if (typeof amount !== "number" || !Number.isFinite(amount)) {
				frappe.throw(__("请输入有效金额"));
			}
			return amount;
		},
		setValue(new_value) {
			stage_inline_amount_change(report, data, field, new_value);
		},
	};
}

function initialize_inline_control(control, value) {
	// These controls are temporary DataTable editors, not document fields. Seed
	// them directly so opening an Account cell never waits for link validation.
	control.value = value;
	control.last_value = value;
	control.label = value;
	if (control.set_input_value) control.set_input_value(value);
	else control.$input?.val(value);
	control.set_focus();
}

function keep_inline_editor_space_local(control) {
	control.$input?.on("keydown.china_inline_voucher_editor", (event) => {
		const key_event = event.originalEvent || event;
		if (key_event.key === " " || key_event.code === "Space" || key_event.keyCode === 32) {
			// Let the input insert the space, but do not let page shortcuts scroll.
			event.stopPropagation();
		}
	});
}

function bind_inline_account_dropdown(control, parent) {
	const dropdown = control.awesomplete?.ul;
	const input = control.input;
	const editing_cell = parent?.closest?.(".dt-cell");
	if (!dropdown || !input || !editing_cell) return () => {};

	let active = false;
	let destroyed = false;
	let position_frame = null;
	let dropdown_observer = null;

	const reset_style = () => {
		dropdown.classList.remove("china-inline-account-options");
		for (const property of [
			"position",
			"top",
			"right",
			"bottom",
			"left",
			"width",
			"max-height",
			"z-index",
		]) {
			dropdown.style.removeProperty(property);
		}
	};

	const position_dropdown = () => {
		position_frame = null;
		if (!active || destroyed || !input.isConnected || !dropdown.isConnected) return;

		const input_rect = input.getBoundingClientRect();
		const viewport_width = document.documentElement.clientWidth;
		const viewport_height = document.documentElement.clientHeight;
		const edge = 8;
		const gap = 2;
		const width = Math.min(input_rect.width, Math.max(0, viewport_width - edge * 2));
		const left = Math.min(
			Math.max(input_rect.left, edge),
			Math.max(edge, viewport_width - width - edge)
		);
		const space_below = Math.max(0, viewport_height - input_rect.bottom - edge - gap);
		const space_above = Math.max(0, input_rect.top - edge - gap);
		const desired_height = Math.min(300, dropdown.scrollHeight || 300);
		const open_above =
			space_below < Math.min(desired_height, 160) && space_above > space_below;
		const available_height = open_above ? space_above : space_below;

		dropdown.classList.add("china-inline-account-options");
		dropdown.style.setProperty("position", "fixed", "important");
		dropdown.style.setProperty("left", `${left}px`, "important");
		dropdown.style.setProperty("right", "auto", "important");
		dropdown.style.setProperty("width", `${width}px`, "important");
		dropdown.style.setProperty(
			"max-height",
			`${Math.min(300, available_height)}px`,
			"important"
		);
		dropdown.style.setProperty("z-index", "1060", "important");
		if (open_above) {
			dropdown.style.setProperty("top", "auto", "important");
			dropdown.style.setProperty(
				"bottom",
				`${viewport_height - input_rect.top + gap}px`,
				"important"
			);
		} else {
			dropdown.style.setProperty("top", `${input_rect.bottom + gap}px`, "important");
			dropdown.style.setProperty("bottom", "auto", "important");
		}
	};

	const schedule_position = () => {
		if (!active || destroyed || position_frame !== null) return;
		position_frame = window.requestAnimationFrame(position_dropdown);
	};

	const on_document_scroll = (event) => {
		const target = event.target;
		if (target === dropdown || (target instanceof Node && dropdown.contains(target))) return;
		control.awesomplete.close();
	};

	const stop_overlay = () => {
		if (position_frame !== null) {
			window.cancelAnimationFrame(position_frame);
			position_frame = null;
		}
		active = false;
		dropdown_observer?.disconnect();
		dropdown_observer = null;
		document.removeEventListener("scroll", on_document_scroll, true);
		window.removeEventListener("resize", schedule_position);
		reset_style();
	};

	const start_overlay = () => {
		stop_overlay();
		active = true;
		dropdown_observer = new MutationObserver(schedule_position);
		dropdown_observer.observe(dropdown, { childList: true, subtree: true });
		document.addEventListener("scroll", on_document_scroll, true);
		window.addEventListener("resize", schedule_position, { passive: true });
		schedule_position();
	};

	const destroy = () => {
		if (destroyed) return;
		destroyed = true;
		stop_overlay();
		editing_cell_observer.disconnect();
		control.$input.off(".china_inline_account_dropdown");
	};

	const editing_cell_observer = new MutationObserver(() => {
		if (!editing_cell.classList.contains("dt-cell--editing")) destroy();
	});
	editing_cell_observer.observe(editing_cell, { attributes: true, attributeFilter: ["class"] });
	control.$input.on("awesomplete-open.china_inline_account_dropdown", start_overlay);
	control.$input.on("awesomplete-close.china_inline_account_dropdown", stop_overlay);

	return destroy;
}

function account_display_label(account, company) {
	const suffix = company ? ` - ${company}` : "";
	return suffix && account.endsWith(suffix) ? account.slice(0, -suffix.length) : account;
}

function inline_change_key(field, edit_key) {
	return `${field}:${edit_key}`;
}

function ensure_inline_edit_state(report, data) {
	let state = get_inline_edit_state(report);
	if (state) return state;
	state = {
		voucher_key: data.edit_voucher_key,
		source_doctype: data.edit_source_doctype,
		source_name: data.edit_source_name,
		modified: data.edit_source_modified,
		changes: new Map(),
		originals: new Map(),
		confirming: false,
		pending: false,
	};
	report._china_inline_edit_state = state;
	return state;
}

function stage_inline_account_change(report, data, account) {
	if (!account) return;
	const state = ensure_inline_edit_state(report, data);
	const state_key = inline_change_key("account", data.edit_key);
	if (!state.originals.has(state_key)) {
		state.originals.set(state_key, {
			field: "account",
			edit_key: data.edit_key,
			value: data.account,
			label: data.account_label,
		});
	}
	const original = state.originals.get(state_key);
	if (account === original.value) state.changes.delete(state_key);
	else
		state.changes.set(state_key, {
			field: "account",
			edit_key: data.edit_key,
			value: account,
		});
	state.confirming = false;

	const label = account_display_label(account, report.get_filter_value("company"));
	for (const report_row of report.data || []) {
		if (
			report_row.edit_voucher_key !== state.voucher_key ||
			report_row.edit_key !== data.edit_key
		)
			continue;
		report_row.account = account;
		report_row.account_label = label;
		report_row._inline_account_dirty = state.changes.has(state_key);
	}
	if (!state.changes.size) report._china_inline_edit_state = null;
	refresh_inline_voucher_rows(report, data.edit_voucher_key);
	show_inline_notice(report, state.changes.size ? __("凭证内容已修改但尚未保存") : "", "blue");
}

function stage_inline_summary_change(report, data, summary) {
	const state = ensure_inline_edit_state(report, data);
	const edit_key = data.summary_edit_key;
	const state_key = inline_change_key("summary", edit_key);
	const value = String(summary || "").trim();
	if (!state.originals.has(state_key)) {
		state.originals.set(state_key, {
			field: "summary",
			edit_key,
			value: data.remarks || "",
		});
	}
	const original = state.originals.get(state_key);
	if (value === original.value) state.changes.delete(state_key);
	else state.changes.set(state_key, { field: "summary", edit_key, value });
	state.confirming = false;

	for (const report_row of report.data || []) {
		if (
			report_row.edit_voucher_key !== state.voucher_key ||
			report_row.summary_edit_group !== data.summary_edit_group
		)
			continue;
		report_row.remarks = value;
		report_row._inline_summary_dirty = state.changes.has(state_key);
	}
	if (!state.changes.size) report._china_inline_edit_state = null;
	refresh_inline_voucher_rows(report, data.edit_voucher_key);
	show_inline_notice(report, state.changes.size ? __("凭证内容已修改但尚未保存") : "", "blue");
}

function stage_inline_amount_change(report, data, field, value) {
	const amount = Number(value);
	if (!Number.isFinite(amount)) return;
	const state = ensure_inline_edit_state(report, data);
	const edit_key = data.amount_edit_key;
	const state_key = inline_change_key(field, edit_key);
	if (!state.originals.has(state_key)) {
		state.originals.set(state_key, { field, edit_key, value: Number(data[field] || 0) });
	}
	if (amount === state.originals.get(state_key).value) state.changes.delete(state_key);
	else state.changes.set(state_key, { field, edit_key, value: amount });
	state.confirming = false;
	for (const row of report.data || []) {
		if (row.edit_voucher_key !== state.voucher_key || row.amount_edit_key !== edit_key) continue;
		row[field] = amount;
		row.base_total_amount = Number(row.debit || 0) + Number(row.credit || 0);
		row[`_inline_${field}_dirty`] = state.changes.has(state_key);
	}
	if (!state.changes.size) report._china_inline_edit_state = null;
	refresh_inline_voucher_rows(report, data.edit_voucher_key);
	show_inline_notice(report, state.changes.size ? __("凭证内容已修改但尚未保存") : "", "blue");
}

function refresh_inline_voucher_rows(report, voucher_key) {
	(report.data || []).forEach((data, index) => {
		if (data.edit_voucher_key !== voucher_key || !report.datatable) return;
		const values = report.datatable.datamanager
			.getColumns(true)
			.map((column) => data[column.id]);
		report.datatable.refreshRow(values, index);
	});
}

function cancel_inline_voucher_changes(report, show_message = true) {
	const state = get_inline_edit_state(report);
	if (!state) return;
	for (const original of state.originals.values()) {
		for (const row of report.data || []) {
			if (row.edit_voucher_key !== state.voucher_key) continue;
			if (original.field === "account" && row.edit_key === original.edit_key) {
				row.account = original.value;
				row.account_label = original.label;
				delete row._inline_account_dirty;
			}
			if (original.field === "summary" && row.summary_edit_group === original.edit_key) {
				row.remarks = original.value;
				delete row._inline_summary_dirty;
			}
			if (
				["debit", "credit"].includes(original.field) &&
				row.amount_edit_key === original.edit_key
			) {
				row[original.field] = original.value;
				row.base_total_amount = Number(row.debit || 0) + Number(row.credit || 0);
				delete row[`_inline_${original.field}_dirty`];
			}
		}
	}
	report._china_inline_edit_state = null;
	refresh_inline_voucher_rows(report, state.voucher_key);
	show_inline_notice(report, show_message ? __("未保存的凭证修改已取消") : "", "blue");
}

function inline_changes_payload(state) {
	return [...state.changes.values()];
}

async function save_inline_voucher_changes(report, confirmed = false) {
	try {
		await report.datatable?.cellmanager?.submitEditing?.();
		report.datatable?.cellmanager?.deactivateEditing(false);
	} catch (error) {
		show_inline_notice(report, inline_error_message(error), "red");
		return;
	}
	const state = get_inline_edit_state(report);
	if (!state || !state.changes.size || state.pending) return;
	state.pending = true;
	refresh_inline_voucher_rows(report, state.voucher_key);
	show_inline_notice(
		report,
		confirmed ? __("正在执行受控更正并重新记账…") : __("正在检查凭证修改…"),
		"blue"
	);
	const args = {
		source_doctype: state.source_doctype,
		source_name: state.source_name,
		modified: state.modified,
		changes: inline_changes_payload(state),
	};
	try {
		if (!confirmed) {
			const preview =
				(
					await frappe.call({
						method: "china_finance.services.source_voucher_edit.preview_inline_voucher_changes",
						args,
					})
				).message || {};
			if (preview.requires_confirmation) {
				state.pending = false;
				state.confirming = true;
				state.preview = preview;
				refresh_inline_voucher_rows(report, state.voucher_key);
				const bank_note =
					preview.bank_action === "unreconcile_and_restore"
						? __("；系统将安全撤销并恢复银行核销")
						: "";
				show_inline_notice(
					report,
					__("该凭证已记账。确认后将取消原凭证、生成修订并重新记账{0}。", [bank_note]),
					"orange",
					preview.bank_transaction
				);
				return;
			}
		}
		const result =
			(
				await frappe.call({
					method: "china_finance.services.source_voucher_edit.apply_inline_voucher_changes",
					type: "POST",
					args,
				})
			).message || {};
		const message = result.message || __("凭证修改已保存");
		report._china_inline_edit_state = null;
		show_inline_notice(report, message, "green");
		frappe.show_alert({ message, indicator: "green" });
		const scroll_state = capture_voucher_ledger_scroll(report);
		report._china_inline_restore_scroll = scroll_state;
		try {
			await report.refresh();
		} finally {
			if (report._china_inline_restore_scroll === scroll_state) {
				report._china_inline_restore_scroll = null;
			}
		}
	} catch (error) {
		state.pending = false;
		state.confirming = false;
		refresh_inline_voucher_rows(report, state.voucher_key);
		show_inline_notice(report, inline_error_message(error), "red");
	}
}

function inline_error_message(error) {
	if (error?.message) return error.message;
	if (error?.exc) return error.exc;
	try {
		const messages = JSON.parse(error?._server_messages || "[]");
		if (messages.length) return JSON.parse(messages[0]).message;
	} catch (parse_error) {
		// Fall through to the stable user-facing message.
	}
	return __("操作失败，请检查凭证状态后重试");
}

function ensure_inline_notice(report) {
	if (report._china_inline_notice?.isConnected) return report._china_inline_notice;
	const notice = document.createElement("div");
	notice.className = "china-inline-edit-notice";
	notice.hidden = true;
	const report_element = report.$report?.[0];
	report_element?.parentElement?.insertBefore(notice, report_element);
	report._china_inline_notice = notice;
	return notice;
}

function show_inline_notice(report, message, indicator = "blue", bank_transaction = null) {
	const notice = ensure_inline_notice(report);
	if (!notice) return;
	if (!message) {
		notice.hidden = true;
		return;
	}
	notice.className = `china-inline-edit-notice ${indicator}`;
	notice.textContent = message;
	if (bank_transaction) {
		const link = document.createElement("a");
		link.href = frappe.utils.get_form_link("Bank Transaction", bank_transaction);
		link.textContent = ` ${__("查看银行流水 {0}", [bank_transaction])}`;
		notice.appendChild(link);
	}
	notice.hidden = false;
}

function bind_inline_voucher_actions(report) {
	const page_wrapper = report?.page?.wrapper?.[0] || report?.page?.wrapper;
	if (!page_wrapper?.addEventListener) return;
	const action_selector =
		".china-inline-open-source, .china-inline-save, .china-inline-confirm, .china-inline-cancel";

	// DataTable focuses a cell on mousedown and scrolls it into view. When the
	// operation column is only partly visible, that horizontal visibility check
	// can also change the vertical scroll position. Keep action buttons outside
	// that cell-navigation path; their click handler below still runs normally.
	report._china_inline_action_pointer_handler = (event) => {
		const target =
			event.target instanceof Element ? event.target : event.target?.parentElement;
		const button = target?.closest?.(action_selector);
		if (!button || !page_wrapper.contains(button)) return;
		event.stopPropagation();
	};
	page_wrapper.addEventListener("mousedown", report._china_inline_action_pointer_handler, true);
	page_wrapper.addEventListener("touchstart", report._china_inline_action_pointer_handler, true);

	report._china_inline_action_handler = (event) => {
		const target =
			event.target instanceof Element ? event.target : event.target?.parentElement;
		const editable_value = target?.closest?.(
			".china-inline-account.is-editable, .china-inline-summary.is-editable, .china-inline-amount.is-editable"
		);
		if (editable_value && page_wrapper.contains(editable_value)) {
			const cell = editable_value.closest(".dt-cell");
			if (!cell || !report.datatable?.cellmanager) return;
			event.preventDefault();
			event.stopPropagation();
			report.datatable.cellmanager.activateEditing(cell);
			return;
		}
		const button = target?.closest?.(action_selector);
		if (!button || !page_wrapper.contains(button)) return;
		event.preventDefault();
		event.stopPropagation();
		if (button.classList.contains("china-inline-open-source")) {
			frappe.set_route(
				"Form",
				decodeURIComponent(button.dataset.sourceDoctype),
				decodeURIComponent(button.dataset.sourceName)
			);
		} else if (button.classList.contains("china-inline-save")) {
			save_inline_voucher_changes(report, false);
		} else if (button.classList.contains("china-inline-confirm")) {
			save_inline_voucher_changes(report, true);
		} else {
			cancel_inline_voucher_changes(report);
		}
	};
	page_wrapper.addEventListener("click", report._china_inline_action_handler, true);
}

function wrap_report_refresh_for_inline_edits(report) {
	if (report._china_inline_native_refresh) return;
	report._china_inline_native_refresh = report.refresh.bind(report);
	report.refresh = (...args) => {
		const restore_scroll = report._china_inline_restore_scroll;
		const dirty = Boolean(get_inline_edit_state(report)?.changes?.size);
		if (dirty) cancel_inline_voucher_changes(report, false);
		if (dirty)
			frappe.show_alert({
				message: __("报表刷新，未保存的凭证修改已取消"),
				indicator: "orange",
			});
		const result = report._china_inline_native_refresh(...args);
		if (!restore_scroll) return result;
		const restore = () => restore_voucher_ledger_scroll(report, restore_scroll);
		if (result && typeof result.then === "function") {
			return Promise.resolve(result).finally(restore);
		}
		restore();
		return result;
	};
}

function get_voucher_ledger_scrollable(report) {
	return (
		report?.datatable?.bodyScrollable ||
		report?.$report?.[0]?.querySelector?.(".dt-scrollable") ||
		null
	);
}

function capture_voucher_ledger_scroll(report) {
	const scrollable = get_voucher_ledger_scrollable(report);
	if (!scrollable) return null;
	return {
		left: scrollable.scrollLeft,
		top: scrollable.scrollTop,
	};
}

function restore_voucher_ledger_scroll(report, state) {
	if (!state) return;
	const restore = () => {
		const scrollable = get_voucher_ledger_scrollable(report);
		if (!scrollable) return;
		scrollable.scrollLeft = state.left;
		scrollable.scrollTop = state.top;
	};
	restore();
	if (typeof window !== "undefined" && typeof window.requestAnimationFrame === "function") {
		window.requestAnimationFrame(() => window.requestAnimationFrame(restore));
	} else if (typeof window !== "undefined") {
		window?.setTimeout?.(restore, 0);
	}
}

function ensure_voucher_ledger_styles() {
	if (document.getElementById("china-voucher-ledger-inline-style")) return;
	$("<style>")
		.attr("id", "china-voucher-ledger-inline-style")
		.text(
			`
			.china-inline-edit-notice {
				margin: 0 0 8px;
				padding: 8px 12px;
				border: 1px solid var(--border-color, #d1d8dd);
				border-left: 4px solid var(--blue-500, #2490ef);
				border-radius: 6px;
				background: var(--subtle-fg, #f7f9fc);
			}
			.china-inline-edit-notice.orange { border-left-color: var(--orange-500, #f39c12); }
			.china-inline-edit-notice.red { border-left-color: var(--red-500, #e74c3c); }
			.china-inline-edit-notice.green { border-left-color: var(--green-500, #2f9e44); }
			.china-inline-account { display: inline-flex; align-items: center; width: 100%; min-height: 24px; }
			.china-inline-account.is-editable { cursor: pointer; }
			.china-inline-account.is-editable:hover { color: var(--primary, #2490ef); }
			.china-inline-account.is-dirty {
				padding: 0 4px;
				background: var(--yellow-100, #fff3bf);
				border-radius: 4px;
				font-weight: 600;
			}
			.china-inline-account__arrow { margin-left: auto; color: var(--text-muted); }
			.china-inline-summary { display: inline-flex; align-items: center; width: 100%; min-height: 24px; }
			.china-inline-summary.is-editable { cursor: text; }
			.china-inline-summary.is-editable:hover { color: var(--primary, #2490ef); }
			.china-inline-summary.is-dirty {
				padding: 0 4px;
				background: var(--yellow-100, #fff3bf);
				border-radius: 4px;
				font-weight: 600;
			}
			.china-inline-summary__edit { margin-left: auto; color: var(--text-muted); }
			.china-inline-amount { display: inline-block; width: 100%; min-height: 24px; text-align: right; }
			.china-inline-amount.is-editable { cursor: text; }
			.china-inline-amount.is-editable:hover { color: var(--primary, #2490ef); }
			.china-inline-amount.is-dirty {
				background: var(--yellow-100, #fff3bf);
				border-radius: 4px;
				font-weight: 600;
			}
			.china-inline-actions {
				display: flex;
				align-items: center;
				gap: 4px;
				flex-wrap: nowrap;
				min-width: max-content;
				height: 100%;
			}
			.china-voucher-ledger-report .dt-cell--editing .dt-cell__edit .form-group { margin: 0; }
			.china-voucher-ledger-report .dt-cell--editing .dt-cell__edit .control-input-wrapper { padding: 0; }
			.china-voucher-ledger-report .dt-cell__content {
				line-height: 24px;
				padding-left: 6px;
				padding-right: 6px;
				white-space: nowrap;
				overflow: hidden;
				text-overflow: ellipsis;
			}
			.china-voucher-ledger-report .dt-row { min-height: 28px; }
		`
		)
		.appendTo(document.head);
}

function set_quick_period(report, period) {
	const today = frappe.datetime.get_today();
	// Select values may be translated, so compare the selected option's index
	// instead of comparing translated labels directly.
	const period_control = report.get_filter("period_preset");
	const selected_index = Number(period_control.$input?.prop("selectedIndex"));
	const raw_options = period_control.df.options || [];
	const options = Array.isArray(raw_options) ? raw_options : raw_options.split("\n");
	const period_index =
		Number.isInteger(selected_index) && selected_index >= 0
			? selected_index
			: options.indexOf(period);
	const period_text = String(period || "");
	let from_date;
	let to_date;
	if (period_text.includes("近三") || period_index === 3) {
		from_date = moment(today).subtract(2, "months").startOf("month").format("YYYY-MM-DD");
		to_date = today;
	} else if (period_text.includes("本年") || period_index === 4) {
		from_date = `${today.slice(0, 4)}-01-01`;
		to_date = today;
	} else if (period_text.includes("本月") || period_index === 1) {
		from_date = moment(today).startOf("month").format("YYYY-MM-DD");
		to_date = moment(today).endOf("month").format("YYYY-MM-DD");
	} else if (period_text.includes("上月") || period_index === 2) {
		const previous = moment(today).subtract(1, "month");
		from_date = previous.clone().startOf("month").format("YYYY-MM-DD");
		to_date = previous.clone().endOf("month").format("YYYY-MM-DD");
	} else {
		from_date = moment(today).startOf("month").format("YYYY-MM-DD");
		to_date = moment(today).endOf("month").format("YYYY-MM-DD");
	}
	report.set_filter_value("from_date", from_date);
	report.set_filter_value("to_date", to_date);
	report.refresh();
}
