"""Explicit monthly numbering generations; preserve cancelled accounting history."""

import hashlib
import json
import re

import frappe
from frappe import _
from frappe.utils import cint, get_last_day, getdate

from china_finance.services import voucher as vouchers
from china_finance.services.month_end import lock_company
from china_finance.services.voucher_number_repair import _month_rows, _protected_periods

NUMBERING_ROLES = ("System Manager", "China Finance Manager", "Accounts Manager")


def _read_plan(scope, start, end, revision, lock):
	rows = _month_rows(scope, start, end, lock)
	by_name = {row.name: row for row in rows}
	active, history, blockers = [], [], []
	for row in rows:
		frappe.get_doc("China Accounting Voucher", row.name).check_permission("read")
		if row.source_doctype not in vouchers.FORMAL_VOUCHER_SOURCES or row.docstatus != 1:
			blockers.append(_("凭证 {0} 的状态或来源不支持整理编号").format(row.name))
			continue
		if not frappe.db.get_value(row.source_doctype, row.source_name, "name", for_update=lock):
			blockers.append(_("{0} 的来源单据已不存在").format(row.name))
			continue
		source = frappe.get_doc(row.source_doctype, row.source_name, for_update=lock)
		source.check_permission("read")
		row.source_creation = source.creation
		row.source_modified = source.modified
		row.source_docstatus = source.docstatus
		if (source.company != scope.company
			or row.accounting_period != scope.accounting_period or row.fiscal_year != scope.fiscal_year
			or not start <= getdate(row.posting_date) <= end
			or vouchers.get_posting_date(source) != getdate(row.posting_date)
			or row.source_key != f"{row.source_event}|{row.source_doctype}|{row.source_name}"):
			blockers.append(_("{0} 的日期或字号信息不一致").format(row.name))
			continue
		if row.source_event == "Cancellation":
			posting = by_name.get(row.reversal_of)
			if (source.docstatus != 2 or row.status != "Posted" or row.sequence_number
				or not posting or posting.source_event != "Posting" or posting.status != "Reversed"
				or posting.reversed_by != row.name or posting.source_doctype != row.source_doctype
				or posting.source_name != row.source_name):
				blockers.append(_("凭证 {0} 的取消审计尚未完成，请先处理冲销同步问题").format(row.name))
			continue
		if row.source_event != "Posting" or row.sequence_number < 1 or row.statutory_number != f"{scope.voucher_word}{row.sequence_number}":
			blockers.append(_("{0} 的日期或字号信息不一致").format(row.name))
			continue
		if source.docstatus == 2 and row.status == "Reversed":
			if not vouchers._cancellation_audit_complete(source, row.reversed_by):
				blockers.append(_("凭证 {0} 的取消审计尚未完成，请先处理冲销同步问题").format(row.name))
			history.append(row)
			continue
		valid_keys = {
			f"{vouchers._formal_sequence_key(row)}|{cint(row.sequence_number):08d}",
			vouchers._build_amendment_voucher_key(row),
		}
		if revision:
			valid_keys = {vouchers._organized_voucher_key(row, revision)}
		if (source.docstatus != 1 or row.status != "Posted" or row.reversed_by or row.reversal_of
			or row.voucher_key not in valid_keys):
			blockers.append(_("凭证 {0} 的状态或来源不支持整理编号").format(row.name))
			continue
		source.check_permission("write")
		snapshot = frappe.get_doc("China Accounting Voucher", row.name)
		if vouchers.calculate_entries_hash(snapshot.entries) != row.source_hash:
			blockers.append(_("凭证 {0} 的分录哈希不一致，请先检查凭证完整性").format(row.name))
		active.append(row)
	numbers = [row.sequence_number for row in active]
	if len(numbers) != len(set(numbers)):
		blockers.append(_("存在无效或重复字号，不能自动重排"))
	active.sort(key=lambda row: (
		row.source_doctype == "Period Closing Voucher", str(row.posting_date),
		str(row.source_creation), row.source_name, row.name,
	))
	return rows, active, history, blockers


@frappe.whitelist(methods=["POST"])
def organize_monthly_voucher_numbers(company, accounting_period, voucher_word="记", apply=0,
		expected_plan_hash=None, reason=None):
	frappe.only_for(NUMBERING_ROLES)
	if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(accounting_period or "")):
		frappe.throw(_("会计期间必须为 YYYY-MM，例如 2026-05"))
	voucher_word = str(voucher_word or "").strip()
	if not voucher_word or len(voucher_word) > 20 or re.search(r"[\d|\s]", voucher_word):
		frappe.throw(_("请填写凭证字，例如记、收、付或转，不要填写流水号"))
	apply = bool(cint(apply))
	if apply and (not expected_plan_hash or not str(reason or "").strip()):
		frappe.throw(_("执行时必须提供预览中的 expected_plan_hash 和修复原因 reason"))
	if len(str(reason or "")) > 500:
		frappe.throw(_("整理原因不能超过 500 个字符"))
	point = "organize_voucher_numbers"
	if apply:
		frappe.db.savepoint(point)
	try:
		if apply:
			lock_company(company)
		company_doc = frappe.get_doc("Company", company, for_update=apply)
		company_doc.check_permission("read")
		if not vouchers.get_company_settings(company):
			frappe.throw(_("该公司未启用中国财务"))
		start = getdate(f"{accounting_period}-01")
		end = get_last_day(start)
		scope = frappe._dict(company=company, fiscal_year=str(start.year),
			accounting_period=accounting_period, voucher_word=voucher_word)
		sequence = frappe.db.get_value("China Voucher Sequence",
			{"sequence_key": vouchers._formal_sequence_key(scope)},
			["name", "current_value", "numbering_revision"], as_dict=True, for_update=apply)
		revision = cint(sequence.numbering_revision) if sequence else 0
		rows, active, history, blockers = _read_plan(scope, start, end, revision, apply)
		blockers.extend(_protected_periods(company_doc, start, end, rows, lock=apply))
		if rows and not sequence:
			blockers.append(_("月份编号序列不存在，需先检查数据完整性"))
		if frappe.db.get_value("China Closing Run", {
			"company": company, "docstatus": 0, "from_date": ["<=", end], "to_date": [">=", start],
			"preparation_state": ["in", ["Posting", "Checking", "Closing", "Ready"]],
		}, "name", for_update=apply):
			blockers.append(_("请先暂停月末结账，再整理本期凭证编号"))
		if frappe.db.get_value("China Voucher Sync Issue", {
			"company": company, "posting_date": ["between", [start, end]], "status": "Pending",
		}, "name", for_update=apply):
			blockers.append(_("本期存在待处理的冲销同步问题，不能整理编号"))
		assignments = [{"name": row.name, "source_doctype": row.source_doctype,
			"source_name": row.source_name, "posting_date": row.posting_date,
			"old_number": row.statutory_number, "new_number": f"{voucher_word}{index}",
			"sequence_number": index} for index, row in enumerate(active, 1)]
		changes = [row for row in assignments if row["old_number"] != row["new_number"]]
		needs_update = bool(sequence and (changes or cint(sequence.current_value) != len(active)))
		plan_hash = hashlib.sha256(json.dumps({
			"scope": scope, "sequence": sequence, "rows": rows, "blockers": blockers,
		}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
		result = {**scope, "voucher_count": len(active), "history_count": len(history),
			"changed_count": len(changes), "changes": assignments, "next_number": f"{voucher_word}{len(active) + 1}",
			"blockers": blockers, "can_apply": not blockers and needs_update,
			"needs_update": needs_update, "plan_hash": plan_hash, "applied": False}
		if not apply:
			return result
		if expected_plan_hash != plan_hash:
			frappe.throw(_("凭证或编号序列已变化，请重新预览"))
		if blockers:
			frappe.throw(_("不能整理编号：{0}").format("；".join(blockers)))
		if not needs_update:
			return result
		batch = f"VNO-{frappe.generate_hash(length=16)}"
		for row, assignment in zip(active, assignments, strict=True):
			row.sequence_number = assignment["sequence_number"]
			frappe.db.set_value("China Accounting Voucher", row.name, {
				"sequence_number": row.sequence_number, "statutory_number": assignment["new_number"],
				"voucher_key": vouchers._organized_voucher_key(row, revision + 1),
			})
			vouchers._sync_source_voucher_number(row.source_doctype, row.source_name, assignment["new_number"])
			message = _("凭证整理编号：{0} → {1}；批次 {2}；原因：{3}。取消历史、分录及金额未变更。").format(
				assignment["old_number"], assignment["new_number"], batch, str(reason).strip())
			for doctype, name in (("China Accounting Voucher", row.name), (row.source_doctype, row.source_name)):
				frappe.get_doc(doctype, name).add_comment("Info", frappe.utils.escape_html(message))
				frappe.clear_document_cache(doctype, name)
		frappe.db.set_value("China Voucher Sequence", sequence.name,
			{"current_value": len(active), "numbering_revision": revision + 1})
		message = _("凭证整理编号：公司 {0}，期间 {1}，凭证字 {2}；有效 {3} 张，保留取消历史 {4} 张；流水号 {5} → {6}；批次 {7}；原因：{8}。").format(
			company, accounting_period, voucher_word, len(active), len(history), sequence.current_value,
			len(active), batch, str(reason).strip())
		frappe.get_doc("China Voucher Sequence", sequence.name).add_comment("Info", frappe.utils.escape_html(message))
		result.update(applied=True, batch_id=batch)
		return result
	except frappe.QueryDeadlockError:
		# The database has already rolled back the entire transaction. A
		# rollback to the now-missing savepoint would hide the original error.
		raise
	except Exception:
		if apply:
			frappe.db.rollback(save_point=point)
		raise
