"""Explicit, preview-first repair for a newly imported month's voucher numbers.

This is deliberately not a migrate patch: posted numbers must never change just
because application code is upgraded. No GL, amounts, or document IDs are changed.
"""

import hashlib
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, get_last_day, getdate

from china_finance.services import voucher as voucher_service
from china_finance.services.month_end import lock_company


def reorder_monthly_voucher_numbers(company, accounting_period, voucher_word="记", apply=0,
		expected_plan_hash=None, reason=None):
	"""Bench-only repair of active, unamended, unarchived formal vouchers."""
	frappe.only_for(("System Manager", "China Finance Manager"))
	if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(accounting_period or "")):
		frappe.throw(_("会计期间必须为 YYYY-MM，例如 2026-05"))
	company_doc = frappe.get_doc("Company", company)
	company_doc.check_permission("read")
	if not voucher_service.get_company_settings(company):
		frappe.throw(_("该公司未启用中国财务"))
	apply = cint(apply)
	start = getdate(f"{accounting_period}-01")
	end = get_last_day(start)
	scope = frappe._dict(company=company, fiscal_year=str(start.year),
		accounting_period=accounting_period, voucher_word=voucher_word)
	sequence_key = voucher_service._formal_sequence_key(scope)
	savepoint = "voucher_number_repair"
	if apply:
		if not expected_plan_hash or not str(reason or "").strip():
			frappe.throw(_("执行时必须提供预览中的 expected_plan_hash 和修复原因 reason"))
		frappe.db.savepoint(savepoint)
	try:
		if apply:
			lock_company(company)
			company_doc = frappe.get_doc("Company", company, for_update=True)
			company_doc.check_permission("read")
		# Number allocation takes this same lock. Re-read the plan under the lock
		# so a newly submitted voucher invalidates a stale preview. Formal source
		# saves take this company lock before Frappe locks the source document.
		sequence = frappe.db.get_value("China Voucher Sequence", {"sequence_key": sequence_key},
			["name", "current_value"], as_dict=True, for_update=bool(apply))
		rows, blockers = _read_month(scope, start, end, lock=bool(apply))
		if rows and not sequence:
			blockers.append(_("月份编号序列不存在，需先检查数据完整性"))
		blockers.extend(_protected_periods(company_doc, start, end, rows))
		ordered = sorted(rows, key=lambda row: (
			row.source_doctype == "Period Closing Voucher", str(row.posting_date),
			str(row.get("source_creation") or ""), row.source_name, row.name,
		))
		# Reassign only the existing numbers; do not fill gaps or recycle numbers.
		numbers = sorted(cint(row.sequence_number) for row in rows)
		if numbers and (numbers[0] < 1 or len(set(numbers)) != len(numbers)):
			blockers.append(_("存在无效或重复字号，不能自动重排"))
		changes = []
		for row, number in zip(ordered, numbers, strict=True):
			if row.sequence_number != number:
				changes.append({"name": row.name, "source_doctype": row.source_doctype,
					"source_name": row.source_name, "posting_date": row.posting_date,
					"old_number": row.statutory_number, "new_number": f"{voucher_word}{number}",
					"sequence_number": number, "voucher_key": f"{sequence_key}|{number:08d}"})
		plan_hash = hashlib.sha256(json.dumps(
			{"scope": scope, "sequence": sequence, "rows": ordered}, sort_keys=True,
			ensure_ascii=False, default=str,
		).encode()).hexdigest()
		result = {"site": frappe.local.site, **scope, "voucher_count": len(rows),
			"changed_count": len(changes), "changes": changes, "blockers": blockers,
			"can_apply": not blockers, "plan_hash": plan_hash, "applied": False}
		if not apply:
			return result
		if expected_plan_hash != plan_hash:
			frappe.throw(_("凭证或编号序列已变化，请重新预览"))
		if blockers:
			frappe.throw(_("不能重排：{0}").format("；".join(blockers)))
		batch = f"VNR-{frappe.generate_hash(length=16)}"
		# Temporarily release unique keys before swapping them in the same
		# transaction; rollback restores every original key on any failure.
		for row in changes:
			frappe.db.set_value("China Accounting Voucher", row["name"],
				"voucher_key", f"number-repair|{batch}|{row['name']}", update_modified=False)
		for row in changes:
			frappe.db.set_value("China Accounting Voucher", row["name"], {
				"sequence_number": row["sequence_number"], "statutory_number": row["new_number"],
				"voucher_key": row["voucher_key"],
			})
			voucher_service._sync_source_voucher_number(
				row["source_doctype"], row["source_name"], row["new_number"])
			message = _("按凭证日期重排字号：{0} → {1}；批次 {2}；原因：{3}。分录及金额未变更。").format(
				row["old_number"], row["new_number"], batch, str(reason))
			for doctype, name in (("China Accounting Voucher", row["name"]),
					(row["source_doctype"], row["source_name"])):
				frappe.get_doc(doctype, name).add_comment("Info", frappe.utils.escape_html(message))
				frappe.clear_document_cache(doctype, name)
		if sequence and numbers:
			frappe.db.set_value("China Voucher Sequence", sequence.name, "current_value",
				max(cint(sequence.current_value), numbers[-1]), update_modified=False)
		result.update(applied=True, batch_id=batch)
		return result
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		if apply:
			frappe.db.rollback(save_point=savepoint)
		raise


def _read_month(scope, start, end, lock=False):
	rows = _month_rows(scope, start, end, lock)
	blockers = []
	from china_finance.services.opening_identity import get_opening_sources

	opening_names = get_opening_sources(scope.company)
	opening_rows = [row for row in rows if row.source_doctype == "Journal Entry" and row.source_name in opening_names]
	if any(row.statutory_number for row in opening_rows):
		blockers.append(_("本期含保留历史字号的期初记录，不能自动重排；请在期初余额查看初始化依据"))
	rows = [row for row in rows if row not in opening_rows]
	for row in rows:
		if (row.docstatus != 1 or row.status != "Posted" or row.source_event != "Posting"
				or row.reversal_of or row.reversed_by
				or row.source_doctype not in voucher_service.FORMAL_VOUCHER_SOURCES
				or row.source_key != f"Posting|{row.source_doctype}|{row.source_name}"):
			blockers.append(_("{0} 存在取消、冲销或非标准快照，需单独处理").format(row.name))
			continue
		if (row.accounting_period != scope.accounting_period or row.fiscal_year != scope.fiscal_year
				or not start <= getdate(row.posting_date) <= end
				or row.statutory_number != f"{row.voucher_word}{row.sequence_number}"
				or row.voucher_key != f"{voucher_service._formal_sequence_key(row)}|{cint(row.sequence_number):08d}"):
			blockers.append(_("{0} 的日期或字号信息不一致").format(row.name))
			continue
		if not frappe.db.exists(row.source_doctype, row.source_name):
			blockers.append(_("{0} 的来源单据已不存在").format(row.name))
			continue
		source = frappe.get_doc(row.source_doctype, row.source_name, for_update=lock)
		source.check_permission("read")
		row.source_creation = source.creation
		row.source_modified = source.modified
		row.source_docstatus = source.docstatus
		if (source.docstatus != 1 or source.company != scope.company or source.get("amended_from")
				or voucher_service.get_posting_date(source) != getdate(row.posting_date)):
			blockers.append(_("{0} 的来源已取消、修订或日期不一致").format(row.name))
	return rows, blockers


def _month_rows(scope, start, end, lock=False):
	index_hint = " FORCE INDEX (`company_voucher_period_date`)" if lock else ""
	rows = frappe.db.sql(
		f"""SELECT name, company, posting_date, fiscal_year, accounting_period,
			voucher_word, sequence_number, statutory_number, voucher_key, source_doctype,
			source_name, source_event, source_key, source_hash, status, docstatus,
			reversal_of, reversed_by, modified
		FROM `tabChina Accounting Voucher`{index_hint}
		WHERE company=%s AND voucher_word=%s
			AND (accounting_period=%s OR posting_date BETWEEN %s AND %s)
			AND (source_doctype IN ('Journal Entry', 'Payment Entry', 'Period Closing Voucher')
				OR sequence_number > 0)
		ORDER BY name""" + (" FOR UPDATE" if lock else ""),
		(scope.company, scope.voucher_word, scope.accounting_period, start, end), as_dict=True,
	)
	return rows


def _protected_periods(company_doc, start, end, rows, lock=False):
	blockers = []
	frozen = company_doc.get("accounts_frozen_till_date")
	if frozen and getdate(frozen) >= start:
		blockers.append(_("该月份已处于公司冻结期间"))
	if frappe.db.get_value("China Closing Run", {"company": company_doc.name,
			"from_date": ["<=", end], "to_date": [">=", start], "status": "Closed"}, "name", for_update=lock):
		blockers.append(_("该月份已完成期末智能结转，请先检查结账及档案状态"))
	if frappe.db.get_value("China Closing Run", {"company": company_doc.name,
			"from_date": ["<=", end], "to_date": [">=", start], "archive_package": ["is", "set"]}, "name", for_update=lock):
		blockers.append(_("该月份存在结账档案包，需单独处理"))
	closed = frappe.db.sql("""SELECT p.name FROM `tabAccounting Period` p
		INNER JOIN `tabClosed Document` d ON d.parent=p.name AND d.parenttype='Accounting Period'
		WHERE p.company=%s AND p.disabled=0 AND p.start_date<=%s AND p.end_date>=%s
			AND d.closed=1 AND d.document_type IN ('Journal Entry', 'Payment Entry', 'Period Closing Voucher')
		LIMIT 1""" + (" FOR UPDATE" if lock else ""), (company_doc.name, end, start))
	if closed:
		blockers.append(_("该月份存在禁止记账的会计期间"))
	for doctype in ("China Accounting Voucher", *voucher_service.FORMAL_VOUCHER_SOURCES):
		names = [row.name if doctype == "China Accounting Voucher" else row.source_name
			for row in rows if doctype == "China Accounting Voucher" or row.source_doctype == doctype]
		if names and frappe.db.get_value("China Electronic Document", {"company": company_doc.name,
				"reference_doctype": doctype, "reference_name": ["in", names],
				"status": ["!=", "Superseded"]}, "name", for_update=lock):
			blockers.append(_("该月份存在已归档凭证，需单独处理"))
			break
	return blockers
