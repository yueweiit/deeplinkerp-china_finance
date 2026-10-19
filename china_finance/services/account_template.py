"""Bundled Yuewei chart-of-accounts template integration."""

import json
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import frappe
from frappe import _
from frappe.utils import cint

SOURCE_COMPANY = "悦为智能技术(东莞)有限公司"
COMPANY_TEMPLATE = f"{SOURCE_COMPANY}（公司科目模板）"
TEMPLATE_COUNTRY = "China"
TEMPLATE_DATA_PATH = Path(__file__).resolve().parent.parent / "setup" / "data" / "yuewei_company_chart.json"

CHART_METADATA_FIELDS = {
	"account_name",
	"account_number",
	"account_type",
	"account_category",
	"root_type",
	"is_group",
	"tax_rate",
	"account_currency",
}


def source_company_available():
	"""Return whether the source company is available for refreshing the snapshot."""
	return bool(
		frappe.db.exists("Company", SOURCE_COMPANY)
		and frappe.db.exists("Account", {"company": SOURCE_COMPANY})
	)


def count_chart_accounts(children):
	"""Count account nodes in a chart tree."""
	count = 0
	for account_name, child in children.items():
		if account_name in CHART_METADATA_FIELDS or not isinstance(child, dict):
			continue
		count += 1 + count_chart_accounts(child)
	return count


@lru_cache(maxsize=1)
def get_company_template_data():
	"""Load the deployable chart snapshot bundled with China Finance."""
	with TEMPLATE_DATA_PATH.open(encoding="utf-8") as template_file:
		data = json.load(template_file)

	if data.get("name") != COMPANY_TEMPLATE or not isinstance(data.get("tree"), dict):
		raise ValueError(f"Invalid company chart template: {TEMPLATE_DATA_PATH}")
	return data


def get_company_template_chart():
	"""Return the bundled chart tree used to create accounts for a new company."""
	return get_company_template_data()["tree"]


def company_template_available():
	"""Return whether a valid bundled chart snapshot is installed."""
	try:
		return bool(get_company_template_chart())
	except (OSError, ValueError, json.JSONDecodeError):
		return False


def build_company_template_chart(company):
	"""Export a coherent chart; ERPNext imports descendants with their root's type."""
	accounts = frappe.get_all(
		"Account",
		filters={"company": company, "disabled": 0},
		fields=[
			"name",
			"account_name",
			"parent_account",
			"account_type",
			"account_category",
			"is_group",
			"root_type",
			"report_type",
			"tax_rate",
			"account_number",
			"account_currency",
		],
		order_by="lft, rgt",
	)
	by_name = {account.name: account for account in accounts}
	children_by_parent = defaultdict(list)
	for account in accounts:
		parent = by_name.get(account.parent_account)
		expected_report = (
			"Balance Sheet" if account.root_type in {"Asset", "Liability", "Equity"} else "Profit and Loss"
		)
		if (parent and parent.root_type != account.root_type) or account.report_type != expected_report:
			raise ValueError(_("科目 {0} 的根类型、报表类型或父级不一致，请核对后再导出模板").format(account.name))
		children_by_parent[account.parent_account or ""].append(account)

	def build_children(parent_name=""):
		tree = {}
		siblings = children_by_parent[parent_name]
		name_counts = Counter(account.account_name for account in siblings)
		for index, account in enumerate(siblings, start=1):
			key = account.account_name
			if name_counts[account.account_name] > 1:
				key = f"{account.account_number or index} - {account.account_name}"
			while key in tree:
				key = f"{key} ({index})"

			child = {"account_name": account.account_name}
			for fieldname in (
				"account_number",
				"account_type",
				"account_category",
				"tax_rate",
				"account_currency",
			):
				if account.get(fieldname):
					child[fieldname] = account.get(fieldname)
			if account.is_group:
				child["is_group"] = 1
			if not parent_name:
				child["root_type"] = account.root_type
			child.update(build_children(account.name))
			tree[key] = child
		return tree

	tree = build_children()
	if count_chart_accounts(tree) != len(accounts):
		raise ValueError(f"Not all accounts for {company} are connected to the account tree")
	return tree


def flatten_company_template_chart():
	"""Return the bundled chart as a parent-first list with stable node keys."""
	rows = []

	def walk(children, parent_key=None, inherited_root_type=None, path=()):
		for dictionary_key, child in children.items():
			if dictionary_key in CHART_METADATA_FIELDS or not isinstance(child, dict):
				continue

			account_name = child.get("account_name") or dictionary_key
			account_number = str(child.get("account_number") or "").strip()
			root_type = child.get("root_type") or inherited_root_type
			node_path = (*path, dictionary_key)
			node_key = "/".join(node_path)
			child_keys = [
				key
				for key, value in child.items()
				if key not in CHART_METADATA_FIELDS and isinstance(value, dict)
			]
			rows.append(
				{
					"node_key": node_key,
					"parent_key": parent_key,
					"account_name": account_name,
					"account_number": account_number,
					"root_type": root_type,
					"report_type": (
						"Balance Sheet"
						if root_type in {"Asset", "Liability", "Equity"}
						else "Profit and Loss"
					),
					"is_group": cint(child.get("is_group") or bool(child_keys)),
					"account_type": child.get("account_type") or "",
					"account_category": child.get("account_category") or "",
					"tax_rate": child.get("tax_rate") or 0,
					"account_currency": child.get("account_currency"),
				}
			)
			walk(child, node_key, root_type, node_path)

	walk(get_company_template_chart())
	return rows


def preview_existing_company_template_sync(company):
	"""Compare an existing company with the bundled template without modifying it."""
	if not frappe.db.exists("Company", company):
		frappe.throw(_("公司不存在：{0}").format(company))

	template_rows = flatten_company_template_chart()
	existing = _load_company_accounts(company)
	matched = _match_template_accounts(template_rows, existing)
	matched_names = {row.name for row in matched.values()}

	renamed = []
	reparented = []
	metadata_updates = []
	for row in template_rows:
		account = matched.get(row["node_key"])
		if not account:
			continue
		if account.account_name != row["account_name"]:
			renamed.append(
				{
					"account_number": row["account_number"],
					"current": account.account_name,
					"target": row["account_name"],
				}
			)
		target_parent = matched.get(row["parent_key"]) if row["parent_key"] else None
		if target_parent and account.parent_account != target_parent.name:
			reparented.append(
				{
					"account_number": row["account_number"],
					"current": account.parent_account,
					"target": target_parent.name,
				}
			)
		if _account_metadata_differs(account, row):
			metadata_updates.append(row["account_number"] or row["account_name"])

	blockers = _get_template_sync_blockers(company)
	return {
		"site": frappe.local.site,
		"company": company,
		"template": COMPANY_TEMPLATE,
		"general_ledger_entry_count": frappe.db.count("GL Entry", {"company": company}),
		"can_apply": not blockers,
		"blockers": blockers,
		"configuration_updates": _plan_configuration_updates(company),
		"template_account_count": len(template_rows),
		"existing_account_count": len(existing),
		"matched_count": len(matched),
		"missing_count": len(template_rows) - len(matched),
		"missing_accounts": [
			{
				"account_number": row["account_number"],
				"account_name": row["account_name"],
			}
			for row in template_rows
			if row["node_key"] not in matched
		],
		"rename_count": len(renamed),
		"renamed_accounts": renamed,
		"reparent_count": len(reparented),
		"reparented_accounts": reparented,
		"metadata_update_count": len(metadata_updates),
		"retained_extra_count": len(existing) - len(matched_names),
		"retained_extra_accounts": [
			{"account_number": row.account_number or "", "account_name": row.account_name}
			for row in existing
			if row.name not in matched_names
		],
	}


def _get_template_sync_blockers(company):
	"""A chart conversion must happen before business or reviewed configuration exists."""
	blockers = []
	# ERPNext propagates account renames to descendants. Keep this operation
	# confined to one independent company instead of changing a group chart.
	if frappe.db.get_value("Company", company, "parent_company") or frappe.db.exists(
		"Company", {"parent_company": company}
	):
		blockers.append({"doctype": "Company", "reason": "公司存在上下级公司关系，需单独规划科目迁移"})
	for doctype in (
		"GL Entry",
		"Stock Ledger Entry",
		"Journal Entry",
		"Payment Entry",
		"Sales Invoice",
		"Purchase Invoice",
		"Sales Order",
		"Purchase Order",
		"Delivery Note",
		"Purchase Receipt",
		"Stock Entry",
		"Asset",
		"Bank Transaction",
		"Period Closing Voucher",
		"China Accounting Voucher",
		"China Period Closing",
	):
		if not frappe.db.exists("DocType", doctype) or not frappe.db.has_column(doctype, "company"):
			continue
		count = frappe.db.count(doctype, {"company": company})
		if count:
			blockers.append(
				{"doctype": doctype, "count": count, "reason": "公司已有业务数据（含草稿和已取消数据）"}
			)
	for doctype, filters in (
		("China Financial Statement Mapping", {"reviewed": 1}),
		("China Financial Statement Mapping", {"mapping_source": "Manual"}),
		("China Cash Equivalent Scope", {"reviewed": 1}),
	):
		count = frappe.db.count(doctype, {"company": company, **filters})
		if count:
			blockers.append(
				{"doctype": doctype, "count": count, "reason": "公司已有人工或已复核配置，需单独核对科目含义"}
			)
	return blockers


def _plan_configuration_updates(company):
	"""List references whose meaning changes when standard numbers are reused."""
	from china_finance.setup.china_coa_profile import get_account_by_number

	updates = []
	old_profit = get_account_by_number(company, "410401", required=False)
	settings = frappe.db.get_value("China Finance Settings", {"company": company}, "name")
	if settings and old_profit:
		current = frappe.db.get_value("China Finance Settings", settings, "retained_earnings_account")
		if current == old_profit.name:
			updates.append(
				{
					"doctype": "China Finance Settings",
					"name": settings,
					"fieldname": "retained_earnings_account",
					"current": current,
					"target_number": "410411",
				}
			)
	old_output = get_account_by_number(company, "22210102", required=False)
	if old_output and old_output.account_name.endswith("销项税额"):
		# Retire the old ordinary output-VAT mapping; initialization creates the
		# correct 22210107 mapping without overlapping any existing target periods.
		for name in frappe.get_all(
			"China Tax Account Mapping",
			filters={
				"company": company,
				"direction": "Output",
				"account": old_output.name,
				"enabled": 1,
			},
			pluck="name",
		):
			updates.append(
				{
					"doctype": "China Tax Account Mapping",
					"name": name,
					"fieldname": "enabled",
					"current": 1,
					"value": 0,
				}
			)
		for parenttype, childtype, fieldname in (
			("Sales Taxes and Charges Template", "Sales Taxes and Charges", "account_head"),
			("Item Tax Template", "Item Tax Template Detail", "tax_type"),
		):
			parents = frappe.get_all(parenttype, filters={"company": company}, pluck="name")
			if not parents:
				continue
			for row in frappe.get_all(
				childtype,
				filters={
					"parenttype": parenttype,
					"parent": ["in", parents],
					fieldname: old_output.name,
				},
				fields=["name", "parent"],
			):
				updates.append(
					{
						"doctype": childtype,
						"name": row.name,
						"parenttype": parenttype,
						"parent": row.parent,
						"fieldname": fieldname,
						"current": old_output.name,
						"target_number": "22210107",
					}
				)
	deposit = get_account_by_number(company, "101201", required=False)
	if deposit:
		for row in frappe.get_all(
			"China Cash Equivalent Scope",
			filters={
				"company": company,
				"account": deposit.name,
				"reviewed": 0,
				"policy_basis": "按中国科目模板编号自动建议，须由财务人员复核资金可随时支用性。",
			},
			fields=["name", "restricted", "restriction_reason"],
		):
			for fieldname, value in {
				"restricted": 0,
				"restriction_reason": "外埠存款是否可随时支用需财务人员复核，不能按保证金认定为受限资金。",
			}.items():
				if row.get(fieldname) != value:
					updates.append(
						{
							"doctype": "China Cash Equivalent Scope",
							"name": row.name,
							"fieldname": fieldname,
							"current": row.get(fieldname),
							"value": value,
						}
					)
	return updates


def sync_existing_company_to_template(company, apply=False):
	"""Upgrade one zero-ledger company to the bundled Yuewei account template.

	The dry-run preview is the default. ``apply=True`` renames matching numbered
	accounts, repairs their hierarchy and metadata, creates missing accounts, and
	repairs company/payment defaults and dependent setup references. Existing
	business records or reviewed/manual mappings block conversion. Accounts
	outside the template are retained.
	"""
	preview = preview_existing_company_template_sync(company)
	if not cint(apply):
		return preview
	if not preview["can_apply"]:
		frappe.throw(
			_("公司 {0} 已有业务数据或人工复核配置，禁止直接更新科目模板：{1}").format(
				company, preview["blockers"]
			)
		)

	try:
		result = _apply_existing_company_template_sync(company, preview)
		frappe.db.commit()
		return result
	except Exception:
		frappe.db.rollback()
		raise


def _apply_existing_company_template_sync(company, preview):
	from erpnext.accounts.doctype.account.account import update_account_number
	from frappe.utils.nestedset import rebuild_tree

	template_rows = flatten_company_template_chart()
	existing = _load_company_accounts(company)
	matched = _match_template_accounts(template_rows, existing)
	actual_names = {node_key: account.name for node_key, account in matched.items()}
	created = []
	renamed = []

	# Rename first so every Link field is migrated by Frappe before hierarchy and
	# defaults are repaired. Numbered accounts are matched by number, not by label.
	for row in template_rows:
		account = matched.get(row["node_key"])
		if not account or account.account_name == row["account_name"]:
			continue
		new_name = update_account_number(
			account.name,
			row["account_name"],
			row["account_number"] or None,
		)
		actual_names[row["node_key"]] = new_name or account.name
		renamed.append(
			{
				"account_number": row["account_number"],
				"from": account.name,
				"to": new_name or account.name,
			}
		)

	default_currency = frappe.get_cached_value("Company", company, "default_currency")
	previous_ignore_nsm = getattr(frappe.local.flags, "ignore_update_nsm", False)
	frappe.local.flags.ignore_update_nsm = True
	try:
		# Some accounts are ledgers in the old standard chart but parents in the
		# bundled chart. Convert matched target parents before inserting children.
		for row in template_rows:
			name = actual_names.get(row["node_key"])
			if not name or not row["is_group"]:
				continue
			frappe.db.set_value(
				"Account",
				name,
				{"is_group": 1, "account_type": ""},
				update_modified=False,
			)
			frappe.clear_document_cache("Account", name)

		for row in template_rows:
			if row["node_key"] in actual_names:
				continue
			parent_account = actual_names.get(row["parent_key"]) if row["parent_key"] else None
			doc = frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": row["account_name"],
					"account_number": row["account_number"],
					"company": company,
					"parent_account": parent_account,
					"is_group": row["is_group"],
					"root_type": row["root_type"],
					"report_type": row["report_type"],
					"account_type": "" if row["is_group"] else row["account_type"],
					"account_category": row["account_category"],
					"tax_rate": row["tax_rate"],
					"account_currency": row["account_currency"] or default_currency,
				}
			)
			doc.flags.ignore_root_company_validation = True
			doc.flags.exclude_account_type_check = True
			if not parent_account:
				doc.flags.ignore_mandatory = True
			doc.insert(ignore_permissions=True)
			actual_names[row["node_key"]] = doc.name
			created.append(
				{
					"account_number": row["account_number"],
					"account_name": row["account_name"],
					"name": doc.name,
				}
			)

		# Parent links are assigned from the final template map in one pass. This
		# also avoids relying on possibly stale NestedSet bounds from the old chart.
		for row in template_rows:
			name = actual_names[row["node_key"]]
			parent_account = actual_names.get(row["parent_key"]) if row["parent_key"] else None
			values = {
				"parent_account": parent_account,
				"root_type": row["root_type"],
				"report_type": row["report_type"],
				"disabled": 0,
				"account_category": row["account_category"],
				"tax_rate": row["tax_rate"],
				"account_currency": row["account_currency"] or default_currency,
			}
			if row["is_group"]:
				values.update({"is_group": 1, "account_type": ""})
			frappe.db.set_value("Account", name, values, update_modified=False)

		# Only turn template leaves into ledgers after all target parent links are
		# in place. A retained custom child keeps its parent as a group.
		retained_group_accounts = []
		for row in template_rows:
			if row["is_group"]:
				continue
			name = actual_names[row["node_key"]]
			if frappe.db.exists("Account", {"parent_account": name}):
				retained_group_accounts.append(row["account_number"] or row["account_name"])
				continue
			frappe.db.set_value(
				"Account",
				name,
				{"is_group": 0, "account_type": row["account_type"]},
				update_modified=False,
			)
	finally:
		frappe.local.flags.ignore_update_nsm = previous_ignore_nsm

	rebuild_tree("Account")
	frappe.clear_cache(doctype="Account")
	frappe.clear_document_cache("Company", company)

	# The profile stores the standard identifier intentionally; reports and
	# readiness checks use that identity while accounts come from this snapshot.
	from china_finance.setup.china_coa_profile import CHART_TEMPLATE, apply_company_defaults

	frappe.db.set_value("Company", company, "chart_of_accounts", CHART_TEMPLATE, update_modified=False)
	defaults = apply_company_defaults(company, repair=True)
	payment_defaults_updated = _repair_invalid_mode_of_payment_accounts(company)
	frappe.clear_document_cache("Company", company)

	from china_finance.api import _initialize_company
	from china_finance.setup.china_coa_profile import get_account_by_number

	for update in preview["configuration_updates"]:
		value = (
			get_account_by_number(company, update["target_number"], leaf=True).name
			if update.get("target_number")
			else update["value"]
		)
		frappe.db.set_value(update["doctype"], update["name"], update["fieldname"], value)
		frappe.clear_document_cache(update["doctype"], update["name"])
		if update.get("parenttype"):
			frappe.clear_document_cache(update["parenttype"], update["parent"])
	profile_sync = _initialize_company(company)

	return {
		**preview,
		"applied": True,
		"created_count": len(created),
		"created_accounts": created,
		"renamed_count": len(renamed),
		"renamed_accounts": renamed,
		"retained_group_accounts": retained_group_accounts,
		"company_defaults_updated": defaults["updated"],
		"mode_of_payment_accounts_updated": payment_defaults_updated,
		"configuration_updates_applied": len(preview["configuration_updates"]),
		"profile_sync": profile_sync,
	}


def _load_company_accounts(company):
	return frappe.get_all(
		"Account",
		filters={"company": company},
		fields=[
			"name",
			"account_name",
			"account_number",
			"parent_account",
			"root_type",
			"report_type",
			"is_group",
			"account_type",
			"account_category",
			"tax_rate",
			"account_currency",
			"disabled",
		],
		order_by="lft, rgt",
	)


def _match_template_accounts(template_rows, existing):
	by_number = {}
	duplicate_numbers = {}
	for account in existing:
		number = str(account.account_number or "").strip()
		if not number:
			continue
		if number in by_number:
			duplicate_numbers.setdefault(number, [by_number[number].name]).append(account.name)
		else:
			by_number[number] = account
	if duplicate_numbers:
		frappe.throw(_("公司科目编号重复，不能更新模板：{0}").format(duplicate_numbers))

	matched = {}
	used_names = set()
	for row in template_rows:
		account = None
		if row["account_number"]:
			account = by_number.get(row["account_number"])
		elif not row["parent_key"]:
			account = next(
				(
					item
					for item in existing
					if not item.parent_account
					and item.root_type == row["root_type"]
					and item.name not in used_names
				),
				None,
			)
		else:
			account = next(
				(
					item
					for item in existing
					if not item.account_number
					and item.account_name == row["account_name"]
					and item.root_type == row["root_type"]
					and item.name not in used_names
				),
				None,
			)
		if account:
			matched[row["node_key"]] = account
			used_names.add(account.name)
	return matched


def _account_metadata_differs(account, row):
	expected = {
		"root_type": row["root_type"],
		"report_type": row["report_type"],
		"report_type": row["report_type"],
		"is_group": row["is_group"],
		"account_type": "" if row["is_group"] else row["account_type"],
		"account_category": row["account_category"],
		"tax_rate": row["tax_rate"],
		"disabled": 0,
	}
	return any((account.get(fieldname) or "") != (value or "") for fieldname, value in expected.items())


def _repair_invalid_mode_of_payment_accounts(company):
	company_doc = frappe.get_cached_doc("Company", company)
	updated = 0
	for row in frappe.get_all(
		"Mode of Payment Account",
		filters={"company": company},
		fields=["name", "parent", "default_account"],
	):
		valid = row.default_account and frappe.db.exists(
			"Account", {"name": row.default_account, "company": company, "disabled": 0, "is_group": 0}
		)
		if valid:
			continue
		mode_type = frappe.db.get_value("Mode of Payment", row.parent, "type")
		default_account = (
			company_doc.default_bank_account
			if mode_type == "Bank"
			else company_doc.default_cash_account
			if mode_type == "Cash"
			else None
		)
		if default_account and frappe.db.exists(
			"Account", {"name": default_account, "company": company, "disabled": 0, "is_group": 0}
		):
			frappe.db.set_value(
				"Mode of Payment Account",
				row.name,
				"default_account",
				default_account,
				update_modified=False,
			)
			updated += 1
	return updated


def export_company_template():
	"""Refresh the bundled snapshot from the current source-company account tree."""
	if not source_company_available():
		frappe.throw(f"Source company is unavailable: {SOURCE_COMPANY}")

	tree = build_company_template_chart(SOURCE_COMPANY)
	payload = {
		"name": COMPANY_TEMPLATE,
		"country": TEMPLATE_COUNTRY,
		"source_company": SOURCE_COMPANY,
		"account_count": count_chart_accounts(tree),
		"tree": tree,
	}
	TEMPLATE_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
	TEMPLATE_DATA_PATH.write_text(
		json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
		encoding="utf-8",
	)
	get_company_template_data.cache_clear()
	return {
		"path": str(TEMPLATE_DATA_PATH),
		"account_count": payload["account_count"],
	}


@frappe.whitelist()
def get_charts_for_country(country, with_standard=False):
	"""Add the bundled Yuewei chart to ERPNext's China template options."""
	from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import (
		get_charts_for_country as get_native_charts_for_country,
	)

	charts = get_native_charts_for_country(country, with_standard=with_standard) or []
	if country == TEMPLATE_COUNTRY and company_template_available():
		charts = [*charts, COMPANY_TEMPLATE]

	return list(dict.fromkeys(charts))
