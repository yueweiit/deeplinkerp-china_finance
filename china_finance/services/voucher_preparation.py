"""Editable voucher preparation; official ledgers are written only on submission."""

# Chinese user-facing messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

import hashlib
import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime

READY_FIELDS = ("custom_china_ready_hash", "custom_china_ready_by", "custom_china_ready_on")
REVIEW_ROLES = ("System Manager", "Accounts Manager", "China Finance Manager", "China Voucher Reviewer")
HEAD_FIELDS = (
	"posting_date",
	"voucher_type",
	"company",
	"finance_book",
	"multi_currency",
	"is_opening",
	"user_remark",
	"cheque_no",
	"cheque_date",
	"custom_china_bank_transaction",
	"custom_china_cash_flow_plan",
)
LINE_FIELDS = (
	"account",
	"account_currency",
	"exchange_rate",
	"debit",
	"credit",
	"debit_in_account_currency",
	"credit_in_account_currency",
	"party_type",
	"party",
	"cost_center",
	"project",
	"reference_type",
	"reference_name",
	"reference_detail_no",
	"is_advance",
	"user_remark",
)


def check_company(company):
	frappe.get_doc("Company", company).check_permission("read")


def mode_enabled(company, posting_date=None):
	from china_finance.services.voucher import get_company_settings

	settings = get_company_settings(company)
	return bool(
		settings
		and settings.get("enable_voucher_preparation")
		and settings.get("preparation_from_date")
		and (not posting_date or getdate(posting_date) >= getdate(settings.preparation_from_date))
	)


@frappe.whitelist()
def get_mode(company, posting_date=None):
	check_company(company)
	return {"enabled": mode_enabled(company, posting_date)}


def content_hash(doc):
	# Include configured accounting dimensions as well as the normal editable fields.
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions

	dimensions = get_accounting_dimensions() or []

	def values(row, fields):
		result = {}
		for field in fields:
			value = row.get(field)
			df = row.meta.get_field(field)
			if df and df.fieldtype in ("Currency", "Float", "Percent", "Int", "Check"):
				value = flt(value)
			result[field] = value or None
		return result

	payload = {
		"header": values(doc, HEAD_FIELDS + tuple(dimensions)),
		"accounts": [values(row, LINE_FIELDS + tuple(dimensions)) for row in doc.accounts],
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def is_ready(doc):
	return bool(doc.get("custom_china_ready_hash") and doc.custom_china_ready_hash == content_hash(doc))


def ensure_open(doc):
	from china_finance.services.source_voucher_edit import _get_closing_blockers

	blockers = _get_closing_blockers(doc)
	if blockers:
		frappe.throw("；".join(blockers))


def validate_preparation(doc, method=None):
	if doc.doctype != "Journal Entry" or not doc.meta.has_field(READY_FIELDS[0]):
		return
	old = doc.get_doc_before_save()
	# Read-only fields must not be forgeable through import, REST or a changed form payload.
	for field in READY_FIELDS:
		doc.set(field, old.get(field) if old else None)
	if doc.docstatus == 0:
		if mode_enabled(doc.company, doc.posting_date):
			ensure_open(doc)
		if old and old.get("custom_china_ready_hash") and not is_ready(doc):
			for field in READY_FIELDS:
				doc.set(field, None)


def guard_submission(doc, method=None):
	if doc.doctype == "Journal Entry" and mode_enabled(doc.company, doc.posting_date):
		if not is_ready(doc):
			frappe.throw(_("凭证尚未核对，或核对后内容已改变。请在查凭证中重新核对后记账。"))


def draft_documents(company, from_date, to_date):
	check_company(company)
	return [
		frappe.get_doc("Journal Entry", row.name)
		for row in frappe.get_list(
			"Journal Entry",
			filters={"company": company, "docstatus": 0, "posting_date": ["between", [from_date, to_date]]},
			fields=["name"],
			order_by="posting_date, creation, name",
			limit_page_length=0,
		)
	]


def validate_draft(doc):
	doc.check_permission("read")
	if doc.docstatus != 0:
		frappe.throw(_("{0} 已经记账或作废，请刷新").format(doc.name))
	ensure_open(doc)
	if not doc.accounts or abs(sum(flt(r.debit) - flt(r.credit) for r in doc.accounts)) > 0.005:
		frappe.throw(_("{0} 分录为空或借贷不平衡").format(doc.name))
	# Run native validation on an in-memory copy. Do not mutate the stored draft on review.
	doc.validate()
	from china_finance.services.bank_receipt_import import validate_linked_voucher

	validate_linked_voucher(doc)


@frappe.whitelist(methods=["POST"])
def review_drafts(names, versions=None, cash_plans=None):
	frappe.only_for(REVIEW_ROLES)
	names = frappe.parse_json(names) if isinstance(names, str) else names
	versions = frappe.parse_json(versions) if isinstance(versions, str) else versions
	cash_plans = frappe.parse_json(cash_plans) if isinstance(cash_plans, str) else cash_plans
	if not isinstance(versions, dict):
		frappe.throw(_("请刷新并核对凭证版本后再确认"))
	if not isinstance(names, list) or not names or len(names) > 300:
		frappe.throw(_("一次请选择 1 至 300 张草稿"))
	results = []
	for name in dict.fromkeys(names):
		point = "review_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(point)
		try:
			doc = frappe.get_doc("Journal Entry", name)
			from china_finance.services.month_end import guard_period_write

			guard_period_write(doc)
			doc = frappe.get_doc("Journal Entry", name, for_update=True)
			doc.check_permission("write")
			if str(doc.modified) != versions.get(doc.name):
				frappe.throw(_("凭证已被修改，请刷新并重新核对"))
			if not mode_enabled(doc.company, doc.posting_date):
				frappe.throw(_("请先在中国财务设置中启用凭证准备模式并设置起始日期"))
			validate_draft(doc)
			cash = cash_plan(doc)
			if cash["error"]:
				frappe.throw(cash["error"])
			if cash["rows"]:
				if not isinstance(cash_plans, dict) or cash_plans.get(doc.name) != cash["rows"]:
					frappe.throw(_("现金流建议已变化，请刷新核对预览"))
				if any(not r["row_code"] for r in cash["rows"]):
					frappe.throw(_("请先在凭证编辑面板选择现金流项目"))
				doc.custom_china_cash_flow_plan = json.dumps(cash["rows"], sort_keys=True, ensure_ascii=False)
				doc.save(ignore_version=False)
			# Review an actual saved version, not any transient defaults set by validation.
			doc.reload()
			doc.db_set(
				{
					READY_FIELDS[0]: content_hash(doc),
					READY_FIELDS[1]: frappe.session.user,
					READY_FIELDS[2]: now_datetime(),
				}
			)
			doc.add_comment("Comment", _("已核对当前凭证内容，待记账；后续修改将要求重新核对。"))
			results.append({"name": name, "success": True})
		except Exception as exc:
			frappe.db.rollback(save_point=point)
			results.append({"name": name, "success": False, "error": str(exc)})
	return results


@frappe.whitelist()
def review_preview(names):
	names = frappe.parse_json(names) if isinstance(names, str) else names
	if not isinstance(names, list) or not 1 <= len(names) <= 300:
		frappe.throw(_("请选择 1 至 300 张凭证"))
	rows = []
	for name in dict.fromkeys(names):
		doc = frappe.get_doc("Journal Entry", name)
		doc.check_permission("read")
		rows.append(
			{
				"name": name,
				"modified": str(doc.modified),
				"cash": cash_plan(doc),
				"posting_date": doc.posting_date,
				"total_debit": doc.total_debit,
				"total_credit": doc.total_credit,
				"accounts": [
					{"account": r.account, "summary": r.user_remark, "debit": r.debit, "credit": r.credit}
					for r in doc.accounts
				],
			}
		)
	return rows


def cash_plan(doc):
	from china_finance.services.cash_equivalent_scope import get_cash_scope_accounts
	from china_finance.services.cash_flow_assignment import (
		INFLOW_ROWS,
		OUTFLOW_ROWS,
		_suggestion_by_account,
		_valid_cash_flow_rows,
		is_direct_assignment_required,
	)

	result = {"rows": [], "options": [], "error": ""}
	if not is_direct_assignment_required(doc.company, doc.posting_date):
		return result
	try:
		cash_accounts = set(get_cash_scope_accounts(doc.company, doc.posting_date))
		counterparts = defaultdict(float)
		cash = defaultdict(float)
		for r in doc.accounts:
			amount = flt(r.debit) - flt(r.credit)
			if r.account in cash_accounts and amount:
				cash[(r.account, "收款" if amount > 0 else "付款")] += abs(amount)
			else:
				counterparts[r.account] += amount
		counterparts = {k: v for k, v in counterparts.items() if abs(v) > 0.005}
		if not cash or not counterparts:
			return result
		codes, template = _valid_cash_flow_rows(doc.company, doc.posting_date)
		result["options"] = [
			{"value": r.row_code, "label": r.label} for r in template.rows if r.row_code in codes
		]
		suggestion = _suggestion_by_account(doc.company, doc.posting_date).get(
			max(counterparts, key=lambda k: abs(counterparts[k]))
		)
		saved = frappe.parse_json(doc.get("custom_china_cash_flow_plan") or "[]")
		if not isinstance(saved, list):
			frappe.throw(_("草稿现金流项目格式错误"))
		for (account, direction), amount in sorted(cash.items()):
			matching = [
				r
				for r in saved
				if r.get("cash_account") == account
				and r.get("direction") == direction
				and abs(flt(r.get("amount")) - amount) < 0.005
			]
			code = (
				matching[0].get("row_code")
				if len(matching) == 1
				else (
					suggestion.get("cash_inflow_row_code" if direction == "收款" else "cash_outflow_row_code")
					if suggestion
					else None
				)
			)
			valid = INFLOW_ROWS if direction == "收款" else OUTFLOW_ROWS
			if code not in codes or code not in valid | {"FX_EFFECT"}:
				code = None
			result["rows"].append(
				{"cash_account": account, "direction": direction, "amount": flt(amount, 2), "row_code": code}
			)
	except frappe.ValidationError as exc:
		result["error"] = str(exc)
	return result


def promote_cash_plan(doc, confirm=True):
	"""Apply the reviewed draft classification to actual GL legs and validate normally."""
	from china_finance.services.cash_flow_assignment import (
		confirm_cash_flow_assignment,
		create_assignment_if_required,
	)

	plan = frappe.parse_json(doc.get("custom_china_cash_flow_plan") or "[]")
	if not plan:
		return
	voucher = frappe.db.get_value(
		"China Accounting Voucher", {"source_key": "Posting|Journal Entry|" + doc.name}, "name"
	)
	name = create_assignment_if_required(voucher)
	if not name:
		return
	assignment = frappe.get_doc("China Cash Flow Assignment", name)
	if assignment.docstatus == 1:
		return
	assignment.check_permission("write")
	groups = defaultdict(float)
	for row in assignment.items:
		groups[(row.cash_account, row.cash_direction)] += flt(row.cash_amount)
	for row in assignment.items:
		matched = [
			r
			for r in plan
			if r["cash_account"] == row.cash_account
			and r["direction"] == row.cash_direction
			and abs(flt(r["amount"]) - groups[(row.cash_account, row.cash_direction)]) < 0.005
		]
		if len(matched) != 1:
			frappe.throw(_("实际现金分录与已核对草稿不同，请核对现金流项目"))
		row.cash_flow_row_code = matched[0]["row_code"]
		row.assigned_amount = row.cash_amount
	assignment.save()
	# Normal confirmation preserves role separation and amount/direction validation.
	if confirm:
		confirm_cash_flow_assignment(name)


def get_draft_ledger_rows(filters):
	if filters.get("voucher_status") in ("已记账", "已冲销", "Posted", "Reversed"):
		return []
	rows = []
	if not filters.get("source_doctype") or filters.source_doctype == "Journal Entry":
		rows.extend(_get_draft_journal_ledger_rows(filters))
	if not filters.get("source_doctype") or filters.source_doctype == "Payment Entry":
		rows.extend(_get_draft_payment_ledger_rows(filters))
	return rows


def _get_draft_journal_ledger_rows(filters):
	rows = []
	for doc in draft_documents(filters.company, filters.from_date, filters.to_date):
		if filters.get("source_name") and filters.source_name != doc.name:
			continue
		if filters.get("voucher_number") and filters.voucher_number != doc.name:
			continue
		if filters.get("accounting_period") and str(doc.posting_date)[:7] != filters.accounting_period:
			continue
		ready = is_ready(doc)
		if filters.get("voucher_status") == "待记账" and not ready:
			continue
		if filters.get("voucher_status") == "未记账" and ready:
			continue
		if filters.get("voucher_word"):
			continue  # Draft identifiers are never official voucher numbers.
		for row in doc.accounts:
			if any(filters.get(f) and filters[f] != row.get(f) for f in ("account", "party_type", "party")):
				continue
			remarks = row.user_remark or doc.user_remark or ""
			if (
				filters.get("search_text")
				and filters.search_text.lower() not in f"{doc.name} {remarks} {row.account}".lower()
			):
				continue
			rows.append(
				frappe._dict(
					voucher_snapshot="draft:" + doc.name,
					draft_name=doc.name,
					posting_date=doc.posting_date,
					accounting_period=str(doc.posting_date)[:7],
					statutory_number="草稿 · " + doc.name,
					source_doctype="Journal Entry",
					source_name=doc.name,
					voucher_status=3 if ready else 0,
					currency=frappe.get_cached_value("Company", doc.company, "default_currency"),
					entry_idx=row.idx,
					source_row_name=row.name,
					account=row.account,
					account_currency=row.account_currency,
					party_type=row.party_type,
					party=row.party,
					remarks=remarks,
					debit=row.debit,
					credit=row.credit,
					base_total_amount=flt(row.debit) + flt(row.credit),
				)
			)
	return rows


def _get_draft_payment_ledger_rows(filters):
	if filters.get("voucher_status") == "待记账" or filters.get("voucher_word"):
		return []
	rows = []
	documents = frappe.get_list(
		"Payment Entry",
		filters={
			"company": filters.company,
			"docstatus": 0,
			"posting_date": ["between", [filters.from_date, filters.to_date]],
		},
		pluck="name",
		order_by="posting_date, creation, name",
		limit_page_length=0,
	)
	for name in documents:
		doc = frappe.get_doc("Payment Entry", name)
		doc.check_permission("read")
		if filters.get("source_name") and filters.source_name != doc.name:
			continue
		if filters.get("voucher_number") and filters.voucher_number != doc.name:
			continue
		if filters.get("accounting_period") and str(doc.posting_date)[:7] != filters.accounting_period:
			continue
		try:
			gl_rows = doc.build_gl_map()
		except Exception:
			# Incomplete Payment Entry drafts cannot yet produce reliable ledger
			# amounts.  Keep them on the source form instead of inventing rows.
			continue
		for index, row in enumerate(gl_rows, start=1):
			row = frappe._dict(row)
			if any(
				filters.get(fieldname) and filters[fieldname] != row.get(fieldname)
				for fieldname in ("account", "party_type", "party")
			):
				continue
			remarks = row.get("remarks") or doc.remarks or ""
			if (
				filters.get("search_text")
				and filters.search_text.lower() not in f"{doc.name} {remarks} {row.account}".lower()
			):
				continue
			rows.append(
				frappe._dict(
					voucher_snapshot=f"draft:Payment Entry:{doc.name}",
					draft_name=doc.name,
					posting_date=doc.posting_date,
					accounting_period=str(doc.posting_date)[:7],
					statutory_number="草稿 · " + doc.name,
					source_doctype="Payment Entry",
					source_name=doc.name,
					voucher_status=0,
					currency=frappe.get_cached_value("Company", doc.company, "default_currency"),
					entry_idx=index,
					voucher_detail_no=row.get("voucher_detail_no"),
					account=row.account,
					account_currency=row.get("account_currency"),
					party_type=row.get("party_type"),
					party=row.get("party"),
					remarks=remarks,
					debit=flt(row.get("debit")),
					credit=flt(row.get("credit")),
					base_total_amount=flt(row.get("debit")) + flt(row.get("credit")),
				)
			)
	return rows


@frappe.whitelist()
def trial_balance(company, from_date, to_date):
	"""A consistent read of actual GL plus eligible drafts, in company currency."""
	check_company(company)
	frappe.has_permission("GL Entry", "read", throw=True)
	if not from_date or not to_date or getdate(from_date) > getdate(to_date):
		frappe.throw(_("请填写有效的试算期间"))
	# Acquire the same company lock used by all source writes; use fresh current reads.
	from china_finance.services.month_end import lock_company

	lock_company(company)
	docs = draft_documents(company, from_date, to_date)
	all_names = frappe.get_all(
		"Journal Entry",
		filters={"company": company, "docstatus": 0, "posting_date": ["between", [from_date, to_date]]},
		pluck="name",
	)
	if len(docs) != len(all_names):
		frappe.throw(_("没有读取本期全部凭证的权限，不能生成完整试算"), frappe.PermissionError)
	accounts = {
		r.name: r
		for r in frappe.get_list(
			"Account", filters={"company": company}, fields=["name", "account_number"], limit_page_length=0
		)
	}
	ledger = frappe.db.sql(
		"""SELECT account, posting_date, debit, credit FROM `tabGL Entry`
		WHERE company=%s AND posting_date<=%s AND is_cancelled=0 FOR UPDATE""",
		(company, to_date),
		as_dict=True,
	)
	visible_count = frappe.get_list(
		"GL Entry",
		filters={"company": company, "posting_date": ["<=", to_date], "is_cancelled": 0},
		fields=[{"COUNT": "name", "as": "total"}],
	)[0].total
	if visible_count != len(ledger):
		frappe.throw(_("没有读取完整总账范围的权限，不能生成完整试算"), frappe.PermissionError)
	if any(r.account not in accounts for r in ledger) or any(
		r.account not in accounts for d in docs for r in d.accounts
	):
		frappe.throw(_("没有读取全部会计科目的权限"), frappe.PermissionError)
	values = defaultdict(
		lambda: {"opening": 0, "posted_debit": 0, "posted_credit": 0, "draft_debit": 0, "draft_credit": 0}
	)
	for row in ledger:
		item = values[row.account]
		if getdate(row.posting_date) < getdate(from_date):
			item["opening"] += flt(row.debit) - flt(row.credit)
		else:
			item["posted_debit"] += flt(row.debit)
			item["posted_credit"] += flt(row.credit)
	excluded = []
	for doc in docs:
		try:
			validate_draft(doc)
		except Exception as exc:
			excluded.append({"name": doc.name, "error": str(exc)})
			continue
		for row in doc.accounts:
			values[row.account]["draft_debit"] += flt(row.debit)
			values[row.account]["draft_credit"] += flt(row.credit)
	rows = []
	for account, amounts in sorted(values.items()):
		rows.append(
			{
				"account": account,
				**{k: flt(v, 2) for k, v in amounts.items()},
				"expected_balance": flt(
					amounts["opening"]
					+ amounts["posted_debit"]
					- amounts["posted_credit"]
					+ amounts["draft_debit"]
					- amounts["draft_credit"],
					2,
				),
			}
		)
	earlier = frappe.db.count(
		"Journal Entry", {"company": company, "docstatus": 0, "posting_date": ["<", from_date]}
	)
	return {
		"rows": rows,
		"excluded": excluded,
		"draft_count": len(docs),
		"earlier_drafts": earlier,
		"currency": frappe.get_cached_value("Company", company, "default_currency"),
		"message": _("试算包含本期有效草稿；期初仅含已记账数据。早期草稿须先处理，正式报表以实际记账数据为准。"),
	}
