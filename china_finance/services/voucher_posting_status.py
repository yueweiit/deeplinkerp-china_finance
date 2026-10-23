"""Posting feedback and read-only visibility for sources missing a formal snapshot."""

# Chinese user-facing messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

import frappe
from frappe import _
from frappe.utils import getdate

from china_finance.services import voucher

SOURCES = ("Journal Entry", "Payment Entry")


@frappe.whitelist()
def get_posting_state(source_doctype, source_name):
	doc = voucher._get_source_document(source_doctype, source_name)
	frappe.get_doc("Company", doc.company).check_permission("read")
	state = voucher.get_source_snapshot_status(source_doctype, source_name)
	settings = voucher.get_company_settings(doc.company)
	eligible = bool(settings and getdate(voucher.get_posting_date(doc)) >= getdate(settings.activation_date))
	has_gl = bool(doc.docstatus == 1 and frappe.db.exists("GL Entry", {
		"voucher_type": doc.doctype, "voucher_no": doc.name, "company": doc.company, "is_cancelled": 0,
	}))
	state.update({"posted": doc.docstatus == 1, "has_gl": has_gl, "eligible": eligible,
		"can_repair": bool(doc.doctype in SOURCES and has_gl and eligible and not state.get("snapshot_ready")
			and set(frappe.get_roles()).intersection(voucher.SNAPSHOT_RETRY_ROLES)
			and frappe.has_permission("China Accounting Voucher", "read"))})
	if not settings:
		state["reason"] = _("公司未启用中国财务，未生成正式凭证展示记录")
	elif not eligible:
		state["reason"] = _("凭证日期早于中国财务启用日期，请联系财务管理员核对历史数据")
	elif doc.docstatus == 1 and not has_gl:
		state["reason"] = _("尚未找到有效总账，请检查后台处理状态")
	if doc.doctype == "Journal Entry" and doc.docstatus == 0:
		from china_finance.services.voucher_preparation import is_ready, mode_enabled

		state.update({"preparation_enabled": mode_enabled(doc.company, doc.posting_date), "ready": is_ready(doc)})
	return state


@frappe.whitelist(methods=["POST"])
def repair_posting_snapshot(source_doctype, source_name):
	"""Idempotently rebuild one snapshot; never submit the source or write its GL."""
	frappe.only_for(voucher.SNAPSHOT_RETRY_ROLES)
	if source_doctype not in SOURCES:
		frappe.throw(_("仅支持补齐记账凭证和收付款凭证的展示记录"))
	from china_finance.services.source_voucher_edit import _get_closing_blockers, _get_locked_source_document

	doc = _get_locked_source_document(source_doctype, source_name)
	frappe.get_doc("Company", doc.company).check_permission("read")
	frappe.has_permission("China Accounting Voucher", "read", throw=True)
	state = get_posting_state(doc.doctype, doc.name)
	if state.get("snapshot_ready"):
		return state
	if not state["can_repair"]:
		frappe.throw(state.get("reason") or _("当前凭证不满足补齐条件"))
	if blockers := _get_closing_blockers(doc):
		frappe.throw("；".join(blockers))
	from china_finance.services.closing_lifecycle import ensure_no_reversal

	ensure_no_reversal(doc.company)
	if frappe.db.get_value("China Closing Run", {"company": doc.company, "docstatus": 0,
		"from_date": ["<=", doc.posting_date], "to_date": [">=", doc.posting_date],
		"preparation_state": ["in", ["Posting", "Checking", "Closing", "Ready"]]}, "name", for_update=True):
		frappe.throw(_("请先暂停月末结账，再补齐正式凭证"))
	if not voucher.create_voucher_from_source(doc):
		frappe.throw(_("未生成正式凭证记录，请检查总账和中国财务设置"))
	result = get_posting_state(doc.doctype, doc.name)
	if not result.get("snapshot_ready"):
		frappe.throw(_("正式凭证记录状态异常，请联系财务管理员核对"))
	doc.add_comment("Comment", _("已补齐正式凭证展示记录，未重复记账。"))
	return result


def get_pending_ledger_rows(filters):
	"""Show actual GL only for readable posted sources with no Posting snapshot.

	Existing inaccessible snapshots are never substituted with their source rows.
	Each source type uses bounded-by-date bulk queries rather than per-document GL reads.
	"""
	if filters.get("voucher_status") not in (None, "", "全部有效凭证", "已记账", "已提交", "Posted") or filters.get("voucher_word"):
		return []
	if filters.get("source_event") not in (None, "", "Posting"):
		return []
	if not frappe.has_permission("China Accounting Voucher", "read") or not frappe.has_permission("GL Entry", "read"):
		return []
	rows = []
	currency = frappe.get_cached_value("Company", filters.company, "default_currency")
	for doctype in SOURCES:
		if filters.get("source_doctype") and filters.source_doctype != doctype:
			continue
		if not frappe.has_permission(doctype, "read"):
			continue
		query = {"company": filters.company, "docstatus": 1, "posting_date": ["between", [filters.from_date, filters.to_date]]}
		name = filters.get("source_name") or filters.get("voucher_number")
		if filters.get("source_name") and filters.get("voucher_number") and filters.source_name != filters.voucher_number:
			continue
		if name:
			query["name"] = name
		sources = frappe.get_list(doctype, filters=query, pluck="name", limit_page_length=0)
		if not sources:
			continue
		existing = set(frappe.get_all("China Accounting Voucher", filters={"source_doctype": doctype,
			"source_name": ["in", sources], "source_event": "Posting"}, pluck="source_name", limit_page_length=0))
		missing = [name for name in sources if name not in existing]
		if not missing:
			continue
		gl_filters = {"company": filters.company, "voucher_type": doctype, "voucher_no": ["in", missing], "is_cancelled": 0,
			"posting_date": ["between", [filters.from_date, filters.to_date]]}
		for field in ("account", "party_type", "party"):
			if filters.get(field):
				gl_filters[field] = filters[field]
		entries = frappe.get_list("GL Entry", filters=gl_filters, fields=["name", "voucher_no", "posting_date", "account",
			"account_currency", "party_type", "party", "cost_center", "project", "finance_book", "remarks", "debit", "credit"],
			order_by="posting_date, voucher_no, creation, name", limit_page_length=0)
		indexes = {}
		for entry in entries:
			period = str(entry.posting_date)[:7]
			if filters.get("accounting_period") and filters.accounting_period != period:
				continue
			if filters.get("search_text") and str(filters.search_text).lower() not in f"{entry.voucher_no} {entry.remarks or ''} {entry.account}".lower():
				continue
			indexes[entry.voucher_no] = indexes.get(entry.voucher_no, 0) + 1
			rows.append(frappe._dict(**entry, voucher_snapshot=f"pending:{doctype}:{entry.voucher_no}",
				gl_entry=entry.name, source_doctype=doctype, source_name=entry.voucher_no, source_event="Posting",
				statutory_number=_("待补齐 · {0}").format(entry.voucher_no), accounting_period=period,
				voucher_status=1, snapshot_pending=True, currency=currency, entry_idx=indexes[entry.voucher_no],
				base_total_amount=entry.debit + entry.credit, editable_account=False, editable_summary=False, editable_amount=False,
				inline_edit_reason=_("请先补齐正式凭证记录，再进行表内更正")))
	return rows
