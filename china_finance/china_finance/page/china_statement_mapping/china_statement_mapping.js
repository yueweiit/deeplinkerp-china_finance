frappe.pages["china-statement-mapping"].on_page_load = function (wrapper) {
	frappe.utils.set_title(__("China Statement Mapping"));
	wrapper.china_statement_mapping = new ChinaStatementMapping(wrapper);
};

frappe.pages["china-statement-mapping"].on_page_show = function (wrapper) {
	frappe.utils.set_title(__("China Statement Mapping"));
	const mapping = wrapper.china_statement_mapping;
	const company = frappe.route_options?.company || window.china_finance?.company_context?.get_company();
	if (company && mapping.company.get_value() !== company) {
		mapping.company.set_value(company);
	} else {
		mapping.refresh();
	}
};

class ChinaStatementMapping {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("China Statement Mapping"),
			single_column: true,
		});
		frappe.breadcrumbs.add("China Finance");
		this.can_write = frappe.user.has_role(["China Finance Manager", "Accounts Manager", "System Manager"]);
		this.can_edit_template = frappe.user.has_role(["China Finance Manager", "System Manager"]);
		this.selected_accounts = new Set();
		this.selected_rows = new Set();
		this.collapsed_rows = new Set();
		this.collapsed_accounts = new Set();
		this.expanded_aggregates = new Set();
		this.data = null;
		this.setup_filters();
		this.setup_content();
		this.page.set_primary_action(__("Refresh"), () => this.refresh(), "refresh-cw");
	}

	setup_filters() {
		this.company = this.page.add_field({
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.route_options?.company || window.china_finance?.company_context?.default_company() || frappe.defaults.get_user_default("Company"),
			change: () => this.refresh(),
		});
		this.statement_type = this.page.add_field({
			fieldname: "statement_type",
			label: __("Statement Type"),
			fieldtype: "Select",
			options: "Balance Sheet\nProfit and Loss\nCash Flow\nChanges in Equity",
			default: "Balance Sheet",
			change: () => this.refresh(),
		});
		this.accounting_standard = this.page.add_field({
			fieldname: "accounting_standard",
			label: __("Accounting Standard"),
			fieldtype: "Select",
			options: "跟随公司设置\n企业会计准则\n小企业会计准则",
			default: "跟随公司设置",
			change: () => this.refresh(),
		});
		this.account_search = this.page.add_field({
			fieldname: "account_search",
			label: __("Account Search"),
			fieldtype: "Data",
			change: () => this.render_accounts(),
		});
		this.pending_only = this.page.add_field({
			fieldname: "pending_only",
			label: __("Pending Review Only"),
			fieldtype: "Check",
			default: 0,
			change: () => this.render_rows(),
		});
		this.unmapped_only = this.page.add_field({
			fieldname: "unmapped_only",
			label: __("Unmapped Only"),
			fieldtype: "Check",
			default: 0,
			change: () => this.render_accounts(),
		});
	}

	setup_content() {
		this.$content = $(
			`<div class="china-statement-mapping">
				<div class="smc-summary"></div>
				<div class="smc-reclassifications"></div>
				<div class="smc-main">
					<div class="smc-rows"></div>
					<div class="smc-accounts"></div>
				</div>
			</div>`,
		).appendTo($(this.wrapper).find(".layout-main-section"));
		this.$summary = this.$content.find(".smc-summary");
		this.$reclassifications = this.$content.find(".smc-reclassifications");
		this.$rows = this.$content.find(".smc-rows");
			this.$accounts = this.$content.find(".smc-accounts");
		this.inject_styles();
	}

	inject_styles() {
		if (document.getElementById("china-statement-mapping-style")) return;
		$(
			`<style id="china-statement-mapping-style">
				.china-statement-mapping { padding: 24px 8px 48px; }
				.smc-summary { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
				.smc-summary .summary-item { min-width: 170px; padding: 14px 18px; border: 1px solid var(--border-color); border-left: 3px solid var(--gray-400); border-radius: 8px; background: var(--card-bg); transition: box-shadow 0.15s ease, transform 0.15s ease; }
				.smc-summary .summary-item.accent-blue { border-left-color: var(--blue-500); }
				.smc-summary .summary-item.accent-orange { border-left-color: var(--orange-500); }
				.smc-summary .summary-item.accent-red { border-left-color: var(--red-500); }
				.smc-summary .summary-item.clickable { cursor: pointer; }
				.smc-summary .summary-item.clickable:hover { box-shadow: var(--shadow-sm, 0 2px 8px rgba(0, 0, 0, 0.08)); transform: translateY(-1px); }
				.smc-summary .summary-label { color: var(--text-muted); font-size: var(--text-xs); }
				.smc-summary .summary-value { margin-top: 4px; font-size: var(--text-xl); font-weight: 600; }
				.smc-summary .summary-value.warning { color: var(--orange-500); }
				.smc-summary .summary-value.danger { color: var(--red-500); }
				.smc-main { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 24px; align-items: start; }
				.smc-rows, .smc-accounts { border: 1px solid var(--border-color); border-radius: 8px; background: var(--card-bg); overflow: hidden; box-shadow: var(--shadow-xs, 0 1px 2px rgba(0, 0, 0, 0.04)); }
				.smc-panel-title { padding: 14px 18px; font-weight: 600; border-bottom: 1px solid var(--border-color); background: var(--subtle-fg); }
				.smc-rows__list, .smc-accounts__list { max-height: 72vh; overflow-y: auto; }
				.smc-row { padding: 8px 18px; transition: background-color 0.12s ease; }
				.smc-row + .smc-row { border-top: 1px solid var(--border-color); }
				.smc-row:hover { background: var(--bg-light-gray); }
				.smc-row--heading, .smc-row--bold { font-weight: 600; }
				.smc-row__head { display: flex; align-items: center; gap: 10px; min-height: 30px; }
				.smc-row__select { flex: none; margin: 0; }
				.smc-row__toggle { width: 20px; height: 20px; line-height: 18px; flex: none; text-align: center; cursor: pointer; color: var(--text-muted); border-radius: 4px; transition: background-color 0.12s ease; }
				.smc-row__toggle:hover { background: var(--border-color); color: var(--text-color); }
				.smc-row__label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
				.smc-row__direction { flex: none; color: var(--text-muted); font-size: var(--text-xs); border: 1px solid var(--border-color); border-radius: 4px; padding: 0 6px; }
				.smc-row__pending { flex: none; font-size: var(--text-xs); color: var(--orange-500); background: var(--bg-orange, #fff4e5); border-radius: 8px; padding: 1px 8px; white-space: nowrap; }
				.smc-review-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 18px; border-bottom: 1px solid var(--border-color); background: var(--subtle-fg); font-size: var(--text-sm); }
				.smc-review-toolbar .text-muted { margin-right: auto; }
				.smc-row__actions { flex: none; display: inline-flex; gap: 6px; }
				.smc-row__formula { margin: 2px 0 5px 30px; padding: 6px 10px; font-size: var(--text-xs); color: var(--text-muted); background: var(--subtle-fg); border-radius: 4px; line-height: 2; }
				.smc-formula-token { display: inline-block; border: 1px solid var(--border-color); border-radius: 4px; padding: 0 5px; margin: 0 1px; background: var(--card-bg); color: var(--text-color); }
				.smc-op-toolbar { display: flex; gap: 6px; margin-bottom: 8px; }
				.smc-op-btn { border: 1px solid var(--border-color); border-radius: 4px; background: var(--card-bg); padding: 1px 12px; font-size: var(--text-sm); cursor: pointer; transition: background-color 0.12s ease; }
				.smc-op-btn:hover { background: var(--bg-light-gray); border-color: var(--gray-400); }
				.smc-op-btn:active { transform: translateY(1px); }
				.smc-code-chips { display: flex; flex-wrap: wrap; gap: 6px; max-height: 160px; overflow-y: auto; padding: 4px 0; }
				.smc-code-chip { border: 1px solid var(--border-color); border-radius: 4px; background: var(--subtle-fg); padding: 2px 8px; font-size: var(--text-xs); cursor: pointer; transition: background-color 0.12s ease, box-shadow 0.12s ease; }
				.smc-code-chip:hover { background: var(--card-bg); box-shadow: var(--shadow-xs, 0 1px 2px rgba(0, 0, 0, 0.06)); }
				.smc-chips { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 8px 30px; }
				.smc-chip { display: inline-flex; align-items: center; gap: 6px; border-radius: 10px; padding: 4px 10px; font-size: var(--text-xs); background: var(--subtle-fg); transition: box-shadow 0.12s ease; }
				.smc-chip:hover { box-shadow: var(--shadow-xs, 0 1px 2px rgba(0, 0, 0, 0.06)); }
				.smc-chip--link { cursor: pointer; }
				.smc-chip__dot { width: 6px; height: 6px; border-radius: 50%; flex: none; }
				.smc-chip__dot--reviewed { background: var(--green-500); }
				.smc-chip__dot--pending { background: var(--orange-500); }
				.smc-chip__sign { color: var(--red-500); font-weight: 600; }
				.smc-chip__remove { border: 0; background: transparent; color: var(--text-muted); cursor: pointer; padding: 0; line-height: 1; visibility: hidden; }
				.smc-chip:hover .smc-chip__remove { visibility: visible; }
				.smc-chip__remove:hover { color: var(--red-500); }
				.smc-aggregate-toggle { margin: 2px 0 5px 30px; font-size: var(--text-xs); color: var(--text-muted); cursor: pointer; }
				.smc-aggregate-toggle:hover { color: var(--text-color); }
				.smc-chips--aggregate .smc-chip { background: transparent; border: 1px dashed var(--border-color); }
				.smc-group-title { position: sticky; top: 0; z-index: 1; padding: 8px 18px; font-size: var(--text-xs); color: var(--text-muted); background: var(--subtle-fg); letter-spacing: 0.3px; }
				.smc-account { display: flex; align-items: center; gap: 10px; padding: 8px 18px; font-size: var(--text-sm); transition: background-color 0.12s ease; }
				.smc-account + .smc-account { border-top: 1px solid var(--border-color); }
				.smc-account:hover { background: var(--bg-light-gray); }
				.smc-account__dot { width: 6px; height: 6px; border-radius: 50%; background: var(--red-500); flex: none; }
				.smc-account--group { font-weight: 600; cursor: pointer; }
				.smc-account__toggle { width: 20px; height: 20px; line-height: 18px; flex: none; color: var(--text-muted); text-align: center; border-radius: 4px; }
				.smc-account--group:hover .smc-account__toggle { background: var(--border-color); color: var(--text-color); }
				.smc-account__grouplabel { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
				.smc-account__badge { margin-left: auto; font-size: var(--text-xs); color: var(--red-500); background: var(--bg-light-red, #fff5f5); border-radius: 8px; padding: 0 7px; white-space: nowrap; }
				.smc-account__target { margin-left: auto; color: var(--text-muted); font-size: var(--text-xs); white-space: nowrap; }
				.smc-account__suggestion { color: var(--blue-600, #1f6feb); background: var(--bg-blue, #f0f6ff); border-radius: 4px; padding: 1px 6px; }
				.smc-selection-bar { display: flex; align-items: center; gap: 8px; padding: 8px 14px; border-bottom: 1px solid var(--border-color); background: var(--bg-blue, #f0f6ff); font-size: var(--text-sm); }
				.smc-selection-hint { color: var(--text-muted); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
				.smc-empty { padding: 28px 14px; color: var(--text-muted); text-align: center; }
				.smc-configuration { border: 1px solid var(--border-color); border-radius: 8px; background: var(--card-bg); margin-bottom: 16px; overflow: hidden; }
				.smc-configuration__title { padding: 12px 16px; font-weight: 600; border-bottom: 1px solid var(--border-color); background: var(--subtle-fg); }
				.smc-configuration__body { padding: 12px 16px 16px; }
				.smc-configuration__field + .smc-configuration__field { margin-top: 8px; }
				.smc-configuration .form-group { margin-bottom: 8px; }
				.smc-configuration .smc-save-configuration { margin-top: 4px; }
				.smc-reclassifications { margin-bottom: 16px; }
				.smc-reclassifications__panel { border: 1px solid var(--border-color); border-radius: 8px; background: var(--card-bg); overflow: hidden; }
				.smc-reclassifications__panel .smc-panel-title { display: flex; align-items: center; gap: 8px; }
				.smc-reclassifications__panel .smc-panel-title button { margin-left: auto; }
				.smc-reclassifications__hint { padding: 8px 18px; color: var(--text-muted); font-size: var(--text-xs); border-bottom: 1px solid var(--border-color); }
				.smc-reclassification-row { display: flex; align-items: center; gap: 14px; padding: 9px 18px; border-bottom: 1px solid var(--border-color); }
				.smc-reclassification-row:last-child { border-bottom: 0; }
				.smc-reclassification-row span:first-child { min-width: 180px; font-weight: 600; }
				.smc-reclassification-row span:nth-child(3) { flex: 1; }
				.smc-reclassification-signs { display: inline-flex; align-items: center; gap: 8px; min-width: 148px; }
				.smc-reclassification-sign { display: inline-block; min-width: 38px; padding: 2px 6px; border-radius: 10px; font-size: var(--text-xs); text-align: center; }
				.smc-reclassification-sign--positive { color: var(--green-700); background: var(--green-100); }
				.smc-reclassification-sign--negative { color: var(--orange-700); background: var(--orange-100); }
				.smc-summary-config { min-width: 210px; border-left-color: var(--gray-400); }
				.smc-summary-config__control { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
				.smc-date-control { width: 126px; }
				.smc-date-control .form-group { margin: 0; }
				.smc-date-control .control-label { display: none; }
				.smc-date-control .form-control { height: 26px; padding: 2px 6px; }
				@media (max-width: 991px) { .smc-main { grid-template-columns: 1fr; } }
			</style>`,
		).appendTo(document.head);
	}

	async refresh(restore_scroll_top = null) {
		const request = this.refresh_request = {};
		const company = this.company.get_value();
		const statement_type = this.statement_type.get_value();
		if (!company || !statement_type) return;
		const accounting_standard = this.accounting_standard.get_value();
		this.selected_accounts.clear();
		this.selected_rows.clear();
		this.collapsed_rows.clear();
		this.collapsed_accounts.clear();
		this.expanded_aggregates.clear();
		this.$rows.html(`<div class="smc-empty">${__("Loading...")}</div>`);
		this.$accounts.empty();
		let data;
		try {
			data = await frappe.xcall(
				"china_finance.services.statement_mapping_console.get_mapping_console",
				{
					company,
					statement_type,
					accounting_standard: accounting_standard === "跟随公司设置" ? null : accounting_standard,
				},
			);
		} catch {
			if (request !== this.refresh_request) return;
			this.data = null;
			this.$summary.empty();
			this.$reclassifications.empty();
			this.$accounts.empty();
			this.$rows.html(`<div class="smc-empty">${__("Unable to load statement mapping. Check the reported error and try again.")}</div>`);
			return;
		}
		// A previous company's slower response must not replace the current view.
		if (request !== this.refresh_request) return;
		this.data = data;
		this.render_all();
		if (restore_scroll_top !== null) {
			this.$rows.find(".smc-rows__list").scrollTop(restore_scroll_top);
		}
	}

	render_all() {
		this.render_summary();
		this.render_reclassification_rules();
		this.render_rows();
		this.render_accounts();
		this.render_configuration();
	}

	render_reclassification_rules() {
		if (this.statement_type.get_value() !== "Balance Sheet") {
			this.$reclassifications.empty();
			return;
		}
		const rules = this.data.reclassification_rules || [];
		const rows = this.data.rows || [];
		const labels = Object.fromEntries(rows.map((row) => [row.row_code, row.label]));
		const normal_sign = () => __("正数");
		const adjusted_sign = () => __("负数");
		const $panel = $(`
			<div class="smc-reclassifications__panel">
				<div class="smc-panel-title">${__("报表余额重分类")}
					${this.can_write ? `<button class="btn btn-xs btn-primary smc-add-reclassification">${__("新增展示规则")}</button>` : ""}
				</div>
				<div class="smc-reclassifications__hint">${__("仅影响报表展示，不修改总账、凭证和原始科目映射")}</div>
				<div class="smc-reclassifications__list"></div>
			</div>`);
		const $list = $panel.find(".smc-reclassifications__list");
		if (!rules.length) $list.html(`<div class="smc-empty">${__("暂无展示调整规则")}</div>`);
		rules.forEach((rule) => {
			const sign_scope = rule.source_account ? __("余额贡献") : "";
			const source_account_label = rule.source_account
				? (rule.source_account_label || this.get_account_label(rule.source_account))
				: "";
			const $row = $(`
				<div class="smc-reclassification-row">
					<span>${frappe.utils.escape_html(labels[rule.source_row_code] || rule.source_row_code)}${rule.source_account ? ` <small class="text-muted">(${frappe.utils.escape_html(source_account_label)})</small>` : ` <small class="text-muted">(${__("全部科目")})</small>`}</span>
					<span class="smc-reclassification-signs">
						<span>${__("正常")} ${sign_scope} <span class="smc-reclassification-sign smc-reclassification-sign--positive">${normal_sign()}</span></span>
						<span>${__("需调整")} ${sign_scope} <span class="smc-reclassification-sign smc-reclassification-sign--negative">${adjusted_sign()}</span></span>
					</span>
					<span>→ ${frappe.utils.escape_html(labels[rule.target_row_code] || rule.target_row_code)}</span>
					<span class="text-muted">${rule.enabled ? __("已启用") : __("已停用")}</span>
				</div>`);
			if (this.can_write) {
				$(`<button class="btn btn-xs btn-default">${__("编辑")}</button>`).on("click", () => this.edit_reclassification_rule(rule)).appendTo($row);
			}
			$list.append($row);
		});
		$panel.find(".smc-add-reclassification").on("click", () => this.edit_reclassification_rule());
		this.$reclassifications.html($panel);
	}

	edit_reclassification_rule(rule = {}) {
		const rows = (this.data.rows || []).filter((row) => row.row_type === "Mapped Accounts");
		const options = rows.map((row) => ({ label: `${row.label} (${row.row_code})`, value: row.row_code }));
		const account_options = [{ label: __("全部科目"), value: "" }].concat(
			rows.flatMap((row) => (row.mappings || []).map((mapping) => ({
				label: `${this.get_account_label(mapping.account, mapping)} → ${row.label}`, value: mapping.account,
			}))),
		);
		const dialog = new frappe.ui.Dialog({
			title: rule.name ? __("编辑展示调整规则") : __("新增展示调整规则"),
			fields: [
				{ fieldname: "source_row_code", label: __("余额来源项目"), fieldtype: "Select", options, reqd: 1 },
				{ fieldname: "source_account", label: __("来源科目"), fieldtype: "Select", options: account_options, description: __("留空表示该项目下的全部科目") },
				{ fieldname: "normal_sign", label: __("正常余额符号"), description: __("报表项目正常显示为正数，负数时按规则重分类"), fieldtype: "Select", options: `${__("正数")}\n${__("负数")}`, reqd: 1 },
				{ fieldname: "target_row_code", label: __("展示列入项目"), fieldtype: "Select", options, reqd: 1 },
				{ fieldname: "effective_from", label: __("生效日期"), fieldtype: "Date", reqd: 1 },
				{ fieldname: "effective_to", label: __("失效日期"), fieldtype: "Date" },
				{ fieldname: "enabled", label: __("启用"), fieldtype: "Check", default: 1 },
				{ fieldname: "review_notes", label: __("复核说明"), fieldtype: "Small Text" },
			],
			primary_action_label: __("保存"),
			primary_action: async (values) => {
				await frappe.xcall("china_finance.services.statement_mapping_console.save_reclassification_rule", {
					...values, source_direction: (this.data.rows.find((row) => row.row_code === values.source_row_code) || {}).balance_direction || "Debit Positive", name: rule.name || null, company: this.company.get_value(), template: this.data.template.name,
				});
				dialog.hide();
				frappe.show_alert({ message: __("展示调整规则已保存"), indicator: "green" });
				this.refresh();
			},
		});
		dialog.set_values({
			source_row_code: rule.source_row_code || options[0]?.value,
			source_account: rule.source_account || "",
			normal_sign: __("正数"),
			target_row_code: rule.target_row_code || options[1]?.value || options[0]?.value,
			effective_from: rule.effective_from || this.data.configuration?.mapping_effective_from,
			effective_to: rule.effective_to || null, enabled: rule.enabled ?? 1, review_notes: rule.review_notes || "",
		});
		const get_source_account_options = (row_code) => [{ label: __("全部科目"), value: "" }].concat(
			(rows.find((row) => row.row_code === row_code)?.mappings || []).map((mapping) => ({
				label: this.get_account_label(mapping.account, mapping), value: mapping.account,
			}))
		);
		const refresh_source_accounts = (preserve_value = false) => {
			const current = preserve_value ? dialog.get_value("source_account") : "";
			const source_options = get_source_account_options(dialog.get_value("source_row_code"));
			dialog.set_df_property("source_account", "options", source_options);
			dialog.set_value(
				"source_account",
				source_options.some((option) => option.value === current) ? current : ""
			);
		};
		refresh_source_accounts(true);
		dialog.fields_dict.source_row_code.$input.on("change", () => refresh_source_accounts());
		const update_sign_scope = () => {
			const has_source_account = Boolean(dialog.get_value("source_account"));
			dialog.set_df_property(
				"normal_sign",
				"label",
				has_source_account ? __("来源科目余额贡献符号") : __("正常余额符号")
			);
			dialog.set_df_property(
				"normal_sign",
				"description",
				has_source_account
					? __("按该科目对来源报表项目的余额贡献判断；负数时按规则重分类")
					: __("报表项目正常显示为正数，负数时按规则重分类")
			);
		};
		dialog.fields_dict.source_account.$input.on("change", update_sign_scope);
		update_sign_scope();
		dialog.show();
	}

	render_configuration() {
		if (!this.can_edit_template) return;
		const configuration = this.data.configuration || {};
		this.$summary.find(".smc-summary-config").remove();
		const configs = [
			["report_effective_from", __("报表生效日期"), configuration.report_effective_from],
			["mapping_effective_from", __("科目映射统一生效日期"), configuration.mapping_effective_from],
		];
		configs.forEach(([fieldname, label, value]) => {
			const $configuration = $(`
				<div class="summary-item smc-summary-config">
					<div class="summary-label">${label}</div>
					<div class="smc-summary-config__control">
						<div class="smc-date-control"></div>
						<button class="btn btn-xs btn-primary smc-save-configuration">${__("保存")}</button>
					</div>
				</div>
			`);
			this.$summary.append($configuration);
			const date_field = frappe.ui.form.make_control({
				parent: $configuration.find(".smc-date-control"),
				df: { fieldname, label, fieldtype: "Date" },
				render_input: true,
			});
			date_field.set_value(value || null);
			$configuration.find(".smc-save-configuration").on("click", async () => {
			await frappe.xcall("china_finance.services.statement_mapping_console.save_mapping_configuration", {
					company: this.company.get_value(),
					template: this.data.template.name,
				report_effective_from: fieldname === "report_effective_from" ? date_field.get_value() : configuration.report_effective_from,
				mapping_effective_from: fieldname === "mapping_effective_from" ? date_field.get_value() : configuration.mapping_effective_from,
			});
			frappe.show_alert({ message: __("生效日期配置已保存"), indicator: "green" });
			this.refresh();
		});
		});
	}

	render_summary() {
		const summary = this.data.summary;
		const items = [
			{ label: __("Mapped Applicable Accounts"), value: `${summary.mapped_accounts} / ${summary.total_leaf_accounts}`, accent: "accent-blue" },
			{
				label: __("Pending Review"), value: summary.pending_review,
				css: summary.pending_review ? "warning" : "", accent: "accent-orange", filter: this.pending_only,
			},
			{
				label: __("Unmapped Applicable Accounts"), value: summary.unmapped_accounts,
				css: summary.unmapped_accounts ? "danger" : "", accent: "accent-red", filter: this.unmapped_only,
			},
			{ label: __("Template"), value: `${this.data.template.accounting_standard} v${this.data.template.version}`, accent: "" },
		];
		this.$summary.empty();
		items.forEach((item) => {
			const clickable = item.filter && Number(item.value) > 0;
			const $item = $(
				`<div class="summary-item ${item.accent} ${clickable ? "clickable" : ""}" ${clickable ? `title="${__("Click to filter")}"` : ""}>
					<div class="summary-label">${frappe.utils.escape_html(String(item.label))}</div>
					<div class="summary-value ${item.css || ""}">${frappe.utils.escape_html(String(item.value))}</div>
				</div>`,
			);
			if (clickable) {
				$item.on("click", () => item.filter.set_value(item.filter.get_value() ? 0 : 1));
			}
			this.$summary.append($item);
		});
	}

	render_rows() {
		const scroll_top = this.$rows.find(".smc-rows__list").scrollTop() || 0;
		const pending_only = this.pending_only.get_value();
		const rows = this.data.rows;
		const visibility = this.compute_row_visibility(rows, pending_only);
		const $panel = $(
			`<div>
				<div class="smc-panel-title">${frappe.utils.escape_html(this.template_title())}</div>
				<div class="smc-review-toolbar"></div>
				<div class="smc-rows__list"></div>
			</div>`,
		);
		const $toolbar = $panel.find(".smc-review-toolbar");
		if (this.can_write) {
			const pending_rows = rows.filter((row) => row.mappings.some((mapping) => !mapping.reviewed));
			$toolbar.html(
				`<span>${__("选择报表行进行批量复核")}</span>
				<span class="text-muted">${this.selected_rows.size ? __("已选择 {0} 行", [this.selected_rows.size]) : __("未选择")}</span>
				<button class="btn btn-xs btn-default smc-review-all">${__("选择待复核行")}</button>
				<button class="btn btn-xs btn-primary smc-review-selected" ${this.selected_rows.size ? "" : "disabled"}>${__("批量复核")}</button>
				<button class="btn btn-xs btn-default smc-unreview-selected" ${this.selected_rows.size ? "" : "disabled"}>${__("取消复核")}</button>
				<button class="btn btn-xs btn-default smc-clear-rows" ${this.selected_rows.size ? "" : "disabled"}>${__("清除选择")}</button>`,
			);
			$toolbar.find(".smc-review-all").on("click", () => {
				pending_rows.forEach((row) => this.selected_rows.add(row.row_code));
				this.render_rows();
			});
			$toolbar.find(".smc-review-selected").on("click", () => this.review_selected_rows(1));
			$toolbar.find(".smc-unreview-selected").on("click", () => this.review_selected_rows(0));
			$toolbar.find(".smc-clear-rows").on("click", () => {
				this.selected_rows.clear();
				this.render_rows();
			});
		}
		const $list = $panel.find(".smc-rows__list");
		rows.forEach((row, index) => {
			if (!visibility.visible.has(index)) return;
			$list.append(this.render_row(row, visibility.children.has(index), index));
		});
		if (!$list.children().length) {
			$list.html(`<div class="smc-empty">${__("No Data")}</div>`);
		}
		this.$rows.html($panel);
		this.$rows.find(".smc-rows__list").scrollTop(scroll_top);
	}

	template_title() {
		const statement_labels = {
			"Balance Sheet": __("Balance Sheet"),
			"Profit and Loss": __("Profit and Loss"),
			"Cash Flow": __("Cash Flow"),
			"Changes in Equity": __("Changes in Equity"),
		};
		const template = this.data.template;
		const statement_label = statement_labels[template.statement_type] || template.statement_type;
		return `${template.accounting_standard} · ${statement_label} · v${template.version}`;
	}

	compute_row_visibility(rows, pending_only) {
		const visible = new Set();
		const children = new Set();
		rows.forEach((row, index) => {
			if (rows[index + 1] && rows[index + 1].indent > row.indent) children.add(index);
		});
		const is_content_visible = (row) =>
			!pending_only || row.row_type !== "Mapped Accounts" || row.mappings.some((mapping) => !mapping.reviewed);
		rows.forEach((row, index) => {
			if (!is_content_visible(row)) return;
			// A row is hidden when any ancestor (nearest previous row with smaller indent) is collapsed.
			let hidden = false;
			for (let parent = index - 1; parent >= 0; parent--) {
				if (rows[parent].indent >= row.indent) continue;
				if (this.collapsed_rows.has(rows[parent].row_code)) {
					hidden = true;
					break;
				}
				if (rows[parent].indent === 0) break;
			}
			if (!hidden) visible.add(index);
		});
		return { visible, children };
	}

	render_row(row, has_children, index) {
		const collapsed = this.collapsed_rows.has(row.row_code);
		const is_heading = row.row_type === "Heading";
		const pending_count = row.mappings.filter((mapping) => !mapping.reviewed).length;
		const $row = $(
			`<div class="smc-row ${is_heading ? "smc-row--heading" : ""} ${row.bold ? "smc-row--bold" : ""}"></div>`,
		);
		const $head = $('<div class="smc-row__head"></div>').appendTo($row);
		if (this.can_write && row.mappings.length) {
			const $select = $('<input type="checkbox" class="smc-row__select">')
				.prop("checked", this.selected_rows.has(row.row_code))
				.attr("aria-label", __("选择报表行"));
			$select.on("click", (event) => event.stopPropagation());
			$select.on("change", () => {
				if ($select.prop("checked")) this.selected_rows.add(row.row_code);
				else this.selected_rows.delete(row.row_code);
				this.render_rows();
			});
			$head.append($select);
		}
		$head.append(
			`<span class="smc-row__toggle" style="margin-left: ${row.indent * 18}px">${has_children ? (collapsed ? "▸" : "▾") : ""}</span>`,
		);
		$head.append(`<span class="smc-row__label" title="${frappe.utils.escape_html(row.row_code)}">${frappe.utils.escape_html(__(row.label))}</span>`);
		if (row.balance_direction) {
			$head.append(`<span class="smc-row__direction">${frappe.utils.escape_html(__(row.balance_direction))}</span>`);
		}
		if (pending_count) {
			$head.append(`<span class="smc-row__pending">${__("Pending Review")} ${pending_count}</span>`);
		}
		if (this.can_write || this.can_edit_template) {
			const $actions = $('<span class="smc-row__actions"></span>').appendTo($head);
			if (this.can_edit_template && row.row_type === "Formula") {
				const $edit = $(`<button class="btn btn-xs btn-default">${__("Edit Formula")}</button>`);
				$edit.on("click", () => this.edit_formula(row));
				$actions.append($edit);
			}
			if (this.can_write) {
				if (this.selected_accounts.size && row.row_type === "Mapped Accounts") {
					const $map = $(`<button class="btn btn-xs btn-primary">${__("Map Here")}</button>`);
					$map.on("click", () => this.map_selected_accounts(row));
					$actions.append($map);
				}
				if (row.mappings.length) {
					const all_reviewed = !pending_count;
					const $review = $(
						`<button class="btn btn-xs btn-default">${all_reviewed ? __("Unreview Row") : __("Review Row")}</button>`,
					);
					$review.on("click", () => this.set_row_reviewed(row, all_reviewed ? 0 : 1));
					$actions.append($review);
				}
			}
		}
		if (has_children) {
			$head.find(".smc-row__toggle").on("click", () => {
				if (collapsed) {
					this.collapsed_rows.delete(row.row_code);
				} else {
					this.collapsed_rows.add(row.row_code);
				}
				this.render_rows();
			});
		}
		if (row.formula) {
			$row.append(`<div class="smc-row__formula">${this.render_formula(row.formula)}</div>`);
		} else if (row.calculation_description) {
			$row.append(`<div class="smc-row__formula smc-row__formula--implicit"><span class="text-muted">${__("计算逻辑")}：</span>${frappe.utils.escape_html(row.calculation_description)}</div>`);
		}
		const pending_only = this.pending_only.get_value();
		const chips = row.mappings.filter((mapping) => !pending_only || !mapping.reviewed);
		if (chips.length) {
			const $chips = $('<div class="smc-chips"></div>').appendTo($row);
			chips.forEach((mapping) => $chips.append(this.render_chip(mapping)));
		}
		if (row.row_type !== "Mapped Accounts") {
			this.render_aggregate_section($row, index, pending_only);
		}
		return $row;
	}

	get_aggregate_accounts(index) {
		const rows = this.data.rows;
		const collected = new Map();
		const collect = (row, visited) => {
			if (visited.has(row.row_code)) return;
			visited.add(row.row_code);
			row.mappings.forEach((mapping) => collected.set(mapping.name, mapping));
			const row_index = rows.indexOf(row);
			if (row.row_type === "Heading") {
				// Every deeper row until the next sibling heading is a descendant.
				for (let i = row_index + 1; i < rows.length && rows[i].indent > row.indent; i++) {
					rows[i].mappings.forEach((mapping) => collected.set(mapping.name, mapping));
				}
			} else if (row.row_type === "Formula") {
				this.extract_codes(row.formula || "").forEach((code) => {
					const target = rows.find((candidate) => candidate.row_code === code);
					if (target) collect(target, visited);
				});
			}
		};
		collect(rows[index], new Set());
		return Array.from(collected.values());
	}

	extract_codes(formula) {
		return formula.match(/[A-Z][A-Z0-9_]*/g) || [];
	}

	render_aggregate_section($row, index, pending_only) {
		const accounts = this.get_aggregate_accounts(index).filter(
			(mapping) => !pending_only || !mapping.reviewed,
		);
		if (!accounts.length) return;
		const row = this.data.rows[index];
		const expanded = this.expanded_aggregates.has(row.row_code);
		const $toggle = $(
			`<div class="smc-aggregate-toggle">${expanded ? "▾" : "▸"} ${__("{0} mapped accounts", [accounts.length])}</div>`,
		);
		$toggle.on("click", () => {
			if (expanded) {
				this.expanded_aggregates.delete(row.row_code);
			} else {
				this.expanded_aggregates.add(row.row_code);
			}
			this.render_rows();
		});
		$row.append($toggle);
		if (expanded) {
			const $chips = $('<div class="smc-chips smc-chips--aggregate"></div>').appendTo($row);
			accounts.forEach((mapping) => {
				const label = this.get_account_label(mapping);
				$chips.append(
					`<span class="smc-chip"><span class="smc-chip__dot ${mapping.reviewed ? "smc-chip__dot--reviewed" : "smc-chip__dot--pending"}"></span><span>${frappe.utils.escape_html(label)}</span></span>`,
				);
			});
		}
	}

	render_formula(formula) {
		// Show row labels instead of raw row codes so the calculation is readable.
		const label_by_code = new Map(this.data.rows.map((row) => [row.row_code, row.label]));
		return formula
			.split(/([A-Za-z_][A-Za-z0-9_]*)/g)
			.map((token) => {
				if (label_by_code.has(token)) {
					return `<span class="smc-formula-token" title="${frappe.utils.escape_html(token)}">${frappe.utils.escape_html(label_by_code.get(token))}</span>`;
				}
				return frappe.utils.escape_html(token);
			})
			.join("");
	}

	edit_formula(row) {
		const dialog = new frappe.ui.Dialog({
			title: `${__("Edit Formula")} - ${row.label}`,
			fields: [
				{
					fieldname: "formula",
					label: __("Formula"),
					fieldtype: "Code",
					options: "PythonExpression",
					default: this.convert_codes_to_labels(row.formula || ""),
					description: __("You can write the formula with row labels"),
					reqd: 1,
				},
				{
					fieldname: "available_codes",
					label: __("Available Row Codes"),
					fieldtype: "HTML",
				},
			],
			primary_action_label: __("Save"),
			primary_action: async (values) => {
				const { formula, unknown } = this.convert_labels_to_codes(values.formula || "");
				if (unknown.length) {
					frappe.msgprint({
						title: __("Unrecognized Items"),
						message: __("Invalid item name or row code: {0}", [unknown.join(", ")]),
						indicator: "red",
					});
					return;
				}
				await frappe.xcall("china_finance.services.statement_mapping_console.save_template_formula", {
					template: this.data.template.name,
					row_code: row.row_code,
					formula,
				});
				frappe.show_alert({ message: __("Formula saved"), indicator: "green" });
				dialog.hide();
				this.refresh();
			},
		});
		const $codes_wrapper = dialog.fields_dict.available_codes.wrapper;
		const $toolbar = $('<div class="smc-op-toolbar"></div>').appendTo($codes_wrapper);
		const operators = [
			["+", "+"],
			["−", "-"],
			["×", "*"],
			["÷", "/"],
			["(", "("],
			[")", ")"],
		];
		operators.forEach(([label, symbol]) => {
			const $button = $(`<button type="button" class="smc-op-btn">${label}</button>`);
			$button.on("click", () => {
				const current = (dialog.get_value("formula") || "").trimEnd();
				let next;
				if (symbol === "(") {
					next = current ? `${current} (` : "(";
				} else if (symbol === ")") {
					next = `${current}) `;
				} else {
					next = current ? `${current} ${symbol} ` : `${symbol} `;
				}
				dialog.set_value("formula", next);
			});
			$toolbar.append($button);
		});
		const $chips = $('<div class="smc-code-chips"></div>').appendTo($codes_wrapper);
		this.data.rows.forEach((template_row) => {
			const $chip = $(
				`<button type="button" class="smc-code-chip" title="${frappe.utils.escape_html(template_row.row_code)}">${frappe.utils.escape_html(__(template_row.label))}</button>`,
			);
			$chip.on("click", () => {
				const current = (dialog.get_value("formula") || "").trimEnd();
				const glue = current && !/[+\-*/(]\s*$/.test(current) ? " + " : "";
				dialog.set_value("formula", `${current}${glue}${template_row.label}`);
			});
			$chips.append($chip);
		});
		dialog.show();
	}

	convert_codes_to_labels(formula) {
		const label_by_code = new Map(this.data.rows.map((row) => [row.row_code, row.label]));
		return formula
			.split(/([\p{L}\p{N}_]+)/gu)
			.map((token) => label_by_code.get(token) || token)
			.join("");
	}

	convert_labels_to_codes(formula) {
		const code_by_label = new Map(this.data.rows.map((row) => [row.label, row.row_code]));
		const codes = new Set(this.data.rows.map((row) => row.row_code));
		const unknown = [];
		const converted = formula
			.split(/([\p{L}\p{N}_]+)/gu)
			.map((token) => {
				if (code_by_label.has(token)) return code_by_label.get(token);
				if (codes.has(token)) return token;
				if (/^[\p{L}_][\p{L}\p{N}_]*$/u.test(token)) unknown.push(token);
				return token;
			})
			.join("");
		return { formula: converted, unknown };
	}

	render_chip(mapping) {
		const label = this.get_account_label(mapping);
		const $chip = $(
			`<span class="smc-chip smc-chip--link" title="${frappe.utils.escape_html(__("查看该科目总账"))}"></span>`,
		);
		$chip.on("click", () => frappe.set_route("query-report", "General Ledger", {
			company: this.company.get_value(), account: mapping.account,
		}));
		$chip.append(
			`<span class="smc-chip__dot ${mapping.reviewed ? "smc-chip__dot--reviewed" : "smc-chip__dot--pending"}"></span>`,
		);
		$chip.append(`<span>${frappe.utils.escape_html(label)}</span>`);
		if (mapping.debit_total !== undefined) {
			const totals = __("借 {0} · 贷 {1} · 余额 {2}", [frappe.format(mapping.debit_total, { fieldtype: "Currency", options: "currency" }, {}, this.data), frappe.format(mapping.credit_total, { fieldtype: "Currency", options: "currency" }, {}, this.data), frappe.format(mapping.balance, { fieldtype: "Currency", options: "currency" }, {}, this.data)]);
			$chip.attr("title", totals);
		}
		if (mapping.sign_multiplier === -1) {
			$chip.append('<span class="smc-chip__sign">×(-1)</span>');
		}
		if (this.can_write) {
			const $remove = $(`<button class="smc-chip__remove" title="${__("Remove Mapping")}">×</button>`);
			$remove.on("click", (event) => { event.stopPropagation(); this.remove_mapping(mapping); });
			$chip.append($remove);
		}
		return $chip;
	}

	render_accounts() {
		if (!this.data) return;
		const scroll_top = this.$accounts.find(".smc-accounts__list").scrollTop() || 0;
		const keyword = (this.account_search.get_value() || "").trim().toLowerCase();
		const $panel = $(`<div><div class="smc-panel-title">${__("Accounts")} (${this.data.summary.total_leaf_accounts})</div></div>`);
		if (this.can_write && this.selected_accounts.size) {
			const selected = this.data.accounts.filter((account) => this.selected_accounts.has(account.name));
			const suggestions = selected.filter((account) => account.likely_row);
			const suggestion_text = suggestions.length
				? suggestions.map((account) => `${account.account_number || account.account_name} -> ${account.likely_row.label}`).join("; ")
				: __("当前报表中无法可靠判断所选科目的映射，请人工选择报表项目。");
			const $bar = $(
				`<div class="smc-selection-bar">
					<span>${__("{0} accounts selected", [this.selected_accounts.size])}</span>
					<span class="smc-selection-hint" title="${frappe.utils.escape_html(suggestion_text)}">${frappe.utils.escape_html(suggestion_text)}</span>
					<button class="btn btn-xs btn-default smc-selection-clear" style="margin-left:auto">${__("Clear")}</button>
				</div>`,
			);
			if (suggestions.length === 1 && selected.length === 1) {
				const $apply = $(`<button class="btn btn-xs btn-primary">${__("按建议映射")}</button>`);
				$apply.on("click", () => {
					const row = this.data.rows.find((item) => item.row_code === suggestions[0].likely_row.row_code);
					if (row) this.map_selected_accounts(row);
				});
				$bar.find(".smc-selection-clear").before($apply);
			}
			$bar.find(".smc-selection-clear").on("click", () => {
				this.selected_accounts.clear();
				this.render_rows();
				this.render_accounts();
			});
			$panel.append($bar);
		}
		const $list = $('<div class="smc-accounts__list"></div>').appendTo($panel);
		if (keyword) {
			this.render_account_search($list, keyword);
		} else {
			this.render_account_tree($list);
		}
		this.$accounts.html($panel);
		this.$accounts.find(".smc-accounts__list").scrollTop(scroll_top);
	}

	render_account_search($list, keyword) {
		const target_by_account = this.account_targets();
		const unmapped_names = new Set(this.data.unmapped_accounts.map((account) => account.name));
		const matches = (account) =>
			(account.account_name || "").toLowerCase().includes(keyword) ||
			(account.account_number || "").toLowerCase().includes(keyword) ||
			account.name.toLowerCase().includes(keyword);
		const leaves = this.data.accounts.filter((account) => !account.is_group && matches(account));
		leaves.forEach((account) =>
			$list.append(this.render_account(account, target_by_account.get(account.name), unmapped_names.has(account.name), 0)),
		);
		if (!leaves.length) {
			$list.append(`<div class="smc-empty">${__("No Data")}</div>`);
		}
	}

	render_account_tree($list) {
		const unmapped_names = new Set(this.data.unmapped_accounts.map((account) => account.name));
		const target_by_account = this.account_targets();
		const only_unmapped = this.unmapped_only.get_value();
		const children_by_parent = new Map();
		this.data.accounts.forEach((account) => {
			const key = account.parent_account || "";
			if (!children_by_parent.has(key)) children_by_parent.set(key, []);
			children_by_parent.get(key).push(account);
		});
		const has_unmapped_leaf = (name) => {
			return (children_by_parent.get(name) || []).some(
				(child) =>
					(!child.is_group && unmapped_names.has(child.name)) ||
					(child.is_group && has_unmapped_leaf(child.name)),
			);
		};
		const count_unmapped_leaf = (name) =>
			(children_by_parent.get(name) || []).reduce(
				(count, child) =>
					count +
					(!child.is_group && unmapped_names.has(child.name) ? 1 : 0) +
					(child.is_group ? count_unmapped_leaf(child.name) : 0),
				0,
			);
		const render_node = (account, depth) => {
			if (account.is_group) {
				if (only_unmapped && !has_unmapped_leaf(account.name)) return;
				const collapsed = this.collapsed_accounts.has(account.name);
				const label = this.get_account_label(account);
				const $group = $(
					`<div class="smc-account smc-account--group" style="padding-left: ${10 + depth * 16}px">
						<span class="smc-account__toggle">${collapsed ? "▸" : "▾"}</span>
						<span class="smc-account__grouplabel" title="${frappe.utils.escape_html(label)}">${frappe.utils.escape_html(label)}</span>
					</div>`,
				);
				const unmapped_count = count_unmapped_leaf(account.name);
				if (unmapped_count) {
					$group.append(`<span class="smc-account__badge">${unmapped_count}</span>`);
				}
				$group.on("click", () => {
					if (collapsed) {
						this.collapsed_accounts.delete(account.name);
					} else {
						this.collapsed_accounts.add(account.name);
					}
					this.render_accounts();
				});
				$list.append($group);
				if (!collapsed) {
					(children_by_parent.get(account.name) || []).forEach((child) => render_node(child, depth + 1));
				}
			} else {
				const is_unmapped = unmapped_names.has(account.name);
				if (only_unmapped && !is_unmapped) return;
				$list.append(this.render_account(account, target_by_account.get(account.name), is_unmapped, depth));
			}
		};
		(children_by_parent.get("") || []).forEach((account) => render_node(account, 0));
		if (!$list.children().length) {
			$list.append(`<div class="smc-empty">${__("No Data")}</div>`);
		}
	}

	render_account(account, target_label, unmapped, depth = 0) {
		const label = this.get_account_label(account);
		const $item = $('<div class="smc-account"></div>').css("padding-left", `${18 + depth * 16}px`);
		if (unmapped) {
			$item.append('<span class="smc-account__dot"></span>');
		}
		if (this.can_write) {
			const $checkbox = $('<input type="checkbox">').prop("checked", this.selected_accounts.has(account.name));
			$checkbox.on("change", () => {
				if ($checkbox.prop("checked")) {
					this.selected_accounts.add(account.name);
				} else {
					this.selected_accounts.delete(account.name);
				}
				this.render_rows();
				this.render_accounts();
			});
			$item.append($checkbox);
		}
		$item.append(`<span title="${frappe.utils.escape_html(label)}">${frappe.utils.escape_html(label)}</span>`);
		if (target_label) {
			$item.append(`<span class="smc-account__target">→ ${frappe.utils.escape_html(target_label)}</span>`);
		} else if (unmapped && account.likely_row) {
			const suggestion = `${account.likely_row.row_code} ${account.likely_row.label}`;
			$item.append(`<span class="smc-account__target smc-account__suggestion">${__("建议映射")}: ${frappe.utils.escape_html(suggestion)}</span>`);
		}
		return $item;
	}

	get_account_label(account_name_or_row, row = null) {
		const account = typeof account_name_or_row === "string"
			? (this.data?.accounts || []).find((item) => item.name === account_name_or_row)
				|| (this.data?.rows || []).flatMap((item) => item.mappings || []).find((item) => item.account === account_name_or_row)
			: account_name_or_row;
		if (account?.account_label) return account.account_label;
		if (account?.account_number || account?.account_name) {
			return [account.account_number, account.account_name].filter(Boolean).join(" ");
		}
		if (row?.account_label) return row.account_label;
		return typeof account_name_or_row === "string" ? account_name_or_row : "";
	}

	account_targets() {
		const targets = new Map();
		this.data.rows.forEach((row) => {
			row.mappings.forEach((mapping) => {
				if (!targets.has(mapping.account)) targets.set(mapping.account, row.label);
			});
		});
		return targets;
	}

	async map_selected_accounts(row) {
		const accounts = Array.from(this.selected_accounts);
		if (!accounts.length) return;
		let cash_options = {};
		if (this.data.template.statement_type === "Cash Flow") {
			cash_options = await this.ask_cash_flow_options(row.row_code);
			if (!cash_options) return;
		}
		for (const account of accounts) {
			await frappe.xcall("china_finance.services.statement_mapping_console.save_mapping", {
				company: this.company.get_value(),
				template: this.data.template.name,
				account,
				row_code: row.row_code,
				...cash_options,
			});
		}
		frappe.show_alert({ message: __("Saved {0} mappings", [accounts.length]), indicator: "green" });
		this.selected_accounts.clear();
		this.refresh();
	}

	ask_cash_flow_options(default_row_code) {
		return new Promise((resolve) => {
			const options = this.data.rows.map((row) => `${row.row_code} | ${row.label}`);
			const dialog = new frappe.ui.Dialog({
				title: __("Cash Flow Mapping"),
				fields: [
					{
						fieldname: "cash_inflow_row_code",
						label: __("Inflow Row"),
						fieldtype: "Select",
						options: options.join("\n"),
						default: options.find((option) => option.startsWith(default_row_code)),
						reqd: 1,
					},
					{
						fieldname: "cash_outflow_row_code",
						label: __("Outflow Row"),
						fieldtype: "Select",
						options: options.join("\n"),
						default: options.find((option) => option.startsWith(default_row_code)),
						reqd: 1,
					},
					{
						fieldname: "sign_multiplier",
						label: __("Sign Multiplier"),
						fieldtype: "Select",
						options: "1\n-1",
						default: "1",
						reqd: 1,
					},
				],
				primary_action_label: __("Save"),
				primary_action: (values) => {
					resolve({
						cash_inflow_row_code: values.cash_inflow_row_code.split(" | ")[0],
						cash_outflow_row_code: values.cash_outflow_row_code.split(" | ")[0],
						sign_multiplier: values.sign_multiplier,
					});
					dialog.hide();
				},
			});
			dialog.onhide = () => resolve(null);
			dialog.show();
		});
	}

	remove_mapping(mapping) {
		frappe.confirm(__("Remove this mapping?"), async () => {
			await frappe.xcall("china_finance.services.statement_mapping_console.remove_mapping", {
				name: mapping.name,
			});
			frappe.show_alert({ message: __("Mapping removed"), indicator: "green" });
			this.refresh();
		});
	}

	async set_row_reviewed(row, reviewed) {
		const scroll_top = this.$rows.find(".smc-rows__list").scrollTop() || 0;
		await frappe.xcall("china_finance.services.statement_mapping_console.set_mappings_reviewed", {
			names: row.mappings.map((mapping) => mapping.name),
			reviewed,
		});
		frappe.show_alert({
			message: reviewed ? __("Row reviewed") : __("Review cleared"),
			indicator: "green",
		});
		this.refresh(scroll_top);
	}

	async review_selected_rows(reviewed) {
		const rows = this.data.rows.filter((row) => this.selected_rows.has(row.row_code));
		const names = rows.flatMap((row) => row.mappings.map((mapping) => mapping.name));
		if (!names.length) return;
		const scroll_top = this.$rows.find(".smc-rows__list").scrollTop() || 0;
		await frappe.xcall("china_finance.services.statement_mapping_console.set_mappings_reviewed", {
			names,
			reviewed,
		});
		frappe.show_alert({
			message: reviewed ? __("已批量复核 {0} 行", [rows.length]) : __("已取消复核 {0} 行", [rows.length]),
			indicator: "green",
		});
		this.refresh(scroll_top);
	}
}
