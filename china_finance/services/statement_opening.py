"""Confirmed filing amounts for report openings; never create or change ledger entries."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime

from china_finance.services.month_end import lock_company
from china_finance.services.opening_balance_validation import decimal


BASELINE = "China Statement Opening Balance"
MANAGERS = ("System Manager", "China Finance Manager")


def _active_key(company, opening_date):
	return hashlib.sha256(f"{company}|{getdate(opening_date)}".encode()).hexdigest()


def _company(company):
	doc = frappe.get_doc("Company", company)
	doc.check_permission("read")
	return doc


@frappe.whitelist()
def get_definition(company, opening_date):
	"""Return selectable report projects, not accounting accounts or inferred amounts."""
	_company(company)
	frappe.has_permission(BASELINE, "create", throw=True)
	from china_finance.services.financial_statement import get_fiscal_year_start, get_template

	date = getdate(opening_date)
	date = get_fiscal_year_start(company, date)
	template = get_template(company, "Balance Sheet", date)
	template.check_permission("read")
	return {
		"opening_date": str(date), "source_period_end": str(add_days(date, -1)), "template": template.name,
		"rows": [{"row_code": row.row_code, "report_label": row.label} for row in template.rows if row.row_type != "Heading"],
	}


def _amount(value):
	try:
		value = decimal(value)
		if value != value.quantize(Decimal("0.01")):
			raise ValueError
		return value
	except (ValueError, ArithmeticError):
		frappe.throw(_("来源金额必须是最多两位小数的有限数值"))


def validate_document(doc):
	from china_finance.services.financial_statement import get_fiscal_year_start

	if doc.is_new() and doc.docstatus != 0:
		frappe.throw(_("请先保存基准草稿，再通过标准提交确认"))
	company = _company(doc.company)
	doc.currency = company.default_currency
	if getdate(doc.opening_date) != get_fiscal_year_start(doc.company, doc.opening_date):
		frappe.throw(_("报表年初日期必须为本公司会计年度首日"))
	if getdate(doc.source_period_end) != getdate(add_days(doc.opening_date, -1)):
		frappe.throw(_("来源报表截止日必须是报表年初日期的前一天"))
	template = frappe.get_doc("China Financial Statement Template", doc.template)
	template.check_permission("read")
	standard = frappe.db.get_value("China Finance Settings", doc.company, "accounting_standard")
	if template.statement_type != "Balance Sheet" or template.accounting_standard != standard:
		frappe.throw(_("请选择符合本公司会计准则的资产负债表模板"))
	definitions = {row.row_code: row for row in template.rows if row.row_type != "Heading"}
	seen = set()
	for row in doc.rows:
		row.provided = int(bool(cint(row.provided)))
		if row.row_code not in definitions or row.row_code in seen:
			frappe.throw(_("报表项目不存在、属于分组标题或重复：{0}").format(frappe.utils.escape_html(str(row.row_code))))
		seen.add(row.row_code)
		row.currency = doc.currency
		row.report_label = definitions[row.row_code].label
		if row.provided:
			_amount(row.amount)
			if doc.docstatus == 1 and not (row.source_label or "").strip():
				frappe.throw(_("确认来源金额时必须填写来源报表项目名称"))
	if doc.docstatus == 0:
		doc.active_key = None
		doc.confirmed_by = doc.confirmed_on = doc.fingerprint = doc.source_sha256 = None
		doc.template_version = template.version


def _values(doc):
	return {row.row_code: _amount(row.amount) for row in doc.rows if row.provided}


def _validate_totals(doc):
	from china_finance.services.financial_statement import evaluate_formula, get_formula_dependencies

	values = _values(doc)
	if not {"TOTAL_ASSETS", "TOTAL_LIABILITIES_EQUITY"} <= values.keys():
		frappe.throw(_("必须确认资产总计及负债和所有者权益总计的来源金额"))
	if abs(values["TOTAL_ASSETS"] - values["TOTAL_LIABILITIES_EQUITY"]) > Decimal("0.01"):
		frappe.throw(_("来源报表的资产总计与负债和所有者权益总计不平衡"))
	template = frappe.get_doc("China Financial Statement Template", doc.template)
	codes = {row.row_code for row in template.rows}
	# Check only identities for which every input was explicitly supplied.
	# Never infer a missing detail as zero or replace a supplied filing amount.
	for row in template.rows:
		if row.row_code not in values or row.row_type != "Formula":
			continue
		dependencies = get_formula_dependencies(row.formula, codes)
		if dependencies <= values.keys():
			calculated = Decimal(str(evaluate_formula(row.formula, values)))
			if abs(calculated - values[row.row_code]) > Decimal("0.01"):
				frappe.throw(_("来源报表项目勾稽不一致：{0}").format(row.label))


def _source_hash(doc):
	name = frappe.db.get_value("File", {
		"file_url": doc.source_file, "attached_to_doctype": BASELINE, "attached_to_name": doc.name,
	}, "name") if doc.source_file else None
	if not name and doc.source_file:
		name = frappe.db.get_value("File", {
			"file_url": doc.source_file, "attached_to_name": ["is", "not set"], "owner": frappe.session.user,
		}, "name")
	if name:
		file = frappe.get_doc("File", name)
		attached = file.attached_to_doctype == BASELINE and file.attached_to_name == doc.name
		owned = not file.attached_to_name and file.owner == frappe.session.user
		if not (attached or owned) or not file.is_private or file.is_remote_file:
			frappe.throw(_("请将私有PDF原件上传到当前报表年初基准"))
		file.check_permission("read")
		content = Path(file.get_full_path()).read_bytes()
		if not content.lstrip().startswith(b"%PDF-"):
			frappe.throw(_("报表年初基准的来源必须是PDF原件"))
		return hashlib.sha256(content).hexdigest()
	frappe.throw(_("请将私有PDF原件上传到当前报表年初基准"))


def fingerprint(doc):
	payload = {key: str(doc.get(key) or "") for key in (
		"company", "opening_date", "source_period_end", "template", "template_version", "currency",
		"source_file", "source_sha256", "confirmed_by", "confirmed_on",
	)}
	payload["rows"] = [
		{"row_code": row.row_code, "source_label": row.source_label, "report_label": row.report_label,
		 "amount": format(_amount(row.amount), ".2f")}
		for row in doc.rows if row.provided
	]
	return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def confirm_document(doc):
	frappe.only_for(MANAGERS)
	lock_company(doc.company)
	_validate_totals(doc)
	doc.source_sha256 = _source_hash(doc)
	doc.template_version = frappe.db.get_value("China Financial Statement Template", doc.template, "version")
	doc.active_key = _active_key(doc.company, doc.opening_date)
	if frappe.db.exists(BASELINE, {"active_key": doc.active_key, "docstatus": 1, "name": ["!=", doc.name]}):
		frappe.throw(_("本公司该会计年度已有确认的报表年初基准，请先取消原基准再修订"))
	doc.confirmed_by = frappe.session.user
	doc.confirmed_on = now_datetime()
	doc.fingerprint = fingerprint(doc)


def cancel_document(doc):
	frappe.only_for(MANAGERS)
	lock_company(doc.company)
	doc.active_key = None


def protect_source_file(doc, method=None):
	if not frappe.db.exists("DocType", BASELINE):
		return
	old = doc.get_doc_before_save()
	url = old.file_url if old else doc.file_url
	if not url or not frappe.db.exists(BASELINE, {"source_file": url, "confirmed_on": ["is", "set"]}):
		return
	if method == "on_trash" or not doc.is_private or doc.is_remote_file or (old and any(doc.get(key) != old.get(key) for key in (
		"file_url", "is_private", "attached_to_doctype", "attached_to_name", "content_hash",
	))):
		frappe.throw(_("该PDF已用于确认报表年初基准，不能删除、公开、替换或移动"))


def resolve_opening(company, year_start, template, ledger_values, finance_book=None, cost_center=None, project=None):
	"""Return direct amounts and their GL differences; None means retain the GL projection."""
	if not frappe.db.exists("DocType", BASELINE):
		return None, None, [], []
	name = frappe.db.get_value(BASELINE, {
		"active_key": _active_key(company, year_start), "company": company, "opening_date": year_start, "docstatus": 1,
	}, "name")
	if not name:
		return None, None, [], []
	if finance_book or cost_center or project:
		return None, None, [], [_("当前使用账簿、成本中心或项目筛选，公司整体报表年初基准不适用，年初数按筛选后的总账显示。")]
	doc = frappe.get_doc(BASELINE, name)
	doc.check_permission("read")
	if doc.company != company or getdate(doc.opening_date) != getdate(year_start) or doc.docstatus != 1:
		frappe.throw(_("报表年初基准状态已变化，请刷新后重试"))
	if doc.template != template.name or doc.template_version != template.version:
		frappe.throw(_("已确认报表年初基准的模板与当前报表不一致，请先复核基准项目映射"))
	if doc.currency != frappe.get_cached_value("Company", company, "default_currency") or doc.fingerprint != fingerprint(doc):
		frappe.throw(_("已确认报表年初基准的币种或内容指纹不一致，请核查来源"))
	if doc.source_sha256 != _source_hash(doc):
		frappe.throw(_("报表年初基准的PDF原件指纹已变化，请核查来源"))
	values = {key: flt(value, 2) for key, value in _values(doc).items()}
	differences = [
		{"row_code": row.row_code, "label": row.report_label, "source_amount": values[row.row_code],
		 "ledger_amount": flt(ledger_values.get(row.row_code), 2),
		 "difference": flt(values[row.row_code] - flt(ledger_values.get(row.row_code)), 2)}
		for row in doc.rows if row.provided and abs(values[row.row_code] - flt(ledger_values.get(row.row_code))) > 0.01
	]
	source = {key: str(doc.get(key) or "") for key in (
		"name", "company", "opening_date", "source_period_end", "template", "template_version", "currency",
		"source_file", "source_sha256", "fingerprint", "confirmed_by", "confirmed_on",
	)}
	source["kind"] = "Confirmed Source"
	source["rows"] = [{"row_code": row.row_code, "source_label": row.source_label, "amount": values[row.row_code]}
		for row in doc.rows if row.provided]
	return values, source, differences, [_("年初数直接引用已确认报表年初基准 {0} 的来源金额；未提供的细项留空，不从总账推算。该基准不新增记账，也不改写已有结账档案。").format(doc.name)]
