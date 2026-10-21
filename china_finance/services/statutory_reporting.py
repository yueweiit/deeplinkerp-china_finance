import io
import json
import zipfile

import frappe
import xlsxwriter
from frappe import _
from frappe.utils import add_days, flt, getdate, now_datetime, strip_html
from frappe.utils.file_manager import save_file
from frappe.utils.pdf import get_pdf

from china_finance.services.cash_equivalent_scope import get_effective_cash_scope
from china_finance.services.cash_flow_assignment import get_assignment_coverage
from china_finance.services.disclosure import get_submitted_notes
from china_finance.services.financial_statement import (
	build_statement,
	get_comparison_period,
	get_fiscal_year_start,
	get_mapping_revisions,
	get_mappings,
	get_template,
	validate_statement_links,
)
from china_finance.setup.china_coa_profile import get_china_coa_master_data_readiness
from china_finance.setup.templates import (
	classify_company_account, is_strictly_excluded_from_statement,
	refine_classification_for_template, requires_manual_cash_flow_assignment,
)
from china_finance.services.prior_period_error import get_prior_period_error_readiness


STATEMENT_TYPES = ("Balance Sheet", "Profit and Loss", "Cash Flow", "Changes in Equity")
STATEMENT_LABELS = {
	"Balance Sheet": "资产负债表", "Profit and Loss": "利润表",
	"Cash Flow": "现金流量表", "Changes in Equity": "所有者权益变动表",
}


def _is_small_enterprise_snapshot(payload):
	return payload.get("accounting_standard") == "小企业会计准则"


def _filing_rows(snapshot, payload, rows, company):
	"""Add filing-specific year-to-date values without changing snapshot data."""
	if snapshot.statement_type != "Profit and Loss" or not _is_small_enterprise_snapshot(payload):
		return rows
	ytd_from = get_fiscal_year_start(company, snapshot.to_date)
	if getdate(ytd_from) == getdate(snapshot.from_date):
		return [{**row, "year_to_date_amount": row.get("amount", 0)} for row in rows]
	ytd = build_statement(company, snapshot.statement_type, ytd_from, snapshot.to_date)
	ytd_values = {row["row_code"]: row["amount"] for row in ytd["rows"]}
	return [{**row, "year_to_date_amount": ytd_values.get(row["row_code"], 0)} for row in rows]


def _item(code, label, passed, details="", count=0):
	return {"code": code, "label": label, "passed": bool(passed), "details": details, "count": count}


def _active_accounts(company, root_types):
	return frappe.get_all(
		"Account", filters={"company": company, "is_group": 0, "disabled": 0, "root_type": ["in", root_types]},
		pluck="name",
	)


def _mapping_readiness(company, statement_type, from_date, to_date):
	template = get_template(company, statement_type, to_date)
	root_types = {
		"Balance Sheet": ("Asset", "Liability", "Equity"),
		"Profit and Loss": ("Income", "Expense"),
		"Changes in Equity": ("Equity",),
		"Cash Flow": ("Asset", "Liability", "Equity", "Income", "Expense"),
	}[statement_type]
	valid_rows = {row.row_code: row.row_type for row in template.rows}
	accounts = frappe.get_all(
		"Account", filters={"company": company, "is_group": 0, "disabled": 0, "root_type": ["in", root_types]},
		fields=["name", "account_name", "account_number", "parent_account", "root_type", "account_type", "is_group"],
	)
	accounts_by_name = {
		row.name: row for row in frappe.get_all(
			"Account", filters={"company": company, "disabled": 0},
			fields=["name", "account_name", "account_number", "parent_account", "root_type", "account_type", "is_group"],
		)
	}
	required = set()
	for account in accounts:
		if is_strictly_excluded_from_statement(account.account_number, statement_type):
			continue
		if statement_type == "Cash Flow" and requires_manual_cash_flow_assignment(account.account_number):
			continue
		classification, _basis = classify_company_account(company, account, statement_type, accounts_by_name)
		classification = refine_classification_for_template(account, statement_type, valid_rows, classification)
		row_code = classification[0] if isinstance(classification, tuple) else classification
		if row_code and valid_rows.get(row_code) == "Mapped Accounts":
			required.add(account.name)
	if statement_type == "Balance Sheet":
		mappings = get_mappings(company, template, to_date)
		by_account = {row.account: row for row in mappings}
		missing = sorted(required - set(by_account))
		unreviewed = sorted(account for account in required if account in by_account and not by_account[account].reviewed)
	else:
		mappings = get_mapping_revisions(company, template, from_date, to_date)
		by_account = {}
		for mapping in mappings:
			by_account.setdefault(mapping.account, []).append(mapping)
		missing = []
		for account in required:
			cursor = getdate(from_date)
			for mapping in sorted(by_account.get(account, ()), key=lambda row: getdate(row.effective_from)):
				mapping_from = getdate(mapping.effective_from)
				mapping_to = getdate(mapping.effective_to) if mapping.effective_to else getdate(to_date)
				if mapping_from > cursor:
					break
				if mapping_to >= cursor:
					cursor = add_days(mapping_to, 1)
				if cursor > getdate(to_date):
					break
			if cursor <= getdate(to_date):
				missing.append(account)
		unreviewed = sorted({row.account for row in mappings if row.account in required and not row.reviewed})
	return {
		"template": template.name, "version": template.version,
		"passed": template.version == "3.0" and not missing and not unreviewed,
		"missing": missing, "unreviewed": unreviewed,
		"details": _("模板 {0}；未映射 {1}；未复核 {2}").format(template.version, len(missing), len(unreviewed)),
	}


def get_statutory_report_readiness_data(company, from_date, to_date):
	from_date, to_date = getdate(from_date), getdate(to_date)
	settings = frappe.get_cached_doc("China Finance Settings", company)
	items = []
	supported_standard = settings.accounting_standard in ("企业会计准则", "小企业会计准则")
	enterprise = settings.enabled and supported_standard
	items.append(_item("ACCOUNTING_STANDARD", _("中国会计准则正式报表范围"), enterprise, settings.accounting_standard))
	master_data = get_china_coa_master_data_readiness(company)
	items.append(_item(
		"CHINA_COA_MASTER_DATA", _("主数据与业务默认科目已就绪"),
		master_data["blocking_count"] == 0,
		_("阻断项 {0} 个，预警 {1} 个").format(master_data["blocking_count"], master_data["warning_count"]),
		master_data["blocking_count"],
	))

	templates = {}
	for statement_type in STATEMENT_TYPES:
		try:
			result = _mapping_readiness(company, statement_type, from_date, to_date)
		except Exception as exc:
			result = {"passed": False, "details": str(exc), "missing": [], "unreviewed": [], "version": None}
		templates[statement_type] = result
		items.append(_item(
			f"MAPPING_{statement_type.upper().replace(' ', '_')}",
			_('{0}模板与映射').format(STATEMENT_LABELS[statement_type]), result["passed"], result["details"],
			len(result.get("missing", [])) + len(result.get("unreviewed", [])),
		))

	cash_scope = get_effective_cash_scope(company, to_date)
	active_cash_accounts = set(frappe.get_all(
		"Account", filters={"company": company, "is_group": 0, "disabled": 0, "account_type": ["in", ["Cash", "Bank"]]}, pluck="name"
	))
	scoped_accounts = {row.account for row in cash_scope}
	missing_scope = active_cash_accounts - scoped_accounts
	unreviewed_scope = [row.account for row in cash_scope if not row.reviewed]
	items.append(_item(
		"CASH_EQUIVALENT_SCOPE", _("现金及现金等价物范围已完整复核"),
		not missing_scope and not unreviewed_scope,
		_("未配置 {0}；未复核 {1}").format(len(missing_scope), len(unreviewed_scope)),
		len(missing_scope) + len(unreviewed_scope),
	))

	assignment = get_assignment_coverage(company, from_date, to_date)
	items.append(_item("CASH_FLOW_ASSIGNMENT", _("现金流量指定已确认"), assignment["passed"], assignment["details"], assignment["count"]))

	prior_errors = get_prior_period_error_readiness(company, from_date, to_date)
	items.append(_item(
		"PRIOR_PERIOD_ERROR_ADJUSTMENT", _("前期差错更正已审批并附依据"),
		prior_errors["passed"], prior_errors["details"], prior_errors["count"],
	))

	links = validate_statement_links(company, from_date, to_date, settings.reconciliation_tolerance)
	for code, label, key in (
		("BALANCE_SHEET_EQUATION", _("资产负债表勾稽"), "balance_sheet"),
		("PROFIT_EQUITY_LINK", _("利润与权益衔接"), "profit_equity"),
		("CASH_FLOW_RECONCILIATION", _("现金流量勾稽"), "cash_flow"),
	):
		items.append(_item(code, label, links[key]["passed"], links[key]["details"]))

	comparison_ok = True
	comparison_details = []
	for statement_type in STATEMENT_TYPES:
		try:
			comparison_from, comparison_to = get_comparison_period(company, statement_type, from_date, to_date)
			comparison_result = build_statement(company, statement_type, comparison_from, comparison_to)
			failed_checks = [
				check["message"] for check in comparison_result.get("checks", [])
				if not check["passed"] and check.get("blocking", True)
			]
			if failed_checks:
				comparison_ok = False
				comparison_details.append(f"{STATEMENT_LABELS[statement_type]}: {'；'.join(failed_checks)}")
		except Exception as exc:
			comparison_ok = False
			comparison_details.append(f"{STATEMENT_LABELS[statement_type]}: {exc}")
	items.append(_item("COMPARATIVE_DATA", _("比较数据可生成"), comparison_ok, "；".join(comparison_details)))

	notes = get_submitted_notes(company, from_date, to_date)
	notes_complete = bool(notes) and (not prior_errors["approved_count"] or bool(notes.prior_period_errors))
	notes_details = notes.name if notes else _("缺少本期已提交附注")
	if notes and prior_errors["approved_count"] and not notes.prior_period_errors:
		notes_details = _("存在已审批前期差错更正，附注必须披露更正原因及比较数据影响")
	items.append(_item("FINANCIAL_STATEMENT_NOTES", _("财务报表附注已提交"), notes_complete, notes_details))

	return {
		"company": company, "from_date": str(from_date), "to_date": str(to_date),
		"accounting_standard": settings.accounting_standard, "formal_supported": enterprise,
		"passed": enterprise and all(item["passed"] for item in items),
		"blocking_count": sum(not item["passed"] for item in items),
		"items": items, "templates": templates,
	}


@frappe.whitelist()
def get_statutory_report_readiness(company, from_date, to_date):
	frappe.has_permission("Company", "read", company, throw=True)
	return get_statutory_report_readiness_data(company, from_date, to_date)


def _snapshot_rows(snapshot):
	payload = json.loads(snapshot.data_json)
	return payload, payload.get("rows", [])


def _balance_sheet_opening_value(row, payload):
	# New source-backed snapshots carry a directly confirmed year opening.
	# Keep historical snapshots' comparative-column semantics unchanged.
	return row.get("opening_amount") if payload.get("opening_source") else row.get("comparison_amount")


def _get_notes_payload(snapshots):
	for snapshot in snapshots:
		payload, _rows = _snapshot_rows(snapshot)
		if payload.get("financial_statement_notes"):
			return payload["financial_statement_notes"]
	return None


def _build_excel(snapshots, company):
	buffer = io.BytesIO()
	workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
	header = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
	amount = workbook.add_format({"num_format": "#,##0.00;[Red]-#,##0.00", "border": 1})
	percent = workbook.add_format({"num_format": "0.00%;[Red]-0.00%", "border": 1})
	text_format = workbook.add_format({"border": 1})
	audit_rows = []
	for snapshot in snapshots:
		payload, rows = _snapshot_rows(snapshot)
		if source := payload.get("opening_source"):
			audit_rows.append([STATEMENT_LABELS[snapshot.statement_type], _("报表年初基准"),
				source.get("name"), source.get("source_period_end"), source.get("fingerprint")])
			audit_rows.append([STATEMENT_LABELS[snapshot.statement_type], _("来源文件SHA-256"),
				source.get("name"), source.get("source_sha256"), ""])
		for check in payload.get("checks", []):
			audit_rows.append([
				STATEMENT_LABELS.get(snapshot.statement_type, snapshot.statement_type),
				"检查",
				check.get("code", ""),
				_("通过") if check.get("passed") else _("需复核"),
				check.get("message", ""),
			])
		for item in payload.get("reclassifications", []):
			audit_rows.append([
				STATEMENT_LABELS.get(snapshot.statement_type, snapshot.statement_type),
				"展示调整",
				item.get("period", ""),
				item.get("amount", 0),
				f"{item.get('source_account') or item.get('reason', '')} → {item.get('target_label', '')}",
			])
		rows = _filing_rows(snapshot, payload, rows, company)
		standard = payload.get("accounting_standard") or ("小企业会计准则" if "小企业" in snapshot.statement_type else "企业会计准则")
		worksheet = workbook.add_worksheet(STATEMENT_LABELS[snapshot.statement_type][:31])
		amount_unit = getattr(snapshot, "amount_unit", None) or "元"
		tax_id = frappe.db.get_value("Company", company, "tax_id") or ""
		if _is_small_enterprise_snapshot(payload):
			form_code = "会小企01表" if snapshot.statement_type == "Balance Sheet" else "会小企02表" if snapshot.statement_type == "Profit and Loss" else ""
			worksheet.write_row(0, 0, [STATEMENT_LABELS[snapshot.statement_type], form_code, f"税款所属期起止：{snapshot.from_date or ''} 至 {snapshot.to_date}", f"单位：{amount_unit}"], header)
			worksheet.write_row(1, 0, [f"纳税人识别号：{tax_id}", f"编制单位：{company}", f"报送日期：{now_datetime().date()}", ""], text_format)
		else:
			worksheet.write_row(0, 0, [company, STATEMENT_LABELS[snapshot.statement_type], f"{snapshot.from_date or ''} 至 {snapshot.to_date}", f"人民币：{amount_unit}"], header)
		worksheet.write_row(
			2 if _is_small_enterprise_snapshot(payload) else 1, 0,
			[standard, f"模板版本：{snapshot.template_version}", "编制状态：正式法定财务报表", f"批准：{snapshot.approved_by or ''} {snapshot.approved_on or ''}"],
			text_format,
		)
		if snapshot.statement_type == "Changes in Equity" and payload.get("equity_matrix"):
			matrix = payload["equity_matrix"]
			headings = ["项目", *[item["label"] for item in matrix["components"]], "所有者权益合计"]
			worksheet.write(2, 0, "本期", header)
			worksheet.write_row(3, 0, headings, header)
			for index, row in enumerate(matrix["rows"], 4):
				worksheet.write(index, 0, row.get("label") or "", text_format)
				for column, component in enumerate(matrix["components"], 1):
					worksheet.write_number(index, column, flt(row.get(component["fieldname"])), amount)
				worksheet.write_number(index, len(matrix["components"]) + 1, flt(row.get("total")), amount)
			worksheet.set_column(0, 0, 42)
			worksheet.set_column(1, len(matrix["components"]) + 1, 18)
			comparison_matrix = payload.get("comparison_equity_matrix")
			if comparison_matrix:
				start = len(matrix["rows"]) + 6
				worksheet.write(start, 0, "上年度对应期间", header)
				worksheet.write_row(start + 1, 0, headings, header)
				for index, row in enumerate(comparison_matrix["rows"], start + 2):
					worksheet.write(index, 0, row.get("label") or "", text_format)
					for column, component in enumerate(comparison_matrix["components"], 1):
						worksheet.write_number(index, column, flt(row.get(component["fieldname"])), amount)
					worksheet.write_number(index, len(comparison_matrix["components"]) + 1, flt(row.get("total")), amount)
			continue
		if snapshot.statement_type == "Balance Sheet":
			asset_end = next((index for index, row in enumerate(rows) if row.get("row_code") == "TOTAL_ASSETS"), len(rows) - 1)
			left, right = rows[: asset_end + 1], rows[asset_end + 1 :]
			opening_label = _("年初余额") if _is_small_enterprise_snapshot(payload) or payload.get("opening_source") else _("比较期余额")
			worksheet.write_row(3 if _is_small_enterprise_snapshot(payload) else 2, 0, ["资产行次", "资产项目", "期末余额", opening_label], header)
			worksheet.write_row(3 if _is_small_enterprise_snapshot(payload) else 2, 5, ["负债及权益行次", "负债及权益项目", "期末余额", opening_label], header)
			for index in range(max(len(left), len(right))):
				for offset, row_set in ((0, left), (5, right)):
					if index >= len(row_set):
						continue
					row = row_set[index]
					start_row = 4 if _is_small_enterprise_snapshot(payload) else 3
					worksheet.write(index + start_row, offset, row.get("statutory_line_number") or "", text_format)
					worksheet.write(index + start_row, offset + 1, ("　" * int(row.get("indent", 0))) + (row.get("label") or ""), text_format)
					worksheet.write_number(index + start_row, offset + 2, flt(row.get("amount")), amount)
					opening = _balance_sheet_opening_value(row, payload)
					if opening is None or row.get("row_type") == "Heading":
						worksheet.write_blank(index + start_row, offset + 3, None, amount)
					else:
						worksheet.write_number(index + start_row, offset + 3, flt(opening), amount)
			worksheet.set_column(0, 0, 10)
			worksheet.set_column(1, 1, 28)
			worksheet.set_column(2, 3, 18)
			worksheet.set_column(5, 5, 12)
			worksheet.set_column(6, 6, 28)
			worksheet.set_column(7, 8, 18)
		else:
			start_row = 3 if _is_small_enterprise_snapshot(payload) else 3
			if _is_small_enterprise_snapshot(payload) and snapshot.statement_type == "Profit and Loss":
				worksheet.write_row(3, 0, ["项目", "行次", "本期金额", "本年累计金额", "比较期金额", "增减额", "增减率"], header)
			elif _is_small_enterprise_snapshot(payload):
				worksheet.write_row(3, 0, ["项目", "行次", "本期金额", "本年累计金额"], header)
			else:
				worksheet.write_row(2, 0, ["行次", "项目", "本期金额/期末余额", "比较期金额"], header)
			for index, row in enumerate(rows, start_row + 1):
				if _is_small_enterprise_snapshot(payload):
					worksheet.write(index, 0, ("　" * int(row.get("indent", 0))) + (row.get("label") or ""), text_format)
					worksheet.write(index, 1, row.get("statutory_line_number") or "", text_format)
					worksheet.write_number(index, 2, flt(row.get("amount")), amount)
					worksheet.write_number(index, 3, flt(row.get("year_to_date_amount")), amount)
					if snapshot.statement_type == "Profit and Loss":
						worksheet.write_number(index, 4, flt(row.get("comparison_amount")), amount)
						worksheet.write_number(index, 5, flt(row.get("variance_amount")), amount)
						worksheet.write_number(index, 6, flt(row.get("variance_rate")) / 100 if row.get("variance_rate") is not None else 0, percent)
					continue
				worksheet.write(index, 0, row.get("statutory_line_number") or "", text_format)
				worksheet.write(index, 1, ("　" * int(row.get("indent", 0))) + (row.get("label") or ""), text_format)
				worksheet.write_number(index, 2, flt(row.get("amount")), amount)
				worksheet.write_number(index, 3, flt(row.get("comparison_amount")), amount)
				if snapshot.statement_type == "Profit and Loss":
					worksheet.write_number(index, 4, flt(row.get("variance_amount")), amount)
					worksheet.write_number(index, 5, flt(row.get("variance_rate")) / 100 if row.get("variance_rate") is not None else 0, percent)
			worksheet.set_column(0, 0, 10)
			worksheet.set_column(1, 1, 48)
			worksheet.set_column(2, 6 if snapshot.statement_type == "Profit and Loss" else 3, 20)
		notes = _get_notes_payload(snapshots)
	if audit_rows:
		worksheet = workbook.add_worksheet("检查与展示调整")
		worksheet.write_row(0, 0, ["报表", "类型", "项目", "状态/金额", "说明"], header)
		for index, row in enumerate(audit_rows, 1):
			for column, value in enumerate(row):
				if column == 3 and isinstance(value, (int, float)):
					worksheet.write_number(index, column, flt(value), amount)
				else:
					worksheet.write(index, column, value, text_format)
		worksheet.set_column(0, 0, 22)
		worksheet.set_column(1, 2, 18)
		worksheet.set_column(3, 3, 18)
		worksheet.set_column(4, 4, 100)
	if notes:
		worksheet = workbook.add_worksheet("财务报表附注")
		worksheet.write_row(0, 0, [company, "财务报表附注", f"{notes.get('from_date')} 至 {notes.get('to_date')}", f"版本：{notes.get('version')}"], header)
		row_index = 2
		for label, value in (
			("编制基础及持续经营说明", notes.get("disclosures", {}).get("basis_of_preparation")),
			("重要会计政策摘要", notes.get("disclosures", {}).get("significant_accounting_policies")),
			("重要会计估计和判断", notes.get("disclosures", {}).get("significant_estimates")),
			("会计政策变更", notes.get("disclosures", {}).get("policy_changes")),
			("会计估计变更", notes.get("disclosures", {}).get("estimate_changes")),
			("前期差错更正", notes.get("disclosures", {}).get("prior_period_errors")),
			("税项说明", notes.get("disclosures", {}).get("tax_disclosures")),
			("应收款项及信用减值说明", notes.get("disclosures", {}).get("receivables_disclosures")),
			("存货说明", notes.get("disclosures", {}).get("inventory_disclosures")),
			("固定资产及在建工程说明", notes.get("disclosures", {}).get("fixed_asset_disclosures")),
			("无形资产及研发支出说明", notes.get("disclosures", {}).get("intangible_rd_disclosures")),
			("重大非现金投资和筹资事项", notes.get("disclosures", {}).get("major_non_cash_transactions")),
			("关联方及关联交易", notes.get("disclosures", {}).get("related_party_disclosures")),
			("承诺及或有事项", notes.get("disclosures", {}).get("commitments_contingencies")),
			("资产负债表日后事项", notes.get("disclosures", {}).get("subsequent_events")),
			("其他重要事项", notes.get("disclosures", {}).get("other_disclosures")),
		):
			if not value:
				continue
			worksheet.write(row_index, 0, label, header)
			worksheet.write(row_index, 1, strip_html(value), text_format)
			row_index += 1
		worksheet.write(row_index, 0, "生效会计政策快照", header)
		worksheet.write(row_index, 1, json.dumps(notes.get("policies", []), ensure_ascii=False, indent=2), text_format)
		worksheet.write(row_index + 1, 0, "报表重要项目及现金流补充资料", header)
		worksheet.write(row_index + 1, 1, json.dumps(notes.get("statement_data", {}), ensure_ascii=False, indent=2), text_format)
		worksheet.set_column(0, 0, 30)
		worksheet.set_column(1, 1, 100)
	workbook.close()
	buffer.seek(0)
	return buffer.getvalue()


def _build_pdf_html(snapshots, company):
	parts = ["<html><head><meta charset='utf-8'><style>body{font-family:sans-serif;font-size:10px} h1{text-align:center} table{width:100%;border-collapse:collapse;margin-bottom:24px} th,td{border:1px solid #999;padding:5px} .num{text-align:right}</style></head><body>"]
	for snapshot in snapshots:
		payload, rows = _snapshot_rows(snapshot)
		rows = _filing_rows(snapshot, payload, rows, company)
		standard = payload.get("accounting_standard") or ("小企业会计准则" if "小企业" in snapshot.statement_type else "企业会计准则")
		amount_unit = getattr(snapshot, "amount_unit", None) or "元"
		if _is_small_enterprise_snapshot(payload):
			form_code = "会小企01表" if snapshot.statement_type == "Balance Sheet" else "会小企02表" if snapshot.statement_type == "Profit and Loss" else ""
			tax_id = frappe.db.get_value("Company", company, "tax_id") or ""
			parts.append(f"<h1>{STATEMENT_LABELS[snapshot.statement_type]}　{form_code}</h1>")
			parts.append(f"<p>纳税人识别号：{frappe.utils.escape_html(tax_id)}　税款所属期起止：{snapshot.from_date or ''} 至 {snapshot.to_date}　编制单位：{frappe.utils.escape_html(company)}　报送日期：{now_datetime().date()}　单位：{amount_unit}</p>")
		else:
			parts.append(f"<h1>{company}<br>{STATEMENT_LABELS[snapshot.statement_type]}</h1>")
			parts.append(f"<p>报表期间：{snapshot.from_date or ''} 至 {snapshot.to_date}　会计准则：{standard}　金额单位：人民币{amount_unit}　模板版本：{snapshot.template_version}　编制状态：正式法定财务报表</p>")
		if snapshot.statement_type == "Changes in Equity" and payload.get("equity_matrix"):
			for matrix_label, matrix in (
				("本期", payload["equity_matrix"]),
				("上年度对应期间", payload.get("comparison_equity_matrix")),
			):
				if not matrix:
					continue
				parts.append(f"<h2>{matrix_label}</h2><table><thead><tr><th>项目</th>" + "".join(f"<th>{item['label']}</th>" for item in matrix["components"]) + "<th>合计</th></tr></thead><tbody>")
				for row in matrix["rows"]:
					parts.append("<tr><td>" + frappe.utils.escape_html(row.get("label") or "") + "</td>" + "".join(f"<td class='num'>{flt(row.get(item['fieldname'])):,.2f}</td>" for item in matrix["components"]) + f"<td class='num'>{flt(row.get('total')):,.2f}</td></tr>")
				parts.append("</tbody></table>")
			_append_pdf_audit(parts, payload)
			continue
		if snapshot.statement_type == "Balance Sheet":
			asset_end = next((index for index, row in enumerate(rows) if row.get("row_code") == "TOTAL_ASSETS"), len(rows) - 1)
			left, right = rows[: asset_end + 1], rows[asset_end + 1 :]
			comparison_label = _("年初余额") if _is_small_enterprise_snapshot(payload) or payload.get("opening_source") else _("比较期余额")
			parts.append(f"<table><thead><tr><th>资产行次</th><th>资产项目</th><th>期末余额</th><th>{comparison_label}</th><th>负债及权益行次</th><th>负债及权益项目</th><th>期末余额</th><th>{comparison_label}</th></tr></thead><tbody>")
			for index in range(max(len(left), len(right))):
				cells = []
				for row in ((left[index] if index < len(left) else {}), (right[index] if index < len(right) else {})):
					opening = _balance_sheet_opening_value(row, payload)
					opening_text = "" if opening is None or row.get("row_type") == "Heading" else f"{flt(opening):,.2f}"
					cells.extend([row.get("statutory_line_number") or "", ("　" * int(row.get("indent", 0))) + (row.get("label") or ""), f"{flt(row.get('amount')):,.2f}", opening_text])
				parts.append("<tr>" + "".join(f"<td>{frappe.utils.escape_html(str(cell))}</td>" for cell in cells) + "</tr>")
			parts.append("</tbody></table>")
		else:
			if _is_small_enterprise_snapshot(payload) and snapshot.statement_type == "Profit and Loss":
				parts.append("<table><thead><tr><th>项目</th><th>行次</th><th>本期金额</th><th>本年累计金额</th><th>比较期金额</th><th>增减额</th><th>增减率</th></tr></thead><tbody>")
			elif _is_small_enterprise_snapshot(payload):
				parts.append("<table><thead><tr><th>项目</th><th>行次</th><th>本期金额</th><th>本年累计金额</th></tr></thead><tbody>")
			else:
				parts.append("<table><thead><tr><th>行次</th><th>项目</th><th>本期金额/期末余额</th><th>比较期金额</th></tr></thead><tbody>")
			for row in rows:
				label = ("　" * int(row.get("indent", 0))) + (row.get("label") or "")
				if _is_small_enterprise_snapshot(payload):
					if snapshot.statement_type == "Profit and Loss":
						variance_rate = f"{flt(row.get('variance_rate')):,.2f}%" if row.get("variance_rate") is not None else "—"
						parts.append(f"<tr><td>{frappe.utils.escape_html(label)}</td><td>{row.get('statutory_line_number') or ''}</td><td class='num'>{flt(row.get('amount')):,.2f}</td><td class='num'>{flt(row.get('year_to_date_amount')):,.2f}</td><td class='num'>{flt(row.get('comparison_amount')):,.2f}</td><td class='num'>{flt(row.get('variance_amount')):,.2f}</td><td class='num'>{variance_rate}</td></tr>")
					else:
						parts.append(f"<tr><td>{frappe.utils.escape_html(label)}</td><td>{row.get('statutory_line_number') or ''}</td><td class='num'>{flt(row.get('amount')):,.2f}</td><td class='num'>{flt(row.get('year_to_date_amount')):,.2f}</td></tr>")
				else:
					if snapshot.statement_type == "Profit and Loss":
						variance_rate = f"{flt(row.get('variance_rate')):,.2f}%" if row.get("variance_rate") is not None else "—"
						parts.append(f"<tr><td>{row.get('statutory_line_number') or ''}</td><td>{frappe.utils.escape_html(label)}</td><td class='num'>{flt(row.get('amount')):,.2f}</td><td class='num'>{flt(row.get('comparison_amount')):,.2f}</td><td class='num'>{flt(row.get('variance_amount')):,.2f}</td><td class='num'>{variance_rate}</td></tr>")
					else:
						parts.append(f"<tr><td>{row.get('statutory_line_number') or ''}</td><td>{frappe.utils.escape_html(label)}</td><td class='num'>{flt(row.get('amount')):,.2f}</td><td class='num'>{flt(row.get('comparison_amount')):,.2f}</td></tr>")
			parts.append("</tbody></table>")
		_append_pdf_audit(parts, payload)
		notes = _get_notes_payload(snapshots)
	if notes:
		parts.append("<h1>财务报表附注</h1>")
		for label, fieldname in (
			("编制基础及持续经营说明", "basis_of_preparation"), ("重要会计政策摘要", "significant_accounting_policies"),
			("重要会计估计和判断", "significant_estimates"), ("会计政策变更", "policy_changes"),
			("会计估计变更", "estimate_changes"), ("前期差错更正", "prior_period_errors"),
			("税项说明", "tax_disclosures"), ("应收款项及信用减值说明", "receivables_disclosures"),
			("存货说明", "inventory_disclosures"), ("固定资产及在建工程说明", "fixed_asset_disclosures"),
			("无形资产及研发支出说明", "intangible_rd_disclosures"), ("关联方及关联交易", "related_party_disclosures"),
			("重大非现金投资和筹资事项", "major_non_cash_transactions"),
			("承诺及或有事项", "commitments_contingencies"), ("资产负债表日后事项", "subsequent_events"),
			("其他重要事项", "other_disclosures"),
		):
			value = notes.get("disclosures", {}).get(fieldname)
			if value:
				parts.append(f"<h2>{label}</h2>{value}")
		parts.append("<h2>生效会计政策快照</h2><pre>" + frappe.utils.escape_html(json.dumps(notes.get("policies", []), ensure_ascii=False, indent=2)) + "</pre>")
		parts.append("<h2>报表重要项目及现金流补充资料</h2><pre>" + frappe.utils.escape_html(json.dumps(notes.get("statement_data", {}), ensure_ascii=False, indent=2)) + "</pre>")
	parts.append(f"<p>生成时间：{now_datetime()}　批准人：{snapshots[0].approved_by or ''}　批准时间：{snapshots[0].approved_on or ''}</p></body></html>")
	return "".join(parts)


def _append_pdf_audit(parts, payload):
	if source := payload.get("opening_source"):
		parts.append(
			"<h2>" + frappe.utils.escape_html(_("报表年初基准")) + "</h2><p>"
			+ frappe.utils.escape_html(str(source.get("name") or "")) + "<br>"
			+ frappe.utils.escape_html(_("来源文件SHA-256")) + ": "
			+ frappe.utils.escape_html(str(source.get("source_sha256") or "")) + "<br>"
			+ frappe.utils.escape_html(str(source.get("fingerprint") or "")) + "</p>"
		)
	checks = payload.get("checks", [])
	reclassifications = payload.get("reclassifications", [])
	if not checks and not reclassifications:
		return
	parts.append("<h2>检查与展示调整</h2><table><thead><tr><th>类型</th><th>项目</th><th>状态/金额</th><th>说明</th></tr></thead><tbody>")
	for check in checks:
		status = "通过" if check.get("passed") else "需复核"
		parts.append(
			f"<tr><td>检查</td><td>{frappe.utils.escape_html(str(check.get('code') or ''))}</td>"
			f"<td>{status}</td><td>{frappe.utils.escape_html(str(check.get('message') or ''))}</td></tr>"
		)
	for item in reclassifications:
		source = item.get("source_account") or item.get("reason") or ""
		destination = f"{source} → {item.get('target_label') or ''}"
		parts.append(
			f"<tr><td>展示调整</td><td>{frappe.utils.escape_html(str(item.get('period') or ''))}</td>"
			f"<td class='num'>{flt(item.get('amount')):,.2f}</td><td>{frappe.utils.escape_html(destination)}</td></tr>"
		)
	parts.append("</tbody></table>")


@frappe.whitelist()
def generate_statutory_report_package(closing_run, formats=None):
	frappe.only_for(("System Manager", "China Finance Manager", "China Finance Auditor"))
	run = frappe.get_doc("China Closing Run", closing_run)
	run.check_permission("read")
	if run.docstatus != 1 or run.status != "Closed":
		frappe.throw(_("只有已通过检查并提交的期末智能结转可以生成正式法定财务报表"))
	readiness = get_statutory_report_readiness_data(run.company, run.from_date, run.to_date)
	if not readiness["passed"]:
		frappe.throw(_("正式法定财务报表就绪度未通过，阻断项 {0} 个").format(readiness["blocking_count"]))
	snapshots = frappe.get_all(
		"China Report Snapshot", filters={"closing_run": run.name},
		fields=["name", "statement_type", "report_status", "from_date", "to_date", "template_version", "amount_unit", "data_json", "approved_by", "approved_on"],
		order_by="statement_type",
	)
	if any(row.report_status != "正式" for row in snapshots):
		frappe.throw(_("本次结账包含草表快照；请处理快照中的未完成检查，并按结账修订流程生成完整的正式快照后再导出。"))
	if len(snapshots) != 4:
		frappe.throw(_("期末智能结转缺少完整四表快照"))
	requested = set(frappe.parse_json(formats) if isinstance(formats, str) else (formats or ["PDF", "Excel"]))
	files = {}
	if "Excel" in requested:
		file_doc = save_file(f"法定财务报表-{run.name}.xlsx", _build_excel(snapshots, run.company), "China Closing Run", run.name, is_private=1)
		files["Excel"] = file_doc.file_url
	if "PDF" in requested:
		file_doc = save_file(f"法定财务报表-{run.name}.pdf", get_pdf(_build_pdf_html(snapshots, run.company)), "China Closing Run", run.name, is_private=1)
		files["PDF"] = file_doc.file_url
	index = {"schema_version": "2.0", "closing_run": run.name, "company": run.company, "generated_on": str(now_datetime()), "files": files, "snapshots": [row.name for row in snapshots]}
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
		archive.writestr("index.json", json.dumps(index, ensure_ascii=False, indent=2))
		for label, url in files.items():
			file_doc = frappe.get_doc("File", frappe.db.get_value("File", {"file_url": url}, "name"))
			archive.writestr(file_doc.file_name, file_doc.get_content())
	package = save_file(f"法定财务报表档案包-{run.name}.zip", buffer.getvalue(), "China Closing Run", run.name, is_private=1)
	return {"closing_run": run.name, "files": files, "archive": package.file_url, "snapshot_count": len(snapshots)}
