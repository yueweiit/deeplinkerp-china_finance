"""A ledger-backed fixed-asset net row for the activity/balance presentation."""

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint


def summarize_fixed_asset_book_value(rows, filters):
	"""Replace asset/contra account trees with one presentation row, never GL.

	Use actual company account types and ancestry. Canonical 1601/1602/1603
	anchors are allowed only for the matching China chart profile; arbitrary
	charts must provide the explicit Fixed Asset/Accumulated Depreciation type.
	Sum leaves once, net opening/closing, and retain gross period turnovers.
	"""
	if (
		not rows or filters.get("statement_type") != "Account Activity and Balance"
		or cint(filters.get("show_fixed_asset_details"))
	):
		return
	from china_finance.setup.china_coa_profile import is_profile_company

	accounts = frappe.get_all(
		"Account", filters={"company": filters.company},
		fields=["name", "parent_account", "root_type", "account_type", "account_number", "is_group"],
	)
	by_name = {account.name: account for account in accounts}
	profile = is_profile_company(filters.company)
	anchors = {
		account.name for account in accounts
		if account.root_type == "Asset" and (
			account.account_type in ("Fixed Asset", "Accumulated Depreciation")
			or (profile and not account.account_type and account.account_number in ("1601", "1602", "1603"))
		)
	}
	if not anchors:
		return
	membership = {}

	def belongs(name, visiting=None):
		if name in membership:
			return membership[name]
		account = by_name.get(name)
		visiting = set(visiting or ())
		if not account or account.root_type != "Asset" or name in visiting:
			return False
		visiting.add(name)
		membership[name] = name in anchors or belongs(account.parent_account, visiting)
		return membership[name]

	members = {name for name in by_name if belongs(name)}
	children = {}
	for account in accounts:
		children.setdefault(account.parent_account, []).append(account.name)
	homogeneous = {}

	def all_fixed(name, visiting=None):
		if name in members:
			return True
		if name in homogeneous:
			return homogeneous[name]
		account = by_name.get(name)
		visiting = set(visiting or ())
		if not account or account.root_type != "Asset" or name in visiting:
			return False
		visiting.add(name)
		homogeneous[name] = bool(account.is_group and children.get(name)) and all(
			all_fixed(child, visiting) for child in children.get(name, [])
		)
		if homogeneous[name]:
			members.add(name)
		return homogeneous[name]

	for name in by_name:
		all_fixed(name)
	source_rows = [
		row for row in rows
		if row.get("account") in members and not row.get("is_group_account")
		and not row.get("warn_if_negative")
	]
	if not source_rows:
		return
	currencies = {row.get("currency") for row in source_rows}
	if len(currencies) != 1:
		frappe.throw(_("固定资产汇总行的币种不一致，请核对报表数据"))
	first = next(row for row in rows if row.get("account") in members)
	parent = first.get("parent_account")
	while parent in members:
		parent = by_name[parent].parent_account
	summary = {
		"account": _("固定资产账面价值"), "account_name": _("固定资产账面价值"),
		"acc_name": _("固定资产账面价值"), "acc_number": "",
		"fixed_asset_summary": 1, "row_code": "FIXED_ASSET_BOOK_VALUE",
		"is_group_account": 0, "bold": 1, "indent": first.get("indent", 0),
		"parent_account": parent, "currency": source_rows[0].get("currency"),
		"from_date": first.get("from_date"), "to_date": first.get("to_date"),
	}

	def total(field):
		return sum((Decimal(str(row.get(field) or 0)) for row in source_rows), Decimal(0))

	for prefix in ("opening", "closing"):
		net = total(prefix + "_debit") - total(prefix + "_credit")
		summary[prefix + "_debit"] = float(max(net, 0))
		summary[prefix + "_credit"] = float(max(-net, 0))
	summary["debit"], summary["credit"] = float(total("debit")), float(total("credit"))
	summary["has_value"] = any(summary[field] for field in (
		"opening_debit", "opening_credit", "debit", "credit", "closing_debit", "closing_credit",
	))
	output = []
	inserted = False
	for row in rows:
		if row.get("account") in members and not row.get("warn_if_negative"):
			if not inserted:
				output.append(summary)
				inserted = True
			continue
		output.append(row)
	rows[:] = output
