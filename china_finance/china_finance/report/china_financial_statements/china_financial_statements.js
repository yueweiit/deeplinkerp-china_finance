frappe.query_reports["China Financial Statements"] = {
	get_chart_data() {
		const report = frappe.query_report;
		// QueryReport empties the chart container without disconnecting the old
		// chart's ResizeObserver. Dispose it before the framework replaces its SVG.
		destroy_statement_chart(report);
		return report.raw_data?.chart;
	},
	get_datatable_options(datatable_options) {
		// Keep the same fixed-column table behavior as China Voucher Ledger.
		// Wide statement tables scroll inside the report instead of resizing
		// individual rows or squeezing the report container.
		datatable_options.layout = "fixed";
		datatable_options.cellHeight = 33;
		return datatable_options;
	},
	after_datatable_render(datatable) {
		const report = frappe.query_report;
		report.__china_statement_chart = report.chart;
		report.page.main.find(".china-balance-sheet-panels").remove();
		report.page.main.find(".china-balance-sheet-checks").remove();
	report.page.main.find(".china-activity-balance-panel").remove();
	report.$report.removeClass("china-activity-balance-report china-financial-statements-native-table");
		report.$report.show();
		const statement_type = report.get_filter_value("statement_type");
		const native_statement_types = ["Profit and Loss", "Cash Flow", "Account Activity and Balance"];
		if (native_statement_types.includes(statement_type)) {
			report.$report.addClass("china-financial-statements-native-table");
			china_finance.datatable_layout.bind_full_width(report, datatable);
		}
		if (statement_type !== "Balance Sheet") return;

		const currency = report.raw_data.chart?.currency;
		const asset_rows = report.data
			.filter((row) => row.asset_label)
			.map((row) => make_balance_sheet_side_row(row, "asset"));
		const liability_equity_rows = report.data
			.filter((row) => row.liability_equity_label)
			.map((row) => make_balance_sheet_side_row(row, "liability_equity"));
		const $panels = $("<div class='china-balance-sheet-panels'></div>");
		apply_balance_sheet_compact_style();
		const report_message = report.$report_message?.html() || report.raw_data?.message || "";
		if (report_message) {
			const $checks = $("<div class='china-balance-sheet-checks'></div>").html(report_message);
			$panels.before($checks);
		}
		const asset_panel = append_balance_sheet_panel($panels, __("资产"));
		const liability_equity_panel = append_balance_sheet_panel($panels, __("负债和所有者权益"));
		report.$report.before($panels).hide();
		// DataTable injects a <style> element into its container, which only gets a
		// stylesheet once the container is attached to the document. Build the
		// tables only after the panels are inserted.
		render_balance_sheet_tree(asset_panel, asset_rows, currency);
		render_balance_sheet_tree(liability_equity_panel, liability_equity_rows, currency);
	},
	// The query report page shows the main datatable again after
	// after_datatable_render, so hide it once the refresh has fully settled.
	// The tree footer (Set Level / Expand All / Collapse All) only controls the
	// hidden main datatable, so hide it as well; each panel has its own buttons.
	after_refresh(report) {
		if (!report.data?.length) destroy_statement_chart(report);
		if (report.get_filter_value("statement_type") === "Balance Sheet") {
			report.$report.hide();
			report.$tree_footer?.hide();
		}
	},
	onload(report) {
		// Returning from a native voucher/account form can leave the shared query
		// report page with that module's sidebar. Restore China Finance whenever
		// this report becomes the active route.
		frappe.app.sidebar?.setup("China Finance");
		if (!frappe.query_reports["China Financial Statements"].china_finance_sidebar_hooked) {
			frappe.query_reports["China Financial Statements"].china_finance_sidebar_hooked = true;
			frappe.router.on("change", () => {
				const route = frappe.get_route();
				if (route[0] === "query-report" && route[1] === "China Financial Statements") {
					frappe.app.sidebar?.setup("China Finance");
				}
			});
		}
		add_china_finance_export_actions(report);
		bind_source_account_links();
		if (!report.get_filter_value("company")) {
			report.set_filter_value("company", window.china_finance?.company_context?.default_company() || frappe.defaults.get_user_default("Company"));
		}
		report.get_filter("company").on_change = () => sync_statutory_header(report);
		sync_statutory_header(report);
		report.get_filter("fiscal_year").get_query = () => ({
			filters: { disabled: 0 },
		});
		report.get_filter("cost_center").get_query = () => ({
			filters: { company: report.get_filter_value("company") },
		});
		report.get_filter("project").get_query = () => ({
			filters: { company: report.get_filter_value("company") },
		});
		report.get_filter("account").get_query = () => ({
			filters: { company: report.get_filter_value("company") },
		});
		// Query Report copies df.on_change only during control creation. Update the
		// created controls so period changes work without reopening the page.
		report.get_filter("fiscal_year").on_change = () => sync_accounting_period(report, true);
		report.get_filter("periodicity").on_change = () => sync_accounting_period(report, true);
		report.get_filter("accounting_period").on_change = () => apply_accounting_period(report, true);

		if (!report.get_filter_value("fiscal_year")) {
			const fiscal_year = erpnext.utils.get_fiscal_year(frappe.datetime.get_today());
			report.set_filter_value("fiscal_year", fiscal_year);
			sync_accounting_period(report, false);
		} else {
			sync_accounting_period(report, false);
		}
	},
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (data?.source_accounts?.length && column.fieldname === "label") {
			value = source_account_link(value, data.source_accounts);
		}
		const balance_sheet_side = column.fieldname === "asset_label" ? "asset" :
			column.fieldname === "liability_equity_label" ? "liability_equity" : null;
		if (data && balance_sheet_side) {
			const indent = data[`${balance_sheet_side}_indent`] || 0;
			value = `${"&nbsp;".repeat(indent * 4)}${value}`;
			if (data[`${balance_sheet_side}_bold`]) value = `<strong>${value}</strong>`;
		} else if (data && data.bold) {
			value = `<strong>${value}</strong>`;
		}
		return value;
	},
};

function destroy_statement_chart(report) {
	const chart = report.__china_statement_chart || report.chart;
	chart?.destroy();
	if (report.chart === chart) report.chart = null;
	report.__china_statement_chart = null;
}

function apply_balance_sheet_compact_style() {
	if (document.getElementById("china-balance-sheet-compact-style")) return;
	$("<style>")
		.attr("id", "china-balance-sheet-compact-style")
		.text(`
        .china-balance-sheet-panels .dt-cell__content {
            font-size: 11px;
            line-height: 1.2;
        }
        .china-balance-sheet-panels .dt-header .dt-cell__content {
            font-size: 11px;
        }
		.china-balance-sheet-checks {
			margin: 8px 0 12px;
			padding: 10px 14px;
			border: 1px solid var(--border-color);
			border-radius: var(--border-radius);
			background: var(--subtle-fg);
			color: var(--text-muted);
			font-size: 12px;
		}
		`)
		.appendTo(document.head);
}

function add_china_finance_export_actions(report) {
	if (report.__china_finance_export_actions_added || !report.page?.add_action_item) return;
	report.__china_finance_export_actions_added = true;
	const excel_item = report.page.add_action_item(__("导出 Excel"), () => {
		frappe.call({
			method: "china_finance.china_finance.report.china_financial_statements.china_financial_statements.export_current_report_xlsx",
			args: { filters: report.get_filter_values() },
			freeze: true,
			freeze_message: __("正在生成 Excel"),
			callback(response) {
				const file_url = response.message?.file_url;
				if (!file_url) {
					frappe.throw(__("Excel 生成失败，未返回文件链接"));
				}
				window.open(file_url, "_blank", "noopener");
			},
		});
	});
	const pdf_item = report.page.add_action_item(__("导出 PDF"), () => {
		frappe.call({
			method: "china_finance.china_finance.report.china_financial_statements.china_financial_statements.export_current_report_pdf",
			args: { filters: report.get_filter_values() },
			freeze: true,
			freeze_message: __("正在生成 PDF"),
			callback(response) {
				const file_url = response.message?.file_url;
				if (!file_url) {
					frappe.throw(__("PDF 生成失败，未返回文件链接"));
				}
				window.open(file_url, "_blank", "noopener");
			},
		});
	});
	// The query-report page is shared across all reports, so keep the button
	// label and these menu items in sync with the currently loaded report.
	const sync_actions = () => {
		const is_china_statements = frappe.get_route()[1] === "China Financial Statements";
		report.page.actions_btn_group
			.find(".actions-btn-group-label")
			.text(is_china_statements ? __("导出") : __("Actions"));
		$(excel_item).closest("li").toggle(is_china_statements);
		$(pdf_item).closest("li").toggle(is_china_statements);
		if (is_china_statements) {
			report.page.actions_btn_group.removeClass("hide");
			return;
		}
		// Hide the whole actions button when nothing visible remains in its menu.
		const has_visible_items =
			report.page.actions
				.find("li")
				.filter((_, el) => $(el).is(":visible") && !$(el).hasClass("dropdown-divider")).length > 0;
		report.page.actions_btn_group.toggleClass("hide", !has_visible_items);
	};
	sync_actions();
	$(report.page.wrapper).on("show", sync_actions);
}

function sync_statutory_header(report) {
	const company = report.get_filter_value("company");
	if (!company) return;
	Promise.all([
		frappe.db.get_value("Company", company, "tax_id"),
		frappe.db.get_value("China Finance Settings", company, "accounting_standard"),
	]).then(([tax, settings]) => {
		if (report.get_filter_value("company") !== company) return;
		report.statutory_header = {
			tax_id: tax?.message?.tax_id || "",
			accounting_standard: settings?.message?.accounting_standard || "",
		};
	});
}

function sync_accounting_period(report, refresh) {
	const fiscal_year = report.get_filter_value("fiscal_year");
	if (!fiscal_year) return;
	frappe.db.get_doc("Fiscal Year", fiscal_year).then((year) => {
		if (report.get_filter_value("fiscal_year") !== fiscal_year) return;
		const period_filter = report.get_filter("accounting_period");
		const options = get_period_options(year, report.get_filter_value("periodicity"));
		period_filter.df.options = options.map((option) => option.label).join("\n");
		period_filter.refresh();
		if (!options.some((option) => option.label === report.get_filter_value("accounting_period"))) {
			report.set_filter_value("accounting_period", options[0].label);
		}
		apply_accounting_period(report, refresh, year, options);
	});
}

function apply_accounting_period(report, refresh, fiscal_year, options) {
	const year_name = report.get_filter_value("fiscal_year");
	if (!year_name) return;
	const set_dates = (year) => {
		const periods = options || get_period_options(year, report.get_filter_value("periodicity"));
		const selected = periods.find((option) => option.label === report.get_filter_value("accounting_period")) || periods[0];
		report.__applying_accounting_period = true;
		report.set_filter_value({ from_date: selected.from_date, to_date: selected.to_date });
		report.__applying_accounting_period = false;
		if (refresh) report.refresh();
	};
	if (fiscal_year) {
		set_dates(fiscal_year);
		return;
	}
	frappe.db.get_doc("Fiscal Year", year_name).then((year) => {
		if (report.get_filter_value("fiscal_year") === year_name) set_dates(year);
	});
}

function get_period_options(fiscal_year, periodicity) {
	const start = moment(fiscal_year.year_start_date);
	const end = moment(fiscal_year.year_end_date);
	if (periodicity === "季度") {
		return [1, 2, 3, 4].map((quarter) => make_period_option(start, end, (quarter - 1) * 3, 3, `第${quarter}季度`));
	}
	if (periodicity === "月度") {
		return Array.from({ length: 12 }, (_, index) => make_period_option(start, end, index, 1, __("第{0}月", [index + 1])));
	}
	return [{ label: __("全年"), from_date: start.format("YYYY-MM-DD"), to_date: end.format("YYYY-MM-DD") }];
}

function make_period_option(year_start, year_end, offset_months, duration_months, label) {
	const start = year_start.clone().add(offset_months, "months");
	const end = start.clone().add(duration_months, "months").subtract(1, "day");
	return {
		label,
		from_date: start.format("YYYY-MM-DD"),
		to_date: moment.min(end, year_end).format("YYYY-MM-DD"),
	};
}

function make_balance_sheet_side_row(row, side) {
	return {
		label: row[`${side}_label`],
		row_type: row[`${side}_row_type`],
		indent: row[`${side}_indent`] || 0,
		bold: row[`${side}_bold`] || 0,
		opening_amount: row[`${side}_opening_amount`],
		amount: row[`${side}_amount`],
		comparison_amount: row[`${side}_comparison_amount`],
		source_accounts: row[`${side}_source_accounts`] || [],
	};
}

function source_account_link(label, accounts) {
	if (!accounts?.length) return label;
	const source_accounts = JSON.stringify([...new Set(accounts)]);
	const title = frappe.utils.escape_html(__("查看来源科目总账") + "：\n" + accounts.join("\n"));
	return `<a href="#" class="china-finance-source-account-link" title="${title}" data-accounts="${frappe.utils.escape_html(source_accounts)}">${label}</a>`;
}

function bind_source_account_links() {
	if (frappe._china_finance_source_account_links_bound) return;
	frappe._china_finance_source_account_links_bound = true;
	$(document).on("click.china_finance_source_account", ".china-finance-source-account-link", function (event) {
		event.preventDefault();
		const accounts = JSON.parse($(this).attr("data-accounts") || "[]");
		const report = frappe.query_report;
		const company = report?.get_filter_value("company");
		if (!accounts.length || !company) return;
		const filters = {
			company,
			account: accounts,
			from_date: report.get_filter_value("from_date"),
			to_date: report.get_filter_value("to_date"),
		};
		for (const fieldname of ["finance_book", "cost_center", "project"]) {
			const value = report.get_filter_value(fieldname);
			if (value) filters[fieldname] = value;
		}
		if (report.get_filter_value("statement_type") === "Profit and Loss") {
			frappe.show_alert({message: __("已打开全部来源科目。总账包含结转分录，核对利润表时请剔除损益结转。"), indicator: "blue"}, 10);
		}
		frappe.set_route("query-report", "General Ledger", filters);
	});
}

function append_balance_sheet_panel($panels, title) {
	const $panel = $("<section class='china-balance-sheet-panel'></section>");
	const $title = $(`<div class="china-balance-sheet-panel__title"><span>${title}</span></div>`);
	const $actions = $("<span class='china-balance-sheet-panel__actions' style='float: right; margin-left: auto;'></span>");
	const $expand = $(`<button class="btn btn-xs btn-secondary" type="button">${__("Expand All")}</button>`);
	const $collapse = $(`<button class="btn btn-xs btn-secondary" type="button">${__("Collapse All")}</button>`);
	$title.append($actions.append($expand, $collapse));
	const $body = $("<div class='china-balance-sheet-panel__body'></div>");
	$panel.append($title, $body);
	$panels.append($panel);
	return { body: $body.get(0), $expand, $collapse };
}

function render_balance_sheet_tree(panel, rows, currency) {
	const has_comparison = rows.some((row) => row.comparison_amount !== null && row.comparison_amount !== undefined);

	const bold_row = (data) => data && (data.bold || data.row_type === "Heading");
	const format_amount = (value, row, column, data) => {
		let formatted = format_currency(value || 0, currency);
		if (bold_row(data)) formatted = `<strong>${formatted}</strong>`;
		return formatted;
	};
	const columns = [
		{
			id: "label",
			name: __("项目"),
			width: 3,
			format: (value, row, column, data) => {
				let label = frappe.utils.escape_html(value ?? "");
				if (data?.source_accounts?.length) {
					label = source_account_link(label, data.source_accounts);
				}
				if (bold_row(data)) label = `<strong>${label}</strong>`;
				return label;
			},
		},
		{ id: "opening_amount", name: __("期初余额"), width: 1, format: format_amount },
		{ id: "amount", name: __("期末余额"), width: 1, format: format_amount },
	];
	if (has_comparison) {
		columns.push({ id: "comparison_amount", name: __("比较期余额"), width: 1, format: format_amount });
	}

	const datatable = new window.DataTable(panel.body, {
		columns,
		data: rows,
		treeView: true,
		// Ratio layout fills the entire panel and keeps the item column wider
		// than the amount columns at every screen size.
		layout: "ratio",
		serialNoColumn: false,
		cellHeight: 29,
		inlineFilters: true,
		language: frappe.boot.lang,
		translations: frappe.utils.datatable.get_translations(),
		direction: frappe.utils.is_rtl() ? "rtl" : "ltr",
	});
	// Start collapsed to the top level, matching the previous custom panels.
	datatable.rowmanager.setTreeDepth(0);
	panel.$expand.on("click", () => datatable.rowmanager.expandAllNodes());
	panel.$collapse.on("click", () => datatable.rowmanager.collapseAllNodes());
}

// All query reports share one page container (frappe.standard_pages["query-report"]);
// panels injected for the balance-sheet layout must not leak into other reports.
if (!frappe._china_fs_panels_cleanup_bound) {
	frappe._china_fs_panels_cleanup_bound = true;
	frappe.router.on("change", () => {
		const route = frappe.get_route();
		if (route[0] !== "query-report" || route[1] !== "China Financial Statements") {
			if (frappe.query_report?.__china_statement_chart) {
				destroy_statement_chart(frappe.query_report);
			}
			$(".china-balance-sheet-panels").remove();
			// Query Report reuses the same page container. Remove the report-only
			// class immediately when navigating elsewhere, otherwise the compact
			// DataTable rules can affect the next report before it initializes.
			frappe.query_report?.$report?.removeClass("china-activity-balance-report");
		}
	});
}
