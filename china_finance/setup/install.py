import json

import frappe


ROLES = (
	"China Finance Manager",
	"China Finance User",
	"China Voucher Maker",
	"China Voucher Reviewer",
	"China Voucher Poster",
	"China Tax User",
	"China Archive User",
	"China Finance Auditor",
)

DAILY_NAVIGATION = (
	("China Cash Flow Assignment", "现金流量指定单", "DocType"),
	("China Tax Invoice", "中国税务发票", "DocType"),
	("China Sales Settlement", "销售结算单", "DocType"),
)

VOUCHER_NAVIGATION = (
	("China Voucher Ledger", "查凭证", "Report"),
	("China Closing Run", "月末结账", "DocType"),
)

REPORT_NAVIGATION = (
	("China Financial Statements", "中国财务报表", "Report"),
)

BANK_NAVIGATION = (
	("China Bank Receipt Import", "银行回单导入", "DocType"),
	("China Bank Receipt", "银行回单", "DocType"),
	("china-banking", "银行对账", "Page"),
	("China Reconciliation Statement", "对账单", "DocType"),
)

CORE_NAVIGATION = DAILY_NAVIGATION + VOUCHER_NAVIGATION + REPORT_NAVIGATION + BANK_NAVIGATION

NAVIGATION_SECTIONS = (
	("日常工作", "briefcase", DAILY_NAVIGATION),
	("凭证与结账", "notebook-pen", VOUCHER_NAVIGATION),
	("报表与查询", "sheet", REPORT_NAVIGATION),
	("银行与对账", "landmark", BANK_NAVIGATION),
)

ADMIN_NAVIGATION_GROUPS = (
	("凭证明细与历史", "notebook-pen", (
		("Journal Entry", "记账凭证明细", "DocType"),
		("China Accounting Voucher", "中国会计凭证快照", "DocType"),
		("Period Closing Voucher", "损益结转凭证", "DocType"),
	)),
	(
		"启用与基础设置", "settings", (
			("China Finance Settings", "中国财务设置", "DocType"),
			("China Reconciliation Scope", "对账范围配置", "DocType"),
			("China Tax Account Mapping", "税务科目映射", "DocType"),
			("Account", "科目表", "DocType"),
		),
	),
	(
		"业务控制规则", "sliders-horizontal", (
			("China Bank Receipt Rule", "银行回单科目规则", "DocType"),
			("China Sales Settlement Rule", "销售结算规则", "DocType"),
			("China Invoice Control Rule", "开票控制规则", "DocType"),
			("China Purchase Reconciliation Rule", "采购应付对账规则", "DocType"),
		),
	),
	(
		"法定报表配置", "file-text", (
			("China Financial Statement Template", "财务报表模板", "DocType"),
		("China Financial Statement Mapping", "财务报表科目映射", "DocType"),
		("China Prior Period Error Adjustment", "前期差错更正", "DocType"),
			("China Cash Equivalent Scope", "现金及现金等价物范围", "DocType"),
			("china-statement-mapping", "科目映射控制台", "Page"),
			("China Accounting Policy", "中国会计政策", "DocType"),
			("China Financial Statement Notes", "财务报表附注", "DocType"),
		),
	),
	(
		"审计与归档", "archive", (
			("China Voucher Sync Issue", "凭证快照同步异常", "DocType"),
			("China Electronic Document", "电子会计档案", "DocType"),
			("China Report Snapshot", "报表快照", "DocType"),
			("China Tax Integration Call", "税务接口调用日志", "DocType"),
		),
	),
)

ADMIN_NAVIGATION = tuple(link for _label, _icon, links in ADMIN_NAVIGATION_GROUPS for link in links)

MAPPING_CONSOLE_LINK = ("china-statement-mapping", "科目映射控制台", "Page")
MAPPING_CONSOLE_ICON = "list-tree"
ACCOUNT_TABLE_LINK = ("Account", "科目表", "DocType")
ACCOUNT_TABLE_URL = "/desk/account/view/tree?sidebar=China%20Finance"

WORKSPACE_CONTENT = json.dumps([
	*[
		{"id": f"cf-nav-{index}", "type": "card", "data": {"card_name": label, "col": 4}}
		for index, (label, _icon, _links) in enumerate(NAVIGATION_SECTIONS, 1)
	],
	*[
		{"id": f"cf-admin-{index}", "type": "card", "data": {"card_name": label, "col": 4}}
		for index, (label, _icon, _links) in enumerate(ADMIN_NAVIGATION_GROUPS, 1)
	],
], ensure_ascii=False, separators=(",", ":"))

LEGACY_WORKSPACE_CONTENT = json.dumps([
	{"id": "cf-daily", "type": "card", "data": {"card_name": "日常工作", "col": 4}},
	{"id": "cf-closing", "type": "card", "data": {"card_name": "报表与结账", "col": 4}},
	{"id": "cf-admin", "type": "card", "data": {"card_name": "管理与审计", "col": 4}},
], ensure_ascii=False, separators=(",", ":"))

LEGACY_NAVIGATION = {
	"Bank Reconciliation Tool",
	"China Accounting Voucher",
	"China Voucher Ledger",
	"China Voucher Integrity",
	"China Reconciliation Difference",
	"China AR AP Ledger Reconciliation",
	"China Purchase Reconciliation",
	"China Purchase Document Chain",
	"China Business Document Chain",
	"China Input Tax Deduction Batch",
	"China Tax Invoice Request",
	"China Output Invoice Reconciliation",
	"China VAT Ledger",
	"China VAT Return Worksheet",
	"Trial Balance",
}

CHINA_FINANCIAL_STATEMENT_REPORT_FILTERS = (
	{"fieldname": "company", "label": "公司", "fieldtype": "Link", "options": "Company", "mandatory": 1},
	{
		"fieldname": "statement_type", "label": "报表类型", "fieldtype": "Select",
		"options": "Balance Sheet\nProfit and Loss\nCash Flow\nAccount Activity and Balance", "mandatory": 1,
		"default": "Balance Sheet",
	},
	{"fieldname": "finance_book", "label": "财务账簿", "fieldtype": "Link", "options": "Finance Book"},
	{"fieldname": "fiscal_year", "label": "财年", "fieldtype": "Link", "options": "Fiscal Year"},
	{"fieldname": "periodicity", "label": "期间", "fieldtype": "Select", "options": "年度\n季度\n月度", "default": "年度"},
	{"fieldname": "accounting_period", "label": "会计期间", "fieldtype": "Select", "options": "全年", "default": "全年"},
	{"fieldname": "from_date", "label": "本期起始日期", "fieldtype": "Date"},
	{"fieldname": "to_date", "label": "本期截止日期", "fieldtype": "Date", "mandatory": 1},
	{"fieldname": "comparison_from_date", "label": "比较期起始日期", "fieldtype": "Date"},
	{"fieldname": "comparison_to_date", "label": "比较期截止日期", "fieldtype": "Date"},
	{"fieldname": "cost_center", "label": "成本中心", "fieldtype": "Link", "options": "Cost Center"},
	{"fieldname": "project", "label": "项目", "fieldtype": "Link", "options": "Project"},
	{"fieldname": "account", "label": "科目", "fieldtype": "Link", "options": "Account"},
	{"fieldname": "show_zero_values", "label": "显示零余额", "fieldtype": "Check", "default": 0},
	{"fieldname": "expand_party", "label": "展开往来明细", "fieldtype": "Check", "default": 1},
)


def after_install():
	sync_roles()
	from china_finance.setup.templates import (
		ensure_company_mappings,
		refresh_small_enterprise_v3_templates,
		seed_cash_equivalent_scope,
		seed_statement_templates,
	)

	seed_statement_templates()
	refresh_small_enterprise_v3_templates()
	seed_cash_equivalent_scope()
	from china_finance.api import initialize_existing_profile_companies
	initialize_existing_profile_companies()
	ensure_company_mappings()
	from china_finance.setup.china_coa_profile import sync_enabled_company_profiles
	sync_enabled_company_profiles()
	sync_sales_settlement_custom_fields()
	sync_china_financial_statement_report_filters()
	sync_china_financial_statement_print_format()
	sync_china_accounting_voucher_print_format()
	backfill_bank_transaction_summaries()
	sync_reclassification_rules()
	sync_account_display_properties()
	sync_navigation_metadata()


def after_migrate():
	sync_roles()
	from china_finance.setup.templates import (
		ensure_company_mappings,
		refresh_small_enterprise_v3_templates,
		seed_cash_equivalent_scope,
		seed_statement_templates,
	)

	seed_statement_templates()
	refresh_small_enterprise_v3_templates()
	seed_cash_equivalent_scope()
	from china_finance.api import initialize_existing_profile_companies
	initialize_existing_profile_companies()
	ensure_company_mappings()
	from china_finance.setup.china_coa_profile import sync_enabled_company_profiles
	sync_enabled_company_profiles()
	sync_sales_settlement_custom_fields()
	sync_china_financial_statement_report_filters()
	sync_china_financial_statement_print_format()
	sync_china_accounting_voucher_print_format()
	backfill_bank_transaction_summaries()
	sync_reclassification_rules()
	sync_account_display_properties()
	sync_navigation_metadata()
	validate_deployment_schema()


def sync_account_display_properties():
	"""Show Account.account_name in Link fields without changing Account.name."""
	properties = (
		("title_field", "account_name", "Data"),
		("show_title_field_in_link", "1", "Check"),
	)
	changed = False
	for property_name, value, property_type in properties:
		filters = {
			"doctype_or_field": "DocType",
			"doc_type": "Account",
			"field_name": None,
			"property": property_name,
		}
		setter_name = frappe.db.exists("Property Setter", filters)
		if setter_name:
			if frappe.db.get_value("Property Setter", setter_name, "value") != value:
				frappe.db.set_value("Property Setter", setter_name, "value", value, update_modified=False)
				changed = True
			continue

		frappe.get_doc(
			{
				"doctype": "Property Setter",
				**filters,
				"value": value,
				"property_type": property_type,
				"is_system_generated": 1,
			}
		).insert(ignore_permissions=True)
		changed = True

	if changed:
		frappe.clear_cache(doctype="Account")


def sync_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)


def sync_china_financial_statement_report_filters():
	"""Update known standard filters without deleting user-added report filters."""
	report_name = "China Financial Statements"
	if not frappe.db.exists("Report", report_name):
		return
	report = frappe.get_doc("Report", report_name)
	if report.is_standard != "Yes" or report.module != "China Finance":
		return
	filter_keys = ("fieldname", "label", "fieldtype", "mandatory", "wildcard_filter", "options", "default")

	def normalize_filter(row):
		filter_row = {key: row.get(key) for key in filter_keys}
		for key in ("mandatory", "wildcard_filter"):
			filter_row[key] = int(bool(filter_row[key]))
		for key in ("options", "default"):
			filter_row[key] = str(filter_row[key]) if filter_row[key] else None
		return filter_row

	known_fieldnames = {row["fieldname"] for row in CHINA_FINANCIAL_STATEMENT_REPORT_FILTERS}
	# Retire the old toggle instead of keeping it as a user-added filter.
	known_fieldnames.add("include_period_closing_entries")
	custom_filters = [
		normalize_filter(row)
		for row in report.filters
		if row.fieldname not in known_fieldnames
	]
	target = [normalize_filter(row) for row in CHINA_FINANCIAL_STATEMENT_REPORT_FILTERS]
	target.extend(custom_filters)
	current = [
		normalize_filter(row)
		for row in report.filters
	]
	if current != target:
		report.set("filters", target)
		report.flags.ignore_permissions = True
		report.save()


def sync_china_financial_statement_print_format():
	"""Create the report print format used by the China Financial Statements PDF dialog."""
	if not frappe.db.exists("Report", "China Financial Statements"):
		return

	name = "中国财务报表法定打印格式"
	html = r'''{%
const is_small = filters.accounting_standard === "小企业会计准则";
const is_balance_sheet = filters.statement_type === "Balance Sheet";
const is_profit_loss = filters.statement_type === "Profit and Loss";
const columns = report.get_columns_for_print().filter(col => !col.hidden);
%}
<style>
  body, html { margin: 0; padding: 0; font-family: Inter, sans-serif; color: #171717; font-size: 12px; }
  .cf-title { text-align: center; font-size: 18px; font-weight: 700; margin: 0 0 4px; }
  .cf-meta { display: flex; justify-content: space-between; border-bottom: 1px solid #777; padding: 4px 0 8px; margin-bottom: 10px; }
  .cf-meta span { margin-right: 14px; }
  .cf-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  .cf-table th, .cf-table td { border: 1px solid #999; padding: 5px 6px; vertical-align: middle; word-break: break-word; }
  .cf-table th { background: #f2f2f2; text-align: center; font-weight: 600; }
  .cf-table td:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
  .cf-table tr.cf-bold td { font-weight: 700; }
  .cf-foot { margin-top: 10px; color: #666; text-align: right; }
  @page { size: A4 landscape; margin: 10mm; }
  @media print { thead { display: table-header-group; } tr { page-break-inside: avoid; } }
</style>
<div>
  <div class="cf-title">{%= is_small && is_balance_sheet ? __("资产负债表") + "　会小企01表" : is_small && is_profit_loss ? __("利润表") + "　会小企02表" : __(report.report_name) %}</div>
  <div class="cf-meta">
    <div>
      {% if (is_small) { %}<span><b>{%= __("编制单位：") %}</b>{%= filters.company || "" %}</span>{% } else { %}<span><b>{%= __("公司：") %}</b>{%= filters.company || "" %}</span><span><b>{%= __("报表类型：") %}</b>{%= __(filters.statement_type || "") %}</span>{% } %}
    </div>
    <div>
      <span><b>{%= is_small ? __("税款所属期起止：") : __("期间：") %}</b>{%= filters.from_date || "" %} {%= __("至") %} {%= filters.to_date || "" %}</span>
      <span><b>{%= __("单位：") %}</b>{%= is_small ? __("元") : (data.find(row => row.currency)?.currency || "") %}</span>
    </div>
  </div>
  {% if (is_small && is_balance_sheet) { %}
  <table class="cf-table"><thead><tr>
    <th>{%= __("资产行次") %}</th><th>{%= __("资产项目") %}</th><th>{%= __("期末余额") %}</th><th>{%= __("年初余额") %}</th>
    <th>{%= __("负债及权益行次") %}</th><th>{%= __("负债及权益项目") %}</th><th>{%= __("期末余额") %}</th><th>{%= __("年初余额") %}</th>
  </tr></thead><tbody>
    {% for (let j = 0; j < data.length; j++) { const row = data[j]; %}
      <tr>
        <td>{%= row.asset_statutory_line_number || "" %}</td><td>{%= __(row.asset_label || "") %}</td><td class="num">{%= frappe.format(row.asset_amount || 0, {fieldtype:"Currency", options:"currency"}, {}, row) %}</td><td class="num">{%= frappe.format(row.asset_opening_amount || 0, {fieldtype:"Currency", options:"currency"}, {}, row) %}</td>
        <td>{%= row.liability_equity_statutory_line_number || "" %}</td><td>{%= __(row.liability_equity_label || "") %}</td><td class="num">{%= frappe.format(row.liability_equity_amount || 0, {fieldtype:"Currency", options:"currency"}, {}, row) %}</td><td class="num">{%= frappe.format(row.liability_equity_opening_amount || 0, {fieldtype:"Currency", options:"currency"}, {}, row) %}</td>
      </tr>
    {% } %}
  </tbody></table>
  {% } else if (is_small && is_profit_loss) { %}
  <table class="cf-table"><thead><tr><th>{%= __("项目") %}</th><th>{%= __("行次") %}</th><th>{%= __("本期金额") %}</th><th>{%= __("本年累计金额") %}</th></tr></thead><tbody>
    {% for (let j = 0; j < data.length; j++) { const row = data[j]; %}
      <tr><td>{%= __(row.label || "") %}</td><td>{%= row.statutory_line_number || "" %}</td><td class="num">{%= frappe.format(row.amount || 0, {fieldtype:"Currency", options:"currency"}, {}, row) %}</td><td class="num">{%= frappe.format(row.year_to_date_amount || row.amount || 0, {fieldtype:"Currency", options:"currency"}, {}, row) %}</td></tr>
    {% } %}
  </tbody></table>
  {% } else { %}
  <table class="cf-table">
    <thead><tr>
      {% for (let i = 0; i < columns.length; i++) { %}<th>{%= __(columns[i].label || columns[i].name || "") %}</th>{% } %}
    </tr></thead>
    <tbody>
      {% for (let j = 0; j < data.length; j++) { const row = data[j]; %}
        <tr class="{%= row.bold == 1 || row.is_total_row == 1 || row.is_group == 1 ? "cf-bold" : "" %}">
          {% for (let i = 0; i < columns.length; i++) { const col = columns[i]; const value = col.fieldname ? row[col.fieldname] : row[col.id]; %}<td>{%= value == null ? "" : col.fieldname === "label" ? __(value) : value %}</td>{% } %}
        </tr>
      {% } %}
    </tbody>
  </table>
  {% } %}
  <div class="cf-foot">{%= __("打印时间：") %}{%= frappe.datetime.str_to_user(frappe.datetime.get_datetime_as_string()) %}</div>
</div>'''

	values = {
		"doctype": "Print Format",
		"name": name,
		"print_format_for": "Report",
		"report": "China Financial Statements",
		"module": "China Finance",
		"print_format_type": "JS",
		"custom_format": 1,
		"disabled": 0,
		"standard": "No",
		"pdf_generator": "chrome",
		"font_size": 12,
		"margin_top": 10,
		"margin_bottom": 10,
		"margin_left": 10,
		"margin_right": 10,
		"page_number": "Hide",
		"html": html,
	}

	if frappe.db.exists("Print Format", name):
		doc = frappe.get_doc("Print Format", name)
		changed = any(doc.get(field) != value for field, value in values.items() if field not in ("doctype", "name"))
		if changed:
			doc.update({k: v for k, v in values.items() if k not in ("doctype", "name")})
			doc.flags.ignore_permissions = True
			doc.save()
	else:
		doc = frappe.get_doc(values)
		doc.flags.ignore_permissions = True
		doc.insert()
	frappe.clear_cache(doctype="Print Format")


def sync_china_accounting_voucher_print_format():
	"""Keep the Chinese accounting voucher printouts aligned with statutory paper layouts."""
	if not frappe.db.exists("DocType", "China Accounting Voucher"):
		return

	formats = (
		("China Accounting Voucher", "china_accounting_voucher", "china_accounting_voucher.html", 10, 8, 10, 10),
		(
			"China Accounting Voucher A5 Landscape",
			"china_accounting_voucher_a5_landscape",
			"china_accounting_voucher_a5_landscape.html",
			7,
			6,
			8,
			8,
		),
	)

	for name, folder, filename, margin_top, margin_bottom, margin_left, margin_right in formats:
		path = frappe.get_app_path("china_finance", "china_finance", "print_format", folder, filename)
		with open(path, encoding="utf-8") as template_file:
			html = template_file.read()

		values = {
			"doctype": "Print Format",
			"name": name,
			"doc_type": "China Accounting Voucher",
			"print_format_for": "DocType",
			"print_format_type": "Jinja",
			"custom_format": 1,
			"disabled": 0,
			"standard": "Yes",
			"module": "China Finance",
			"pdf_generator": "wkhtmltopdf",
			"font_size": 10,
			"margin_top": margin_top,
			"margin_bottom": margin_bottom,
			"margin_left": margin_left,
			"margin_right": margin_right,
			"page_number": "Hide",
			"html": html,
		}

		if frappe.db.exists("Print Format", name):
			doc = frappe.get_doc("Print Format", name)
			changed = any(doc.get(field) != value for field, value in values.items() if field not in ("doctype", "name"))
			if changed:
				doc.update({key: value for key, value in values.items() if key not in ("doctype", "name")})
				doc.flags.ignore_permissions = True
				doc.save()
		else:
			doc = frappe.get_doc(values)
			doc.flags.ignore_permissions = True
			doc.insert()
	frappe.clear_cache(doctype="Print Format")


def sync_sales_settlement_custom_fields():
	"""Install additive metadata for ERPNext and China Finance documents."""
	from china_finance.setup.preparation import PREPARATION_FIELDS
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields({
		"Period Closing Voucher": [
			{
				"fieldname": "custom_china_period_entries_section",
				"label": "会计分录",
				"fieldtype": "Section Break",
				"insert_after": "error_message",
			},
			{
				"fieldname": "custom_china_period_entries",
				"label": "",
				"fieldtype": "HTML",
				"insert_after": "custom_china_period_entries_section",
			},
		],
		"Journal Entry": [
			*PREPARATION_FIELDS,
			{"fieldname": "custom_china_voucher_number", "label": "凭证字号", "fieldtype": "Data", "read_only": 1, "in_list_view": 1, "insert_before": "name"},
			{"fieldname": "custom_china_bank_transaction", "label": "银行流水来源", "fieldtype": "Link", "options": "Bank Transaction", "read_only": 1, "insert_after": "cheque_no"},
		],
		"Payment Entry": [
			{"fieldname": "custom_china_voucher_number", "label": "凭证字号", "fieldtype": "Data", "read_only": 1, "in_list_view": 1, "insert_before": "name"},
		],
		"Bank Transaction": [
			{"fieldname": "custom_summary", "label": "摘要", "fieldtype": "Small Text", "insert_after": "description"},
			{"fieldname": "custom_china_journal_entry", "label": "对应记账凭证", "fieldtype": "Link", "options": "Journal Entry", "read_only": 1, "insert_after": "payment_entries"},
		],
		"Sales Order": [
			{"fieldname": "custom_china_settlement_section", "label": "中国财务结算", "fieldtype": "Section Break", "insert_after": "customer_name"},
			{"fieldname": "custom_china_settlement_mode", "label": "销售结算模式", "fieldtype": "Select", "options": "直接确认应收\n对账结算后确认应收", "read_only": 1, "insert_after": "custom_china_settlement_section"},
			{"fieldname": "custom_china_settlement_rule", "label": "销售结算规则", "fieldtype": "Link", "options": "China Sales Settlement Rule", "read_only": 1, "insert_after": "custom_china_settlement_mode"},
			{"fieldname": "custom_china_settlement_confirmation_method", "label": "确认方式", "fieldtype": "Data", "read_only": 1, "insert_after": "custom_china_settlement_rule"},
			{"fieldname": "custom_china_settlement_override", "label": "已覆盖结算模式", "fieldtype": "Check", "read_only": 1, "insert_after": "custom_china_settlement_confirmation_method"},
			{"fieldname": "custom_china_settlement_override_reason", "label": "覆盖原因", "fieldtype": "Small Text", "read_only": 1, "insert_after": "custom_china_settlement_override"},
			{"fieldname": "custom_china_settlement_override_by", "label": "覆盖操作人", "fieldtype": "Link", "options": "User", "read_only": 1, "insert_after": "custom_china_settlement_override_reason"},
			{"fieldname": "custom_china_settlement_override_on", "label": "覆盖时间", "fieldtype": "Datetime", "read_only": 1, "insert_after": "custom_china_settlement_override_by"},
			{"fieldname": "custom_china_settled_amount", "label": "已结算金额", "fieldtype": "Currency", "options": "currency", "read_only": 1, "insert_after": "custom_china_settlement_override_on"},
		],
		"Delivery Note": [
			{"fieldname": "custom_china_settlement_section", "label": "中国财务结算", "fieldtype": "Section Break", "insert_after": "customer_name"},
			{"fieldname": "custom_china_settlement_mode", "label": "销售结算模式", "fieldtype": "Select", "options": "直接确认应收\n对账结算后确认应收", "read_only": 1, "insert_after": "custom_china_settlement_section"},
			{"fieldname": "custom_china_settlement_confirmation_method", "label": "确认方式", "fieldtype": "Data", "read_only": 1, "insert_after": "custom_china_settlement_mode"},
			{"fieldname": "custom_china_settled_amount", "label": "已结算金额", "fieldtype": "Currency", "options": "currency", "read_only": 1, "insert_after": "custom_china_settlement_confirmation_method"},
			{"fieldname": "custom_china_pending_settlement_amount", "label": "待结算金额", "fieldtype": "Currency", "options": "currency", "read_only": 1, "insert_after": "custom_china_settled_amount"},
		],
		"Sales Invoice": [
			{"fieldname": "custom_china_sales_settlement", "label": "销售结算单", "fieldtype": "Link", "options": "China Sales Settlement", "read_only": 1, "insert_after": "customer_name"},
		],
	}, update=True)
	sync_period_closing_voucher_number_field()
	backfill_source_voucher_numbers()


def sync_period_closing_voucher_number_field():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields({
		"Period Closing Voucher": [
			{
				"fieldname": "custom_china_voucher_number", "label": "凭证字号", "fieldtype": "Data",
				"read_only": 1, "in_list_view": 1, "insert_after": "company",
			},
		],
	}, update=True)


def backfill_source_voucher_numbers():
	"""Keep source-document list columns synchronized with voucher snapshots."""
	from china_finance.services.voucher import FORMAL_VOUCHER_SOURCES

	for voucher in frappe.get_all(
		"China Accounting Voucher",
		filters={"docstatus": 1, "source_event": "Posting", "source_doctype": ["in", FORMAL_VOUCHER_SOURCES]},
		fields=["source_doctype", "source_name", "source_key", "statutory_number"],
	):
		if not voucher.statutory_number or voucher.source_key != f"Posting|{voucher.source_doctype}|{voucher.source_name}":
			continue
		if not frappe.db.has_column(voucher.source_doctype, "custom_china_voucher_number"):
			continue
		frappe.db.set_value(
			voucher.source_doctype,
			voucher.source_name,
			"custom_china_voucher_number",
			voucher.statutory_number,
			update_modified=False,
		)


def backfill_bank_transaction_summaries():
	"""Backfill imported summaries after the additive custom field exists."""
	if not frappe.db.has_column("Bank Transaction", "custom_summary"):
		return
	rows = frappe.get_all(
		"Bank Transaction",
		filters={"custom_summary": ["in", ["", None]]},
		fields=["name", "description"],
	)
	for row in rows:
		description = (row.description or "").strip()
		if "｜" not in description:
			continue
		summary = description.split("｜", 1)[0].strip()
		if summary:
			frappe.db.set_value("Bank Transaction", row.name, "custom_summary", summary, update_modified=False)


def sync_navigation_metadata():
	"""Keep route identifiers stable while translations provide Chinese labels."""
	sync_payments_voucher_report_link()
	sync_banking_sidebar_navigation()
	navigation_name = "China Finance"
	values_by_doctype = {
		"Workspace": {"label": navigation_name, "title": navigation_name},
		"Workspace Sidebar": {"title": navigation_name, "app": "china_finance"},
		"Desktop Icon": {
			"label": navigation_name,
			"link_type": "Workspace Sidebar",
			"link_to": navigation_name,
			"sidebar": navigation_name,
			"app": "china_finance",
			"hidden": 0,
		},
	}
	for doctype, values in values_by_doctype.items():
		if frappe.db.exists(doctype, navigation_name):
			frappe.db.set_value(doctype, navigation_name, values, update_modified=False)
	sync_simplified_navigation(navigation_name)
	if frappe.db.exists("Report", "China Purchase Document Chain"):
		frappe.db.set_value("Report", "China Purchase Document Chain", "disabled", 1, update_modified=False)

	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")
	frappe.clear_cache()


def sync_reclassification_rules():
	"""Seed default presentation rules without overwriting console changes."""
	path = frappe.get_app_path("china_finance", "config", "balance_sheet_reclassifications.json")
	with open(path, encoding="utf-8") as rules_file:
		rules = json.load(rules_file)
	for company in frappe.get_all("Company", filters={"is_group": 0}, pluck="name"):
		if not frappe.db.exists("China Finance Settings", company):
			continue
		settings = frappe.get_cached_doc("China Finance Settings", company)
		accounting_standard = settings.accounting_standard
		for template in frappe.get_all(
			"China Financial Statement Template",
			filters={"statement_type": "Balance Sheet", "accounting_standard": accounting_standard, "is_active": 1},
			fields=["name", "effective_from"],
		):
			rows = {row.row_code for row in frappe.get_cached_doc("China Financial Statement Template", template.name).rows}
			for rule in rules:
				target = next((code for code in rule.get("target_row_codes", []) if code in rows), None)
				if not target or rule["source_row_code"] not in rows:
					continue
				if frappe.db.exists(
					"China Financial Statement Reclassification Rule",
					{"company": company, "template": template.name, "source_row_code": rule["source_row_code"]},
				):
					continue
				frappe.get_doc({
					"doctype": "China Financial Statement Reclassification Rule",
					"company": company, "template": template.name,
					"source_row_code": rule["source_row_code"],
					"source_direction": rule["source_direction"],
					"target_row_code": target,
					"effective_from": template.effective_from,
					"enabled": 1,
				}).insert(ignore_permissions=True)


def sync_payments_voucher_report_link():
	"""Add the voucher report to Payments without replacing user-defined links."""
	if not frappe.db.exists("Workspace Sidebar", "Payments") or not frappe.db.exists("Report", "China Voucher Ledger"):
		return

	sidebar = frappe.get_doc("Workspace Sidebar", "Payments")
	if any(item.link_to == "China Voucher Ledger" and item.link_type == "Report" for item in sidebar.items):
		return

	item = sidebar.append("items", _sidebar_link("China Voucher Ledger", "查凭证", "Report"))
	financial_reports_index = next(
		(
			index
			for index, existing in enumerate(sidebar.items)
			if existing.link_to == "Financial Reports" and existing.link_type == "Workspace"
		),
		len(sidebar.items),
	)
	sidebar.items.remove(item)
	sidebar.items.insert(financial_reports_index, item)
	sidebar.flags.ignore_permissions = True
	sidebar.save()


def sync_banking_sidebar_navigation():
	"""Route the standard Banking sidebar entry into the Desk workspace."""
	if not frappe.db.exists("Workspace Sidebar", "Banking"):
		return

	sidebar = frappe.get_doc("Workspace Sidebar", "Banking")
	changed = False
	for item in sidebar.items:
		is_legacy_link = item.link_type == "DocType" and item.link_to == "Bank Reconciliation Tool"
		is_previous_url = item.link_type == "URL" and item.url == "/banking"
		if is_legacy_link or is_previous_url:
			item.label = "银行对账"
			item.link_type = "Page"
			item.link_to = "china-banking"
			item.url = None
			changed = True

	if changed:
		sidebar.flags.ignore_permissions = True
		sidebar.save()


def _known_navigation_links():
	return {link[0] for link in (*CORE_NAVIGATION, *ADMIN_NAVIGATION)} | LEGACY_NAVIGATION | {"China Finance"}


def _sidebar_link(link_to, label, link_type, sidebar=None):
	item = {
		"type": "Link", "label": label, "link_to": link_to, "link_type": link_type,
		"child": 1, "collapsible": 1, "indent": 0, "keep_closed": 0, "show_arrow": 0,
	}
	if link_type == "URL":
		item["url"] = link_to
		item["link_to"] = None
	if sidebar:
		item["route_options"] = json.dumps({"sidebar": sidebar}, ensure_ascii=False)
	return item


def _sidebar_url(url, label):
	return {
		"type": "Link", "label": label, "link_type": "URL", "url": url,
		"child": 1, "collapsible": 1, "indent": 0, "keep_closed": 0, "show_arrow": 0,
	}


def _sidebar_section(label, icon, keep_closed=0):
	return {
		"type": "Section Break", "label": label, "link_type": "DocType", "icon": icon,
		"child": 0, "collapsible": 1, "indent": 1, "keep_closed": keep_closed, "show_arrow": 0,
	}


def _desired_sidebar_items(custom_links=None):
	items = [
		{
			"type": "Link", "label": "中国财务工作台",
			"link_type": "URL", "url": "/desk/china-finance", "child": 0,
			"collapsible": 1, "indent": 0, "keep_closed": 0, "show_arrow": 0,
			"icon": "landmark",
		},
		{
			**_sidebar_link(*MAPPING_CONSOLE_LINK, sidebar="China Finance"),
			"child": 0,
			"icon": MAPPING_CONSOLE_ICON,
		},
		{
			**_sidebar_url(ACCOUNT_TABLE_URL, ACCOUNT_TABLE_LINK[1]),
			"child": 0,
			"icon": "list-tree",
		},
	]
	for label, icon, links in NAVIGATION_SECTIONS:
		items.append(_sidebar_section(label, icon))
		for link in links:
			items.append(_sidebar_link(*link, sidebar="China Finance"))
	items.append(_sidebar_section("管理与审计", "settings", 1))
	for label, icon, links in ADMIN_NAVIGATION_GROUPS:
		items.append(_sidebar_section(label, icon, 1))
		items.extend(
			_sidebar_link(*link, sidebar="China Finance")
			for link in links
			if link[0] not in {MAPPING_CONSOLE_LINK[0], ACCOUNT_TABLE_LINK[0]}
		)
	if custom_links:
		items.append(_sidebar_section("自定义", "folder", 1))
		items.extend(custom_links)
	return items


def _workspace_link(link_to, label, link_type):
	return {
		"type": "Link", "label": label, "link_to": link_to, "link_type": link_type,
		"is_query_report": link_type == "Report", "hidden": 0, "onboard": 0,
	}


def _workspace_card(label, link_count):
	return {
		"type": "Card Break", "label": label, "link_type": "DocType", "link_count": link_count,
		"is_query_report": 0, "hidden": 0, "onboard": 0,
	}


def _desired_workspace_links(custom_links=None):
	links = []
	for label, _icon, group_links in NAVIGATION_SECTIONS:
		links.append(_workspace_card(label, len(group_links)))
		links.extend(_workspace_link(*link) for link in group_links)
	for label, _icon, group_links in ADMIN_NAVIGATION_GROUPS:
		links.append(_workspace_card(label, len(group_links)))
		links.extend(_workspace_link(*link) for link in group_links)
	if custom_links:
		links.append(_workspace_card("自定义", len(custom_links)))
		links.extend(custom_links)
	return links


def sync_simplified_navigation(navigation_name="China Finance"):
	"""Apply the standard navigation while preserving unknown links and all private sidebars."""
	known = _known_navigation_links()
	if frappe.db.exists("Workspace Sidebar", navigation_name):
		sidebar = frappe.get_doc("Workspace Sidebar", navigation_name)
		custom = []
		for item in sidebar.items:
			if (
				item.type == "Link"
				and item.link_type != "URL"
				and item.link_to
				and item.link_to not in known
			):
				custom.append(_sidebar_link(item.link_to, item.label, item.link_type))
		desired = _desired_sidebar_items(custom)
		current = [
			(
				row.type, row.label, row.link_to, row.link_type, row.child,
				row.keep_closed, row.icon, row.route_options,
			)
			for row in sidebar.items
		]
		target = [
			(
				row.get("type"), row.get("label"), row.get("link_to"), row.get("link_type"),
				row.get("child", 0), row.get("keep_closed", 0), row.get("icon"),
				row.get("route_options"),
			)
			for row in desired
		]
		if current != target:
			sidebar.set("items", desired)
			sidebar.flags.ignore_permissions = True
			sidebar.save()

	if frappe.db.exists("Workspace", navigation_name):
		workspace = frappe.get_doc("Workspace", navigation_name)
		custom = []
		for item in workspace.links:
			if item.type == "Link" and item.link_to not in known:
				custom.append(_workspace_link(item.link_to, item.label, item.link_type))
		desired = _desired_workspace_links(custom)
		current = [(row.type, row.label, row.link_to, row.link_type) for row in workspace.links]
		target = [(row.get("type"), row.get("label"), row.get("link_to"), row.get("link_type")) for row in desired]
		if current != target:
			workspace.set("links", desired)
			workspace.flags.ignore_permissions = True
			workspace.save()
		if not workspace.custom_blocks:
			workspace.db_set("content", WORKSPACE_CONTENT, update_modified=False)


def validate_deployment_schema():
	required = {
		"China Finance Settings": ("company", "activation_date", "accounting_standard", "cash_flow_assignment_activation_date", "statutory_reporting_activation_date", "report_amount_unit", "coa_template", "coa_version", "coa_hash", "coa_integrity_status"),
		"China Sales Settlement Rule": ("rule_key", "company", "customer", "settlement_mode", "effective_from"),
		"China Sales Settlement": ("company", "customer", "status", "posting_date", "sales_invoice"),
		"China Sales Settlement Item": ("delivery_note", "delivery_note_item", "settlement_qty", "settlement_amount"),
		"China Accounting Voucher": ("source_key", "voucher_key", "statutory_number"),
		"China Voucher Sync Issue": ("issue_key", "company", "source_doctype", "source_name", "status", "retry_count"),
		"China Cash Flow Assignment": ("company", "posting_date", "status", "china_accounting_voucher", "revision", "assignment_key"),
		"China Cash Flow Assignment Item": ("gl_entry", "cash_account", "cash_flow_row_code", "assigned_amount"),
		"China Prior Period Error Adjustment": ("company", "journal_entry", "prior_period_end", "evidence_file", "status"),
		"China Prior Period Error Adjustment Line": ("account", "statement_type", "row_code", "amount"),
		"China Voucher Sequence": ("sequence_key", "current_value", "numbering_revision"),
		"China Tax Invoice": ("invoice_key", "invoice_number", "gross_amount", "file_hash"),
		"China Invoice Control Rule": ("company", "requirement", "effective_from"),
		"China Sales Invoice Control": ("sales_invoice", "requirement", "reason"),
		"China Tax Invoice Request": ("company", "customer", "status"),
		"China Tax Invoice Request Item": ("sales_invoice", "net_amount", "tax_amount"),
		"China Tax Integration Call": ("company", "operation", "idempotency_key", "status"),
		"China Tax Account Mapping": ("mapping_key", "company", "direction", "account", "effective_from"),
		"China Input Tax Deduction Batch": ("company", "deduction_period", "status", "tax_amount"),
		"China Input Tax Deduction Item": ("tax_invoice", "tax_amount"),
		"China Electronic Document": ("sha256", "reference_doctype", "reference_name"),
		"China Reconciliation Statement": ("statement_type", "scope", "period_key", "closing_balance", "difference"),
		"China Reconciliation Scope": ("scope_key", "company", "scope_type", "reference_name"),
		"China Reconciliation Difference": ("statement", "difference_type", "amount", "status"),
		"China Purchase Reconciliation Rule": ("company", "policy", "effective_from"),
		"China Financial Statement Template": ("template_key", "accounting_standard", "statement_type"),
		"China Financial Statement Row": ("row_code", "statutory_line_number", "row_type", "balance_direction"),
		"China Financial Statement Mapping": (
			"mapping_key", "template", "account", "reviewed", "cash_inflow_row_code", "cash_outflow_row_code",
			"account_number_snapshot", "mapping_basis", "mapping_rule_version",
		),
		"China Cash Equivalent Scope": ("scope_key", "company", "account", "classification", "effective_from", "reviewed"),
		"China Accounting Policy": ("policy_key", "company", "category", "effective_from"),
		"China Financial Statement Notes": (
			"notes_key", "company", "from_date", "to_date", "policies_json", "statement_data_json",
			"major_non_cash_transactions",
		),
		"China Closing Run": ("company", "to_date", "status"),
		"China Report Snapshot": (
			"closing_run", "statement_type", "sha256", "notes", "notes_sha256", "template_version",
			"report_status", "mapping_sha256", "cash_scope_sha256", "validation_json",
		),
	}
	missing = []
	for doctype, fields in required.items():
		if not frappe.db.exists("DocType", doctype):
			missing.append(doctype)
			continue
		for fieldname in fields:
			if not frappe.db.has_column(doctype, fieldname):
				missing.append(f"{doctype}.{fieldname}")
	if frappe.db.exists("DocType", "China Accounting Voucher") and not frappe.get_meta(
		"China Accounting Voucher"
	).has_field("entries"):
		missing.append("China Accounting Voucher.entries")
	if frappe.db.exists("DocType", "China Tax Invoice Request") and not frappe.get_meta(
		"China Tax Invoice Request"
	).has_field("items"):
		missing.append("China Tax Invoice Request.items")
	if frappe.db.exists("DocType", "China Input Tax Deduction Batch") and not frappe.get_meta(
		"China Input Tax Deduction Batch"
	).has_field("items"):
		missing.append("China Input Tax Deduction Batch.items")
	if frappe.db.exists("DocType", "China Cash Flow Assignment") and not frappe.get_meta(
		"China Cash Flow Assignment"
	).has_field("items"):
		missing.append("China Cash Flow Assignment.items")
	if frappe.db.exists("DocType", "China Sales Settlement") and not frappe.get_meta(
		"China Sales Settlement"
	).has_field("items"):
		missing.append("China Sales Settlement.items")
	for doctype, fieldname in (
		("Sales Order", "custom_china_settlement_mode"),
		("Delivery Note", "custom_china_settlement_mode"),
		("Sales Invoice", "custom_china_sales_settlement"),
	):
		if frappe.db.exists("DocType", doctype) and not frappe.get_meta(doctype).has_field(fieldname):
			missing.append(f"{doctype}.{fieldname}")
	for report in (
		"China Voucher Ledger", "China Voucher Integrity", "China VAT Ledger", "China Financial Statements",
		"China Business Document Chain", "China VAT Return Worksheet", "China Purchase Reconciliation", "China Output Invoice Reconciliation", "China Purchase Document Chain", "China AR AP Ledger Reconciliation",
	):
		if not frappe.db.exists("Report", report):
			missing.append(f"Report:{report}")
	if not frappe.db.exists("Workspace", "China Finance"):
		missing.append("Workspace:China Finance")
	if not frappe.db.exists("Print Format", "China Accounting Voucher"):
		missing.append("Print Format:China Accounting Voucher")
	if not frappe.db.exists("Workspace Sidebar", "China Finance"):
		missing.append("Workspace Sidebar:China Finance")
	if not frappe.db.exists("Desktop Icon", "China Finance"):
		missing.append("Desktop Icon:China Finance")
	if frappe.db.exists("Report", "China Purchase Document Chain") and not frappe.db.get_value(
		"Report", "China Purchase Document Chain", "disabled"
	):
		missing.append("DisabledReport:China Purchase Document Chain")
	if missing:
		frappe.throw("China Finance schema is incomplete: " + ", ".join(missing))
