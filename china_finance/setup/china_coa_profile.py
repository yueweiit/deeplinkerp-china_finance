import hashlib
import json
from functools import lru_cache

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime

CHART_TEMPLATE = "中国企业会计准则－一般纳税人制造业（1.0）"
CHART_VERSION = "1.0"
MAPPING_RULE_VERSION = "1.7"

COMPANY_DEFAULT_ACCOUNTS = {
	"default_cash_account": "1001",
	"default_bank_account": "100201",
	"default_receivable_account": "1122",
	"default_payable_account": "2202",
	"default_income_account": "6001",
	"default_expense_account": "6401",
	"default_inventory_account": "1405",
	"stock_received_but_not_billed": "2205",
	"asset_received_but_not_billed": "2206",
	"stock_adjustment_account": "640199",
	"accumulated_depreciation_account": "160201",
	"depreciation_expense_account": "660203",
	"capital_work_in_progress_account": "1604",
	"round_off_account": "660307",
	"exchange_gain_loss_account": "660304",
	"unrealized_exchange_gain_loss_account": "660305",
	"write_off_account": "660308",
	"disposal_account": "6115",
}

SETTINGS_ACCOUNTS = {"profit_loss_account": "4103", "retained_earnings_account": "410401"}
TAX_ACCOUNT_RULES = {"Input": "22210101", "Output": "22210102"}
TAX_ACCOUNT_RULE_OVERRIDES = {
	# In the source chart, the ordinary output VAT detail is 22210107;
	# 22210102 is specifically "销项税额抵减".
	"悦为智能技术(东莞)有限公司": {"Output": "22210107"},
}
TEMPORARY_ACCOUNT_NUMBERS = ("1901", "6901", "999901")
COMPANY_DEFAULT_ACCOUNT_OVERRIDES = {
	# The Yuewei source chart uses 660203 for property management and 660225
	# for depreciation, while the standard profile uses 660203 for depreciation.
	"悦为智能技术(东莞)有限公司": {"depreciation_expense_account": "660225"},
}

# A numbered chart may use a different but valid account type for a company
# specific detail account. Keep this override in the profile instead of
# forcing a rename or restructuring of the company's existing chart.
COMPANY_ACCOUNT_TYPE_OVERRIDES = {
	"悦为智能技术(东莞)有限公司": {"660203": "Expense Account"},
}


def uses_yuewei_company_chart(company):
	"""Identify the bundled chart by stable account signatures, not company name."""
	return bool(
		frappe.db.exists(
			"Account",
			{
				"company": company,
				"account_number": "660225",
				"account_name": "管理费用－折旧费",
			},
		)
		and frappe.db.exists(
			"Account",
			{
				"company": company,
				"account_number": "660203",
				"account_name": "管理费用－物业管理费",
			},
		)
	)


def get_company_default_accounts(company):
	overrides = {}
	if uses_yuewei_company_chart(company):
		overrides = COMPANY_DEFAULT_ACCOUNT_OVERRIDES["悦为智能技术(东莞)有限公司"]
	return {
		**COMPANY_DEFAULT_ACCOUNTS,
		**overrides,
	}


def get_tax_account_rules(company):
	overrides = {}
	if uses_yuewei_company_chart(company):
		overrides = TAX_ACCOUNT_RULE_OVERRIDES["悦为智能技术(东莞)有限公司"]
	return {
		**TAX_ACCOUNT_RULES,
		**overrides,
	}


def get_company_account_type_overrides(company):
	overrides = {}
	if uses_yuewei_company_chart(company):
		overrides = COMPANY_ACCOUNT_TYPE_OVERRIDES["悦为智能技术(东莞)有限公司"]
	return overrides


def get_settings_accounts(company):
	"""Resolve closing defaults from the actual chart, including renamed companies."""
	if uses_yuewei_company_chart(company):
		return {**SETTINGS_ACCOUNTS, "retained_earnings_account": "410411"}
	return dict(SETTINGS_ACCOUNTS)


@lru_cache(maxsize=1)
def get_chart_tree():
	from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import get_chart

	return get_chart(CHART_TEMPLATE) or {}


@lru_cache(maxsize=1)
def get_chart_hash():
	payload = json.dumps(get_chart_tree(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _flatten_chart(children, root_type=None, parent_number=None):
	rows = []
	metadata = {"root_type", "is_group", "account_number", "account_type", "account_category", "tax_rate"}
	for account_name, child in children.items():
		if account_name in metadata or not isinstance(child, dict):
			continue
		child_root = child.get("root_type") or root_type
		number = str(child.get("account_number") or "").strip() or None
		account_children = [key for key in child if key not in metadata]
		is_group = cint(child.get("is_group") or bool(account_children))
		if number:
			rows.append({
				"account_number": number,
				"account_name": account_name,
				"root_type": child_root,
				"is_group": is_group,
				"account_type": child.get("account_type") or None,
				"parent_number": parent_number,
			})
		rows.extend(_flatten_chart(child, child_root, number or parent_number))
	return rows


@lru_cache(maxsize=1)
def get_profile_accounts():
	return _flatten_chart(get_chart_tree())


@lru_cache(maxsize=1)
def get_profile_account_numbers():
	return frozenset(row["account_number"] for row in get_profile_accounts())


def get_company_profile(company):
	"""Validate against the selected chart snapshot, not a different chart's numbers."""
	if uses_yuewei_company_chart(company):
		from china_finance.services.account_template import (
			COMPANY_TEMPLATE,
			flatten_company_template_chart,
			get_company_template_chart,
		)

		tree = get_company_template_chart()
		payload = json.dumps(tree, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		return {
			"template": COMPANY_TEMPLATE,
			"version": CHART_VERSION,
			"hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
			"accounts": [row for row in flatten_company_template_chart() if row["account_number"]],
		}
	return {
		"template": CHART_TEMPLATE,
		"version": CHART_VERSION,
		"hash": get_chart_hash(),
		"accounts": get_profile_accounts(),
	}


def is_profile_company(company):
	return frappe.db.get_value("Company", company, "chart_of_accounts") == CHART_TEMPLATE


def get_company_accounts_by_number(company):
	rows = frappe.get_all(
		"Account", filters={"company": company},
		fields=["name", "account_name", "account_number", "root_type", "report_type", "account_type", "is_group", "disabled", "parent_account", "lft", "rgt"],
	)
	by_number, duplicates = {}, {}
	for row in rows:
		number = str(row.account_number or "").strip()
		if not number:
			continue
		if number in by_number:
			duplicates.setdefault(number, [by_number[number].name]).append(row.name)
		else:
			by_number[number] = row
	return by_number, duplicates


def get_account_by_number(company, account_number, *, leaf=None, required=True):
	rows = frappe.get_all(
		"Account", filters={"company": company, "account_number": str(account_number)},
		fields=["name", "account_name", "account_number", "root_type", "account_type", "is_group", "disabled", "parent_account"],
	)
	if len(rows) > 1:
		frappe.throw(_("公司 {0} 的科目编号 {1} 存在重复").format(company, account_number))
	if not rows:
		if required:
			frappe.throw(_("公司 {0} 缺少科目 {1}").format(company, account_number))
		return None
	if rows[0].disabled:
		frappe.throw(_("科目 {0} 已停用").format(rows[0].name))
	if leaf is not None and bool(rows[0].is_group) == bool(leaf):
		frappe.throw(_("科目 {0} 的分组属性不正确").format(rows[0].name))
	return rows[0]


def _company_default_matches(company_doc, fieldname, account, account_type):
	"""A grouped template default requires a usable leaf in its actual subtree."""
	selected = company_doc.get(fieldname)
	if not account.is_group:
		return selected == account.name
	if not selected or account.disabled or not account.lft or not account.rgt:
		return False
	filters = {
		"name": selected, "company": company_doc.name, "is_group": 0, "disabled": 0,
		"root_type": account.root_type, "account_currency": company_doc.default_currency,
		"lft": [">", account.lft], "rgt": ["<", account.rgt],
	}
	if account_type:
		filters["account_type"] = account_type
	return bool(frappe.db.exists("Account", filters))


def validate_profile(company, include_defaults=True):
	if not is_profile_company(company):
		return {"supported": False, "status": "Legacy", "errors": [], "warnings": []}
	profile = get_company_profile(company)
	expected = {row["account_number"]: row for row in profile["accounts"]}
	actual, duplicates = get_company_accounts_by_number(company)
	errors = [_("科目编号 {0} 重复：{1}").format(number, "、".join(names)) for number, names in duplicates.items()]
	warnings = []
	child_parents = {
		row.parent_account
		for row in frappe.get_all(
			"Account", filters={"company": company, "parent_account": ["is", "set"]}, fields=["parent_account"]
		)
		if row.parent_account
	}
	account_type_overrides = get_company_account_type_overrides(company)
	for number, rule in expected.items():
		account = actual.get(number)
		if not account:
			errors.append(_("缺少科目 {0} {1}").format(number, rule["account_name"]))
			continue
		if account.root_type != rule["root_type"]:
			errors.append(_("科目 {0} 根类型应为 {1}").format(number, rule["root_type"]))
		report_type = "Balance Sheet" if rule["root_type"] in {"Asset", "Liability", "Equity"} else "Profit and Loss"
		if account.report_type != report_type:
			errors.append(_("科目 {0} 报表类型应为 {1}").format(number, report_type))
		if account.disabled:
			errors.append(_("必需科目 {0} 已停用").format(number))
		has_children = account.name in child_parents
		if bool(account.is_group) != has_children:
			errors.append(_("科目 {0} 分组属性与实际父子关系不一致").format(number))
		expected_account_type = account_type_overrides.get(number, rule["account_type"]) or None
		if not account.is_group and (account.account_type or None) != expected_account_type:
			errors.append(_("科目 {0} 类型应为 {1}").format(number, expected_account_type or _("空")))
		if account.account_name != rule["account_name"]:
			warnings.append(
				_("科目 {0} 名称与模板不一致：当前为 {1}，模板为 {2}").format(
					number, account.account_name, rule["account_name"]
				)
			)
	default_differences = []
	if include_defaults:
		company_doc = frappe.get_cached_doc("Company", company)
		for fieldname, number in get_company_default_accounts(company).items():
			expected_account = actual.get(number)
			account_type = account_type_overrides.get(number, expected.get(number, {}).get("account_type"))
			if expected_account and not _company_default_matches(company_doc, fieldname, expected_account, account_type):
				message = (
					_("公司默认科目 {0} 应为 {1} 下本公司已启用且类型、币种匹配的明细科目")
					if expected_account.is_group else _("公司默认科目 {0} 应为 {1}")
				).format(fieldname, expected_account.name)
				warnings.append(message)
				difference = {
					"fieldname": fieldname,
					"account_number": number,
					"expected": expected_account.name,
					"actual": company_doc.get(fieldname),
				}
				if expected_account.is_group:
					difference["details"] = message
				default_differences.append(difference)
	return {
		"supported": True,
		"template": profile["template"],
		"version": profile["version"],
		"hash": profile["hash"],
		"status": "Ready" if not errors else "Needs Attention",
		"errors": errors,
		"warnings": warnings,
		"default_differences": default_differences,
	}


def apply_company_defaults(company, repair=False):
	if not is_profile_company(company):
		return {"updated": 0, "skipped": True}
	accounts, duplicates = get_company_accounts_by_number(company)
	if duplicates:
		frappe.throw(_("科目编号存在重复，不能同步公司默认科目"))
	company_doc = frappe.get_doc("Company", company)
	updated = 0
	for fieldname, number in get_company_default_accounts(company).items():
		account = accounts.get(number)
		# Choosing among descendants is a finance decision. Never write a group,
		# even when explicitly restoring template defaults.
		if not account or account.is_group or account.disabled:
			continue
		if repair or not company_doc.get(fieldname):
			if company_doc.get(fieldname) != account.name:
				company_doc.db_set(fieldname, account.name, update_modified=False)
				updated += 1
	if updated:
		frappe.clear_document_cache("Company", company)
	return {"updated": updated, "skipped": False}


def update_settings_profile(settings, status=None):
	if not is_profile_company(settings.company):
		settings.db_set({
			"coa_last_checked_on": now_datetime(),
			"coa_integrity_status": "Legacy",
			"coa_integrity_details": _("公司未使用受支持的中国标准科目模板，继续使用旧版兼容逻辑。"),
		}, update_modified=False)
		return
	status = status or validate_profile(settings.company)
	values = {
		"coa_template": status["template"],
		"coa_version": status["version"],
		"coa_hash": status["hash"],
		"coa_last_checked_on": now_datetime(),
		"coa_integrity_status": status["status"],
		"coa_integrity_details": "\n".join([*status["errors"], *status["warnings"]]) or _("科目模板完整"),
	}
	if not settings.coa_initialized_on:
		values["coa_initialized_on"] = now_datetime()
	for fieldname, number in get_settings_accounts(settings.company).items():
		if not settings.get(fieldname):
			account = get_account_by_number(settings.company, number, leaf=True, required=False)
			if account:
				values[fieldname] = account.name
	settings.db_set(values, update_modified=False)


def ensure_tax_mappings(company, effective_from):
	if not is_profile_company(company):
		return 0
	created = 0
	for direction, number in get_tax_account_rules(company).items():
		account = get_account_by_number(company, number, leaf=True)
		filters = {"company": company, "direction": direction, "account": account.name, "effective_from": getdate(effective_from)}
		effective_to = _available_tax_mapping_end(company, direction, account.name, effective_from)
		if effective_to is False or frappe.db.exists("China Tax Account Mapping", filters):
			continue
		frappe.get_doc({"doctype": "China Tax Account Mapping", **filters, "effective_to": effective_to, "enabled": 1}).insert(ignore_permissions=True)
		created += 1
	return created


def _available_tax_mapping_end(company, direction, account, effective_from):
	"""Return a non-overlapping end date, or False when already covered."""
	start = getdate(effective_from)
	rows = frappe.get_all(
		"China Tax Account Mapping",
		filters={"company": company, "direction": direction, "account": account},
		fields=["effective_from", "effective_to"],
		order_by="effective_from asc",
	)
	for row in rows:
		row_start = getdate(row.effective_from)
		row_end = getdate(row.effective_to) if row.effective_to else None
		if row_start <= start and (not row_end or row_end >= start):
			return False
		if row_start > start:
			return add_days(row_start, -1)
	return None


def _get_generic_vat_account(company):
	"""Return the unnumbered ERPNext VAT account only when it is clearly generic."""
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 0},
		fields=["name", "account_name", "account_number", "root_type"],
	)
	for account in accounts:
		if (
			not account.account_number
			and account.root_type == "Liability"
			and (account.account_name or "").strip().upper() in {"VAT", "VAT - YC"}
		):
			return account
	return None


def _remove_empty_generic_tax_parent(company):
	"""Remove ERPNext's empty generic tax group from a numbered China CoA.

	The China template owns tax accounts below ``222101xx``. ERPNext may leave its
	unnumbered ``Duties and Taxes`` group behind after the generic VAT child is
	normalized. Only remove the exact setup artifact when it is an empty group,
	so user-created tax groups and accounting history remain untouched.
	"""
	for account in frappe.get_all(
		"Account",
		filters={"company": company, "is_group": 1, "root_type": "Liability"},
		fields=["name", "account_name", "account_number"],
	):
		if account.account_number or (account.account_name or "").strip() not in {"Duties and Taxes", "关税与税项"}:
			continue
		if frappe.db.exists("Account", {"parent_account": account.name}):
			continue
		frappe.delete_doc("Account", account.name, ignore_permissions=True)
		return True
	return False


def _tax_template_is_unused(doctype, company, name):
	reference_doctype = "Sales Invoice" if doctype == "Sales Taxes and Charges Template" else "Purchase Invoice"
	return not frappe.db.exists(reference_doctype, {"company": company, "taxes_and_charges": name})


def _has_non_template_tax_reference(doctype, account):
	child_doctype = "Sales Taxes and Charges" if doctype == "Sales Taxes and Charges Template" else "Purchase Taxes and Charges"
	return frappe.db.exists(child_doctype, {"account_head": account, "parenttype": ["!=", doctype]})


def _item_tax_template_is_unused(name):
	return not any((
		frappe.db.exists("Item Tax", {"item_tax_template": name}),
		frappe.db.exists("Sales Invoice Item", {"item_tax_template": name}),
		frappe.db.exists("Purchase Invoice Item", {"item_tax_template": name}),
	))


def _remove_unused_generic_tax_templates(company):
	"""Remove the legacy ``China Tax`` templates after their links are repaired."""
	removed = 0
	for doctype, checker in (
		("Sales Taxes and Charges Template", _tax_template_is_unused),
		("Purchase Taxes and Charges Template", _tax_template_is_unused),
		("Item Tax Template", lambda _doctype, _company, name: _item_tax_template_is_unused(name)),
	):
		for template in frappe.get_all(doctype, filters={"company": company, "title": ["like", "China Tax%"]}, pluck="name"):
			if not checker(doctype, company, template):
				continue
			try:
				frappe.delete_doc(doctype, template, ignore_permissions=True)
				removed += 1
			except frappe.LinkExistsError:
				frappe.db.set_value(doctype, template, "disabled", 1, update_modified=False)
	return removed


def _normalize_generic_item_tax_templates(company, vat_account):
	"""Split an unused generic item tax template into China output and input variants."""
	updated = created = 0
	output_account = get_account_by_number(company, get_tax_account_rules(company)["Output"], leaf=True)
	input_account = get_account_by_number(company, get_tax_account_rules(company)["Input"], leaf=True)
	for template in frappe.get_all(
		"Item Tax Template", filters={"company": company, "disabled": 0}, fields=["name", "title"]
	):
		rows = frappe.get_all(
			"Item Tax Template Detail",
			filters={"parent": template.name, "parenttype": "Item Tax Template"},
			fields=["name", "tax_type", "tax_rate", "not_applicable"],
		)
		if not rows or any(row.tax_type != vat_account for row in rows) or not _item_tax_template_is_unused(template.name):
			continue
		# ERPNext's generic VAT setup can contain the same VAT account more than
		# once.  China has separate input/output accounts, but the input template
		# still may contain only one row per account or ERPNext rejects it during
		# validation.  Keep the first row's rate and discard duplicate setup rows.
		unique_rows = []
		seen_tax_types = set()
		for row in rows:
			if row.tax_type in seen_tax_types:
				frappe.delete_doc("Item Tax Template Detail", row.name, ignore_permissions=True)
				continue
			seen_tax_types.add(row.tax_type)
			unique_rows.append(row)
		rows = unique_rows
		for row in rows:
			frappe.db.set_value("Item Tax Template Detail", row.name, "tax_type", output_account.name, update_modified=False)
		output_title = f"{template.title}（销项）"
		frappe.db.set_value("Item Tax Template", template.name, "title", output_title, update_modified=False)
		input_title = f"{template.title}（进项）"
		if not frappe.db.exists("Item Tax Template", {"company": company, "title": input_title}):
			frappe.get_doc({
				"doctype": "Item Tax Template", "company": company, "title": input_title,
				"taxes": [
					{"tax_type": input_account.name, "tax_rate": rows[0].tax_rate, "not_applicable": rows[0].not_applicable}
				],
			}).insert(ignore_permissions=True)
			created += 1
		updated += 1
	return {"updated": updated, "created": created}


def _ensure_china_tax_templates(company):
	"""Create the three usable China tax templates for a profile company.

	ERPNext does not create these templates when the company is created with an
	imported chart. Keep this idempotent and use numbered China VAT accounts.
	"""
	output_account = get_account_by_number(company, get_tax_account_rules(company)["Output"], leaf=True)
	input_account = get_account_by_number(company, get_tax_account_rules(company)["Input"], leaf=True)
	created = {"sales": 0, "purchase": 0, "item": 0}

	definitions = (
		("Sales Taxes and Charges Template", "销售税费模板（13%销项税）", "sales", output_account.name, "销项税额", "销售税费"),
		("Purchase Taxes and Charges Template", "采购税费模板（13%进项税）", "purchase", input_account.name, "进项税额", "采购税费"),
	)
	for doctype, title, key, account, description, _label in definitions:
		if frappe.db.exists(doctype, {"company": company, "title": title}):
			continue
		frappe.get_doc({
			"doctype": doctype,
			"company": company,
			"title": title,
			"taxes": [{
				"charge_type": "On Net Total",
				"account_head": account,
				"description": description,
				"rate": 13,
			}],
		}).insert(ignore_permissions=True)
		created[key] += 1

	if not frappe.db.exists("Item Tax Template", {"company": company, "title": "物料税费模板（13%销项税）"}):
		frappe.get_doc({
			"doctype": "Item Tax Template",
			"company": company,
			"title": "物料税费模板（13%销项税）",
			"taxes": [{"tax_type": output_account.name, "tax_rate": 13}],
		}).insert(ignore_permissions=True)
		created["item"] += 1
	if not frappe.db.exists("Item Tax Template", {"company": company, "title": "物料税费模板（13%进项税）"}):
		frappe.get_doc({
			"doctype": "Item Tax Template",
			"company": company,
			"title": "物料税费模板（13%进项税）",
			"taxes": [{"tax_type": input_account.name, "tax_rate": 13}],
		}).insert(ignore_permissions=True)
		created["item"] += 1
	return created


def normalize_generic_vat_templates(company):
	"""Replace safe, unused generic VAT template rows with China input/output VAT.

	ERPNext can create an unnumbered ``VAT`` account while setting up tax templates.
	It cannot represent both China input and output VAT, so it must not survive on a
	company using the numbered China chart. Existing documents are deliberately
	never changed; only wholly-generic, unused templates are normalized.
	"""
	if not is_profile_company(company):
		return {"templates_updated": 0, "item_templates_updated": 0, "item_templates_created": 0, "vat_account_disabled": False, "vat_account_deleted": False, "skipped": True}
	china_templates = _ensure_china_tax_templates(company)
	vat = _get_generic_vat_account(company)
	if not vat:
		# Older migrations may have removed the orphaned VAT account while leaving
		# its string link in unused templates. Repair those links by value.
		for doctype, child_doctype, direction in (
			("Sales Taxes and Charges Template", "Sales Taxes and Charges", "Output"),
			("Purchase Taxes and Charges Template", "Purchase Taxes and Charges", "Input"),
		):
			target = get_account_by_number(company, get_tax_account_rules(company)[direction], leaf=True)
			for template in frappe.get_all(doctype, filters={"company": company, "disabled": 0}, pluck="name"):
				if not _tax_template_is_unused(doctype, company, template):
					continue
				for row in frappe.get_all(child_doctype, filters={"parent": template, "parenttype": doctype}, fields=["name", "account_head"]):
					if str(row.account_head or "").strip().upper().startswith("VAT"):
						frappe.db.set_value(child_doctype, row.name, "account_head", target.name, update_modified=False)
				if str(frappe.db.get_value(doctype, template, "title") or "").startswith("China Tax"):
					frappe.db.set_value(doctype, template, "disabled", 1, update_modified=False)
		for template in frappe.get_all("Item Tax Template", filters={"company": company, "disabled": 0}, pluck="name"):
				if not _item_tax_template_is_unused(template):
					continue
				for row in frappe.get_all("Item Tax Template Detail", filters={"parent": template, "parenttype": "Item Tax Template"}, fields=["name", "tax_type"]):
					if str(row.tax_type or "").strip().upper().startswith("VAT"):
						frappe.db.set_value("Item Tax Template Detail", row.name, "tax_type", get_account_by_number(company, get_tax_account_rules(company)["Output"], leaf=True).name, update_modified=False)
				if str(frappe.db.get_value("Item Tax Template", template, "title") or "").startswith("China Tax"):
					frappe.db.set_value("Item Tax Template", template, "disabled", 1, update_modified=False)
		removed_templates = _remove_unused_generic_tax_templates(company)
		return {
			"templates_updated": 0, "item_templates_updated": 0, "item_templates_created": 0,
			"china_templates_created": china_templates,
			"generic_templates_deleted": removed_templates,
			"vat_account_disabled": False, "vat_account_deleted": False,
			"generic_tax_parent_deleted": _remove_empty_generic_tax_parent(company), "skipped": False,
		}

	updated = 0
	for template_doctype, child_doctype, direction in (
		("Sales Taxes and Charges Template", "Sales Taxes and Charges", "Output"),
		("Purchase Taxes and Charges Template", "Purchase Taxes and Charges", "Input"),
	):
		target = get_account_by_number(company, get_tax_account_rules(company)[direction], leaf=True)
		for template in frappe.get_all(template_doctype, filters={"company": company, "disabled": 0}, pluck="name"):
			rows = frappe.get_all(child_doctype, filters={"parent": template, "parenttype": template_doctype}, fields=["name", "account_head"])
			if not rows or any(row.account_head != vat.name for row in rows):
				continue
			if not _tax_template_is_unused(template_doctype, company, template):
				continue
			for row in rows:
				frappe.db.set_value(child_doctype, row.name, "account_head", target.name, update_modified=False)
			updated += 1
	item_templates = _normalize_generic_item_tax_templates(company, vat.name)

	vat_account_disabled = vat_account_deleted = False
	# Template normalization and account deletion must be independently idempotent.
	# A previous migration may already have replaced every template reference while
	# leaving the setup artifact behind, so never gate deletion on this run updating
	# a template.
	if not frappe.db.exists("GL Entry", {"account": vat.name, "is_cancelled": 0}):
		template_references = sum(
			frappe.db.count(child_doctype, {"account_head": vat.name, "parenttype": template_doctype})
			for template_doctype, child_doctype in (
				("Sales Taxes and Charges Template", "Sales Taxes and Charges"),
				("Purchase Taxes and Charges Template", "Purchase Taxes and Charges"),
			)
		)
		business_references = any(
			_has_non_template_tax_reference(template_doctype, vat.name)
			for template_doctype in ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template")
		)
		if not template_references and not business_references:
			try:
				# No GL or tax-template references remain, so this is an ERPNext setup
				# artifact rather than accounting history. Delete it to keep the China CoA clean.
				frappe.delete_doc("Account", vat.name, ignore_permissions=True)
				vat_account_deleted = True
			except frappe.LinkExistsError:
				# Keep an unexpectedly linked account out of normal selection without
				# bypassing Frappe's link integrity safeguards.
				frappe.db.set_value("Account", vat.name, "disabled", 1, update_modified=False)
				vat_account_disabled = True
	generic_tax_parent_deleted = _remove_empty_generic_tax_parent(company)
	generic_templates_deleted = _remove_unused_generic_tax_templates(company)
	return {
		"templates_updated": updated, "item_templates_updated": item_templates["updated"],
		"item_templates_created": item_templates["created"],
		"china_templates_created": china_templates,
		"generic_templates_deleted": generic_templates_deleted,
		"vat_account_disabled": vat_account_disabled, "vat_account_deleted": vat_account_deleted,
		"generic_tax_parent_deleted": generic_tax_parent_deleted, "skipped": False,
	}


def ensure_cash_scope(company, effective_from):
	if not is_profile_company(company):
		return 0
	created = 0
	rules = {
		"1001": ("库存现金", 1, 0, None),
		"100201": ("随时可用存款", 1, 0, None),
		"101201": ("排除项", 0, 1, "银行承兑汇票保证金属于受限资金"),
		"101299": ("排除项", 0, 0, "其他货币资金是否可随时支用无法由科目编号判断，需财务人员复核。"),
	}
	if uses_yuewei_company_chart(company):
		rules["101201"] = ("排除项", 0, 0, "外埠存款是否可随时支用需财务人员复核，不能按保证金认定为受限资金。")
	accounts, _duplicates = get_company_accounts_by_number(company)
	for number, (classification, included, restricted, reason) in rules.items():
		account = accounts.get(number)
		if not account:
			continue
		key = f"{company}|{account.name}|{getdate(effective_from)}"
		effective_to = _available_scope_end(company, account.name, effective_from)
		if effective_to is False or frappe.db.exists("China Cash Equivalent Scope", {"scope_key": key}):
			continue
		frappe.get_doc({
			"doctype": "China Cash Equivalent Scope", "scope_key": key,
			"company": company, "account": account.name, "classification": classification,
			"included": included, "restricted": restricted, "restriction_reason": reason,
			"effective_from": getdate(effective_from), "effective_to": effective_to, "reviewed": 0,
			"policy_basis": "按中国科目模板编号自动建议，须由财务人员复核资金可随时支用性。",
		}).insert(ignore_permissions=True)
		created += 1
	# Include company-created bank children as review-required suggestions.
	for account in frappe.get_all(
		"Account", filters={"company": company, "is_group": 0, "disabled": 0, "account_type": "Bank"},
		fields=["name"],
	):
		key = f"{company}|{account.name}|{getdate(effective_from)}"
		effective_to = _available_scope_end(company, account.name, effective_from)
		if effective_to is False or frappe.db.exists("China Cash Equivalent Scope", {"scope_key": key}):
			continue
		frappe.get_doc({
			"doctype": "China Cash Equivalent Scope", "scope_key": key,
			"company": company, "account": account.name, "classification": "随时可用存款",
			"included": 1, "restricted": 0, "effective_from": getdate(effective_from),
			"effective_to": effective_to, "reviewed": 0,
			"policy_basis": "按 Bank 科目类型自动建议，须由财务人员复核。",
		}).insert(ignore_permissions=True)
		created += 1
	return created


def _available_scope_end(company, account, effective_from):
	"""Return a non-overlapping end date, or False when already covered."""
	start = getdate(effective_from)
	rows = frappe.get_all(
		"China Cash Equivalent Scope",
		filters={"company": company, "account": account},
		fields=["effective_from", "effective_to"],
		order_by="effective_from asc",
	)
	for row in rows:
		row_start = getdate(row.effective_from)
		row_end = getdate(row.effective_to) if row.effective_to else None
		if row_start <= start and (not row_end or row_end >= start):
			return False
		if row_start > start:
			return add_days(row_start, -1)
	return None


def get_profile_status(company):
	status = validate_profile(company)
	if not status["supported"]:
		return status
	settings = frappe.db.get_value("China Finance Settings", company, "name")
	status["settings"] = settings
	status["mapping_rule_version"] = MAPPING_RULE_VERSION
	status["pending_counts"] = {
		"unreviewed_statement_mappings": frappe.db.count(
			"China Financial Statement Mapping", {"company": company, "reviewed": 0}
		),
		"unreviewed_cash_scope": frappe.db.count(
			"China Cash Equivalent Scope", {"company": company, "reviewed": 0}
		),
		"tax_mappings": frappe.db.count(
			"China Tax Account Mapping", {"company": company, "enabled": 1}
		),
	}
	return status


def sync_enabled_company_profiles():
	"""Migrate metadata and missing suggestions without changing company defaults."""
	if not frappe.db.exists("DocType", "China Finance Settings"):
		return 0
	updated = 0
	for row in frappe.get_all(
		"China Finance Settings", filters={"enabled": 1}, fields=["name", "company", "activation_date"]
	):
		settings = frappe.get_doc("China Finance Settings", row.name)
		if not is_profile_company(row.company):
			update_settings_profile(settings)
			continue
		ensure_tax_mappings(row.company, row.activation_date)
		ensure_cash_scope(row.company, row.activation_date)
		update_settings_profile(settings)
		updated += 1
	return updated


def _readiness_item(code, label, passed, details, route, *, severity="Blocking", count=0, repairable=False):
	"""Keep master-data readiness payloads compatible with the settings dashboard."""
	return {
		"code": code,
		"label": label,
		"passed": bool(passed),
		"details": details,
		"route": route,
		"severity": severity,
		"count": cint(count),
		"repairable": bool(repairable),
	}


def _route(doctype, filters=None):
	return {"type": "doctype", "name": doctype, "filters": filters or {}}


def _account_is_usable(company, account, account_types=None):
	if not account:
		return False
	row = frappe.db.get_value(
		"Account", account, ["company", "is_group", "disabled", "account_type"], as_dict=True
	)
	return bool(
		row and row.company == company and not cint(row.is_group) and not cint(row.disabled)
		and (not account_types or row.account_type in account_types)
	)


def _has_company_activity(doctype, company):
	if not frappe.db.exists("DocType", doctype):
		return False
	return frappe.db.exists(doctype, {"company": company}) if frappe.db.has_column(doctype, "company") else False


def _company_default_readiness(company):
	status = validate_profile(company)
	issues = [*status.get("errors", []), *status.get("default_differences", [])]
	details = []
	for issue in issues:
		if isinstance(issue, dict):
			details.append(issue.get("details") or _("{0} 应为科目编号 {1}").format(issue["fieldname"], issue["account_number"]))
		else:
			details.append(issue)
	return _readiness_item(
		"COMPANY_DEFAULT_ACCOUNTS", _("公司默认科目"), not issues,
		"；".join(details) or _("模板默认科目完整"), _route("Company", {"name": company}),
		count=len(issues), repairable=bool(issues),
	)


def _bank_and_payment_readiness(company):
	bank_accounts = frappe.get_all(
		"Bank Account", filters={"company": company, "is_company_account": 1}, fields=["name", "account"]
	)
	invalid_banks = [row.name for row in bank_accounts if not _account_is_usable(company, row.account, {"Bank"})]
	payment_rows = frappe.get_all(
		"Mode of Payment Account", filters={"company": company}, fields=["name", "parent", "default_account"]
	)
	invalid_payment = [row.name for row in payment_rows if row.default_account and not _account_is_usable(company, row.default_account, {"Cash", "Bank"})]
	empty_payment = [row.name for row in payment_rows if not row.default_account]
	used = bool(bank_accounts or payment_rows or _has_company_activity("Payment Entry", company))
	passed = not invalid_banks and not invalid_payment and not empty_payment
	severity = "Blocking" if used else "Info"
	details = []
	if invalid_banks:
		details.append(_("无效银行账户绑定 {0} 个").format(len(invalid_banks)))
	if invalid_payment:
		details.append(_("无效收付款方式科目 {0} 个").format(len(invalid_payment)))
	if empty_payment:
		details.append(_("待补齐收付款方式科目 {0} 个").format(len(empty_payment)))
	if not details:
		details.append(_("尚未启用银行或收付款方式") if not used else _("银行与收付款方式科目完整"))
	return _readiness_item(
		"BANK_AND_PAYMENT", _("银行及收付款方式"), passed or not used,
		"；".join(details), _route("Bank Account", {"company": company}),
		severity=severity, count=len(invalid_banks) + len(invalid_payment) + len(empty_payment),
		repairable=bool(empty_payment),
	)


def _warehouse_readiness(company):
	warehouses = frappe.get_all(
		"Warehouse", filters={"company": company, "is_group": 0}, fields=["name", "account"]
	)
	perpetual = cint(frappe.db.get_value("Company", company, "enable_perpetual_inventory"))
	invalid = [row.name for row in warehouses if not _account_is_usable(company, row.account, {"Stock"})]
	passed = not invalid
	severity = "Blocking" if perpetual and warehouses else ("Warning" if warehouses else "Info")
	details = (
		_("启用永续库存时，仓库必须绑定可用库存科目；缺少 {0} 个").format(len(invalid))
		if invalid else (_("仓库库存科目完整") if warehouses else _("尚未建立仓库，暂不检查库存科目"))
	)
	return _readiness_item(
		"WAREHOUSE_INVENTORY", _("仓库与库存估值科目"), passed or not warehouses,
		details, _route("Warehouse", {"company": company}), severity=severity, count=len(invalid),
	)


def _tax_template_readiness(company):
	results = []
	for doctype, label, invoice_doctype in (
		("Sales Taxes and Charges Template", _("销售税费模板"), "Sales Invoice"),
		("Purchase Taxes and Charges Template", _("采购税费模板"), "Purchase Invoice"),
	):
		templates = frappe.get_all(doctype, filters={"company": company}, pluck="name")
		invalid = 0
		for name in templates:
			doc = frappe.get_cached_doc(doctype, name)
			invalid += sum(1 for row in doc.get("taxes", []) if row.account_head and not _account_is_usable(company, row.account_head))
		used = bool(templates or _has_company_activity(invoice_doctype, company))
		results.append(_readiness_item(
			f"{doctype.upper().replace(' ', '_')}", label, invalid == 0,
			_("税费科目完整") if used and not invalid else (
				_("无效税费科目 {0} 个").format(invalid) if invalid else _("尚未配置或使用该业务模块")
			), _route(doctype, {"company": company}),
			severity="Blocking" if used else "Info", count=invalid,
		))
	return results


def _asset_readiness(company):
	categories = frappe.get_all("Asset Category", pluck="name")
	assets_exist = _has_company_activity("Asset", company)
	invalid = 0
	for category in categories:
		for row in frappe.get_all(
			"Asset Category Account", filters={"parent": category, "company_name": company},
			fields=["fixed_asset_account", "accumulated_depreciation_account", "depreciation_expense_account", "capital_work_in_progress_account"],
		):
			for account in row.values():
				if account and not _account_is_usable(company, account):
					invalid += 1
	configured = frappe.db.count("Asset Category Account", {"company_name": company})
	used = bool(assets_exist or configured)
	return _readiness_item(
		"ASSET_CATEGORY_ACCOUNTS", _("资产类别科目"), invalid == 0,
		_("资产类别科目完整") if used and not invalid else (
			_("无效资产科目 {0} 个").format(invalid) if invalid else _("未启用固定资产模块")
		), _route("Asset Category", {}), severity="Blocking" if used else "Info", count=invalid,
	)


def _cost_center_and_finance_book_readiness(company):
	# Cost Center / Finance Book carry a company link. Their accounts are not configured here;
	# only records explicitly selected by this company are allowed to be used by transaction validation.
	default_cost_center = (
		frappe.db.get_value("Company", company, "cost_center")
		if frappe.db.has_column("Company", "cost_center") else None
	)
	valid_default = not default_cost_center or frappe.db.get_value("Cost Center", default_cost_center, "company") == company
	return _readiness_item(
		"COST_CENTER_FINANCE_BOOK", _("成本中心与财务账簿"), valid_default,
		_("默认成本中心公司不一致") if not valid_default else _("默认成本中心有效；财务账簿由原生单据按公司校验"),
		_route("Cost Center", {"company": company}), severity="Blocking" if not valid_default else "Info",
		count=0 if valid_default else 1,
	)


def _manufacturing_readiness(company):
	boms = frappe.db.count("BOM", {"company": company, "is_active": 1})
	workstations = frappe.db.count("Workstation")
	stock_account = frappe.db.get_value("Company", company, "default_inventory_account")
	manufacturing_accounts = frappe.get_all(
		"Account", filters={"company": company, "account_number": ["in", ["5001", "5101"]]},
		fields=["name", "root_type", "is_group", "disabled"],
	)
	invalid = [row.name for row in manufacturing_accounts if row.root_type != "Asset" or cint(row.is_group) or cint(row.disabled)]
	used = bool(boms or frappe.db.exists("Work Order", {"company": company}) or frappe.db.exists("Stock Entry", {"company": company, "stock_entry_type": "Manufacture"}))
	passed = _account_is_usable(company, stock_account, {"Stock"}) and not invalid
	details = _("尚未启用生产业务；BOM、工序和工作站不会自动创建") if not used else (
		_("制造成本和库存估值科目可用") if passed else
		_("制造成本/库存估值科目无效：{0}").format("、".join(invalid) or _("缺少可用默认库存科目"))
	)
	return _readiness_item(
		"MANUFACTURING_MASTER_DATA", _("生产主数据与成本科目"), passed or not used,
		details, _route("BOM", {"company": company}), severity="Blocking" if used else "Info",
		count=len(invalid) + (0 if _account_is_usable(company, stock_account, {"Stock"}) else 1),
	)


def _optional_module_readiness(company):
	items = []
	optional = (
		("POS", _("POS 默认科目"), "POS Profile", "POS Invoice", ("income_account", "expense_account", "write_off_account", "account_for_change_amount")),
		("EXPENSE_CLAIM", _("费用报销科目"), "Expense Claim", "Expense Claim", ()),
		("PAYROLL", _("薪资科目"), "Payroll Entry", "Payroll Entry", ()),
	)
	for code, label, config_doctype, transaction_doctype, fields in optional:
		installed = frappe.db.exists("DocType", config_doctype) and frappe.db.exists("DocType", transaction_doctype)
		if not installed:
			items.append(_readiness_item(
				code, label, True, _("相关模块未安装，不阻断结账"),
				_route("Company", {"name": company}), severity="Info",
			))
			continue
		active = _has_company_activity(config_doctype, company) or _has_company_activity(transaction_doctype, company)
		invalid = 0
		if fields:
			for row in frappe.get_all(config_doctype, filters={"company": company}, fields=["name", *fields]):
				invalid += sum(1 for fieldname in fields if row.get(fieldname) and not _account_is_usable(company, row.get(fieldname)))
		items.append(_readiness_item(
			code, label, invalid == 0,
			_("启用后科目配置完整") if active and not invalid else (
				_("无效公司科目 {0} 个").format(invalid) if invalid else _("功能未启用，不阻断结账")
			), _route(config_doctype, {"company": company}), severity="Blocking" if active else "Info", count=invalid,
		))
	return items


def get_china_coa_master_data_readiness(company):
	"""Return only configuration facts; never creates or updates ERPNext business masters."""
	if not is_profile_company(company):
		return {
			"company": company, "supported": False, "status": "Legacy", "blocking_count": 0,
			"warning_count": 0,
			"items": [_readiness_item(
				"MASTER_DATA_LEGACY", _("主数据与业务默认科目"), True,
				_("旧科目表公司不自动套用中国模板主数据检查"), _route("Company", {"name": company}), severity="Info",
			)],
		}
	items = [
		_company_default_readiness(company), _bank_and_payment_readiness(company), _warehouse_readiness(company),
		*_tax_template_readiness(company), _asset_readiness(company), _cost_center_and_finance_book_readiness(company),
		_manufacturing_readiness(company), *_optional_module_readiness(company),
	]
	blocking = [item for item in items if not item["passed"] and item["severity"] == "Blocking"]
	warnings = [item for item in items if not item["passed"] and item["severity"] == "Warning"]
	return {
		"company": company, "supported": True, "status": "Ready" if not blocking else "Needs Attention",
		"blocking_count": len(blocking), "warning_count": len(warnings), "items": items,
	}


def _repair_mode_of_payment_accounts(company):
	company_doc = frappe.get_cached_doc("Company", company)
	updated = 0
	for row in frappe.get_all("Mode of Payment Account", filters={"company": company}, fields=["name", "parent", "default_account"]):
		if row.default_account:
			continue
		mode_type = frappe.db.get_value("Mode of Payment", row.parent, "type")
		default_account = company_doc.default_bank_account if mode_type == "Bank" else company_doc.default_cash_account if mode_type == "Cash" else None
		if default_account and _account_is_usable(company, default_account, {"Bank", "Cash"}):
			frappe.db.set_value("Mode of Payment Account", row.name, "default_account", default_account, update_modified=False)
			updated += 1
	return updated


def sync_china_coa_master_data(company, repair=False):
	"""Synchronize deterministic suggestions. Repair mode fills empty fields only."""
	if not is_profile_company(company):
		frappe.throw(_("公司未使用受支持的中国科目模板"))
	settings = frappe.get_cached_doc("China Finance Settings", company)
	# Master-data repair deliberately fills blanks only. Restoring a nonempty Company
	# default remains the separate, explicit "恢复模板默认科目" operation.
	defaults = apply_company_defaults(company, repair=False)
	tax_mappings = ensure_tax_mappings(company, settings.activation_date)
	vat_templates = normalize_generic_vat_templates(company)
	cash_scope = ensure_cash_scope(company, settings.activation_date)
	payment_accounts = _repair_mode_of_payment_accounts(company) if cint(repair) else 0
	status = get_china_coa_master_data_readiness(company)
	return {
		"status": status, "company_defaults_updated": defaults["updated"],
		"tax_mappings_created": tax_mappings, "cash_scope_created": cash_scope,
		"vat_templates_updated": vat_templates["templates_updated"],
		"item_tax_templates_updated": vat_templates["item_templates_updated"],
		"item_tax_templates_created": vat_templates["item_templates_created"],
		"vat_account_disabled": vat_templates["vat_account_disabled"],
		"vat_account_deleted": vat_templates["vat_account_deleted"],
		"mode_of_payment_accounts_updated": payment_accounts,
	}
