import frappe
from frappe import _
from frappe.utils import cint, getdate, today

from china_finance.services.financial_statement import get_company_finance_settings, get_template
from china_finance.services.statement_mapping_review import REVIEW_ROLES, set_mapping_reviewed
from china_finance.services.account_display import (
	get_account_display_label,
	get_account_display_title,
	strip_account_company_suffix,
)
from china_finance.setup.templates import (
	TEMPLATE_EFFECTIVE_FROM,
	classify_company_account,
	is_strictly_excluded_from_statement,
	refine_classification_for_template,
)

READ_ROLES = (
	"System Manager",
	"Accounts Manager",
	"Accounts User",
	"China Finance Manager",
	"China Finance Auditor",
)
WRITE_ROLES = REVIEW_ROLES
TEMPLATE_WRITE_ROLES = ("System Manager", "China Finance Manager")
STATEMENT_TYPES = ("Balance Sheet", "Profit and Loss", "Cash Flow", "Changes in Equity")
STATEMENT_ROOT_TYPES = {
	"Balance Sheet": {"Asset", "Liability", "Equity"},
	"Profit and Loss": {"Income", "Expense"},
	"Changes in Equity": {"Equity"},
}


def _require_read_access():
	frappe.only_for(READ_ROLES)


def _require_write_access():
	frappe.only_for(WRITE_ROLES)


def _require_template_write_access():
	frappe.only_for(TEMPLATE_WRITE_ROLES)


@frappe.whitelist()
def get_mapping_console(company, statement_type, accounting_standard=None):
	"""Aggregate the statement template rows, mappings and unmapped accounts for the console."""
	_require_read_access()
	if statement_type not in STATEMENT_TYPES:
		frappe.throw(_("未知的报表类型 {0}").format(statement_type))
	if accounting_standard not in ("企业会计准则", "小企业会计准则"):
		accounting_standard = None
	settings = get_company_finance_settings(company)
	report_effective_from = settings.statutory_reporting_activation_date or settings.activation_date
	mapping_effective_from = settings.statement_mapping_activation_date or report_effective_from or TEMPLATE_EFFECTIVE_FROM
	template = get_template(company, statement_type, today(), accounting_standard=accounting_standard)
	report_effective_from = template.effective_from or report_effective_from
	mappings = frappe.get_all(
		"China Financial Statement Mapping",
		filters={"company": company, "template": template.name},
		fields=[
			"name", "account", "row_code", "cash_inflow_row_code", "cash_outflow_row_code",
			"supplementary_row_code", "mapping_basis", "sign_multiplier", "mapping_source", "reviewed", "effective_from", "effective_to",
		],
		order_by="account",
	)
	leaf_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 0, "disabled": 0},
		fields=["name", "account_name", "account_number", "parent_account", "root_type", "account_type"],
		order_by="name",
	)
	all_accounts = frappe.get_all(
		"Account",
		filters={"company": company, "disabled": 0},
		fields=["name", "account_name", "account_number", "parent_account", "is_group", "root_type"],
		order_by="lft",
	)
	payload = build_console_payload(template, mappings, leaf_accounts, all_accounts, company=company)
	payload["currency"] = frappe.get_cached_value("Company", company, "default_currency")
	_totals = get_mapping_account_totals(company, [mapping.account for mapping in mappings], today())
	for row in payload["rows"]:
		for mapping in row["mappings"]:
			mapping.update(_totals.get(mapping["account"], {}))
	payload["configuration"] = {
		"report_effective_from": str(report_effective_from or ""),
		"mapping_effective_from": str(mapping_effective_from or ""),
	}
	payload["reclassification_rules"] = get_reclassification_rules_for_console(company, template)
	return payload


def get_mapping_account_totals(company, accounts, as_of_date):
	"""Return read-only debit, credit and net totals for mapped leaf accounts."""
	accounts = tuple(sorted(set(accounts)))
	if not accounts:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT account, SUM(debit) AS debit_total, SUM(credit) AS credit_total
		FROM `tabGL Entry`
		WHERE company = %(company)s
			AND account IN %(accounts)s
			AND posting_date <= %(as_of_date)s
			AND is_cancelled = 0
		GROUP BY account
		""",
		{"company": company, "accounts": accounts, "as_of_date": as_of_date},
		as_dict=True,
	)
	return {
		row.account: {
			"debit_total": row.debit_total or 0,
			"credit_total": row.credit_total or 0,
			"balance": (row.debit_total or 0) - (row.credit_total or 0),
		}
		for row in rows
	}


def get_reclassification_rules_for_console(company, template):
	rules = frappe.get_all(
		"China Financial Statement Reclassification Rule",
		filters={"company": company, "template": template.name},
		fields=[
			"name", "source_row_code", "source_account", "source_direction", "target_row_code",
			"effective_from", "effective_to", "enabled", "review_notes",
		],
		order_by="source_row_code, effective_from",
	)
	account_names = {rule.source_account for rule in rules if rule.source_account}
	accounts = (
		frappe.get_all(
			"Account",
			filters={"company": company, "name": ["in", list(account_names)]},
			fields=["name", "account_name", "account_number"],
		)
		if account_names
		else []
	)
	account_by_name = {account.name: account for account in accounts}
	for rule in rules:
		if rule.source_account:
			rule.source_account_label = get_account_display_label(
				account_by_name.get(rule.source_account) or rule.source_account,
				company,
			) or strip_account_company_suffix(rule.source_account, company)
	return rules


def is_account_applicable_to_statement(account, statement_type):
	"""Return whether a leaf account belongs in the selected statement's mapping scope."""
	if is_strictly_excluded_from_statement(account.account_number, statement_type):
		return False
	if statement_type == "Cash Flow":
		return account.account_type not in {"Cash", "Bank"}
	root_types = STATEMENT_ROOT_TYPES.get(statement_type)
	return not root_types or account.root_type in root_types


def build_console_payload(template, mappings, leaf_accounts, all_accounts=None, company=None):
	"""Pure aggregation so tests can run without a database."""
	account_index = {account.name: account for account in leaf_accounts}
	all_account_index = {account.name: account for account in (all_accounts or [])}

	def account_label(account):
		if not account:
			return ""
		parent = all_account_index.get(account.parent_account)
		return get_account_display_title(
			account.account_number,
			account.account_name,
			parent_account_name=parent.account_name if parent else None,
			parent_account_number=parent.account_number if parent else None,
		)

	row_mappings = {}
	mapped_accounts = set()
	pending_review = 0
	for mapping in mappings:
		account = account_index.get(mapping.account)
		account = account or all_account_index.get(mapping.account)
		account_name = account.account_name if account else strip_account_company_suffix(mapping.account, company)
		account_number = account.account_number if account else None
		item = {
			"name": mapping.name,
			"account": mapping.account,
			"account_name": account_name,
			"account_number": account_number,
			"account_label": account_label(account) or get_account_display_title(account_number, account_name),
			"root_type": account.root_type if account else None,
			"row_code": mapping.row_code,
			"supplementary_row_code": getattr(mapping, "supplementary_row_code", None),
			"mapping_basis": getattr(mapping, "mapping_basis", None),
			"cash_inflow_row_code": mapping.cash_inflow_row_code,
			"cash_outflow_row_code": mapping.cash_outflow_row_code,
			"sign_multiplier": -1 if cint(mapping.sign_multiplier) == -1 else 1,
			"mapping_source": mapping.mapping_source,
			"reviewed": bool(mapping.reviewed),
			"effective_from": str(mapping.effective_from or ""),
			"effective_to": str(mapping.effective_to or ""),
		}
		row_mappings.setdefault(mapping.row_code, []).append(item)
		mapped_accounts.add(mapping.account)
		pending_review += 0 if mapping.reviewed else 1
	rows = []
	rollup_codes = {
		"TAX_SURCHARGES", "SELLING_EXPENSES", "ADMIN_EXPENSES", "FINANCE_EXPENSES",
		"NONOPERATING_INCOME", "NONOPERATING_EXPENSE",
	}
	for index, row in enumerate(template.rows):
		children = []
		if row.row_code in rollup_codes:
			for child in template.rows[index + 1:]:
				if child.indent <= row.indent:
					break
				if child.row_type == "Mapped Accounts" and not (child.label or "").strip().startswith("其中："):
					children.append(child)
		supplementary = (row.label or "").strip().startswith("其中：")
		if row.formula:
			calculation_description = ""
		elif supplementary:
			calculation_description = "仅补充披露，不参与父项金额计算"
		elif children:
			parts = ["本行直接映射科目汇总"] + [child.label for child in children]
			calculation_description = " + ".join(parts)
		elif row.row_type == "Mapped Accounts":
			calculation_description = "本行映射科目汇总"
		else:
			calculation_description = ""
		rows.append(
		{
			"row_code": row.row_code,
			"label": row.label,
			"row_type": row.row_type,
			"indent": int(bool(row.indent)),
			"bold": int(bool(row.bold)),
			"formula": row.formula,
			"calculation_description": calculation_description,
			"balance_direction": row.balance_direction,
			"mappings": row_mappings.get(row.row_code, []),
		}
		)
	root_order = {"Asset": 0, "Liability": 1, "Equity": 2, "Income": 3, "Expense": 4}
	valid_rows = {row.row_code: row.row_type for row in template.rows}
	row_labels = {row.row_code: row.label for row in template.rows}
	applicable_accounts = [
		account
		for account in leaf_accounts
		if (
			account.name in mapped_accounts
			or is_account_applicable_to_statement(account, template.statement_type)
		)
	]
	applicable_account_names = {account.name for account in applicable_accounts}
	unmapped_accounts = [account for account in applicable_accounts if account.name not in mapped_accounts]
	likely_row_by_account = {}
	for account in unmapped_accounts:
		classification, _basis = classify_company_account(company, account, template.statement_type) if company else (None, None)
		classification = refine_classification_for_template(account, template.statement_type, valid_rows, classification)
		account.likely_row_code = classification[0] if isinstance(classification, tuple) else classification
		if account.likely_row_code in row_labels:
			likely_row_by_account[account.name] = {
				"row_code": account.likely_row_code,
				"label": row_labels[account.likely_row_code],
			}
	unmapped_accounts.sort(key=lambda account: (root_order.get(account.root_type, 99), account.account_number or "", account.name))
	accounts = [
		{
			"name": account.name,
			"account_name": account.account_name,
			"account_number": account.account_number,
			"account_label": account_label(account),
			"parent_account": account.parent_account or "",
			"is_group": int(bool(account.is_group)),
			"root_type": account.root_type,
			"likely_row": likely_row_by_account.get(account.name),
		}
		for account in (all_accounts or [])
	]
	return {
		"template": {
			"name": template.name,
			"version": template.version,
			"accounting_standard": template.accounting_standard,
			"statement_type": template.statement_type,
		},
		"rows": rows,
		"accounts": accounts,
		"unmapped_accounts": unmapped_accounts,
		"summary": {
			"total_leaf_accounts": len(applicable_accounts),
			"mapped_accounts": len(mapped_accounts & applicable_account_names),
			"unmapped_accounts": len(unmapped_accounts),
			"total_mappings": len(mappings),
			"pending_review": pending_review,
		},
	}


@frappe.whitelist()
def save_mapping(company, template, account, row_code, cash_inflow_row_code=None, cash_outflow_row_code=None, sign_multiplier=1):
	"""Create or update the mapping of one account to a statement row; DocType validates."""
	_require_write_access()
	effective_from = str(_default_effective_from(company))
	mapping_key = "|".join((company, template, account, effective_from))
	values = {
		"row_code": row_code,
		"cash_inflow_row_code": cash_inflow_row_code or None,
		"cash_outflow_row_code": cash_outflow_row_code or None,
		"sign_multiplier": "-1" if cint(sign_multiplier) == -1 else "1",
	}
	doc = frappe.get_doc(
			{
				"doctype": "China Financial Statement Mapping",
				"company": company,
				"template": template,
				"account": account,
				"mapping_key": mapping_key,
				"effective_from": effective_from,
				"mapping_source": "Manual",
			}
	)
	doc.update(values)
	doc.save()
	return {"name": doc.name, "row_code": doc.row_code, "reviewed": bool(doc.reviewed)}


def _default_effective_from(company):
	settings = frappe.get_cached_doc("China Finance Settings", company)
	return settings.statement_mapping_activation_date or settings.statutory_reporting_activation_date or settings.activation_date or TEMPLATE_EFFECTIVE_FROM


@frappe.whitelist()
def save_mapping_configuration(company, template=None, report_effective_from=None, mapping_effective_from=None):
	"""Save the company-level dates used by reports and new statement mappings."""
	_require_template_write_access()
	settings = frappe.get_doc("China Finance Settings", company)
	report_effective_from = getdate(report_effective_from) if report_effective_from else None
	mapping_effective_from = getdate(mapping_effective_from) if mapping_effective_from else None
	if report_effective_from and getdate(settings.activation_date) > report_effective_from:
		frappe.throw(_("报表生效日期不能早于中国财务启用日期"))
	if mapping_effective_from and getdate(settings.activation_date) > mapping_effective_from:
		frappe.throw(_("科目映射统一生效日期不能早于中国财务启用日期"))
	settings.statutory_reporting_activation_date = report_effective_from
	settings.statement_mapping_activation_date = mapping_effective_from
	settings.save()
	if template and report_effective_from:
		template_doc = frappe.get_doc("China Financial Statement Template", template)
		template_doc.effective_from = report_effective_from
		template_doc.save()
	frappe.clear_cache(doctype="China Finance Settings")
	return {
		"report_effective_from": str(report_effective_from or ""),
		"mapping_effective_from": str(mapping_effective_from or ""),
	}


@frappe.whitelist()
def save_reclassification_rule(
	company, template, source_row_code, source_direction, target_row_code, source_account=None,
	effective_from=None, effective_to=None, enabled=1, name=None, review_notes=None,
):
	_require_write_access()
	template_doc = frappe.get_doc("China Financial Statement Template", template)
	if template_doc.statement_type != "Balance Sheet":
		frappe.throw(_("异常余额重分类目前只支持资产负债表"))
	rows = {row.row_code: row for row in template_doc.rows}
	for code in (source_row_code, target_row_code):
		if code not in rows or rows[code].row_type != "Mapped Accounts":
			frappe.throw(_("重分类项目必须是模板中的明细项目"))
	if source_row_code == target_row_code:
		frappe.throw(_("重分类来源项目和目标项目不能相同"))
	if source_account:
		account = frappe.db.get_value("Account", source_account, ["company", "is_group"], as_dict=True)
		if not account or account.company != company or account.is_group:
			frappe.throw(_("来源科目必须是当前公司的末级科目"))
		if not frappe.db.exists(
			"China Financial Statement Mapping",
			{"company": company, "template": template, "row_code": source_row_code, "account": source_account},
		):
			frappe.throw(_("来源科目尚未映射到来源项目"))
	if source_direction not in ("Debit Positive", "Credit Positive"):
		frappe.throw(_("余额方向无效"))
	effective_from = getdate(effective_from or template_doc.effective_from)
	if effective_to and getdate(effective_to) < effective_from:
		frappe.throw(_("失效日期不能早于生效日期"))
	if name:
		doc = frappe.get_doc("China Financial Statement Reclassification Rule", name)
		if doc.company != company or doc.template != template:
			frappe.throw(_("不能修改其他公司的重分类规则"), frappe.PermissionError)
	else:
		existing = frappe.db.get_value(
			"China Financial Statement Reclassification Rule",
			{"company": company, "template": template, "source_row_code": source_row_code, "source_account": source_account or ""},
			"name",
		)
		doc = frappe.get_doc("China Financial Statement Reclassification Rule", existing) if existing else frappe.new_doc("China Financial Statement Reclassification Rule")
	doc.update({
		"company": company, "template": template, "source_row_code": source_row_code,
		"source_account": source_account or None,
		"source_direction": source_direction, "target_row_code": target_row_code,
		"effective_from": effective_from, "effective_to": getdate(effective_to) if effective_to else None,
		"enabled": 1 if cint(enabled) else 0, "review_notes": review_notes or None,
	})
	doc.insert(ignore_permissions=True) if doc.is_new() else doc.save(ignore_permissions=True)
	return get_reclassification_rules_for_console(company, template_doc)


@frappe.whitelist()
def remove_mapping(name):
	_require_write_access()
	doc = frappe.get_doc("China Financial Statement Mapping", name)
	if not doc.has_permission("delete"):
		frappe.throw(_("无权删除该财务报表科目映射"), frappe.PermissionError)
	doc.delete()
	return {"deleted": name}


@frappe.whitelist()
def set_mappings_reviewed(names, reviewed=1):
	"""Bulk review/unreview through the existing single-mapping review API."""
	_require_write_access()
	if isinstance(names, str):
		names = frappe.parse_json(names)
	updated = [set_mapping_reviewed(name, reviewed=reviewed) for name in names]
	return {"updated": len(updated)}


@frappe.whitelist()
def save_template_formula(template, row_code, formula):
	"""Edit one Formula row of a statement template; DocType validation checks the formula."""
	_require_template_write_access()
	doc = frappe.get_doc("China Financial Statement Template", template)
	if not doc.has_permission("write"):
		frappe.throw(_("无权编辑该报表模板"), frappe.PermissionError)
	return update_template_formula(doc, row_code, formula)


def update_template_formula(template_doc, row_code, formula):
	row = next((row for row in template_doc.rows if row.row_code == row_code), None)
	if not row:
		frappe.throw(_("模板中不存在报表行 {0}").format(row_code))
	if row.row_type != "Formula":
		frappe.throw(_("报表行 {0} 不是公式行，不能编辑公式").format(row_code))
	row.formula = (formula or "").strip()
	# validate() on the template checks every Formula row via validate_formula.
	template_doc.save()
	return {"template": template_doc.name, "row_code": row.row_code, "formula": row.formula}
