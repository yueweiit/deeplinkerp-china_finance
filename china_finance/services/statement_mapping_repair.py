"""Narrow, auditable correction of the pre-1.8 cash-flow suggestions."""

from frappe import _


import frappe
from frappe.utils import cint

from china_finance.setup.templates import (
	MAPPING_RULE_VERSION,
	TEMPLATE_VERSION,
	classify_account_number,
	is_employee_cash_account,
)


def corrected_cash_flow_classification(account, mapping):
	number = str(account.account_number or "")
	old = (mapping.row_code, mapping.cash_inflow_row_code, mapping.cash_outflow_row_code)
	if is_employee_cash_account(number, account):
		expected = ("OTHER_OPERATING_PAYMENTS", "OTHER_OPERATING_RECEIPTS", "OTHER_OPERATING_PAYMENTS")
	elif number.startswith("660302"):
		expected = ("CASH_PAID_DIVIDENDS_INTEREST", "OTHER_FINANCING_RECEIPTS", "CASH_PAID_DIVIDENDS_INTEREST")
	else:
		return None
	if old != expected:
		return None
	return classify_account_number(number, "Cash Flow", account)


def repair_cash_flow_mappings(company=None, apply=False, include_manual=False):
	"""Preview by default. Manual repairs require an explicitly selected company.

	Never alter journal entries, assignment documents or archived report snapshots.
	Only exact matches to the known old classification are eligible.
	"""
	apply, include_manual = cint(apply), cint(include_manual)
	if include_manual and not company:
		frappe.throw(_("修正手工现金流映射必须指定公司"))
	from china_finance.setup.china_coa_profile import is_profile_company

	templates = frappe.get_all(
		"China Financial Statement Template",
		filters={"statement_type": "Cash Flow", "version": TEMPLATE_VERSION}, pluck="name",
	)
	if not templates:
		return []
	filters = {"template": ["in", templates]}
	if company:
		filters["company"] = company
	if not include_manual:
		filters["mapping_source"] = "Automatic"
	changes = []
	for mapping in frappe.get_all(
		"China Financial Statement Mapping", filters=filters,
		fields=["name", "company", "account", "row_code", "cash_inflow_row_code", "cash_outflow_row_code"],
	):
		if not is_profile_company(mapping.company):
			continue
		account = frappe.db.get_value(
			"Account", mapping.account, ["account_number", "account_name", "account_type", "is_group"], as_dict=True
		)
		if not account or account.is_group:
			continue
		classification = corrected_cash_flow_classification(account, mapping)
		if not classification:
			continue
		fields = ("row_code", "cash_inflow_row_code", "cash_outflow_row_code")
		change = {"name": mapping.name, "account": mapping.account,
			"before": {key: mapping.get(key) for key in fields}, "after": dict(zip(fields, classification))}
		changes.append(change)
		if apply:
			doc = frappe.get_doc("China Financial Statement Mapping", mapping.name)
			doc.update(change["after"])
			doc.mapping_rule_version = MAPPING_RULE_VERSION
			doc.save(ignore_permissions=True)
			doc.add_comment("Comment", "现金流分类修正（规则1.8），原映射及新映射：" + frappe.as_json(change))
	return changes
