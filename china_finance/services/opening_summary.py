"""Explicit summary counterparties for opening balances with no party detail.

This prepares a draft only. Native posting and all opening checks stay intact.
Company-specific aggregate records are retained on the batch for later allocation.
"""

import frappe
from frappe import _
from frappe.utils import cint

from china_finance.services import opening_balance as opening


def candidates(doc):
	rows = opening._rows(doc)
	names = sorted({row.get("account") for row in rows if row.get("account")})
	if not names:
		return {}
	accounts = frappe.get_list(
		"Account",
		filters={"name": ["in", names], "company": doc.company, "is_group": 0, "disabled": 0,
			"report_type": "Balance Sheet", "account_type": ["in", ["Receivable", "Payable"]]},
		fields=["name", "account_type"], limit_page_length=0,
	)
	types = {account.name: "Customer" if account.account_type == "Receivable" else "Supplier"
		for account in accounts}
	result = {}
	for index, row in enumerate(rows):
		doctype = types.get(row.get("account"))
		if doctype and not row.get("party") and row.get("party_type") in (None, "", doctype):
			result.setdefault(doctype, []).append(index)
	return result


def _party(doc, doctype, customer_group=None, territory=None, supplier_group=None):
	field = "summary_customer" if doctype == "Customer" else "summary_supplier"
	if doc.get(field):
		party = frappe.get_doc(doctype, doc.get(field))
		party.check_permission("read")
		if party.get("disabled"):
			frappe.throw(_("历史汇总档案已停用，请先核对该档案"))
		return party.name
	party = frappe.new_doc(doctype)
	label = (_("历史汇总待分配客户 - {0}") if doctype == "Customer" else _("历史汇总待分配供应商 - {0}")).format(doc.company)
	if len(label) > 140:
		label = label[:128] + "-" + opening.exact.fingerprint({"company": doc.company})[:10]
	if doctype == "Customer":
		party.customer_name = label
		party.customer_type = "Company"
		party.customer_group = customer_group or frappe.db.get_single_value("Selling Settings", "customer_group")
		party.territory = territory or frappe.db.get_single_value("Selling Settings", "territory")
	else:
		party.supplier_name = label
		party.supplier_type = "Company"
		party.supplier_group = supplier_group or frappe.db.get_single_value("Buying Settings", "supplier_group")
	party.insert()
	party.check_permission("read")
	doc.set(field, party.name)
	return party.name


@frappe.whitelist(methods=["POST"])
def prepare_summary_parties(name, expected_version, customer_group=None, territory=None, supplier_group=None):
	"""Create/reuse explicitly identified aggregates and associate missing parties.

	No source names, accounts, amounts, remarks or already-linked parties change.
	Saving, previewing and activating alone never create these master records.
	"""
	frappe.only_for(opening.ACTIVATE_ROLES)
	doc = opening._get_batch(name, write=True, lock=True)
	opening._check_version(doc, expected_version)
	if doc.status != "Draft" or doc.mode not in ("New", "Adjustment"):
		frappe.throw(_("历史往来汇总仅支持新建或调整中的期初草稿，不能改写历史接管来源"))
	targets = candidates(doc)
	if not targets:
		frappe.throw(_("没有可汇总的缺失往来行，请先选择正确的末级应收应付科目"))
	for doctype in targets:
		field = "summary_customer" if doctype == "Customer" else "summary_supplier"
		if not doc.get(field) and not frappe.has_permission(doctype, "create"):
			frappe.throw(_("建立历史汇总档案需要客户或供应商的创建权限"), frappe.PermissionError)
	point = "opening_summary_parties"
	frappe.db.savepoint(point)
	try:
		parties = {doctype: _party(doc, doctype, customer_group, territory, supplier_group) for doctype in sorted(targets)}
		by_row = {index: doctype for doctype, indexes in targets.items() for index in indexes}
		for index, row in enumerate(doc.rows):
			if doctype := by_row.get(index):
				row.party_type = doctype
				row.party = parties[doctype]
		doc.version = cint(doc.version) + 1
		opening._save(doc)
		return opening._envelope(doc)
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point=point)
		raise
