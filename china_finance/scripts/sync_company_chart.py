"""Synchronize a company's numbered account chart from an Excel chart.

The workbook used here is a chart/ledger balance export, not a voucher import.
Only the account code, display name and numbered parent hierarchy are read from
it. ERPNext root types, account types and existing non-template accounts are
preserved unless a parent must be converted to a group for the requested tree.
"""

from __future__ import annotations

from frappe import _


import re
from collections import Counter
from pathlib import Path

import frappe
from frappe.utils import cint


COMPANY = "悦为智能技术(东莞)有限公司"
NAME_SEPARATOR = "－"
CODE_RE = re.compile(r"^\d{4,}$")
ROOT_PARENT_HINTS = {
	"1321": "流动资产",
	"1407": "存货",
	"1532": "非流动资产",
	"1621": "非流动资产",
	"1622": "非流动资产",
	"2401": "非流动负债",
	"3101": "资产",
	"3201": "资产",
	"3202": "资产",
	"6201": "其他收益",
	"6801": "所得税费用",
}


def sync_company_chart(file_path, company=COMPANY, apply=False):
	"""Preview or apply the numbered chart in ``file_path`` to one company.

	The operation is intentionally limited to the Yuewei company. The caller
	should make a site backup before calling with ``apply=True``.
	"""
	if company != COMPANY:
		frappe.throw(_("此同步脚本仅允许处理 aaa 的悦为智能技术(东莞)有限公司"))

	source = read_chart(file_path)
	plan = build_plan(source, company)
	if not apply:
		return plan

	if frappe.db.exists("GL Entry", {"company": company, "is_cancelled": 0}):
		frappe.throw(_("悦为公司已有未冲销总账分录，不能直接同步科目主数据；请先完成迁移方案评估"))

	try:
		result = apply_plan(plan, company)
		frappe.db.commit()
		return result
	except Exception:
		frappe.db.rollback()
		raise


def preview_summary(file_path, company=COMPANY):
	"""Return a compact preview suitable for console review."""
	plan = sync_company_chart(file_path, company=company, apply=False)
	return {
		"company": plan["company"],
		"source_account_count": plan["source_account_count"],
		"existing_numbered_account_count": plan["existing_numbered_account_count"],
		"missing_count": len(plan["source_codes_missing_in_company"]),
		"missing_codes": plan["source_codes_missing_in_company"],
		"retained_extra_count": len(plan["company_codes_not_in_source_retained"]),
		"retained_extra_codes": plan["company_codes_not_in_source_retained"],
		"actions": dict(Counter(row["action"] for row in plan["preview"])),
		"unresolved_parent_codes": [
			row["code"] for row in plan["preview"] if not row["target_parent"]
		],
		"key_accounts": [
			row for row in plan["preview"] if row["code"] in {
				"1001", "1002", "100201", "1501", "1502", "1503", "2203",
				"2231", "2232", "5001", "5101", "5201", "5301", "530101",
				"530102", "6201", "660203", "660204", "660208", "660212",
			}
		],
		"bank_account_name_assumption": plan["bank_account_name_assumption"],
	}


def read_chart(file_path):
	"""Read numbered account rows from the first worksheet in an xlsx file."""
	try:
		from openpyxl import load_workbook
	except ImportError:
		frappe.throw(_("当前环境缺少 openpyxl，无法读取科目余额表"))

	path = Path(file_path)
	if not path.exists():
		frappe.throw(_("找不到科目表文件：{0}").format(file_path))

	workbook = load_workbook(filename=path, read_only=True, data_only=True)
	worksheet = workbook.active
	rows = []
	seen = {}
	for excel_row, values in enumerate(worksheet.iter_rows(min_row=1, values_only=True), start=1):
		if not values:
			continue
		code = _clean(values[0] if len(values) > 0 else None)
		name = _clean(values[1] if len(values) > 1 else None)
		if not CODE_RE.fullmatch(code) or not name:
			continue
		if code in seen:
			frappe.throw(_("科目表第 {0} 行与第 {1} 行重复使用编码 {2}").format(excel_row, seen[code], code))
		seen[code] = excel_row
		rows.append({"code": code, "raw_name": name, "excel_row": excel_row})
	workbook.close()

	if not rows:
		frappe.throw(_("科目表没有读取到数字科目编码"))
	return rows


def build_plan(source, company=COMPANY):
	"""Build a non-mutating comparison and synchronization plan."""
	source_by_code = {}
	for row in source:
		code = row["code"]
		parts = _name_parts(row["raw_name"], code)
		source_by_code[code] = {
			**row,
			"parts": parts,
			"account_name": NAME_SEPARATOR.join(parts),
			"parent_code": _parent_code(code, source_by_code),
		}

	# Resolve parent codes after all source rows are known.
	for code, row in source_by_code.items():
		row["parent_code"] = _parent_code(code, source_by_code)
		row["is_group"] = any(other != code and other.startswith(code) for other in source_by_code)

	existing = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=[
			"name", "account_number", "account_name", "parent_account", "is_group",
			"root_type", "account_type", "disabled",
		],
	)
	existing_by_code = {}
	duplicate_existing = []
	for account in existing:
		if not account.account_number:
			continue
		code = str(account.account_number).strip()
		if code in existing_by_code:
			duplicate_existing.append({"code": code, "names": [existing_by_code[code].name, account.name]})
		else:
			existing_by_code[code] = account
	if duplicate_existing:
		frappe.throw(_("悦为公司存在重复科目编码：{0}").format(duplicate_existing))

	parent_by_code = {str(a.account_number): a for a in existing if a.account_number}
	root_parents = _root_parent_choices(existing)
	preview = []
	for code, row in sorted(source_by_code.items(), key=lambda item: (len(item[0]), int(item[0]))):
		account = existing_by_code.get(code)
		parent_code = row["parent_code"]
		parent = parent_by_code.get(parent_code) if parent_code else None
		if not parent and not parent_code:
			parent = _fallback_parent(code, existing_by_code, root_parents)
		target_parent = parent.name if parent else None
		if not target_parent and parent_code and parent_code in source_by_code:
			planned_parent = source_by_code[parent_code]
			target_parent = f"{parent_code} - {planned_parent['account_name']}（将创建）"
		preview.append(
			{
				"code": code,
				"source_name": row["raw_name"],
				"target_name": row["account_name"],
				"current_name": account.account_name if account else None,
				"current_parent": account.parent_account if account else None,
				"target_parent_code": parent_code,
				"target_parent": target_parent,
				"is_group": row["is_group"],
				"current_is_group": cint(account.is_group) if account else None,
				"action": _action(account, row, parent),
			}
		)

	return {
		"company": company,
		"source_account_count": len(source_by_code),
		"existing_numbered_account_count": len(existing_by_code),
		"source_codes_missing_in_company": sorted(set(source_by_code) - set(existing_by_code), key=lambda x: (len(x), int(x))),
		"company_codes_not_in_source_retained": sorted(set(existing_by_code) - set(source_by_code), key=lambda x: (len(x), int(x))),
		"preview": preview,
		"bank_account_name_assumption": "100201 使用悦为现有‘基本存款账户’作为末级名称，改为完整显示‘银行存款－基本存款账户’，不复制深圳公司的名称",
	}


def apply_plan(plan, company=COMPANY):
	"""Apply the plan in parent-first order and retain out-of-source accounts."""
	source = {row["code"]: row for row in plan["preview"]}
	accounts = _load_accounts(company)
	existing_by_code = {str(a.account_number): a for a in accounts if a.account_number}
	root_parents = _root_parent_choices(accounts)

	created = []
	renamed = []
	reparented = []
	converted_to_group = []
	retained_group_exceptions = []

	# First make all source names canonical. 100201 is company-specific: the
	# source company's name is replaced by Yuewei's existing末级名称, while the
	# parent category remains visible in the stored hierarchy.
	for code, item in sorted(source.items(), key=lambda pair: (len(pair[0]), int(pair[0]))):
		account = existing_by_code.get(code)
		if not account or account.account_name == item["target_name"]:
			continue
		from erpnext.accounts.doctype.account.account import update_account_number

		new_name = update_account_number(account.name, item["target_name"], code)
		renamed.append({"code": code, "from": account.name, "to": new_name or account.name})
		accounts = _load_accounts(company)
		existing_by_code = {str(a.account_number): a for a in accounts if a.account_number}

	# Rebuild the index after every rename because Account.name is the link key.
	accounts = _load_accounts(company)
	existing_by_code = {str(a.account_number): a for a in accounts if a.account_number}
	root_parents = _root_parent_choices(accounts)

	for code, item in sorted(source.items(), key=lambda pair: (len(pair[0]), int(pair[0]))):
		account = existing_by_code.get(code)
		parent = _get_target_parent(item, source, existing_by_code, root_parents)
		if not parent:
			frappe.throw(_("科目 {0} 找不到可用的父科目").format(code))

		wants_group = bool(item["is_group"])
		if not account:
			account = frappe.get_doc(
				{
					"doctype": "Account",
					"account_number": code,
					"account_name": item["target_name"],
					"company": company,
					"parent_account": parent.name,
					"is_group": 1 if wants_group else 0,
					"account_type": "",
					"root_type": parent.root_type,
					"report_type": parent.report_type,
					"account_currency": frappe.get_cached_value("Company", company, "default_currency"),
				}
			)
			account.flags.ignore_root_company_validation = True
			account.flags.exclude_account_type_check = True
			account.insert(ignore_permissions=True)
			created.append({"code": code, "name": account.name, "parent": parent.name})
			existing_by_code[code] = account
			continue

		# A source leaf may still have retained company-specific children. Keep it
		# as a group in that case rather than deleting or orphaning those accounts.
		actual_children = frappe.get_all(
			"Account", filters={"parent_account": account.name}, fields=["name"], limit_page_length=1
		)
		must_remain_group = wants_group or bool(actual_children)
		if must_remain_group and not cint(account.is_group):
			account = frappe.get_doc("Account", account.name)
			account.is_group = 1
			account.account_type = ""
			account.flags.exclude_account_type_check = True
			account.flags.ignore_root_company_validation = True
			account.save(ignore_permissions=True)
			converted_to_group.append(code)
		elif not must_remain_group and cint(account.is_group):
			# Do not convert a group with retained children into a ledger.
			account = frappe.get_doc("Account", account.name)
			account.is_group = 0
			account.account_type = account.account_type or ""
			account.flags.exclude_account_type_check = True
			account.flags.ignore_root_company_validation = True
			account.save(ignore_permissions=True)

		if account.parent_account != parent.name:
			account = frappe.get_doc("Account", account.name)
			old_parent = account.parent_account
			account.parent_account = parent.name
			account.flags.ignore_root_company_validation = True
			account.save(ignore_permissions=True)
			reparented.append({"code": code, "from": old_parent, "to": parent.name})
		existing_by_code[code] = frappe.get_doc("Account", account.name)

	# Canonicalize retained numbered descendants too, so the visible tree uses
	# code-category-detail consistently even where the source has no row.
	accounts = _load_accounts(company)
	by_name = {a.name: a for a in accounts}
	by_code = {str(a.account_number): a for a in accounts if a.account_number}
	for account in sorted(by_code.values(), key=lambda a: (len(str(a.account_number)), int(a.account_number))):
		if str(account.account_number) in source:
			continue
		parent = by_name.get(account.parent_account)
		if not parent or not parent.account_number or not parent.account_name:
			continue
		leaf = _leaf_name(account.account_name, parent.account_name)
		desired = f"{parent.account_name}{NAME_SEPARATOR}{leaf}" if leaf else parent.account_name
		if desired == account.account_name:
			continue
		from erpnext.accounts.doctype.account.account import update_account_number

		update_account_number(account.name, desired, account.account_number)
		accounts = _load_accounts(company)
		by_name = {a.name: a for a in accounts}
		by_code = {str(a.account_number): a for a in accounts if a.account_number}

	# Record source leaves that remain groups only because retained accounts exist.
	for code, item in source.items():
		if item["is_group"]:
			continue
		account = by_code.get(code)
		if account and cint(account.is_group):
			retained_group_exceptions.append(code)

	frappe.clear_cache(doctype="Account")
	return {
		"company": company,
		"source_account_count": len(source),
		"created": created,
		"renamed": renamed,
		"reparented": reparented,
		"converted_to_group": converted_to_group,
		"retained_group_exceptions": retained_group_exceptions,
		"retained_out_of_source_count": len(plan["company_codes_not_in_source_retained"]),
		"bank_account_name_assumption": plan["bank_account_name_assumption"],
	}


def _load_accounts(company):
	return frappe.get_all(
		"Account",
		filters={"company": company},
		fields=[
			"name", "account_number", "account_name", "parent_account", "is_group",
			"root_type", "account_type", "report_type", "disabled",
		],
	)


def _name_parts(raw_name, code):
	parts = [part.strip() for part in re.split(r"[_＿]", raw_name) if part.strip()]
	if len(parts) == 1 and code == "6201" and re.search(r"[-－—–]", raw_name):
		parts = [part.strip() for part in re.split(r"[-－—–]", raw_name) if part.strip()]
	if code == "100201":
		# The workbook is for Shenzhen Lemon Tree; do not copy that company into
		# Yuewei's chart. The existing Yuewei label is handled during apply.
		parts = ["银行存款", "基本存款账户"]
	return parts or [raw_name]


def _parent_code(code, source_by_code):
	for length in (len(code) - 2, len(code) - 4):
		if length >= 4 and code[:length] in source_by_code:
			return code[:length]
	return None


def _root_parent_choices(accounts):
	choices = {}
	for account in accounts:
		if account.account_number and account.parent_account:
			choices.setdefault(str(account.account_number), account.parent_account)
	return choices


def _fallback_parent(code, existing_by_code, root_parents):
	if code in ROOT_PARENT_HINTS:
		parent = _account_by_name(existing_by_code, ROOT_PARENT_HINTS[code])
		if parent:
			return parent

	# Preserve an existing numbered account's current parent whenever possible.
	if code in existing_by_code and existing_by_code[code].parent_account:
		return _account_by_name(existing_by_code, existing_by_code[code].parent_account)

	# 5301 already has children in the current chart; use the parent of 530101
	# so the existing R&D account semantics are not silently moved to assets.
	if code == "5301" and "530101" in existing_by_code:
		return _account_by_name(existing_by_code, existing_by_code["530101"].parent_account)

	for candidate in _candidate_root_codes(code):
		parent_name = root_parents.get(candidate)
		if parent_name:
			return _account_by_name(existing_by_code, parent_name)
		candidate_account = _account_by_name(existing_by_code, candidate)
		if candidate_account and candidate_account.is_group:
			return candidate_account
	return None


def _candidate_root_codes(code):
	number = int(code)
	if 1000 <= number < 1500:
		return ("1001", "1121", "资产")
	if 1500 <= number < 2000:
		return ("1501", "1601", "资产")
	if 2000 <= number < 2500:
		return ("2001", "2201", "负债")
	if 2500 <= number < 3000:
		return ("2501", "负债")
	if 3000 <= number < 4000:
		# ERPNext has no separate "Common" root type. Keep these uncommon
		# accounts under the existing Asset root until their business nature is
		# explicitly reviewed.
		return ("资产",)
	if 4000 <= number < 5000:
		return ("4001", "所有者权益")
	if 5000 <= number < 6000:
		return ("5001", "530101", "费用")
	if 6000 <= number < 6300:
		return ("6001", "6101", "营业收入", "收入")
	if 6300 <= number < 6400:
		return ("6301", "营业收入", "收入")
	if 6400 <= number < 6800:
		return ("6401", "6601", "营业成本", "营业费用", "费用")
	if 6800 <= number < 6900:
		return ("6801", "所得税费用", "费用")
	if number >= 6900:
		return ("6901", "费用")
	return ()


def _account_by_name(accounts_by_code, name):
	if not name:
		return None
	fields = [
		"name", "account_number", "account_name", "parent_account", "is_group",
		"root_type", "account_type", "report_type", "disabled",
	]
	rows = frappe.get_all(
		"Account", filters={"company": COMPANY, "name": name},
		fields=fields, limit_page_length=1,
	)
	if rows:
		return rows[0]
	rows = frappe.get_all(
		"Account", filters={"company": COMPANY, "account_name": name},
		fields=fields, order_by="is_group desc, lft asc", limit_page_length=20,
	)
	if rows:
		return rows[0]
	return None


def _get_target_parent(item, source, existing_by_code, root_parents):
	parent_code = item["target_parent_code"]
	if parent_code:
		return existing_by_code.get(parent_code)
	return _fallback_parent(item["code"], existing_by_code, root_parents)


def _leaf_name(account_name, parent_name):
	value = str(account_name or "").strip()
	parent = str(parent_name or "").strip()
	if not value:
		return ""
	if parent and value.startswith(parent):
		remainder = value[len(parent):]
		if re.match(r"^\s*[-－—–:：]\s*", remainder):
			return re.sub(r"^\s*[-－—–:：]\s*", "", remainder).strip()
	return value


def _action(account, row, parent):
	if not account:
		return "create"
	if account.account_name != row["account_name"] or (parent and account.parent_account != parent.name):
		return "update"
	if row["is_group"] and not cint(account.is_group):
		return "convert_to_group"
	return "keep"


def _clean(value):
	if value is None:
		return ""
	if isinstance(value, float) and value.is_integer():
		return str(int(value))
	return str(value).strip()
