"""Explicit, transactional cleanup of cancelled test vouchers from a saved preview.

This is a bench-only maintenance operation, never an HTTP or migration hook.
Live business vouchers, bank allocations and non-zero ledgers block deletion.
"""

from frappe import _


# Chinese user-facing messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

import json
import os
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

import frappe
from frappe.utils import cint, get_bench_path, getdate

from china_finance.services.finance_cleanup import (
	AUDIT_DOCTYPES,
	SOURCE_DATE_FIELDS,
	_collect,
	_incoming_links,
	_outgoing_business_links,
	_rows,
)

CANCELLED_SOURCE_TYPES = ("Journal Entry", "Payment Entry")
RELATED_TYPES = (
	"GL Entry",
	"Payment Ledger Entry",
	"China Accounting Voucher",
	"China Cash Flow Assignment",
	"China Voucher Sync Issue",
)
AUDIT_REFERENCE_FIELDS = {
	"Comment": ("reference_doctype", "reference_name"),
	"Communication": ("reference_doctype", "reference_name"),
	"ToDo": ("reference_type", "reference_name"),
	"Version": ("ref_doctype", "docname"),
	"File": ("attached_to_doctype", "attached_to_name"),
}


def cleanup_cancelled_finance(
	company,
	preview_file="finance-cleanup-preview.json",
	apply=0,
	include_orphan_snapshots=0,
	include_cancelled_ledger_residue=0,
):
	"""Preview by default; apply only the saved, cancelled-source scope.

	A missing source's China snapshot can be included explicitly, but only if
	its source is a known finance type and has no remaining ledger whatsoever.
	Mixed delinked/live cancellation pairs need a separate explicit option,
	and are eligible only for cancelled, self-referenced JEs without parties/GL.
	The caller owns commit; failures roll back every database mutation here.
	"""
	frappe.only_for("System Manager")
	# bench execute changes cwd to sites, whereas shell output redirection
	# creates the inventory in the operator's bench root directory.
	preview_path = Path(preview_file)
	if not preview_path.is_absolute():
		preview_path = Path(get_bench_path()) / preview_path
	preview = json.loads(preview_path.read_text(encoding="utf-8"))
	if (
		preview.get("mode") != "preview_only"
		or preview.get("site") != frappe.local.site
		or preview.get("company") != company
	):
		frappe.throw(_("预览文件的模式、站点或公司不匹配，未执行清理"))
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("公司不存在"))
	apply = bool(cint(apply))
	if apply:
		_assert_maintenance_window()
	savepoint = "cancelled_finance_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(savepoint)
	try:
		plan = _build_plan(
			preview,
			bool(cint(include_orphan_snapshots)),
			lock=apply,
			include_cancelled_ledger_residue=bool(cint(include_cancelled_ledger_residue)),
		)
		result = {
			"site": frappe.local.site,
			"company": company,
			"status": "blocked" if plan["blockers"] else "ready",
			"delete_counts": {dt: len(docs) for dt, docs in plan["documents"].items() if docs},
			"delete_names": {dt: sorted(docs) for dt, docs in plan["documents"].items() if docs},
			"preserved_sources": plan["preserved_sources"],
			"preserved_other_snapshots": plan["preserved_other_snapshots"],
			"cancelled_ledger_residue": plan["cancelled_ledger_residue"],
			"blockers": plan["blockers"],
		}
		if not apply or plan["blockers"]:
			return result
		if not any(plan["documents"].values()):
			return {**result, "status": "nothing_to_remove"}

		# Export before the first mutation. This survives rollback for diagnosis;
		# it supplements, but does not replace, a full site backup with files.
		result["record_backup"] = _export_records(plan, result)
		trash = {}
		for dt, docs in plan["documents"].items():
			for name, doc in docs.items():
				archive = frappe.get_doc(
					{
						"doctype": "Deleted Document",
						"deleted_doctype": dt,
						"deleted_name": name,
						"data": doc.as_json(),
						"owner": frappe.session.user,
					}
				)
				archive.db_insert()
				trash[(dt, name)] = archive.name
		for dt, docs in plan["documents"].items():
			for name, doc in docs.items():
				_rehome_audit_records(dt, name, trash[(dt, name)])
				_purge_document(doc)
		for dt, docs in plan["documents"].items():
			if docs and frappe.db.exists(dt, {"name": ["in", sorted(docs)]}):
				frappe.throw(_("{0} 清理后仍有残留，整批回滚").format(dt))
		return {**result, "status": "removed", "deleted_document_records": list(trash.values())}
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise


def _assert_maintenance_window():
	from frappe.utils.background_jobs import get_jobs

	if not cint(frappe.conf.maintenance_mode):
		frappe.throw(_("请先进入站点维护模式再执行清理；预览不需要维护模式"))
	if get_jobs(site=frappe.local.site).get(frappe.local.site):
		frappe.throw(_("站点仍有排队或运行中的后台任务，请待任务完成后重试"))


def _get_doc(doctype, name, lock=False):
	if lock:
		# Database locks remain held until bench execute commits or rolls back.
		if not frappe.db.get_value(doctype, name, "name", for_update=True):
			return None
	elif not frappe.db.exists(doctype, name):
		return None
	return frappe.get_doc(doctype, name)


def _build_plan(preview, include_orphans, lock=False, include_cancelled_ledger_residue=False):
	company = preview["company"]
	documents = defaultdict(dict)
	blockers, preserved = [], []
	for dt, rows in preview.get("sources", {}).items():
		for row in rows:
			if dt not in CANCELLED_SOURCE_TYPES or row["docstatus"] != 2:
				preserved.append({"doctype": dt, "name": row["name"], "docstatus": row["docstatus"]})
				continue
			doc = _get_doc(dt, row["name"], lock)
			if not doc:
				for child_dt in RELATED_TYPES:
					type_field, name_field = (
						("voucher_type", "voucher_no")
						if child_dt in ("GL Entry", "Payment Ledger Entry")
						else ("source_doctype", "source_name")
					)
					if frappe.db.exists("DocType", child_dt) and frappe.db.exists(
						child_dt, {type_field: dt, name_field: row["name"]}
					):
						blockers.append(
							{
								"reason": "预览中的来源凭证已删除但仍有关联记录，请重新预览",
								"doctype": dt,
								"name": row["name"],
								"related_doctype": child_dt,
							}
						)
				continue
			if doc.company != company or doc.docstatus != 2 or str(doc.modified) != str(row.get("modified")):
				blockers.append({"reason": "来源凭证已变化，请重新预览", "doctype": dt, "name": doc.name})
				continue
			documents[dt][doc.name] = doc

	related = defaultdict(dict)
	for dt in CANCELLED_SOURCE_TYPES:
		names = set(documents[dt])
		for child_dt in RELATED_TYPES:
			type_field, name_field = (
				("voucher_type", "voucher_no")
				if child_dt in ("GL Entry", "Payment Ledger Entry")
				else ("source_doctype", "source_name")
			)
			_collect(related, child_dt, {type_field: dt, name_field: ["in", sorted(names)]}, names)

	preserved_snapshots = []
	orphan_sources = defaultdict(set)
	for row in preview.get("other_accounting_vouchers_to_preserve", []):
		if (
			not include_orphans
			or row.get("source_exists") is not False
			or row.get("source_doctype") not in SOURCE_DATE_FIELDS
		):
			preserved_snapshots.append(row["name"])
			continue
		doc = _get_doc("China Accounting Voucher", row["name"], lock)
		if not doc:
			continue
		if (
			any(
				str(doc.get(field)) != str(row.get(field))
				for field in ("source_doctype", "source_name", "docstatus", "posting_date", "source_event")
			)
			or doc.company != company
		):
			blockers.append({"reason": "无来源会计凭证已变化", "name": doc.name})
			continue
		if not doc.source_name or frappe.db.exists(doc.source_doctype, doc.source_name):
			blockers.append({"reason": "会计凭证来源已存在或来源编号为空", "name": doc.name})
			continue
		orphan_sources[doc.source_doctype].add(doc.source_name)
		for ledger in (
			"GL Entry",
			"Payment Ledger Entry",
			"Advance Payment Ledger Entry",
			"Stock Ledger Entry",
		):
			if frappe.db.exists("DocType", ledger) and frappe.db.exists(
				ledger, {"voucher_type": doc.source_doctype, "voucher_no": doc.source_name}
			):
				blockers.append(
					{"reason": "无来源会计凭证仍有分类账记录，需单独核查", "name": doc.name, "ledger": ledger}
				)
		related["China Accounting Voucher"][doc.name] = doc

	for dt, names in orphan_sources.items():
		for child_dt in ("China Cash Flow Assignment", "China Voucher Sync Issue"):
			_collect(related, child_dt, {"source_doctype": dt, "source_name": ["in", sorted(names)]}, names)

	voucher_names = set(related["China Accounting Voucher"])
	_collect(
		related,
		"China Cash Flow Assignment",
		{"china_accounting_voucher": ["in", sorted(voucher_names)]},
		voucher_names,
	)
	_collect(
		related,
		"China Voucher Sync Issue",
		{"cancellation_voucher": ["in", sorted(voucher_names)]},
		voucher_names,
	)
	# Older sync issues may identify the China snapshot itself as the source,
	# rather than its original Journal Entry or a cancellation_voucher link.
	_collect(
		related,
		"China Voucher Sync Issue",
		{"source_doctype": "China Accounting Voucher", "source_name": ["in", sorted(voucher_names)]},
		voucher_names,
	)
	for dt, rows in related.items():
		for name in rows:
			doc = _get_doc(dt, name, lock)
			if not doc:
				frappe.throw(_("关联记录在扫描期间发生变化，请重新执行"))
			if doc.get("company") != company:
				blockers.append({"reason": "关联记录属于其他公司或未指定公司", "doctype": dt, "name": name})
			documents[dt][name] = doc

	targets = {dt: set(docs) for dt, docs in documents.items() if docs}
	# Missing sources can still be referenced by retained business documents.
	# Include them in dependency checks, without treating them as deletions.
	for dt, names in orphan_sources.items():
		targets.setdefault(dt, set()).update(names)
	for link in _incoming_links(targets):
		if link["reference_name"] in targets.get(link["reference_doctype"], set()):
			continue
		if link["reference_doctype"] in AUDIT_DOCTYPES:
			# Only known audit reference pairs can be rehomed safely.
			pair = AUDIT_REFERENCE_FIELDS.get(link["reference_doctype"])
			if link["reference_table"] == link["reference_doctype"] and (
				(pair and link["reference_field"] == pair[1])
				or (link["reference_doctype"] == "DocShare" and link["reference_field"] == "share_name")
			):
				continue
		blockers.append({"reason": "保留单据仍引用清理目标（含银行核销）", **link})
	blockers.extend(
		_outgoing_business_links({dt: list(docs.values()) for dt, docs in documents.items()}, targets)
	)
	for dt in ("China Accounting Voucher", "China Cash Flow Assignment"):
		for doc in documents[dt].values():
			for child in doc.get_all_children():
				if (
					child.get("gl_entry")
					and child.gl_entry not in targets.get("GL Entry", set())
					and frappe.db.exists("GL Entry", child.gl_entry)
				):
					blockers.append(
						{
							"reason": "会计快照或现金流指定仍引用范围外总账",
							"doctype": dt,
							"name": doc.name,
							"gl_entry": child.gl_entry,
						}
					)
	for doc in documents["GL Entry"].values():
		if not cint(doc.is_cancelled):
			blockers.append(
				{"reason": "仍存在有效总账，未自动删除", "doctype": doc.doctype, "name": doc.name}
			)
	ledger_blockers, ledger_residue = _payment_ledger_review(
		documents["Payment Ledger Entry"].values(),
		set(documents["Journal Entry"]),
		include_cancelled_ledger_residue,
	)
	blockers.extend(ledger_blockers)
	# A source-to-bank link can exist even when no bank allocation points back.
	for doc in documents["Journal Entry"].values():
		if doc.get("custom_china_bank_transaction"):
			blockers.append(
				{
					"reason": "记账凭证仍关联银行交易，需先核查",
					"name": doc.name,
					"bank_transaction": doc.custom_china_bank_transaction,
				}
			)
	dates = [
		getdate(doc.posting_date)
		for docs in documents.values()
		for doc in docs.values()
		if doc.get("posting_date")
	]
	if dates:
		first_date = min(dates)
		frozen_until = frappe.db.get_value("Company", company, "accounts_frozen_till_date")
		if frozen_until and first_date <= getdate(frozen_until):
			blockers.append({"reason": "涉及公司已冻结期间，需先核查", "frozen_until": frozen_until})
		for row in _rows(
			"Period Closing Voucher",
			{"company": company, "docstatus": 1, "period_end_date": [">=", first_date]},
			["name"],
		):
			blockers.append(
				{
					"reason": "存在覆盖清理期间的已提交损益结转，需先核查",
					"doctype": "Period Closing Voucher",
					"name": row.name,
				}
			)
	return {
		"documents": documents,
		"blockers": blockers,
		"preserved_sources": preserved,
		"preserved_other_snapshots": preserved_snapshots,
		"cancelled_ledger_residue": ledger_residue,
	}


def _payment_ledger_review(documents, cancelled_journals, include_residue):
	# Immutable ledgers retain live cancellation pairs. Require zero by date,
	# voucher, counterparty and against-voucher, not merely a zero grand total.
	groups = defaultdict(list)
	fields = (
		"posting_date",
		"voucher_type",
		"voucher_no",
		"account",
		"party_type",
		"party",
		"against_voucher_type",
		"against_voucher_no",
		"account_currency",
		"cost_center",
		"project",
		"finance_book",
	)
	for doc in documents:
		groups[tuple(str(doc.get(field) or "") for field in fields)].append(doc)
	blockers, residue = [], []
	has_gl = {}
	for key, rows in groups.items():
		live = [doc for doc in rows if not cint(doc.delinked)]
		amount = sum((Decimal(str(doc.amount or 0)) for doc in live), Decimal(0))
		currency_amount = sum((Decimal(str(doc.amount_in_account_currency or 0)) for doc in live), Decimal(0))
		if amount == 0 and currency_amount == 0:
			continue
		detail = {
			"group": dict(zip(fields, key, strict=True)),
			"names": [doc.name for doc in live],
			"amount": str(amount),
			"amount_in_account_currency": str(currency_amount),
		}
		if _is_cancelled_ledger_residue(rows, cancelled_journals, has_gl):
			residue.append(
				{
					**detail,
					"delinked_names": [doc.name for doc in rows if cint(doc.delinked)],
					"included": include_residue,
				}
			)
			if include_residue:
				continue
		blockers.append({"reason": "支付分类账仍有有效余额，未自动删除", **detail})
	return blockers, residue


def _is_cancelled_ledger_residue(rows, cancelled_journals, has_gl):
	"""Recognize a strictly bounded stale cancellation pair, not a live zero sum.

	One side is already delinked, so deleting the remaining side changes the
	active payment ledger. This exception is only for confirmed test JEs with
	no party, no outside against-voucher, no GL, and exact per-detail reversals.
	The caller separately checks all incoming and outgoing business references.
	"""
	first = rows[0]
	if (
		first.voucher_type != "Journal Entry"
		or first.voucher_no not in cancelled_journals
		or first.against_voucher_type != first.voucher_type
		or first.against_voucher_no != first.voucher_no
		or first.party_type
		or first.party
	):
		return False
	if first.voucher_no not in has_gl:
		has_gl[first.voucher_no] = bool(
			frappe.db.exists("GL Entry", {"voucher_type": "Journal Entry", "voucher_no": first.voucher_no})
		)
	if has_gl[first.voucher_no]:
		return False
	live, reversed_delinked = Counter(), Counter()
	for doc in rows:
		sign = -1 if cint(doc.delinked) else 1
		pair = (
			str(doc.get("voucher_detail_no") or ""),
			str(doc.get("account_type") or ""),
			str(doc.get("due_date") or ""),
			Decimal(str(doc.amount or 0)) * sign,
			Decimal(str(doc.amount_in_account_currency or 0)) * sign,
		)
		(reversed_delinked if cint(doc.delinked) else live)[pair] += 1
	return bool(live) and live == reversed_delinked


def _export_records(plan, result):
	directory = Path(frappe.get_site_path("private", "backups"))
	directory.mkdir(parents=True, exist_ok=True)
	audit = {}
	for dt, docs in plan["documents"].items():
		for name in docs:
			for audit_dt, (type_field, name_field) in AUDIT_REFERENCE_FIELDS.items():
				for row in _rows(audit_dt, {type_field: dt, name_field: name}, ["*"]):
					audit[(audit_dt, row.name)] = {"doctype": audit_dt, **row}
			for row in _rows("DocShare", {"share_doctype": dt, "share_name": name}, ["*"]):
				audit[("DocShare", row.name)] = {"doctype": "DocShare", **row}
	data = {
		"plan": result,
		"documents": [doc.as_dict() for docs in plan["documents"].values() for doc in docs.values()],
		"audit_records": list(audit.values()),
	}
	fd, path = tempfile.mkstemp(prefix="cancelled-finance-", suffix=".json", dir=directory)
	with os.fdopen(fd, "w") as stream:
		stream.write(frappe.as_json(data))
	return str(Path(path).resolve())


def _rehome_audit_records(doctype, name, archive_name):
	for audit_dt, (type_field, name_field) in AUDIT_REFERENCE_FIELDS.items():
		for row in _rows(audit_dt, {type_field: doctype, name_field: name}, ["name"]):
			values = {type_field: "Deleted Document", name_field: archive_name}
			if audit_dt == "File":
				values["attached_to_field"] = None
			frappe.db.set_value(audit_dt, row.name, values, update_modified=False)
	# Never transfer shares to Deleted Document: only System Managers should
	# access the removed test records. The original shares are in the export.
	frappe.db.delete("DocShare", {"share_doctype": doctype, "share_name": name})


def _purge_document(doc):
	# Deliberately avoid cancel/on_trash hooks: sources were already cancelled,
	# the exact remaining records were checked and archived, and normal deletion
	# would remove attachment bytes and rewind global naming series.
	for field in doc.meta.get_table_fields():
		frappe.db.delete(field.options, {"parenttype": doc.doctype, "parent": doc.name})
	frappe.db.delete(doc.doctype, {"name": doc.name})
	frappe.db.delete("__global_search", {"doctype": doc.doctype, "name": doc.name})
	doc.clear_cache()
