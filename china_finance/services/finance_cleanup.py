"""Read-only inventory for a company-scoped cleanup of old financial vouchers.

Run through bench execute. This module intentionally exposes no delete endpoint:
the inventory must first be reviewed against the actual server's business data.
"""

from frappe import _


from collections import Counter, defaultdict
from datetime import date

import frappe
from frappe.utils import cint, getdate

MAX_RECORDS = 10000
SOURCE_DATE_FIELDS = {
	"Journal Entry": "posting_date",
	"Payment Entry": "posting_date",
	"Period Closing Voucher": "period_end_date",
}
AUDIT_DOCTYPES = {"Comment", "Version", "Communication", "ToDo", "File", "DocShare"}


def preview_finance_cleanup(company, from_date=None, to_date=None, cancelled_only=False):
	"""Inventory sources and dependencies without saving, cancelling or deleting.

	Omitting dates inventories all dates of the exact company. Dates select
	source vouchers; dependencies outside that interval remain visible. Cancelled
	sources can still be referenced by a live amendment or cancellation snapshot.
	"""
	frappe.only_for("System Manager")
	company = str(company or "").strip()
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("请指定已存在的公司完整名称，不支持简称或模糊匹配"))
	from_date, to_date = _date(from_date), _date(to_date)
	if from_date and to_date and from_date > to_date:
		frappe.throw(_("起始日期不能晚于截止日期"))
	cancelled_only = bool(cint(cancelled_only))
	sources = {}
	for doctype, date_field in SOURCE_DATE_FIELDS.items():
		filters = _scope_filters(company, date_field, from_date, to_date)
		if cancelled_only:
			filters.append(["docstatus", "=", 2])
		fields = ["name", "company", "docstatus", "modified", date_field, "amended_from"]
		if doctype == "Journal Entry":
			fields += ["total_debit", "total_credit"]
		elif doctype == "Payment Entry":
			fields += ["paid_amount", "received_amount"]
		sources[doctype] = _rows(doctype, filters, fields)

	selected = {dt: {row.name for row in rows} for dt, rows in sources.items()}
	related = defaultdict(dict)
	for source_type, names in selected.items():
		for doctype, type_field, name_field in (
			("GL Entry", "voucher_type", "voucher_no"),
			("Payment Ledger Entry", "voucher_type", "voucher_no"),
			("China Accounting Voucher", "source_doctype", "source_name"),
			("China Cash Flow Assignment", "source_doctype", "source_name"),
			("China Voucher Sync Issue", "source_doctype", "source_name"),
		):
			_collect(related, doctype, {type_field: source_type, name_field: ["in", sorted(names)]}, names)
	pcv_names = selected["Period Closing Voucher"]
	for doctype, field in (
		("Account Closing Balance", "period_closing_voucher"),
		("Process Period Closing Voucher", "parent_pcv"),
		("China Closing Run", "period_closing_voucher"),
	):
		_collect(related, doctype, {field: ["in", sorted(pcv_names)]}, pcv_names)
	closing_names = set(related["China Closing Run"])
	_collect(related, "China Report Snapshot", {"closing_run": ["in", sorted(closing_names)]}, closing_names)

	voucher_names = set(related["China Accounting Voucher"])
	_collect(related, "China Cash Flow Assignment", {"china_accounting_voucher": ["in", sorted(voucher_names)]}, voucher_names)
	_collect(related, "China Voucher Sync Issue", {"cancellation_voucher": ["in", sorted(voucher_names)]}, voucher_names)

	# Include references to sources, their snapshots and closing records. Do not
	# recursively absorb outside business documents into a deletion scope.
	targets = {dt: set(names) for dt, names in selected.items() if names}
	for dt, rows in related.items():
		if rows and dt not in {"GL Entry", "Payment Ledger Entry", "Account Closing Balance"}:
			targets[dt] = set(rows)
	links = _incoming_links(targets)
	external = []
	audit_links = []
	bank_names = set()
	for link in links:
		if link["reference_name"] in selected.get(link["reference_doctype"], set()):
			continue
		if link["reference_name"] in related.get(link["reference_doctype"], {}):
			continue
		if link["reference_doctype"] == "Bank Transaction":
			bank_names.add(link["reference_name"])
		elif link["reference_doctype"] in AUDIT_DOCTYPES:
			audit_links.append(link)
		else:
			external.append(link)

	# Some imported drafts retain only the source-to-bank backlink, without an
	# allocation row or the reverse link on the bank transaction.
	if selected["Journal Entry"] and frappe.get_meta("Journal Entry").has_field("custom_china_bank_transaction"):
		bank_names.update(row.custom_china_bank_transaction for row in _rows(
			"Journal Entry", {"name": ["in", sorted(selected["Journal Entry"])]},
			["custom_china_bank_transaction"],
		) if row.custom_china_bank_transaction)
	bank_transactions = _rows(
		"Bank Transaction", {"name": ["in", sorted(bank_names)]},
		["name", "company", "docstatus", "date", "bank_account", "allocated_amount", "unallocated_amount"],
	) if bank_names else []
	bank_allocations = _rows(
		"Bank Transaction Payments", {"parenttype": "Bank Transaction", "parent": ["in", sorted(bank_names)]},
		["name", "parent", "payment_document", "payment_entry", "allocated_amount"],
	) if bank_names else []
	for row in bank_allocations:
		row["references_selected_source"] = row.payment_entry in selected.get(row.payment_document, set())
	attachments = []
	for dt, names in targets.items():
		attachments.extend(_rows("File", {"attached_to_doctype": dt, "attached_to_name": ["in", sorted(names)]},
			["name", "file_name", "file_url", "is_private", "attached_to_doctype", "attached_to_name"]))

	blockers = []
	for dt, rows in related.items():
		for row in rows.values():
			if row.get("company") and row.company != company:
				blockers.append({"reason": "关联记录属于其他公司", "doctype": dt, "name": row.name, "company": row.company})
	for row in bank_transactions:
		if row.company != company:
			blockers.append({"reason": "关联银行交易属于其他公司", "doctype": "Bank Transaction", "name": row.name})
	for link in external:
		blockers.append({"reason": "范围外单据仍引用待清理记录", **link})
	blockers.extend(_outgoing_business_links(sources, selected))

	source_dates = [row[SOURCE_DATE_FIELDS[dt]] for dt, rows in sources.items() for row in rows if row.get(SOURCE_DATE_FIELDS[dt])]
	first_date = min(map(getdate, source_dates)) if source_dates else None
	frozen_until = frappe.db.get_value("Company", company, "accounts_frozen_till_date")
	if frozen_until and first_date and first_date <= getdate(frozen_until):
		blockers.append({"reason": "涉及公司已冻结期间，需先确定重新开账范围", "frozen_until": frozen_until})
	closing_dependencies = _rows("Period Closing Voucher", [
		["company", "=", company], ["docstatus", "=", 1], ["period_end_date", ">=", first_date],
	], ["name", "period_start_date", "period_end_date", "gle_processing_status"]) if first_date else []
	for row in closing_dependencies:
		if row.name not in pcv_names:
			blockers.append({"reason": "存在需要重新核算的范围外期末结转", "doctype": "Period Closing Voucher", "name": row.name})
		if row.gle_processing_status == "In Progress":
			blockers.append({"reason": "期末结转仍在处理总账", "doctype": "Period Closing Voucher", "name": row.name})

	other_snapshots = []
	for row in _rows("China Accounting Voucher", _scope_filters(company, "posting_date", from_date, to_date),
		["name", "docstatus", "posting_date", "source_doctype", "source_name", "source_event"]):
		if row.name not in voucher_names:
			row["source_exists"] = bool(row.source_doctype and row.source_name and frappe.db.exists("DocType", row.source_doctype) and frappe.db.exists(row.source_doctype, row.source_name))
			other_snapshots.append(row)

	return {
		"mode": "preview_only",
		"site": frappe.local.site,
		"company": company,
		"scope": {"from_date": from_date, "to_date": to_date, "cancelled_only": cancelled_only},
		"source_counts": {dt: {"total": len(rows), "by_docstatus": dict(Counter(row.docstatus for row in rows))} for dt, rows in sources.items()},
		"sources": sources,
		"related_counts": {dt: len(rows) for dt, rows in related.items() if rows},
		"related_records": {dt: list(rows.values()) for dt, rows in related.items() if rows},
		"bank_transactions_to_preserve": bank_transactions,
		"bank_allocations_to_review": bank_allocations,
		"audit_and_attachment_links": audit_links,
		"attachments_to_preserve": attachments,
		"closing_dependencies": closing_dependencies,
		"other_accounting_vouchers_to_preserve": other_snapshots,
		"blockers": blockers,
		"notes": [
			"本命令只读取数据，不会取消、删除、解除核销、重置编号或保存任何单据。",
			"关联记录清单不是自动删除授权；关联到保留业务时必须先确定处理方式。",
			"其他来源（如发票、库存）的中国会计凭证及无来源快照另列，不自动纳入。",
			"银行交易原始记录、附件文件、凭证编号状态和基础配置保留。",
			"后续清理必须在备份后停止录入和相关后台任务；预览结果只反映当前时刻。",
		],
	}


def _date(value):
	if not value:
		return None
	try:
		return date.fromisoformat(str(value)).isoformat()
	except ValueError:
		frappe.throw(_("日期必须使用 YYYY-MM-DD 格式"))


def _scope_filters(company, date_field, from_date, to_date):
	filters = [["company", "=", company]]
	if from_date:
		filters.append([date_field, ">=", from_date])
	if to_date:
		filters.append([date_field, "<=", to_date])
	return filters


def _rows(doctype, filters, fields):
	if not frappe.db.exists("DocType", doctype):
		return []
	rows = frappe.get_all(doctype, filters=filters, fields=fields, order_by="name", limit_page_length=MAX_RECORDS + 1)
	if len(rows) > MAX_RECORDS:
		frappe.throw(_("{0} 超过 {1} 条，请缩小日期范围；没有生成不完整的预览").format(doctype, MAX_RECORDS))
	return rows


def _collect(related, doctype, filters, names):
	if not names or not frappe.db.exists("DocType", doctype):
		return
	meta = frappe.get_meta(doctype)
	fields = ["name", "docstatus", "modified"]
	fields += [field for field in ("company", "posting_date", "source_doctype", "source_name", "source_event", "is_cancelled", "delinked", "status") if meta.has_field(field)]
	for row in _rows(doctype, filters, fields):
		related[doctype][row.name] = row


def _incoming_links(targets):
	"""Inspect static and dynamic links, including cancelled rows and custom fields.

	Do not use cached distinct dynamic-link values or the normal deletion API's
	ignored-link lists: both could hide dependencies in historical imported data.
	"""
	if not targets:
		return []
	fields = frappe.get_all("DocField", filters={"fieldtype": ["in", ["Link", "Dynamic Link"]]}, fields=["parent", "fieldname", "fieldtype", "options"])
	fields += [frappe._dict(parent=row.dt, **{key: row[key] for key in ("fieldname", "fieldtype", "options")}) for row in frappe.get_all(
		"Custom Field", filters={"fieldtype": ["in", ["Link", "Dynamic Link"]]}, fields=["dt", "fieldname", "fieldtype", "options"],
	)]
	links = []
	seen = set()
	for field in fields:
		if field.fieldtype == "Link" and field.options not in targets:
			continue
		meta = frappe.get_meta(field.parent)
		df = meta.get_field(field.fieldname)
		if meta.is_virtual or not df or df.is_virtual:
			continue
		if not meta.issingle and not frappe.db.has_column(field.parent, field.fieldname):
			continue
		for dt, names in targets.items():
			if field.fieldtype == "Link" and field.options != dt:
				continue
			filters = {field.fieldname: ["in", sorted(names)]}
			columns = ["name", "docstatus", field.fieldname]
			if field.fieldtype == "Dynamic Link":
				filters[field.options] = dt
			if meta.istable:
				columns += ["parent", "parenttype"]
			if meta.issingle:
				single = frappe._dict(frappe.db.get_singles_dict(field.parent))
				rows = [single] if single.get(field.fieldname) in names and (field.fieldtype == "Link" or single.get(field.options) == dt) else []
			else:
				rows = _rows(field.parent, filters, columns)
			for row in rows:
				ref_dt = row.parenttype if meta.istable else field.parent
				ref_name = row.parent if meta.istable else (field.parent if meta.issingle else row.name)
				key = (dt, row[field.fieldname], ref_dt, ref_name, field.parent, field.fieldname, row.get("name"))
				if key in seen:
					continue
				seen.add(key)
				links.append({"doctype": dt, "name": row[field.fieldname], "reference_doctype": ref_dt,
					"reference_name": ref_name, "reference_docstatus": row.get("docstatus"),
					"reference_table": field.parent, "reference_field": field.fieldname, "reference_row": row.get("name")})
	return links


def _outgoing_business_links(sources, selected):
	blockers = []
	for dt, rows in sources.items():
		for row in rows:
			doc = frappe.get_doc(dt, row.name)
			for detail in [doc, *doc.get_all_children()]:
				for df in detail.meta.fields:
					value = detail.get(df.fieldname)
					if not value or df.fieldtype not in ("Link", "Dynamic Link"):
						continue
					target_dt = detail.get(df.options) if df.fieldtype == "Dynamic Link" else df.options
					if not target_dt or not frappe.db.exists("DocType", target_dt):
						continue
					# Master data references are preserved. Other submittable
					# business documents can carry paid/outstanding/stock state.
					if target_dt == "Bank Transaction" or not frappe.get_meta(target_dt).is_submittable:
						continue
					if value in selected.get(target_dt, set()):
						continue
					blockers.append({"reason": "来源凭证关联到范围外业务，需检查核销、往来或资产状态",
						"doctype": dt, "name": row.name, "field": df.fieldname,
						"reference_doctype": target_dt, "reference_name": value})
	return blockers
