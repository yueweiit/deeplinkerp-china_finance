"""Controlled editing of formal vouchers from the China voucher ledger."""

# Chinese user-facing messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

import json
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.model.workflow import get_workflow_name
from frappe.utils import cint, flt, getdate

from china_finance.services.month_end import lock_company
from china_finance.services.voucher import _complete_voucher_workflow, get_company, get_posting_date

EDITABLE_SOURCE_DOCTYPES = ("Journal Entry", "Payment Entry")
EDIT_TOLERANCE = 0.005
MAX_INLINE_CHANGES = 100
MAX_INLINE_SUMMARY_LENGTH = 500
INLINE_AMOUNT_FIELDS = {
	"debit": "debit_in_account_currency",
	"credit": "credit_in_account_currency",
}
MAX_INLINE_ENTRY_ROWS = 100
ENTRY_REFERENCE_FIELDS = (
	"reference_type", "reference_name", "reference_detail_no", "advance_voucher_type", "advance_voucher_no",
)


def _get_source_document(source_doctype, source_name, *, for_update=False):
	if source_doctype not in EDITABLE_SOURCE_DOCTYPES:
		frappe.throw(_("查凭证只支持修改记账凭证和收付款凭证"))
	if not source_name or not frappe.db.get_value(source_doctype, source_name, "name", for_update=for_update):
		frappe.throw(_("来源凭证不存在"))

	doc = frappe.get_doc(source_doctype, source_name, for_update=for_update)
	if not doc.has_permission("read"):
		frappe.throw(_("无权查看来源凭证"), frappe.PermissionError)
	return doc


def _permission_blockers(doc):
	blockers = []
	if doc.docstatus == 0:
		if not doc.has_permission("write"):
			blockers.append(_("当前用户没有修改该凭证的权限"))
		return blockers

	if doc.docstatus != 1:
		return [_("来源凭证不是草稿或已提交状态，不能修改")]

	if not doc.has_permission("write"):
		blockers.append(_("当前用户没有修改该凭证的权限"))
	if not doc.has_permission("cancel"):
		blockers.append(_("当前用户没有取消原凭证的权限"))
	if not doc.has_permission("amend"):
		blockers.append(_("当前用户没有修订凭证的权限"))
	if not frappe.has_permission(doc.doctype, "create"):
		blockers.append(_("当前用户没有创建修订凭证的权限"))
	return blockers


def _get_closing_blockers(doc):
	company = get_company(doc)
	posting_date = getdate(get_posting_date(doc))
	blockers = []

	if not company or not posting_date:
		return blockers

	frozen_date = frappe.db.get_value("Company", company, "accounts_frozen_till_date")
	if frozen_date and posting_date <= getdate(frozen_date):
		blockers.append(_("凭证日期 {0} 已被冻结（冻结至 {1}）").format(posting_date, getdate(frozen_date)))

	period_closing_vouchers = frappe.db.get_all(
		"Period Closing Voucher",
		filters=[
			["company", "=", company],
			["docstatus", "=", 1],
			["period_start_date", "<=", posting_date],
			["period_end_date", ">=", posting_date],
		],
		fields=["name", "period_start_date", "period_end_date"],
		order_by="period_end_date desc",
		limit=1,
	)
	for voucher in period_closing_vouchers:
		blockers.append(_("所属期间已提交损益结转凭证 {0}").format(voucher.name))

	if frappe.db.exists("DocType", "China Closing Run"):
		closing_runs = frappe.db.get_all(
			"China Closing Run",
			filters=[
				["company", "=", company],
				["docstatus", "=", 1],
				["status", "=", "Closed"],
				["from_date", "<=", posting_date],
				["to_date", ">=", posting_date],
			],
			fields=["name", "from_date", "to_date"],
			order_by="to_date desc",
			limit=1,
		)
		for run in closing_runs:
			blockers.append(_("所属期间已提交期末智能结转 {0}").format(run.name))

	return blockers


def _get_bank_transaction_names(doc):
	names = []
	if frappe.db.exists("DocType", "Bank Transaction Payments"):
		names.extend(
			frappe.db.get_all(
				"Bank Transaction Payments",
				filters={"payment_document": doc.doctype, "payment_entry": doc.name},
				pluck="parent",
			)
			or []
		)

	if doc.meta.has_field("custom_china_bank_transaction") and doc.get("custom_china_bank_transaction"):
		names.append(doc.custom_china_bank_transaction)

	return list(dict.fromkeys(name for name in names if name))


def _get_bank_blockers(doc):
	blockers = []
	names = _get_bank_transaction_names(doc)
	if not names:
		if doc.get("clearance_date"):
			return [_("凭证已完成银行清账，请先撤销银行对账")]
		return blockers

	for name in names:
		bank_transaction = frappe.db.get_value(
			"Bank Transaction",
			{"name": name, "company": get_company(doc)},
			["name", "docstatus", "status", "allocated_amount"],
			as_dict=True,
		)
		if not bank_transaction or bank_transaction.docstatus == 2:
			continue

		allocated_row = frappe.db.get_value(
			"Bank Transaction Payments",
			{
				"parent": name,
				"payment_document": doc.doctype,
				"payment_entry": doc.name,
			},
			"allocated_amount",
		)
		if flt(allocated_row) > EDIT_TOLERANCE or doc.get("clearance_date"):
			blockers.append(_("已参与银行对账（银行流水 {0}），请先撤销银行对账").format(name))

	return list(dict.fromkeys(blockers))


def _get_quick_unreconcile_info(doc):
	"""Allow inline editing only for one unambiguous, full bank allocation."""
	if doc.doctype not in EDITABLE_SOURCE_DOCTYPES or doc.docstatus != 1:
		return None

	names = _get_bank_transaction_names(doc)
	if len(names) != 1 or not frappe.db.exists("Bank Transaction", names[0]):
		return None

	bank_transaction = frappe.get_doc("Bank Transaction", names[0])
	if (
		bank_transaction.docstatus != 1
		or bank_transaction.company != get_company(doc)
		or bank_transaction.status not in ("Reconciled", "Settled")
		or not bank_transaction.has_permission("write")
		or len(bank_transaction.payment_entries) != 1
	):
		return None

	entry = bank_transaction.payment_entries[0]
	transaction_amount = abs(flt(bank_transaction.deposit) - flt(bank_transaction.withdrawal))
	if (
		entry.payment_document != doc.doctype
		or entry.payment_entry != doc.name
		or entry.reconciliation_type != "Matched"
		or flt(entry.allocated_amount) <= EDIT_TOLERANCE
		or abs(flt(entry.allocated_amount) - transaction_amount) > EDIT_TOLERANCE
		or flt(bank_transaction.unallocated_amount) > EDIT_TOLERANCE
	):
		return None

	if (
		doc.doctype == "Journal Entry"
		and frappe.db.has_column("Bank Transaction", "custom_china_journal_entry")
		and bank_transaction.get("custom_china_journal_entry") not in (None, "", doc.name)
	):
		return None

	bank_account = frappe.db.get_value("Bank Account", bank_transaction.bank_account, "account")
	return {
		"bank_transaction": bank_transaction.name,
		"amount": flt(entry.allocated_amount),
		"currency": bank_transaction.currency,
		"bank_account": bank_account,
	}


def _get_bank_reconciliation_feedback(doc, bank_transaction_names):
	"""Describe whether the amended voucher was linked back to its bank transaction."""
	if not bank_transaction_names:
		return {"status": "not_applicable", "successful": [], "failed": []}

	successful = []
	failed = []
	for name in bank_transaction_names:
		bank_transaction = frappe.db.get_value(
			"Bank Transaction",
			name,
			["name", "docstatus", "status", "allocated_amount", "unallocated_amount"],
			as_dict=True,
		)
		allocated_row = (
			frappe.db.get_value(
				"Bank Transaction Payments",
				{
					"parent": name,
					"payment_document": doc.doctype,
					"payment_entry": doc.name,
				},
				"allocated_amount",
			)
			if frappe.db.exists("DocType", "Bank Transaction Payments")
			else None
		)

		is_reconciled = bool(
			bank_transaction
			and bank_transaction.docstatus == 1
			and bank_transaction.status in ("Reconciled", "Settled")
			and flt(bank_transaction.unallocated_amount) <= EDIT_TOLERANCE
			and flt(allocated_row) > EDIT_TOLERANCE
		)
		detail = {
			"name": name,
			"status": bank_transaction.status if bank_transaction else _("不存在"),
			"allocated_amount": flt(bank_transaction.allocated_amount) if bank_transaction else 0,
			"unallocated_amount": flt(bank_transaction.unallocated_amount) if bank_transaction else 0,
			"voucher_allocated_amount": flt(allocated_row),
		}
		(successful if is_reconciled else failed).append(detail)

	return {
		"status": "success" if not failed else "failed",
		"successful": successful,
		"failed": failed,
	}


def _has_existing_amendment(doc):
	return frappe.db.get_value(doc.doctype, {"amended_from": doc.name}, "name")


def _reset_amendment_workflow_state(doc):
	"""Reset a copied submitted document to the workflow's initial draft state."""
	workflow_name = get_workflow_name(doc.doctype)
	if not workflow_name:
		return

	workflow = frappe.get_cached_doc("Workflow", workflow_name)
	workflow_state_field = workflow.workflow_state_field
	if not workflow_state_field or not doc.meta.has_field(workflow_state_field):
		return

	initial_state = workflow.states[0].state if workflow.states else None
	if initial_state:
		doc.set(workflow_state_field, initial_state)


_AMENDMENT_SYSTEM_FIELDS = {
	"name",
	"doctype",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"workflow_state",
	"amended_from",
	"amendment_date",
	"custom_china_voucher_number",
	"clearance_date",
	"idx",
	"parent",
	"parentfield",
	"parenttype",
}


def _normalise_amendment_value(value):
	"""Return a document value without naming/workflow fields generated by an amendment."""
	if isinstance(value, list):
		return [_normalise_amendment_value(item) for item in value]
	if isinstance(value, dict):
		return {
			key: _normalise_amendment_value(item)
			for key, item in value.items()
			if key not in _AMENDMENT_SYSTEM_FIELDS and not key.startswith("_")
		}
	return value


def _has_meaningful_amendment_changes(doc):
	"""Check the draft against its cancelled source, ignoring amendment metadata."""
	if not doc.get("amended_from"):
		return True

	source = frappe.get_doc(doc.doctype, doc.amended_from)
	return _normalise_amendment_value(doc.as_dict()) != _normalise_amendment_value(source.as_dict())


def _build_status(doc):
	permission_blockers = _permission_blockers(doc)
	closing_blockers = []
	bank_blockers = []
	amendment_blocker = []
	blockers = list(permission_blockers)
	if doc.docstatus in (0, 1):
		closing_blockers = _get_closing_blockers(doc)
		blockers.extend(closing_blockers)
	if doc.docstatus == 1:
		bank_blockers = _get_bank_blockers(doc)
		blockers.extend(bank_blockers)
		if _has_existing_amendment(doc):
			amendment_blocker.append(_("该凭证已经存在修订凭证，不能重复修订"))
			blockers.extend(amendment_blocker)

	if doc.docstatus == 0:
		action = "open_draft"
	elif doc.docstatus == 1:
		action = "amend"
	else:
		action = "unavailable"
		blockers.append(_("已取消的凭证不能从查凭证重复发起修订"))

	quick_unreconcile = None
	if (
		doc.docstatus == 1
		and bank_blockers
		and not permission_blockers
		and not closing_blockers
		and not amendment_blocker
		and not (doc.doctype == "Journal Entry" and len(doc.get("accounts") or []) > 100)
	):
		quick_unreconcile = _get_quick_unreconcile_info(doc)

	return {
		"source_doctype": doc.doctype,
		"source_name": doc.name,
		"company": get_company(doc),
		"posting_date": str(get_posting_date(doc)),
		"docstatus": doc.docstatus,
		"action": action,
		"requires_amendment": doc.docstatus == 1,
		"can_edit": not blockers and doc.docstatus in (0, 1),
		"reason": "；".join(dict.fromkeys(blockers)),
		"bank_transactions": _get_bank_transaction_names(doc),
		"quick_unreconcile": quick_unreconcile,
	}


def _account_values(account):
	return frappe.db.get_value(
		"Account",
		account,
		[
			"name",
			"company",
			"account_number",
			"account_name",
			"account_currency",
			"account_type",
			"is_group",
			"disabled",
		],
		as_dict=True,
	)


def _inline_edit_targets(doc):
	"""Return the only source fields that the ledger may edit.

	The edit key is deliberately resolved again on every request.  Clients never
	get to submit an arbitrary field name or child-row mutation.
	"""
	targets = {}

	def add(key, fieldname, account, *, row=None, parentfield=None, party_type=None, party=None, role=None):
		if not account:
			return
		account_values = _account_values(account) or frappe._dict()
		targets[key] = frappe._dict(
			key=key,
			fieldname=fieldname,
			account=account,
			account_currency=account_values.get("account_currency"),
			account_type=account_values.get("account_type"),
			row_name=row.name if row else None,
			row_idx=row.idx if row else None,
			parentfield=parentfield,
			party_type=party_type,
			party=party,
			role=role,
		)

	if doc.doctype == "Journal Entry":
		for row in doc.get("accounts") or []:
			add(
				f"je:{row.name}",
				"account",
				row.account,
				row=row,
				parentfield="accounts",
				party_type=row.party_type,
				party=row.party,
				role="journal_line",
			)
		return targets

	party_field = {
		"Receive": "paid_from",
		"Pay": "paid_to",
	}.get(doc.payment_type)
	for fieldname in ("paid_from", "paid_to"):
		add(
			f"pe:field:{fieldname}",
			fieldname,
			doc.get(fieldname),
			party_type=doc.party_type if fieldname == party_field else None,
			party=doc.party if fieldname == party_field else None,
			role="payment_party" if fieldname == party_field else "payment_bank",
		)
	for row in doc.get("deductions") or []:
		add(
			f"pe:deductions:{row.name}",
			"account",
			row.account,
			row=row,
			parentfield="deductions",
			role="payment_deduction",
		)
	for row in doc.get("taxes") or []:
		add(
			f"pe:taxes:{row.name}",
			"account_head",
			row.account_head,
			row=row,
			parentfield="taxes",
			role="payment_tax",
		)
	return targets


def _resolve_inline_edit_target(doc, edit_key):
	target = _inline_edit_targets(doc).get(str(edit_key or ""))
	if not target:
		frappe.throw(_("该分录已变化或不支持表内修改，请刷新报表"))
	return target


def _inline_summary_targets(doc):
	"""Return the source summary fields that may be edited from the ledger."""
	targets = {}
	if doc.doctype == "Journal Entry":
		for row in doc.get("accounts") or []:
			key = f"je-summary:{row.name}"
			targets[key] = frappe._dict(
				key=key,
				fieldname="user_remark",
				value=row.user_remark or "",
				row_name=row.name,
				row_idx=row.idx,
				parentfield="accounts",
				scope="line",
			)
		return targets

	if doc.doctype == "Payment Entry":
		key = "pe-summary:remarks"
		targets[key] = frappe._dict(
			key=key,
			fieldname="remarks",
			value=doc.remarks or "",
			row_name=None,
			row_idx=None,
			parentfield=None,
			scope="voucher",
		)
	return targets


def _resolve_inline_summary_target(doc, edit_key):
	target = _inline_summary_targets(doc).get(str(edit_key or ""))
	if not target:
		frappe.throw(_("该摘要已变化或不支持表内修改，请刷新报表"))
	return target


def get_inline_summary_edit_metadata(doc, ledger_row):
	"""Map a rendered summary back to one source row or one voucher field."""
	targets = _inline_summary_targets(doc)
	if doc.doctype == "Payment Entry":
		target = targets.get("pe-summary:remarks")
		return {
			"editable": bool(target),
			"edit_key": target.key if target else None,
			"edit_group": target.key if target else None,
			"scope": target.scope if target else None,
			"hint": _("修改后会更新整张收付款凭证的摘要") if target else None,
		}

	voucher_detail_no = ledger_row.get("voucher_detail_no") or ledger_row.get("source_row_name")
	candidates = []
	if voucher_detail_no:
		candidate = targets.get(f"je-summary:{voucher_detail_no}")
		if candidate:
			candidates.append(candidate)
	else:
		entry_idx = cint(ledger_row.get("entry_idx"))
		candidates.extend(target for target in targets.values() if target.row_idx == entry_idx)

	unique = {candidate.key: candidate for candidate in candidates}
	if len(unique) != 1:
		return {
			"editable": False,
			"reason": _("无法把该摘要唯一对应到来源凭证分录"),
		}
	target = next(iter(unique.values()))
	return {
		"editable": True,
		"edit_key": target.key,
		"edit_group": target.key,
		"scope": target.scope,
	}


def get_inline_row_edit_metadata(doc, ledger_row):
	"""Map a rendered ledger row back to exactly one editable source field."""
	targets = _inline_edit_targets(doc)
	account = ledger_row.get("account")
	voucher_detail_no = ledger_row.get("voucher_detail_no") or ledger_row.get("source_row_name")
	candidates = []

	if doc.doctype == "Journal Entry":
		if voucher_detail_no:
			candidate = targets.get(f"je:{voucher_detail_no}")
			if candidate and candidate.account == account:
				candidates.append(candidate)
		else:
			candidates.extend(target for target in targets.values() if target.account == account)
	else:
		# Main Payment Entry fields can produce several GL rows.  They deliberately
		# share one edit key so the browser can update them as one linked group.
		for fieldname in ("paid_from", "paid_to"):
			candidate = targets.get(f"pe:field:{fieldname}")
			if candidate and candidate.account == account:
				candidates.append(candidate)
		if voucher_detail_no:
			for parentfield in ("deductions", "taxes"):
				candidate = targets.get(f"pe:{parentfield}:{voucher_detail_no}")
				if candidate and candidate.account == account:
					candidates.append(candidate)
		else:
			candidates.extend(
				target
				for target in targets.values()
				if target.parentfield in ("deductions", "taxes") and target.account == account
			)

	unique = {candidate.key: candidate for candidate in candidates}
	if len(unique) != 1:
		return {
			"editable": False,
			"reason": _("无法把该展示行唯一对应到来源单据字段")
			if unique
			else _("该展示行由系统计算，不能直接修改科目"),
		}
	target = next(iter(unique.values()))
	return {
		"editable": True,
		"edit_key": target.key,
		"edit_group": target.key,
		"account_currency": target.account_currency,
	}


def _inline_amount_reason(doc, row):
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	if doc.multi_currency or row.account_currency != company_currency or flt(row.exchange_rate) != 1:
		return _("外币或汇率换算分录请打开原单修改金额")
	return None


def _inline_split_reason(doc, row):
	if doc.doctype != "Journal Entry":
		return _("只有本位币记账凭证支持新增一行")
	if len(doc.accounts) >= MAX_INLINE_ENTRY_ROWS:
		return _("新增后分录不能超过 {0} 行").format(MAX_INLINE_ENTRY_ROWS)
	if any(row.get(field) for field in ENTRY_REFERENCE_FIELDS):
		return _("关联业务单据的分录请打开原单新增")
	return _inline_amount_reason(doc, row)


def _inline_delete_reason(doc, row):
	if doc.doctype != "Journal Entry":
		return _("只有本位币记账凭证支持删除行")
	if reason := _amendment_size_reason(doc):
		return reason
	if len(doc.accounts) <= 2:
		return _("删除后至少保留两条分录")
	if any(row.get(field) for field in ENTRY_REFERENCE_FIELDS):
		return _("关联业务单据的分录请打开原单处理删除")
	return _inline_amount_reason(doc, row)


def _amendment_size_reason(doc):
	if doc.doctype == "Journal Entry" and doc.docstatus == 1 and len(doc.accounts) > MAX_INLINE_ENTRY_ROWS:
		return _("该凭证分录超过100行，请打开原凭证按标准流程取消并修订")
	return None


def get_inline_amount_edit_metadata(doc, ledger_row, mapping):
	if doc.doctype != "Journal Entry":
		return {"editable": False, "reason": _("该来源金额由系统计算，请打开原单修改")}
	if not mapping.get("editable"):
		return {"editable": False, "reason": _("无法把该金额唯一对应到来源凭证分录")}
	row = next(row for row in doc.accounts if f"je:{row.name}" == mapping["edit_key"])
	reason = _inline_amount_reason(doc, row)
	# A merged/split GL display must not overwrite the complete source amount.
	if not reason and any(
		Decimal(str(ledger_row.get(field) or 0)) != Decimal(str(row.get(field) or 0))
		for field in INLINE_AMOUNT_FIELDS
	):
		reason = _("该金额为合并或拆分展示结果，请打开原单修改")
	return {
		"editable": not reason,
		"reason": reason,
		"edit_key": mapping["edit_key"],
		"precision": row.precision("debit_in_account_currency"),
	}


def annotate_inline_edit_rows(rows):
	"""Attach non-persistent editing metadata to report rows in-place."""
	documents = {}
	statuses = {}
	for row in rows:
		doctype = row.get("source_doctype")
		name = row.get("source_name")
		if doctype not in EDITABLE_SOURCE_DOCTYPES or not name:
			row["inline_edit_reason"] = _("该来源类型不支持表内修改")
			continue
		key = (doctype, name)
		if key not in documents:
			try:
				documents[key] = _get_source_document(doctype, name)
				statuses[key] = _build_status(documents[key])
			except Exception as exc:
				documents[key] = None
				statuses[key] = {"can_edit": False, "reason": str(exc), "quick_unreconcile": None}
		doc = documents[key]
		status = statuses[key]
		row["edit_source_doctype"] = doctype
		row["edit_source_name"] = name
		row["edit_voucher_key"] = f"{doctype}:{name}"
		if not doc:
			row["inline_edit_reason"] = status.get("reason")
			continue
		row["edit_source_modified"] = str(doc.modified)
		mapping = get_inline_row_edit_metadata(doc, row)
		summary_mapping = get_inline_summary_edit_metadata(doc, row)
		amount_mapping = get_inline_amount_edit_metadata(doc, row, mapping)
		can_edit = bool(status.get("can_edit") or status.get("quick_unreconcile"))
		row["inline_bank_transaction"] = next(iter(status.get("bank_transactions") or []), None)
		row["editable_account"] = bool(mapping.get("editable") and can_edit)
		row["edit_key"] = mapping.get("edit_key")
		row["edit_group"] = mapping.get("edit_group")
		row["account_currency"] = mapping.get("account_currency") or row.get("account_currency")
		row["inline_edit_reason"] = mapping.get("reason") or (None if can_edit else status.get("reason"))
		row["editable_summary"] = bool(summary_mapping.get("editable") and can_edit)
		row["summary_edit_key"] = summary_mapping.get("edit_key")
		row["summary_edit_group"] = summary_mapping.get("edit_group")
		row["summary_edit_scope"] = summary_mapping.get("scope")
		row["inline_summary_edit_hint"] = summary_mapping.get("hint")
		row["inline_summary_edit_reason"] = (
			summary_mapping.get("reason") or (None if can_edit else status.get("reason"))
		)
		row["editable_amount"] = bool(amount_mapping.get("editable") and can_edit)
		row["amount_edit_key"] = amount_mapping.get("edit_key")
		row["amount_precision"] = amount_mapping.get("precision")
		row["inline_amount_edit_reason"] = (
			amount_mapping.get("reason") or (None if can_edit else status.get("reason"))
		)
		if doc.doctype == "Journal Entry" and amount_mapping.get("editable"):
			source_row = next(item for item in doc.accounts if f"je:{item.name}" == mapping["edit_key"])
			row["editable_split"] = bool(can_edit and not _inline_split_reason(doc, source_row))
			row["editable_delete"] = bool(can_edit and not _inline_delete_reason(doc, source_row))
			row["inline_entry_count"] = len(doc.accounts)
	return rows


def _compatible_account_reason(doc, target, account):
	values = _account_values(account)
	if not values:
		return _("科目不存在")
	if values.company != get_company(doc) or values.is_group or values.disabled:
		return _("只能选择本公司启用的明细科目")
	if target.account_currency and values.account_currency != target.account_currency:
		return _("只能选择币种为 {0} 的科目").format(target.account_currency)

	expected_party_type = {"Customer": "Receivable", "Supplier": "Payable"}.get(target.party_type)
	if expected_party_type and values.account_type != expected_party_type:
		return _("当前往来单位要求使用{0}科目").format(_(expected_party_type))
	if not expected_party_type and values.account_type in ("Receivable", "Payable"):
		return _("当前分录没有往来单位，不能选择应收或应付科目")
	if target.role in ("payment_party", "payment_bank") and target.account_type:
		if values.account_type != target.account_type:
			return _("收付款主科目必须保持科目类型 {0}").format(_(target.account_type))
	return None


def _validate_compatible_account(doc, target, account):
	reason = _compatible_account_reason(doc, target, account)
	if reason:
		frappe.throw(reason)
	return _account_values(account)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_compatible_accounts(doctype, txt, searchfield, start, page_len, filters):
	"""Link-field query for accounts that remain valid when only account changes."""
	filters = frappe.parse_json(filters) if isinstance(filters, str) else frappe._dict(filters or {})
	doc = _get_source_document(filters.get("source_doctype"), filters.get("source_name"))
	target = _resolve_inline_edit_target(doc, filters.get("edit_key"))
	frappe.has_permission("Account", "read", throw=True)

	account_filters = {
		"company": get_company(doc),
		"is_group": 0,
		"disabled": 0,
		"account_currency": target.account_currency,
	}
	expected_party_type = {"Customer": "Receivable", "Supplier": "Payable"}.get(target.party_type)
	if expected_party_type:
		account_filters["account_type"] = expected_party_type
	elif target.role in ("payment_party", "payment_bank") and target.account_type:
		account_filters["account_type"] = target.account_type
	else:
		account_filters["account_type"] = ["not in", ["Receivable", "Payable"]]

	rows = frappe.get_list(
		"Account",
		filters=account_filters,
		or_filters={
			"name": ["like", f"%{txt}%"],
			"account_number": ["like", f"%{txt}%"],
			"account_name": ["like", f"%{txt}%"],
		},
		fields=["name", "account_number", "account_name"],
		order_by="account_number, account_name, name",
		start=cint(start),
		page_length=cint(page_len),
	)
	return [
		[row.name, " - ".join(part for part in (row.account_number, row.account_name) if part)]
		for row in rows
	]


def _parse_inline_changes(changes):
	changes = frappe.parse_json(changes) if isinstance(changes, str) else changes
	if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_INLINE_CHANGES:
		frappe.throw(_("一次只能修改 1 至 {0} 项凭证内容").format(MAX_INLINE_CHANGES))
	parsed = {}
	for change in changes:
		if not isinstance(change, dict) or not change.get("edit_key"):
			frappe.throw(_("凭证修改数据不完整，请刷新后重试"))

		fieldname = change.get("field") or ("account" if change.get("account") else None)
		if fieldname == "account":
			value = change.get("value", change.get("account"))
			if not value:
				frappe.throw(_("科目不能为空"))
			value = str(value)
		elif fieldname == "summary":
			value = str(change.get("value") or "").strip()
			if not value:
				frappe.throw(_("摘要不能为空"))
			if "\n" in value or "\r" in value:
				frappe.throw(_("摘要只能填写一行内容"))
			if len(value) > MAX_INLINE_SUMMARY_LENGTH:
				frappe.throw(_("摘要不能超过 {0} 个字符").format(MAX_INLINE_SUMMARY_LENGTH))
		elif fieldname == "split":
			value = change.get("value")
			if not isinstance(value, dict) or set(value) - {"account", "amount", "summary"}:
				frappe.throw(_("凭证修改数据不完整，请刷新后重试"))
		elif fieldname == "delete":
			value = change.get("value")
			if value is not True:
				frappe.throw(_("凭证修改数据不完整，请刷新后重试"))
		elif fieldname in INLINE_AMOUNT_FIELDS:
			try:
				value = Decimal(str(change.get("value")))
			except (InvalidOperation, ValueError):
				frappe.throw(_("请输入有效金额"))
			if not value.is_finite() or value.adjusted() > 11:
				frappe.throw(_("请输入有效金额"))
		else:
			frappe.throw(_("不支持修改该凭证字段"))

		key = (fieldname, str(change["edit_key"]))
		if key in parsed:
			frappe.throw(_("同一凭证字段不能重复提交"))
		parsed[key] = value
	return parsed


def _resolve_inline_split(doc, edit_key, value):
	if doc.doctype != "Journal Entry":
		frappe.throw(_("只有本位币记账凭证支持新增一行"))
	target = _resolve_inline_edit_target(doc, edit_key)
	row = _find_child_by_idx(doc, target.parentfield, target.row_idx)
	reason = _inline_split_reason(doc, row)
	if reason:
		frappe.throw(reason)
	account = str(value.get("account") or "")
	new_account = _validate_compatible_account(doc, target, account)
	try:
		amount = Decimal(str(value.get("amount")))
	except (InvalidOperation, ValueError):
		frappe.throw(_("请输入有效金额"))
	if not amount.is_finite() or amount.adjusted() > 11:
		frappe.throw(_("请输入有效金额"))
	debit, credit = (Decimal(str(row.get(field) or 0)) for field in INLINE_AMOUNT_FIELDS)
	if bool(debit) == bool(credit):
		frappe.throw(_("原分录金额无效，请打开原单核对"))
	side = "debit" if debit else "credit"
	original = debit or credit
	if not amount or (amount > 0) != (original > 0) or abs(amount) >= abs(original):
		frappe.throw(_("新增行金额须与原金额同方向，且绝对值大于零、小于原金额的绝对值"))
	precision = row.precision(INLINE_AMOUNT_FIELDS[side])
	if amount != amount.quantize(Decimal(1).scaleb(-precision)):
		frappe.throw(_("金额最多保留 {0} 位小数").format(precision))
	summary = str(value.get("summary") or "").strip()
	if summary and (len(summary) > MAX_INLINE_SUMMARY_LENGTH or "\n" in summary or "\r" in summary):
		frappe.throw(_("新增分录摘要须为一行，且不超过 {0} 个字符").format(MAX_INLINE_SUMMARY_LENGTH))
	return frappe._dict(
		change_field="split", edit_key=edit_key, parentfield="accounts", row_idx=row.idx,
		fieldname=INLINE_AMOUNT_FIELDS[side], side=side, remaining_amount=float(original - amount),
		split_amount=float(amount), new_account=account, new_account_currency=new_account.account_currency,
		summary=summary or row.user_remark or "", old_value=f"{row.account}: {original}",
		new_value=f"{row.account}: {original - amount}; {account}: {amount}; {summary or row.user_remark or ''}",
	)


def _resolve_inline_changes(doc, changes):
	diffs = []
	for (change_field, edit_key), value in _parse_inline_changes(changes).items():
		if change_field == "split":
			diffs.append(_resolve_inline_split(doc, edit_key, value))
			continue
		if change_field == "delete":
			if doc.doctype != "Journal Entry":
				frappe.throw(_("只有本位币记账凭证支持删除行"))
			target = _resolve_inline_edit_target(doc, edit_key)
			row = _find_child_by_idx(doc, "accounts", target.row_idx)
			reason = _inline_delete_reason(doc, row)
			if reason:
				frappe.throw(reason)
			diffs.append(frappe._dict(
				change_field="delete", edit_key=edit_key, parentfield="accounts", row_idx=row.idx,
				old_value=f"{row.account}; {row.debit_in_account_currency}; {row.credit_in_account_currency}; {row.user_remark or ''}",
				new_value=_("（删除行）"),
			))
			continue
		if change_field in INLINE_AMOUNT_FIELDS:
			if doc.doctype != "Journal Entry":
				frappe.throw(_("该来源金额由系统计算，请打开原单修改"))
			target = _resolve_inline_edit_target(doc, edit_key)
			row = _find_child_by_idx(doc, target.parentfield, target.row_idx)
			reason = _inline_amount_reason(doc, row)
			if reason:
				frappe.throw(reason)
			fieldname = INLINE_AMOUNT_FIELDS[change_field]
			precision = row.precision(fieldname)
			if value != value.quantize(Decimal(1).scaleb(-precision)):
				frappe.throw(_("金额最多保留 {0} 位小数").format(precision))
			if Decimal(str(row.get(fieldname) or 0)) == value:
				continue
			diffs.append(
				frappe._dict(
					change_field=change_field,
					edit_key=edit_key,
					old_value=row.get(fieldname) or 0,
					new_value=float(value),
					fieldname=fieldname,
					parentfield=target.parentfield,
					row_idx=target.row_idx,
					role="journal_line",
				)
			)
			continue
		if change_field == "account":
			target = _resolve_inline_edit_target(doc, edit_key)
			if target.account == value:
				continue
			new_account = _validate_compatible_account(doc, target, value)
			diffs.append(
				frappe._dict(
					change_field="account",
					edit_key=edit_key,
					old_value=target.account,
					new_value=value,
					old_account=target.account,
					new_account=value,
					new_account_currency=new_account.account_currency,
					new_account_type=new_account.account_type,
					fieldname=target.fieldname,
					parentfield=target.parentfield,
					row_idx=target.row_idx,
					role=target.role,
				)
			)
			continue

		target = _resolve_inline_summary_target(doc, edit_key)
		if target.value == value:
			continue
		diffs.append(
			frappe._dict(
				change_field="summary",
				edit_key=edit_key,
				old_value=target.value,
				new_value=value,
				fieldname=target.fieldname,
				parentfield=target.parentfield,
				row_idx=target.row_idx,
				role="summary",
			)
		)
	if not diffs:
		frappe.throw(_("没有检测到需要保存的凭证变化"))
	split_rows = {diff.row_idx for diff in diffs if diff.change_field == "split"}
	if any(diff.parentfield == "accounts" and diff.row_idx in split_rows and diff.change_field != "split" for diff in diffs):
		frappe.throw(_("新增一行不能与同一行的其他修改一起提交，请先保存或取消其他修改"))
	deleted_rows = {diff.row_idx for diff in diffs if diff.change_field == "delete"}
	if any(diff.parentfield == "accounts" and diff.row_idx in deleted_rows and diff.change_field != "delete" for diff in diffs):
		frappe.throw(_("待删除分录不能同时修改其他内容，请先保存或取消该行修改"))
	return diffs


def _assert_source_version(doc, modified):
	if not modified or str(doc.modified) != str(modified):
		frappe.throw(_("凭证已被其他人修改，请刷新后重试"), frappe.TimestampMismatchError)


def _inline_preflight(doc, modified, changes):
	_assert_source_version(doc, modified)
	status = _build_status(doc)
	can_apply = status.get("can_edit") or status.get("quick_unreconcile")
	if not can_apply:
		frappe.throw(_("当前凭证不能修改：{0}").format(status.get("reason") or _("不满足修改条件")))
	diffs = _resolve_inline_changes(doc, changes)
	if any(diff.change_field in {*INLINE_AMOUNT_FIELDS, "split", "delete"} for diff in diffs):
		_validate_inline_amount_changes(doc, diffs)
	quick_info = status.get("quick_unreconcile")
	if quick_info and quick_info.get("bank_account"):
		for diff in diffs:
			if diff.change_field == "account" and diff.old_account == quick_info.get("bank_account"):
				frappe.throw(_("已核销凭证不能从查凭证修改银行流水对应的银行科目"))
	return status, diffs


def _validate_inline_amount_changes(doc, diffs):
	proposed = frappe.copy_doc(doc)
	_apply_inline_diffs(proposed, diffs)
	if any(diff.change_field == "delete" for diff in diffs) and len(proposed.accounts) < 2:
		frappe.throw(_("删除后至少保留两条分录"))
	if any(diff.change_field == "split" for diff in diffs) and len(proposed.accounts) > MAX_INLINE_ENTRY_ROWS:
		frappe.throw(_("新增后分录不能超过 {0} 行").format(MAX_INLINE_ENTRY_ROWS))
	totals = {field: Decimal(0) for field in INLINE_AMOUNT_FIELDS}
	for row in proposed.accounts:
		amounts = {field: Decimal(str(row.get(field) or 0)) for field in INLINE_AMOUNT_FIELDS}
		if amounts["debit"] and amounts["credit"]:
			frappe.throw(_("第 {0} 行不能同时填写借方和贷方金额").format(row.idx))
		if not amounts["debit"] and not amounts["credit"]:
			frappe.throw(_("第 {0} 行借方和贷方金额不能同时为零").format(row.idx))
		for field, amount in amounts.items():
			totals[field] += amount
	if totals["debit"] != totals["credit"]:
		frappe.throw(_("借贷不平衡，请把同一张凭证的相关金额修改完成后一起保存"))

	# Bank evidence is immutable here: expense/accrual allocations may change,
	# but an amount edit cannot change the payment represented by its source.
	bank_accounts = set()
	for name in _get_bank_transaction_names(doc):
		bank_account = frappe.db.get_value("Bank Transaction", name, "bank_account")
		account = frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
		if account:
			bank_accounts.add(account)
	if frappe.db.exists("DocType", "China Bank Receipt"):
		for bank_account in frappe.get_all(
			"China Bank Receipt",
			filters={"voucher_type": doc.doctype, "voucher_name": doc.name},
			pluck="bank_account",
		):
			account = frappe.db.get_value("Bank Account", bank_account, "account")
			if account:
				bank_accounts.add(account)
	for account in bank_accounts:
		for field in INLINE_AMOUNT_FIELDS:
			before = sum(Decimal(str(row.get(field) or 0)) for row in doc.accounts if row.account == account)
			after = sum(Decimal(str(row.get(field) or 0)) for row in proposed.accounts if row.account == account)
			if before != after:
				frappe.throw(_("关联回单或银行流水的银行收付款金额不能在此修改，请先核对原始回单和银行流水"))


@frappe.whitelist()
def preview_inline_voucher_changes(source_doctype, source_name, modified, changes):
	"""Validate staged changes without mutating the source voucher."""
	doc = _get_source_document(source_doctype, source_name)
	status, diffs = _inline_preflight(doc, modified, changes)
	return {
		"mode": "amend" if doc.docstatus == 1 else "draft",
		"requires_confirmation": doc.docstatus == 1,
		"changes": [
			{
				"edit_key": diff.edit_key,
				"field": diff.change_field,
				"old_value": diff.old_value,
				"new_value": diff.new_value,
				"old_account": diff.get("old_account"),
				"new_account": diff.get("new_account"),
			}
			for diff in diffs
		],
		"bank_action": "unreconcile_and_restore" if status.get("quick_unreconcile") else "none",
		"bank_transaction": (status.get("quick_unreconcile") or {}).get("bank_transaction"),
	}


@frappe.whitelist()
def preview_inline_account_changes(source_doctype, source_name, modified, changes):
	"""Backward-compatible endpoint for clients that only submit account changes."""
	return preview_inline_voucher_changes(source_doctype, source_name, modified, changes)


def _get_locked_source_document(source_doctype, source_name):
	# Number organization and month-end work take the company lock first.
	# Never hold a source/bank lock while waiting for that shared lock.
	doc = _get_source_document(source_doctype, source_name)
	lock_company(doc.company)
	current = _get_source_document(source_doctype, source_name, for_update=True)
	if current.company != doc.company:
		# A draft may have moved companies before we acquired its lock. Taking
		# another company's lock now would reintroduce the reversed lock order.
		frappe.throw(_("凭证已被其他人修改，请刷新后重试"), frappe.TimestampMismatchError)
	return current


@frappe.whitelist()
def prepare_source_voucher_edit(source_doctype, source_name):
	"""Open a draft or atomically cancel-and-create an amendment for a submitted voucher."""
	doc = _get_locked_source_document(source_doctype, source_name)
	status = _build_status(doc)
	if not status["can_edit"]:
		frappe.throw(_("当前凭证不能修改：{0}").format(status["reason"] or _("不满足修改条件")))

	if doc.docstatus == 0:
		return {
			"mode": "draft",
			"source_doctype": source_doctype,
			"source_name": source_name,
			"name": source_name,
		}

	if reason := _amendment_size_reason(doc):
		frappe.throw(reason)

	bank_transaction_names = _get_bank_transaction_names(doc)
	doc.cancel()
	doc.reload()
	if doc.docstatus != 2:
		frappe.throw(_("原凭证取消尚未完成，请稍后再试"))

	amended = frappe.copy_doc(doc)
	amended.docstatus = 0
	amended.amended_from = source_name
	_reset_amendment_workflow_state(amended)
	if amended.meta.has_field("custom_china_voucher_number"):
		amended.custom_china_voucher_number = None
	if amended.meta.has_field("clearance_date"):
		amended.clearance_date = None

	if amended.meta.has_field("custom_china_bank_transaction") and bank_transaction_names:
		amended.custom_china_bank_transaction = bank_transaction_names[0]

	amended.insert()
	if (
		source_doctype == "Journal Entry"
		and bank_transaction_names
		and frappe.db.has_column("Bank Transaction", "custom_china_journal_entry")
	):
		for bank_transaction_name in bank_transaction_names:
			frappe.db.set_value(
				"Bank Transaction",
				bank_transaction_name,
				"custom_china_journal_entry",
				amended.name,
				update_modified=False,
			)

	return {
		"mode": "amended_draft",
		"source_doctype": source_doctype,
		"source_name": source_name,
		"name": amended.name,
		"docstatus": amended.docstatus,
		"workflow_state": amended.get("workflow_state"),
		"message": _("原凭证已取消，修订凭证 {0} 已生成草稿；修改并保存后将自动审核并记账").format(
			amended.name
		),
	}


def _remove_quick_reconciliation(doc, quick_info=None):
	quick_info = quick_info or _get_quick_unreconcile_info(doc)
	if not quick_info:
		frappe.throw(_("当前银行流水不满足安全的一键撤销条件，请按提示先手动撤销核销"))

	bank_transaction_name = quick_info["bank_transaction"]
	if not frappe.db.sql(
		"SELECT name FROM `tabBank Transaction` WHERE name=%s FOR UPDATE",
		(bank_transaction_name,),
	):
		frappe.throw(_("银行流水不存在或已被删除，请刷新后重试"))

	bank_transaction = frappe.get_doc("Bank Transaction", bank_transaction_name)
	if not bank_transaction.has_permission("write"):
		frappe.throw(_("当前用户没有修改该银行流水的权限"), frappe.PermissionError)
	quick_info = _get_quick_unreconcile_info(doc)
	if not quick_info or quick_info["bank_transaction"] != bank_transaction_name:
		frappe.throw(_("银行流水状态已变化，请刷新后重试"))

	entry = next(
		(
			row
			for row in bank_transaction.payment_entries
			if row.payment_document == doc.doctype and row.payment_entry == doc.name
		),
		None,
	)
	if not entry:
		frappe.throw(_("未找到当前凭证对应的银行对账分配，请刷新后重试"))

	bank_transaction.remove_payment_entry(entry)
	bank_transaction.save()
	if (
		doc.doctype == "Journal Entry"
		and frappe.db.has_column("Bank Transaction", "custom_china_journal_entry")
		and bank_transaction.get("custom_china_journal_entry") == doc.name
	):
		frappe.db.set_value(
			"Bank Transaction",
			bank_transaction.name,
			"custom_china_journal_entry",
			None,
			update_modified=False,
		)
	return quick_info


@frappe.whitelist()
def complete_source_voucher_edit(source_doctype, source_name):
	"""Automatically review and post an amended voucher after its real edit is saved."""
	doc = _get_locked_source_document(source_doctype, source_name)
	if doc.docstatus != 0 or not doc.get("amended_from"):
		frappe.throw(_("只有已保存的修订草稿才能自动审核并记账"))

	if not doc.has_permission("write") or not doc.has_permission("submit"):
		frappe.throw(_("当前用户没有保存并记账该修订凭证的权限"), frappe.PermissionError)

	if not _has_meaningful_amendment_changes(doc):
		return {
			"posted": False,
			"name": doc.name,
			"message": _("未检测到凭证内容变化，修订草稿暂未记账"),
		}
	if doc.doctype == "Journal Entry":
		from china_finance.services.voucher_preparation import READY_FIELDS, content_hash, mode_enabled

		if mode_enabled(doc.company, doc.posting_date):
			doc.db_set(
				{
					READY_FIELDS[0]: content_hash(doc),
					READY_FIELDS[1]: frappe.session.user,
					READY_FIELDS[2]: frappe.utils.now_datetime(),
				}
			)
			doc.reload()

	doc = _complete_voucher_workflow(doc)
	bank_transaction_names = _get_bank_transaction_names(doc)
	if (
		doc.doctype == "Journal Entry"
		and bank_transaction_names
		and frappe.db.has_column("Bank Transaction", "custom_china_journal_entry")
	):
		for bank_transaction_name in bank_transaction_names:
			frappe.db.set_value(
				"Bank Transaction",
				bank_transaction_name,
				"custom_china_journal_entry",
				doc.name,
				update_modified=False,
			)
	bank_reconciliation = _get_bank_reconciliation_feedback(doc, bank_transaction_names)
	message = _("修订凭证 {0} 已自动审核并记账").format(doc.name)
	if bank_reconciliation["status"] == "success":
		message += _("；关联银行流水 {0} 已自动重新核销").format(
			"、".join(row["name"] for row in bank_reconciliation["successful"])
		)
	elif bank_reconciliation["status"] == "failed":
		message += _("；但关联银行流水未能自动重新核销，请及时处理")

	return {
		"posted": True,
		"name": doc.name,
		"docstatus": doc.docstatus,
		"workflow_state": doc.get("workflow_state"),
		"doc": doc.as_dict(),
		"bank_reconciliation": bank_reconciliation,
		"message": message,
	}


def _find_child_by_idx(doc, parentfield, row_idx):
	rows = [row for row in doc.get(parentfield) or [] if row.idx == row_idx]
	if len(rows) != 1:
		frappe.throw(_("来源分录结构已变化，请刷新报表后重试"))
	return rows[0]


def _apply_inline_diffs(doc, diffs):
	deleted = [
		_find_child_by_idx(doc, "accounts", diff.row_idx)
		for diff in diffs if diff.change_field == "delete"
	]
	for diff in diffs:
		if diff.change_field == "delete":
			continue
		if diff.change_field == "split":
			target = _find_child_by_idx(doc, "accounts", diff.row_idx)
			new_row = frappe.copy_doc(target)
			new_row.idx = 0
			new_row.docstatus = 0
			new_row.account = diff.new_account
			new_row.account_currency = diff.new_account_currency
			new_row.user_remark = diff.summary
			for side, fieldname in INLINE_AMOUNT_FIELDS.items():
				amount = diff.split_amount if side == diff.side else 0
				new_row.set(side, amount)
				new_row.set(fieldname, amount)
			target.set(diff.side, diff.remaining_amount)
			target.set(diff.fieldname, diff.remaining_amount)
			doc.append("accounts", new_row)
			continue
		if diff.parentfield:
			target = _find_child_by_idx(doc, diff.parentfield, diff.row_idx)
			target.set(diff.fieldname, diff.new_value)
			if diff.change_field in INLINE_AMOUNT_FIELDS:
				target.set(diff.change_field, diff.new_value)
			if diff.change_field == "account" and doc.doctype == "Journal Entry":
				target.account_currency = diff.new_account_currency
			continue

		doc.set(diff.fieldname, diff.new_value)
		if diff.change_field == "summary" and doc.doctype == "Payment Entry":
			doc.custom_remarks = 1
		if diff.change_field == "account" and doc.doctype == "Payment Entry" and diff.fieldname in (
			"paid_from",
			"paid_to",
		):
			doc.set(f"{diff.fieldname}_account_currency", diff.new_account_currency)
			account_type_field = f"{diff.fieldname}_account_type"
			if doc.meta.has_field(account_type_field):
				doc.set(account_type_field, diff.new_account_type)
	for row in deleted:
		doc.remove(row)
	if deleted:
		for index, row in enumerate(doc.accounts, 1):
			row.idx = index


def _inline_change_label(diffs):
	fields = {diff.change_field for diff in diffs}
	if fields & {"split", "delete"}:
		return _("凭证内容")
	if fields == {"account"}:
		return _("科目")
	if fields == {"summary"}:
		return _("摘要")
	if fields <= set(INLINE_AMOUNT_FIELDS):
		return _("金额")
	if fields & set(INLINE_AMOUNT_FIELDS):
		return _("凭证内容")
	return _("科目和摘要")


def _audit_inline_changes(doc, diffs, *, amended_from=None):
	label = _inline_change_label(diffs)
	heading = _("查凭证表内科目更正") if label == _("科目") else _("查凭证表内{0}修改").format(label)
	lines = [heading]
	for diff in diffs:
		field_label = {
			"account": _("科目"), "summary": _("摘要"), "debit": _("借方"), "credit": _("贷方"),
			"split": _("新增一行"),
			"delete": _("删除行"),
		}[diff.change_field]
		lines.append(
			"{0}：{1} → {2}".format(
				field_label,
				frappe.utils.escape_html(str(diff.old_value) if diff.old_value is not None else _("（空）")),
				frappe.utils.escape_html(str(diff.new_value)),
			)
		)
	if amended_from:
		lines.append(_("原凭证：{0}").format(frappe.utils.escape_html(amended_from)))
	doc.add_comment("Comment", "<br>".join(lines))


def _restore_quick_reconciliation(doc, quick_info):
	if not quick_info:
		return {"status": "not_applicable", "successful": [], "failed": []}
	bank_transaction_name = quick_info["bank_transaction"]
	if not frappe.db.sql(
		"SELECT name FROM `tabBank Transaction` WHERE name=%s FOR UPDATE",
		(bank_transaction_name,),
	):
		frappe.throw(_("银行流水不存在，无法恢复核销"))

	bank_transaction = frappe.get_doc("Bank Transaction", bank_transaction_name)
	if doc.doctype == "Journal Entry" and frappe.db.has_column(
		"Bank Transaction", "custom_china_journal_entry"
	):
		frappe.db.set_value(
			"Bank Transaction",
			bank_transaction_name,
			"custom_china_journal_entry",
			doc.name,
			update_modified=False,
		)

	existing = next(
		(
			row
			for row in bank_transaction.payment_entries
			if row.payment_document == doc.doctype and row.payment_entry == doc.name
		),
		None,
	)
	if not existing:
		from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
			reconcile_vouchers,
		)

		reconcile_vouchers(
			bank_transaction_name,
			json.dumps(
				[
					{
						"payment_doctype": doc.doctype,
						"payment_name": doc.name,
						"amount": quick_info["amount"],
					}
				]
			),
		)
	feedback = _get_bank_reconciliation_feedback(doc, [bank_transaction_name])
	if feedback["status"] != "success":
		frappe.throw(_("更正后未能恢复银行流水 {0} 的核销，全部修改已回滚").format(bank_transaction_name))
	return feedback


@frappe.whitelist(methods=["POST"])
def apply_inline_voucher_changes(source_doctype, source_name, modified, changes):
	"""Apply one voucher's changes atomically, including cancellation and re-posting."""
	savepoint = "inline_voucher_edit_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	try:
		return _apply_inline_voucher_changes(source_doctype, source_name, modified, changes)
	except frappe.QueryDeadlockError:
		# InnoDB already rolled back the transaction (including its savepoints).
		# Preserve the retryable error for the request's full rollback/retry.
		raise
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise


@frappe.whitelist(methods=["POST"])
def apply_inline_account_changes(source_doctype, source_name, modified, changes):
	"""Backward-compatible endpoint for clients that only submit account changes."""
	return apply_inline_voucher_changes(source_doctype, source_name, modified, changes)


def _apply_inline_voucher_changes(source_doctype, source_name, modified, changes):
	doc = _get_locked_source_document(source_doctype, source_name)
	status, diffs = _inline_preflight(doc, modified, changes)
	change_label = _inline_change_label(diffs)

	if doc.docstatus == 0:
		_apply_inline_diffs(doc, diffs)
		doc.save(ignore_version=False)
		_audit_inline_changes(doc, diffs)
		return {
			"mode": "draft",
			"posted": False,
			"source_doctype": doc.doctype,
			"source_name": doc.name,
			"modified": str(doc.modified),
			"message": _("{0}已保存；如该凭证原已核对，请重新核对后记账").format(change_label),
		}

	quick_info = status.get("quick_unreconcile")
	if quick_info:
		_remove_quick_reconciliation(doc, quick_info)
	prepare_result = prepare_source_voucher_edit(source_doctype, source_name)
	if not prepare_result.get("name"):
		frappe.throw(_("后台未生成修订凭证，全部修改已回滚"))

	from china_finance.services.voucher import process_cancellation_snapshot

	cancellation = process_cancellation_snapshot(source_doctype, source_name)
	if cancellation.get("status") != "resolved":
		frappe.throw(_("原凭证冲销审计记录生成失败，全部修改已回滚"))

	amended = _get_source_document(source_doctype, prepare_result["name"])
	_apply_inline_diffs(amended, diffs)
	amended.save(ignore_version=False)
	post_result = complete_source_voucher_edit(source_doctype, amended.name)
	if not post_result.get("posted"):
		frappe.throw(post_result.get("message") or _("修订凭证未完成记账，全部修改已回滚"))
	amended = _get_source_document(source_doctype, amended.name)
	reconciliation = _restore_quick_reconciliation(amended, quick_info)

	original = _get_source_document(source_doctype, source_name)
	_audit_inline_changes(original, diffs, amended_from=amended.name)
	_audit_inline_changes(amended, diffs, amended_from=source_name)
	return {
		"mode": "amended",
		"posted": True,
		"source_doctype": amended.doctype,
		"source_name": amended.name,
		"amended_from": source_name,
		"modified": str(amended.modified),
		"bank_reconciliation": reconciliation,
		"message": _("{0}更正已完成，修订凭证 {1} 已重新记账").format(
			change_label, amended.name
		),
	}
