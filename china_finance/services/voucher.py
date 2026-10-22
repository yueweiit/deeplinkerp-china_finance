import hashlib
import json
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime


GL_SOURCE_DOCTYPES = (
	"Journal Entry",
	"Payment Entry",
	"Sales Invoice",
	"Purchase Invoice",
	"Stock Entry",
	"Delivery Note",
	"Purchase Receipt",
	"Asset",
	"Asset Capitalization",
	"Asset Depreciation Entry",
	"Payroll Entry",
	"Period Closing Voucher",
)

FORMAL_VOUCHER_SOURCES = ("Journal Entry", "Payment Entry", "Period Closing Voucher")

SNAPSHOT_RETRY_ROLES = ("System Manager", "China Finance Manager")
SNAPSHOT_BACKLINK_DOCTYPES = ("China Accounting Voucher", "China Cash Flow Assignment")
IMPORT_BATCH_POSTING_ROLES = ("System Manager", "Accounts Manager", "China Finance Manager")
IMPORT_VOUCHER_TITLE_PREFIX = "Excel导入："
IMPORT_BATCH_WORKFLOW_STATES = ("Pending Review", "Approved", "Posted")


def _get_source_document(source_doctype, source_name):
	if source_doctype not in GL_SOURCE_DOCTYPES:
		frappe.throw(_("{0} 不是支持的总账来源单据").format(source_doctype))
	if not frappe.db.exists(source_doctype, source_name):
		frappe.throw(_("来源单据不存在"))
	doc = frappe.get_doc(source_doctype, source_name)
	if not doc.has_permission("read"):
		frappe.throw(_("无权查看来源单据"), frappe.PermissionError)
	return doc


def get_company_settings(company):
	if not company or not frappe.db.exists("DocType", "China Finance Settings"):
		return None
	name = frappe.db.get_value(
		"China Finance Settings",
		{"company": company, "enabled": 1},
		"name",
	)
	return frappe.get_cached_doc("China Finance Settings", name) if name else None


def get_posting_date(doc):
	# A period closing voucher is created on the day the user runs the closing,
	# but its accounting date belongs to the period being closed.  Use the
	# period end so the generated China Accounting Voucher is reported in the
	# correct month and does not inherit the user's current date.
	if getattr(doc, "doctype", None) == "Period Closing Voucher" and doc.get("period_end_date"):
		return getdate(doc.period_end_date)
	for fieldname in ("posting_date", "transaction_date", "purchase_date"):
		if doc.meta.has_field(fieldname) and doc.get(fieldname):
			return getdate(doc.get(fieldname))
	return getdate(doc.creation)


def get_company(doc):
	if doc.meta.has_field("company") and doc.get("company"):
		return doc.company
	return None


def validate_source_approval(doc, method=None):
	from china_finance.services.voucher_preparation import guard_submission
	guard_submission(doc)
	settings = get_company_settings(get_company(doc))
	if not settings or not settings.enforce_role_separation:
		return
	if doc.doctype == "Journal Entry" and getattr(frappe.flags, "china_finance_direct_posting", False):
		return
	if (
		getattr(frappe.flags, "china_finance_import_batch_names", None)
		and doc.doctype == "Journal Entry"
		and doc.name in frappe.flags.china_finance_import_batch_names
		and is_batch_postable_journal_entry(doc)
	):
		return
	posting_date = get_posting_date(doc)
	if posting_date < getdate(settings.activation_date):
		return
	workflow_name = frappe.db.get_value("Workflow", {"document_type": doc.doctype, "is_active": 1}, "name")
	if not workflow_name:
		frappe.throw(_("{0} 已启用制单审核分离，但 {1} 没有启用审批工作流").format(settings.company, doc.doctype))
	reviewer = get_reviewer(doc.doctype, doc.name)
	if not reviewer:
		frappe.throw(_("单据尚未完成审核，不能记账"))
	# A small accounting team may have one user complete preparation, review,
	# and posting. Keep the workflow review record and audit fields, but do not
	# reject the posting only because the same user performed each step.


@frappe.whitelist(methods=["POST"])
def save_and_post_journal_entry(doc):
	"""Save and submit a new manual Journal Entry in one explicit action.

	Bank-generated and Excel-imported entries keep their existing workflows. This
	shortcut is intentionally limited to a new manual entry so existing drafts
	remain editable and the standard review process is not changed globally.
	"""
	if isinstance(doc, str):
		doc = frappe.parse_json(doc)
	if not isinstance(doc, dict) or doc.get("doctype") != "Journal Entry":
		frappe.throw(_("只支持新建记账凭证的保存并记账"))

	journal_entry = frappe.get_doc(doc)
	if not journal_entry.is_new():
		frappe.throw(_("保存并记账仅支持新建记账凭证，已有草稿请使用原审核流程"))
	if journal_entry.docstatus != 0:
		frappe.throw(_("只能保存并记账草稿凭证"))
	if is_batch_postable_journal_entry(journal_entry):
		frappe.throw(_("银行流水或 Excel 导入凭证请使用原有批量审核并记账流程"))

	journal_entry.check_permission("create")
	journal_entry.check_permission("submit")
	journal_entry.insert()

	previous_direct_posting = getattr(frappe.flags, "china_finance_direct_posting", None)
	frappe.flags.china_finance_direct_posting = True
	try:
		journal_entry.submit()
	finally:
		if previous_direct_posting is None:
			frappe.flags.pop("china_finance_direct_posting", None)
		else:
			frappe.flags.china_finance_direct_posting = previous_direct_posting

	return journal_entry.as_dict()


def is_imported_journal_entry(doc):
	"""Return whether a Journal Entry was created by the Excel import flow."""
	return (
		getattr(doc, "doctype", None) == "Journal Entry"
		and str(doc.get("title") or "").strip().startswith(IMPORT_VOUCHER_TITLE_PREFIX)
	)


def is_bank_journal_entry(doc):
	"""Return whether a Journal Entry was created from a Bank Transaction."""
	return (
		getattr(doc, "doctype", None) == "Journal Entry"
		and bool(str(doc.get("custom_china_bank_transaction") or "").strip())
	)


def is_batch_postable_journal_entry(doc):
	"""Return whether the document belongs to a supported batch-posting source."""
	return is_imported_journal_entry(doc) or is_bank_journal_entry(doc)


def _get_import_batch_transition(doc, workflow, next_state):
	from frappe.model.workflow import get_transitions

	transitions = [
		transition
		for transition in get_transitions(doc, workflow, raise_exception=True)
		if transition.next_state == next_state
	]
	if len(transitions) != 1:
		frappe.throw(
			_("{0} 无法从 {1} 进入 {2}，请检查审批工作流配置").format(
				doc.doctype,
				doc.get(workflow.workflow_state_field), next_state
			)
		)
	return transitions[0]


def _complete_voucher_workflow(doc):
	"""Run the configured review path before submitting a formal voucher."""
	from frappe.model.workflow import apply_workflow, get_workflow

	workflow_name = frappe.db.get_value(
		"Workflow", {"document_type": doc.doctype, "is_active": 1}, "name"
	)
	if not workflow_name:
		doc.submit()
		return doc

	workflow = get_workflow(doc.doctype)
	workflow_state_field = workflow.workflow_state_field
	current_state = doc.get(workflow_state_field)
	if current_state == "Rejected":
		frappe.throw(_("凭证 {0} 已被退回，请先重新提交审核").format(doc.name))
	if current_state not in ("Draft", "Pending Review", "Approved"):
		frappe.throw(_("凭证 {0} 当前状态为 {1}，不能使用批量审核并记账").format(doc.name, current_state))

	states = ("Draft", *IMPORT_BATCH_WORKFLOW_STATES)
	for next_state in states[states.index(current_state) + 1:]:
		if current_state == next_state:
			continue
		transition = _get_import_batch_transition(doc, workflow, next_state)
		doc = apply_workflow(doc, transition.action)
		current_state = doc.get(workflow_state_field)

	if doc.docstatus != 1:
		frappe.throw(_("凭证 {0} 未完成记账，当前状态为 {1}").format(doc.name, current_state))
	return doc


def _complete_import_batch_workflow(doc):
	"""Backward-compatible wrapper for the imported-voucher batch action."""
	return _complete_voucher_workflow(doc)


def _validate_import_batch_document(doc):
	if not is_batch_postable_journal_entry(doc):
		frappe.throw(
			_("凭证 {0} 不是 Excel 导入或银行流水生成的凭证，快捷处理已停止").format(doc.name)
		)
	if doc.docstatus != 0:
		frappe.throw(_("凭证 {0} 不是草稿，不能重复处理").format(doc.name))
	if not doc.accounts:
		frappe.throw(_("凭证 {0} 没有会计分录").format(doc.name))
	if is_bank_journal_entry(doc):
		bank_transaction_name = str(doc.get("custom_china_bank_transaction") or "").strip()
		bank_transaction = frappe.db.get_value(
			"Bank Transaction",
			bank_transaction_name,
			["company", "docstatus"],
			as_dict=True,
		)
		if not bank_transaction:
			frappe.throw(_("凭证 {0} 关联的银行流水不存在").format(doc.name))
		if bank_transaction.company != doc.company:
			frappe.throw(_("凭证 {0} 与银行流水所属公司不一致").format(doc.name))
		if bank_transaction.docstatus != 1:
			frappe.throw(_("凭证 {0} 关联的银行流水尚未提交").format(doc.name))

	doc.set_total_debit_credit()
	if abs(flt(doc.difference)) > 0.005:
		frappe.throw(
			_("凭证 {0} 借贷不平衡，差额为 {1}").format(doc.name, flt(doc.difference, 2))
		)

	if frappe.db.exists(
		"China Accounting Voucher",
		{"source_doctype": "Journal Entry", "source_name": doc.name, "source_event": "Posting"},
	):
		frappe.throw(_("凭证 {0} 已经生成中国会计凭证，不能重复处理").format(doc.name))


@frappe.whitelist(methods=["POST"])
def batch_post_imported_journal_entries(names, reason=None):
	"""Batch review and post supported imported or bank-generated Journal Entries.

	The regular Journal Entry workflow remains unchanged. Each selected entry is
	processed in its own transaction so a failed entry does not roll back entries
	that were already posted. The old method name is retained for compatibility
	with existing clients.
	"""
	frappe.only_for(IMPORT_BATCH_POSTING_ROLES)

	if isinstance(names, str):
		names = frappe.parse_json(names)
	if not isinstance(names, (list, tuple)):
		frappe.throw(_("请选择需要处理的凭证"))

	names = list(dict.fromkeys(str(name).strip() for name in names if str(name).strip()))
	if not names:
		frappe.throw(_("请选择需要处理的凭证"))
	if len(names) > 100:
		frappe.throw(_("一次最多处理 100 张凭证，请分批操作"))

	reason = str(reason or "").strip()
	if not reason:
				frappe.throw(_("请填写批量审核并记账的处理原因"))
	if len(reason) > 500:
		frappe.throw(_("处理原因不能超过 500 个字符"))

	batch_id = f"IBP-{now_datetime().strftime('%Y%m%d%H%M%S')}-{frappe.generate_hash(length=6).upper()}"
	from china_finance.services.voucher_batch import sort_voucher_names

	names = sort_voucher_names("Journal Entry", names)
	results = []
	for name in names:
		save_point = f"china_import_batch_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(save_point)
		previous_batch_names = getattr(frappe.flags, "china_finance_import_batch_names", None)
		try:
			doc = frappe.get_doc("Journal Entry", name, for_update=True)
			doc.check_permission("read")
			doc.check_permission("submit")
			settings = get_company_settings(doc.company)
			if not settings or not cint(getattr(settings, "enable_import_batch_posting", 0)):
				frappe.throw(_("公司 {0} 尚未启用批量审核并记账").format(doc.company))
			_validate_import_batch_document(doc)

			frappe.flags.china_finance_import_batch_names = {doc.name}
			workflow_name = frappe.db.get_value(
				"Workflow", {"document_type": "Journal Entry", "is_active": 1}, "name"
			)
			if settings.enforce_role_separation and not workflow_name:
				frappe.throw(_("{0} 已启用制单审核分离，但 Journal Entry 没有启用审批工作流").format(doc.company))
			if workflow_name:
				doc = _complete_import_batch_workflow(doc)
			else:
				doc.submit()

			doc.add_comment(
				"Comment",
				_("批量审核并记账<br>批次：{0}<br>处理原因：{1}").format(
					batch_id, frappe.utils.escape_html(reason)
				),
			)
			frappe.db.commit()
			results.append({"name": name, "status": "success", "message": _("已批量审核并记账")})
		except Exception as exc:
			frappe.db.rollback(save_point=save_point)
			results.append({"name": name, "status": "failed", "message": str(exc)})
			frappe.log_error(
				message=frappe.get_traceback(),
				title=_("导入凭证快捷处理失败：{0}").format(name),
				reference_doctype="Journal Entry",
				reference_name=name,
			)
		finally:
			if previous_batch_names is None:
				frappe.flags.pop("china_finance_import_batch_names", None)
			else:
				frappe.flags.china_finance_import_batch_names = previous_batch_names
			try:
				frappe.db.release_savepoint(save_point)
			except Exception:
				pass

	return {
		"batch_id": batch_id,
		"total": len(results),
		"success_count": sum(result["status"] == "success" for result in results),
		"failed_count": sum(result["status"] == "failed" for result in results),
		"results": results,
	}


def on_gl_source_submit(doc, method=None):
	create_voucher_from_source(doc, "Posting")


@contextmanager
def _mute_audit_messages():
	previous = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	try:
		yield
	finally:
		frappe.flags.mute_messages = previous


@_mute_audit_messages()
def on_gl_source_cancel(doc, method=None):
	"""Do not let an audit snapshot failure roll back an ERPNext cancellation."""
	# The snapshot and its cash-flow assignment dynamically link back to the
	# source document. They are audit records, not business dependants, so they
	# must not trigger Frappe's "cancel all linked documents" flow.
	_ignore_snapshot_backlinks(doc)
	save_point = f"china_cancellation_issue_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(save_point)
	retryable_failure = False
	try:
		settings = get_company_settings(get_company(doc))
		if not settings or getdate(get_posting_date(doc)) < getdate(settings.activation_date):
			return
		issue = _ensure_cancellation_sync_issue(doc)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		# The database may already have rolled back a deadlock victim. Preserve
		# the original error so the native transaction owner can retry safely.
		retryable_failure = True
		raise
	except Exception:
		# Undo only audit creation work performed inside this hook, preserving
		# ERPNext's stock/payment/GL cancellation before this savepoint.
		try:
			frappe.db.rollback(save_point=save_point)
			frappe.log_error(
				title=_("中国会计凭证冲销同步记录创建失败"), message=frappe.get_traceback(),
				reference_doctype=doc.doctype, reference_name=doc.name,
			)
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			retryable_failure = True
			raise
		return
	finally:
		if not retryable_failure:
			frappe.db.release_savepoint(save_point)
	# The closing batch completes native GL reversal before synchronizing audit.
	# A short-queue snapshot job here would race the long-queue GL job.
	if doc.doctype == "Period Closing Voucher" and getattr(frappe.flags, "china_closing_reversal", None):
		return
	try:
		frappe.enqueue(
			"china_finance.services.voucher.process_cancellation_snapshot_job",
			queue="short",
			enqueue_after_commit=True,
			source_doctype=doc.doctype,
			source_name=doc.name,
			issue_name=issue.name,
		)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		raise
	except Exception as exc:
		_record_sync_failure(issue.name, exc)
		frappe.log_error(title=_("中国会计凭证冲销快照排队失败"), message=frappe.get_traceback())


def prepare_source_cancellation(doc, method=None):
	"""Exclude audit-only backlinks before Frappe validates source cancellation."""
	if doc.doctype == "Journal Entry":
		from china_finance.services.bank_reconciliation import prepare_journal_entry_bank_cancellation

		prepare_journal_entry_bank_cancellation(doc)
	_ignore_snapshot_backlinks(doc)


def _ignore_snapshot_backlinks(doc):
	existing = doc.get("ignore_linked_doctypes") or ()
	if isinstance(existing, str):
		existing = (existing,)
	doc.ignore_linked_doctypes = tuple(dict.fromkeys((*existing, *SNAPSHOT_BACKLINK_DOCTYPES)))


def _cancellation_issue_key(doc):
	return f"Cancellation|{doc.doctype}|{doc.name}"


def _ensure_cancellation_sync_issue(doc):
	if doc.doctype not in GL_SOURCE_DOCTYPES or frappe.db.get_value(doc.doctype, doc.name, "docstatus", for_update=True) != 2:
		frappe.throw(_("来源单据尚未取消或不是支持的总账来源，不能创建冲销同步记录"))
	issue_key = _cancellation_issue_key(doc)
	name = frappe.db.get_value("China Voucher Sync Issue", {"issue_key": issue_key}, "name", for_update=True)
	if name:
		return frappe.get_doc("China Voucher Sync Issue", name, for_update=True)
	issue = frappe.get_doc(
		{
			"doctype": "China Voucher Sync Issue",
			"company": get_company(doc),
			"posting_date": get_posting_date(doc),
			"source_doctype": doc.doctype,
			"source_name": doc.name,
			"issue_key": issue_key,
			"status": "Pending",
		}
	)
	issue.flags.ignore_permissions = True
	# This audit record exists precisely to retain its cancelled source link;
	# the persisted-source guard above scopes this exception to cancellation.
	issue.flags.ignore_links = True
	try:
		issue.insert()
	except frappe.DuplicateEntryError:
		name = frappe.db.get_value("China Voucher Sync Issue", {"issue_key": issue_key}, "name", for_update=True)
		issue = frappe.get_doc("China Voucher Sync Issue", name, for_update=True)
	return issue


def _record_sync_failure(issue_name, exc):
	frappe.db.set_value(
		"China Voucher Sync Issue",
		issue_name,
		{"status": "Pending", "last_attempted_on": now_datetime(), "last_error": str(exc)},
		update_modified=False,
	)


def _get_cancellation_posting(doc):
	"""Return only a submitted, matching original snapshot under the source lock."""
	company = get_company(doc)
	name = frappe.db.get_value(
		"China Accounting Voucher",
		{
			"source_key": f"Posting|{doc.doctype}|{doc.name}", "docstatus": 1,
			"company": company, "source_doctype": doc.doctype, "source_name": doc.name,
			"source_event": "Posting",
		},
		"name", for_update=True,
	)
	if not name:
		return None
	posting = frappe.get_doc("China Accounting Voucher", name, for_update=True)
	if (
		posting.status not in ("Posted", "Reversed") or posting.reversal_of
		or posting.currency != frappe.get_cached_value("Company", company, "default_currency")
	):
		return None
	return posting


def _voucher_entries_by_accounting_key(entries):
	"""Compare all four native-loaded amount columns without display rounding or netting."""
	key_fields = (
		"account", "account_currency", "party_type", "party", "cost_center", "project",
		"finance_book", "against_voucher_type", "against_voucher",
	)
	amount_fields = ("debit", "credit", "debit_in_account_currency", "credit_in_account_currency")
	result = {}
	for row in entries:
		if not row.get("account") or not row.get("account_currency"):
			return None
		try:
			dimensions = json.loads(row.get("dimensions_json") or "{}")
			if not isinstance(dimensions, dict):
				return None
			dimensions_key = json.dumps(dimensions, sort_keys=True, ensure_ascii=False, allow_nan=False)
			amounts = [Decimal(str(row.get(field) or 0)) for field in amount_fields]
		except (InvalidOperation, ValueError, TypeError):
			return None
		if not all(amount.is_finite() for amount in amounts):
			return None
		key = (*[row.get(field) or "" for field in key_fields], dimensions_key)
		totals = result.setdefault(key, [Decimal(0) for field in amount_fields])
		for index, amount in enumerate(amounts):
			totals[index] += amount
	return result or None


def _cancellation_audit_complete(doc, voucher_name):
	if not voucher_name or doc.docstatus != 2:
		return False
	filters = {
		"name": voucher_name, "docstatus": 1, "company": get_company(doc),
		"source_doctype": doc.doctype, "source_name": doc.name,
		"source_event": "Cancellation", "source_key": _cancellation_issue_key(doc),
	}
	name = frappe.db.get_value("China Accounting Voucher", filters, "name", for_update=True)
	if not name:
		return False
	cancellation = frappe.get_doc("China Accounting Voucher", name, for_update=True)
	posting = _get_cancellation_posting(doc)
	if not posting or (
		cancellation.reversal_of != posting.name or cancellation.currency != posting.currency
		or cancellation.status != "Posted"
		or (posting.status, posting.reversed_by) != ("Reversed", voucher_name)
		or cancellation.total_debit != posting.total_credit
		or cancellation.total_credit != posting.total_debit
	):
		return False
	expected = _voucher_entries_by_accounting_key(reverse_voucher_entries(posting.name))
	if not expected or _voucher_entries_by_accounting_key(cancellation.entries) != expected:
		return False
	return not frappe.db.get_value(
		"China Cash Flow Assignment",
		{"source_doctype": doc.doctype, "source_name": doc.name, "status": ["!=", "Cancelled"]},
		"name", for_update=True,
	)


def _empty_period_closing_cancellation_reason(doc):
	"""Recognize only a completed cancellation of a wholly empty calendar month.

	No ledger rows is not proof for an arbitrary source: historic GL may have
	been removed. Require the entire month to have no GL, including cancelled
	rows, and refuse any source audit or processing residue. Never net amounts
	or synthesize a Posting snapshot to satisfy the cancellation guard.
	"""
	if (
		doc.doctype != "Period Closing Voucher" or doc.docstatus != 2
		or doc.get("gle_processing_status") != "Completed" or doc.get("error_message")
		or not doc.get("period_start_date") or not doc.get("period_end_date")
	):
		return None
	from frappe.utils import get_first_day, get_last_day

	start, end = getdate(doc.period_start_date), getdate(doc.period_end_date)
	if start != get_first_day(start) or end != get_last_day(start):
		return None
	company = get_company(doc)
	if not company:
		return None
	source = {"source_doctype": doc.doctype, "source_name": doc.name}
	for doctype, filters in (
		("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name}),
		("GL Entry", {"company": company, "posting_date": ["between", [start, end]]}),
		("China Accounting Voucher", source),
		("China Accounting Voucher", {"source_key": ["in", [f"Posting|{doc.doctype}|{doc.name}", _cancellation_issue_key(doc)]]}),
		("China Cash Flow Assignment", source),
		("Account Closing Balance", {"period_closing_voucher": doc.name}),
	):
		if frappe.db.get_value(doctype, filters, "name", for_update=True):
			return None
	processes = frappe.db.get_values(
		"Process Period Closing Voucher", {"parent_pcv": doc.name},
		["name", "docstatus", "status", "p_l_closing_balance"], as_dict=True, for_update=True,
	)
	for process in processes:
		if (
			process.docstatus != 2 or process.status != "Cancelled"
			or frappe.parse_json(process.p_l_closing_balance) not in (None, {}, [])
		):
			return None
		for detail in frappe.db.get_values(
			"Process Period Closing Voucher Detail", {"parent": process.name},
			["status", "report_type", "closing_balance"], as_dict=True, for_update=True,
		):
			if detail.status not in ("Completed", "Cancelled") or detail.report_type not in ("Profit and Loss", "Balance Sheet"):
				return None
			if detail.report_type == "Profit and Loss" and frappe.parse_json(detail.closing_balance) not in (None, {}, []):
				return None
	return _("已核验空月份结转 {0}（{1} 至 {2}）：已取消且总账处理完成，期间无总账记录，来源无记账及审计残留；无需生成冲销快照。").format(doc.name, start, end)


@_mute_audit_messages()
def process_cancellation_snapshot(source_doctype, source_name, issue_name=None):
	"""Idempotently create the cancellation snapshot after the source cancellation commits."""
	if source_doctype not in GL_SOURCE_DOCTYPES or not frappe.db.get_value(source_doctype, source_name, "name", for_update=True):
		return {"status": "skipped", "reason": "source_not_found"}
	doc = frappe.get_doc(source_doctype, source_name, for_update=True)
	issue = frappe.get_doc("China Voucher Sync Issue", issue_name, for_update=True) if issue_name else _ensure_cancellation_sync_issue(doc)
	if (issue.source_doctype, issue.source_name, issue.company) != (doc.doctype, doc.name, get_company(doc)):
		frappe.throw(_("冲销同步记录与来源单据不匹配"))
	empty_reason = _empty_period_closing_cancellation_reason(doc) if not issue.cancellation_voucher else None
	# A locking source read alone does not refresh an older REPEATABLE READ
	# snapshot. Every audit read below must see the preceding worker's commit.
	if issue.status == "Resolved" and empty_reason:
		return {"status": "resolved", "issue": issue.name, "voucher": None, "reason": "empty_period_closing"}
	if issue.status == "Resolved" and _cancellation_audit_complete(doc, issue.cancellation_voucher):
		return {"status": "resolved", "issue": issue.name, "voucher": issue.cancellation_voucher}
	frappe.db.set_value(
		"China Voucher Sync Issue", issue.name,
		{"retry_count": cint(issue.retry_count) + 1, "last_attempted_on": now_datetime(), "last_error": None},
		update_modified=False,
	)
	# Keep the attempt counter outside the savepoint, but undo every audit
	# transition on failure before recording the pending retry. Native source
	# cancellation has already committed and is never rolled back here.
	save_point = f"china_cancellation_sync_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(save_point)
	retryable_failure = False
	try:
		if doc.docstatus != 2:
			raise frappe.ValidationError(_("来源单据尚未取消，不能生成冲销审计快照"))
		if empty_reason:
			issue.add_comment("Comment", empty_reason)
			frappe.db.set_value(
				"China Voucher Sync Issue", issue.name,
				{"status": "Resolved", "cancellation_voucher": None, "resolved_on": now_datetime(), "last_error": None},
				update_modified=False,
			)
			return {"status": "resolved", "issue": issue.name, "voucher": None, "reason": "empty_period_closing"}
		from china_finance.services.cash_flow_assignment import cancel_assignments_for_source

		cancel_assignments_for_source(doc)
		voucher_name = create_voucher_from_source(doc, "Cancellation")
		if not voucher_name:
			raise frappe.ValidationError(_("未找到可生成冲销审计快照的总账分录"))
		if not _cancellation_audit_complete(doc, voucher_name):
			raise frappe.ValidationError(_("冲销审计快照或来源凭证状态不完整，不能标记同步完成"))
		frappe.db.set_value(
			"China Voucher Sync Issue",
			issue.name,
			{
				"status": "Resolved", "cancellation_voucher": voucher_name,
				"resolved_on": now_datetime(), "last_error": None,
			},
			update_modified=False,
		)
		return {"status": "resolved", "issue": issue.name, "voucher": voucher_name}
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		# MariaDB may invalidate a transaction's older snapshot or roll back
		# a deadlock victim completely. Its owner must retry the transaction;
		# do not overwrite newer audit state or replace the original exception.
		retryable_failure = True
		raise
	except Exception as exc:
		try:
			frappe.db.rollback(save_point=save_point)
			_record_sync_failure(issue.name, exc)
			frappe.log_error(title=_("中国会计凭证冲销快照补齐失败"), message=frappe.get_traceback())
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			retryable_failure = True
			raise
		return {"status": "pending", "issue": issue.name, "error": str(exc)}
	finally:
		if not retryable_failure:
			frappe.db.release_savepoint(save_point)


def process_cancellation_snapshot_job(source_doctype, source_name, issue_name=None):
	"""Adapt concurrency failures to Frappe's bounded, transaction-owning worker retry."""
	try:
		return process_cancellation_snapshot(source_doctype, source_name, issue_name)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as exc:
		raise frappe.RetryBackgroundJobError(str(exc)) from exc


def get_pending_cancellation_sync_issues(company, from_date, to_date):
	issues = frappe.get_all(
		"China Voucher Sync Issue",
		filters={
			"company": company,
			"posting_date": ["between", [from_date, to_date]],
			"status": "Pending",
		},
		fields=["name", "source_doctype", "source_name", "last_error", "retry_count"],
	)
	pending = []
	for issue in issues:
		# A cancelled source may later be deleted in ERPNext. Keep the audit row,
		# but do not leave an impossible retry as a permanent month-end blocker.
		if not frappe.db.exists(issue.source_doctype, issue.source_name):
			frappe.db.set_value(
				"China Voucher Sync Issue",
				issue.name,
				{
					"status": "Resolved",
					"resolved_on": now_datetime(),
					"last_error": _("来源业务单据已删除，无需补齐冲销审计快照"),
				},
				update_modified=False,
			)
			continue
		pending.append(issue)
	return pending


@frappe.whitelist()
def retry_cancellation_snapshot(issue_name):
	frappe.only_for(SNAPSHOT_RETRY_ROLES)
	issue = frappe.get_doc("China Voucher Sync Issue", issue_name)
	if issue.status != "Pending":
		return {"status": issue.status, "issue": issue.name, "voucher": issue.cancellation_voucher}
	return process_cancellation_snapshot(issue.source_doctype, issue.source_name, issue.name)


@frappe.whitelist()
def get_source_snapshot_status(source_doctype, source_name):
	doc = _get_source_document(source_doctype, source_name)
	settings = get_company_settings(get_company(doc))
	if not settings:
		return {"not_applicable": True}
	voucher_name = frappe.db.get_value(
		"China Accounting Voucher", {"source_key": f"Posting|{source_doctype}|{source_name}"}, "name"
	)
	if not voucher_name:
		from china_finance.services.cash_equivalent_scope import get_cash_scope_accounts
		cash_accounts = get_cash_scope_accounts(get_company(doc), now_datetime().date())
		has_cash_entry = bool(cash_accounts and frappe.db.exists(
			"GL Entry",
			{
				"voucher_type": source_doctype,
				"voucher_no": source_name,
				"account": ["in", cash_accounts],
				"is_cancelled": 0,
			},
		))
		return {
			"snapshot_ready": False,
			"can_create_assignment": has_cash_entry,
			"reason": _("审计快照尚未生成"),
		}
	voucher = frappe.get_doc("China Accounting Voucher", voucher_name)
	assignment = frappe.db.get_value(
		"China Cash Flow Assignment",
		{"china_accounting_voucher": voucher.name},
		["name", "status"],
		as_dict=True,
		order_by="revision desc, creation desc",
	)
	from china_finance.services.cash_flow_assignment import get_cash_legs_for_voucher

	cash_legs = get_cash_legs_for_voucher(voucher)
	needs_assignment = bool(doc.docstatus == 1 and cash_legs)
	return {
		"snapshot_ready": voucher.docstatus == 1,
		"snapshot_name": voucher.name,
		"statutory_number": voucher.statutory_number,
		"can_view_snapshot": voucher.has_permission("read"),
		"assignment": assignment,
		"assignment_required": needs_assignment,
		"assignment_reason": _("该单据不包含现金/银行分录") if not needs_assignment and not assignment else "",
	}


@frappe.whitelist()
def create_cash_flow_assignment_from_source(source_doctype, source_name):
	frappe.only_for(("Accounts User", "China Finance User", "Accounts Manager", "China Finance Manager", "System Manager"))
	doc = _get_source_document(source_doctype, source_name)
	# The public missing-snapshot path can create a voucher before cash-flow
	# creation acquires its gate. Lock the source first, as cancellation does,
	# and recheck current state/permission after waiting for another writer.
	doc = frappe.get_doc(source_doctype, source_name, for_update=True)
	doc.check_permission("read")
	if doc.docstatus != 1:
		frappe.throw(_("仅已提交来源单据可以指定现金流量"))
	voucher_name = frappe.db.get_value(
		"China Accounting Voucher", {"source_key": f"Posting|{source_doctype}|{source_name}", "docstatus": 1}, "name",
		for_update=True,
	)
	if not voucher_name:
		voucher_name = create_voucher_from_source(doc, "Posting", force=True)
	if not voucher_name:
		frappe.throw(_("审计快照尚未生成，且来源单据没有可用总账分录"))
	from china_finance.services.cash_flow_assignment import create_cash_flow_assignment

	name = create_cash_flow_assignment(voucher_name, source=(doc.doctype, doc.name))
	if not name:
		frappe.throw(_("该单据不包含需要指定的外部现金流"))
	return {"name": name}


@frappe.whitelist()
def recreate_cash_flow_assignment_from_source(source_doctype, source_name):
	frappe.only_for(("Accounts User", "China Finance User", "Accounts Manager", "China Finance Manager", "System Manager"))
	_get_source_document(source_doctype, source_name)
	voucher_name = frappe.db.get_value(
		"China Accounting Voucher", {"source_key": f"Posting|{source_doctype}|{source_name}", "docstatus": 1}, "name"
	)
	if not voucher_name:
		frappe.throw(_("审计快照尚未生成"))
	assignment_name = frappe.db.get_value(
		"China Cash Flow Assignment",
		{"china_accounting_voucher": voucher_name, "status": "Cancelled"},
		"name",
		order_by="revision desc, creation desc",
	)
	if not assignment_name:
		frappe.throw(_("没有可重新创建的已作废现金流量指定单"))
	from china_finance.services.cash_flow_assignment import recreate_cash_flow_assignment

	return recreate_cash_flow_assignment(assignment_name)


def create_voucher_from_source(doc, source_event="Posting", force=False):
	company = get_company(doc)
	settings = get_company_settings(company)
	if not settings:
		return None
	posting_date = get_posting_date(doc)
	if posting_date < getdate(settings.activation_date) and not force:
		return None

	source_key = f"{source_event}|{doc.doctype}|{doc.name}"
	existing = frappe.db.get_value(
		"China Accounting Voucher", {"source_key": source_key}, "name", for_update=source_event == "Cancellation",
	)
	if existing:
		return existing

	reversal_of = None
	if source_event == "Cancellation":
		if doc.docstatus != 2 or frappe.db.get_value(doc.doctype, doc.name, "docstatus", for_update=True) != 2:
			frappe.throw(_("来源单据尚未取消，不能生成冲销审计快照"))
		posting = _get_cancellation_posting(doc)
		if not posting or posting.status != "Posted" or posting.reversed_by:
			frappe.throw(_("未找到可信的原始已提交 Posting 快照，不能生成冲销审计快照"))
		# Native cancellation marks both the original and its swapped reversal
		# GL rows cancelled. Reading all cancelled GL would cancel out the audit
		# itself; only the original immutable Posting snapshot is authoritative.
		reversal_of = posting.name
		entries = reverse_voucher_entries(reversal_of)
		if not _voucher_entries_by_accounting_key(entries):
			frappe.throw(_("原始已提交 Posting 快照分录或会计维度无效，不能生成冲销审计快照"))
	else:
		entries = get_gl_entries(doc.doctype, doc.name)
		entries = restore_negative_journal_entry_debits(doc, entries, source_event)
	if not entries:
		return None

	voucher_word = classify_voucher_word(entries, settings)
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	voucher = frappe.get_doc(
		{
			"doctype": "China Accounting Voucher",
			"company": company,
			"posting_date": posting_date,
			"voucher_word": voucher_word,
			"source_doctype": doc.doctype,
			"source_name": doc.name,
			"source_event": source_event,
			"source_key": source_key,
			"reversal_of": reversal_of,
			"prepared_by": doc.owner,
			"reviewed_by": get_reviewer(doc.doctype, doc.name),
			"posted_by": frappe.session.user,
			"currency": company_currency,
			"remarks": getattr(doc, "remarks", None) or getattr(doc, "user_remark", None),
			"entries": entries,
		}
	)
	voucher.flags.ignore_permissions = True
	# The cancellation snapshot must retain the link to its cancelled source
	# document. Frappe normally rejects links to cancelled documents, but this
	# audit snapshot is created precisely because the source was cancelled.
	if source_event == "Cancellation":
		voucher.flags.ignore_links = True
	voucher.insert()
	voucher.submit()
	if source_event == "Posting":
		_sync_source_voucher_number(doc.doctype, doc.name, voucher.statutory_number)
		from china_finance.services.cash_flow_assignment import create_assignment_if_required

		create_assignment_if_required(voucher.name)
	if reversal_of:
		frappe.db.set_value(
			"China Accounting Voucher",
			reversal_of,
			{"status": "Reversed", "reversed_by": voucher.name},
			update_modified=False,
		)
	return voucher.name


def get_gl_entries(voucher_type, voucher_no, cancelled=False):
	filters = {"voucher_type": voucher_type, "voucher_no": voucher_no}
	if frappe.db.has_column("GL Entry", "is_cancelled"):
		filters["is_cancelled"] = 1 if cancelled else 0
	rows = frappe.get_all(
		"GL Entry",
		filters=filters,
		fields=[
			"name", "account", "account_currency", "debit", "credit",
			"debit_in_account_currency", "credit_in_account_currency", "party_type", "party",
			"cost_center", "project", "finance_book", "against_voucher_type", "against_voucher", "remarks",
		],
		order_by="creation asc, name asc",
	)
	return [to_voucher_entry(row) for row in rows]


def restore_negative_journal_entry_debits(doc, entries, source_event="Posting"):
	"""Preserve a negative debit used for an accounting offset in snapshots.

	ERPNext normalizes a negative debit to a positive GL credit while posting.
	The source Journal Entry still retains the original debit-column sign, which
	is required by the Chinese voucher presentation for interest offsets.
	"""
	if getattr(doc, "doctype", None) != "Journal Entry" or source_event != "Posting":
		return entries

	negative_rows = [
		row for row in doc.accounts
		if flt(row.debit) < -0.005 and abs(flt(row.credit)) <= 0.005
	]
	if not negative_rows:
		return entries

	used_entries = set()
	for source_row in negative_rows:
		candidate = next(
			(
				entry for entry in entries
				if id(entry) not in used_entries
				and entry.get("account") == source_row.account
				and abs(flt(entry.get("debit"))) <= 0.005
				and flt(entry.get("credit")) > 0.005
				and abs(flt(entry.get("credit")) - abs(flt(source_row.debit))) <= 0.005
				and abs(flt(entry.get("credit_in_account_currency")) - abs(flt(source_row.debit_in_account_currency))) <= 0.005
			),
			None,
		)
		if not candidate:
			continue
		used_entries.add(id(candidate))
		candidate["debit"] = source_row.debit
		candidate["credit"] = 0
		candidate["debit_in_account_currency"] = source_row.debit_in_account_currency
		candidate["credit_in_account_currency"] = 0
		candidate["remarks"] = source_row.user_remark or candidate.get("remarks")

	return entries


def repair_negative_debit_snapshot(source_name, company=None):
	"""Repair an existing posted Journal Entry snapshot once after sign loss."""
	journal_entry = frappe.get_doc("Journal Entry", source_name)
	if company and journal_entry.company != company:
		frappe.throw(_("来源凭证公司不匹配：{0}").format(source_name))
	snapshot_name = frappe.db.get_value(
		"China Accounting Voucher",
		{"source_doctype": "Journal Entry", "source_name": source_name, "source_event": "Posting"},
		"name",
	)
	if not snapshot_name:
		frappe.throw(_("未找到来源凭证对应的中国会计凭证：{0}").format(source_name))

	snapshot = frappe.get_doc("China Accounting Voucher", snapshot_name)
	entries = restore_negative_journal_entry_debits(
		journal_entry, get_gl_entries("Journal Entry", source_name), "Posting"
	)
	changed = 0
	if len(snapshot.entries) != len(entries):
		frappe.throw(_("中国会计凭证分录数量与总账不一致：{0}").format(source_name))
	for current, entry in zip(snapshot.entries, entries):
		if current.account != entry["account"]:
			frappe.throw(_("中国会计凭证分录顺序与总账不一致：{0}").format(source_name))
		values = {
			"debit": entry["debit"],
			"credit": entry["credit"],
			"debit_in_account_currency": entry["debit_in_account_currency"],
			"credit_in_account_currency": entry["credit_in_account_currency"],
			"remarks": entry["remarks"],
		}
		if any(flt(current.get(fieldname)) != flt(value) for fieldname, value in values.items() if fieldname != "remarks"):
			changed += 1
		frappe.db.set_value("China Accounting Voucher Entry", current.name, values, update_modified=False)

	total_debit = sum(flt(entry["debit"], 2) for entry in entries)
	total_credit = sum(flt(entry["credit"], 2) for entry in entries)
	frappe.db.set_value(
		"China Accounting Voucher",
		snapshot.name,
		{
			"total_debit": total_debit,
			"total_credit": total_credit,
			"source_hash": calculate_entries_hash(entries),
		},
		update_modified=False,
	)
	frappe.db.commit()
	return {
		"snapshot": snapshot.name,
		"source": source_name,
		"changed_rows": changed,
		"total_debit": total_debit,
		"total_credit": total_credit,
	}


def to_voucher_entry(row):
	known_fields = {
		"name", "account", "account_currency", "debit", "credit", "debit_in_account_currency",
		"credit_in_account_currency", "party_type", "party", "cost_center", "project", "finance_book",
		"against_voucher_type", "against_voucher", "remarks",
	}
	return {
		"gl_entry": row.name,
		"account": row.account,
		"account_currency": row.account_currency,
		"debit": row.debit,
		"credit": row.credit,
		"debit_in_account_currency": row.debit_in_account_currency,
		"credit_in_account_currency": row.credit_in_account_currency,
		"party_type": row.party_type,
		"party": row.party,
		"cost_center": row.cost_center,
		"project": row.project,
		"finance_book": row.finance_book,
		"against_voucher_type": row.against_voucher_type,
		"against_voucher": row.against_voucher,
		"remarks": row.remarks,
		"dimensions_json": json.dumps({key: value for key, value in row.items() if key not in known_fields and value}, ensure_ascii=False),
	}


def reverse_voucher_entries(voucher_name):
	doc = frappe.get_doc("China Accounting Voucher", voucher_name, for_update=True)
	return [
		{
			"account": row.account,
			"account_currency": row.account_currency,
			"debit": row.credit,
			"credit": row.debit,
			"debit_in_account_currency": row.credit_in_account_currency,
			"credit_in_account_currency": row.debit_in_account_currency,
			"party_type": row.party_type,
			"party": row.party,
			"cost_center": row.cost_center,
			"project": row.project,
			"finance_book": row.finance_book,
			"against_voucher_type": row.against_voucher_type,
			"against_voucher": row.against_voucher,
			"remarks": _("冲销 {0}").format(doc.statutory_number),
			"dimensions_json": row.dimensions_json,
		}
		for row in doc.entries
	]


def classify_voucher_word(entries, settings):
	return "记"


def _sync_source_voucher_number(source_doctype, source_name, statutory_number):
	if source_doctype not in FORMAL_VOUCHER_SOURCES or not source_name:
		return
	if not frappe.db.has_column(source_doctype, "custom_china_voucher_number"):
		return
	frappe.db.set_value(
		source_doctype,
		source_name,
		"custom_china_voucher_number",
		statutory_number,
		update_modified=False,
	)


def _formal_sequence_key(voucher):
	return "|".join((voucher.company, voucher.fiscal_year, voucher.accounting_period, voucher.voucher_word, "formal"))


def _get_or_create_formal_sequence(voucher, sequence_key):
	row = frappe.db.sql(
		"SELECT name, current_value, numbering_revision FROM `tabChina Voucher Sequence` WHERE sequence_key=%s FOR UPDATE",
		(sequence_key,),
		as_dict=True,
	)
	if row:
		return row[0]

	try:
		frappe.get_doc(
			{
				"doctype": "China Voucher Sequence",
				"sequence_key": sequence_key,
				"company": voucher.company,
				"fiscal_year": voucher.fiscal_year,
				"accounting_period": voucher.accounting_period,
				"voucher_word": voucher.voucher_word,
				"current_value": 0,
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		pass

	row = frappe.db.sql(
		"SELECT name, current_value, numbering_revision FROM `tabChina Voucher Sequence` WHERE sequence_key=%s FOR UPDATE",
		(sequence_key,),
		as_dict=True,
	)
	if not row:
		frappe.throw(_("无法创建凭证字号流水号记录：{0}").format(sequence_key))
	return row[0]


def _get_inherited_formal_number(voucher, numbering_revision=0):
	"""Return the original posting number for an amended formal source document."""
	if voucher.source_doctype == "Period Closing Voucher":
		# Reclosing after supplementary postings must take the new month-end
		# number, not inherit an earlier closing's number ahead of those postings.
		return None
	if voucher.source_doctype not in FORMAL_VOUCHER_SOURCES:
		return None
	if not frappe.db.has_column(voucher.source_doctype, "amended_from"):
		return None
	# The transaction may have started a consistent read before waiting for
	# number organization. Both lookups must see the current numbering version.
	amended_from = frappe.db.get_value(
		voucher.source_doctype, voucher.source_name, "amended_from", for_update=True
	)
	if not amended_from:
		return None
	original = frappe.db.get_value(
		"China Accounting Voucher",
		{
			"source_key": f"Posting|{voucher.source_doctype}|{amended_from}",
			"source_event": "Posting",
			"docstatus": 1,
		},
		[
			"company",
			"fiscal_year",
			"accounting_period",
			"voucher_word",
			"sequence_number",
			"statutory_number",
			"source_key",
			"voucher_key",
		],
		as_dict=True,
		for_update=True,
	)
	if not original:
		return None
	if any(
		str(original.get(fieldname) or "") != str(voucher.get(fieldname) or "")
		for fieldname in ("company", "fiscal_year", "accounting_period", "voucher_word")
	):
		# Moving an amendment to another fiscal period/凭证字 starts a new sequence.
		return None
	if numbering_revision and original.voucher_key != _organized_voucher_key(original, numbering_revision):
		# A source cancelled before the latest organization no longer owns its old number.
		return None
	return original


def _get_formal_high_water_mark(voucher, active_only=False):
	"""Find the largest existing formal number without renumbering history."""
	row = frappe.db.sql(
		"""
		SELECT COALESCE(MAX(sequence_number), 0) AS max_sequence
		FROM `tabChina Accounting Voucher` FORCE INDEX (`company_voucher_period_date`)
		WHERE company=%s AND fiscal_year=%s AND accounting_period=%s
			AND voucher_word=%s AND source_event IN ('Posting', 'Manual')
			AND docstatus=1
			AND (%s=0 OR status='Posted')
		FOR UPDATE
		""",
		(voucher.company, voucher.fiscal_year, voucher.accounting_period, voucher.voucher_word, int(active_only)),
		as_dict=True,
	)
	return cint(row[0].max_sequence) if row else 0


def _next_formal_sequence(sequence_value, high_water_mark):
	"""Return the next number while preserving all previously assigned numbers."""
	return max(cint(sequence_value), cint(high_water_mark)) + 1


def _build_amendment_voucher_key(voucher):
	"""Keep an amended snapshot unique even when it inherits the original字号."""
	return f"amendment|{voucher.source_key}"


def _organized_voucher_key(voucher, revision):
	scope = hashlib.sha256(_formal_sequence_key(voucher).encode()).hexdigest()
	source = hashlib.sha256(voucher.source_key.encode()).hexdigest()
	return f"organized|{scope}|{revision}|{cint(voucher.sequence_number):08d}|{source}"


def assign_voucher_number(voucher):
	if voucher.voucher_key:
		return
	if voucher.source_event == "Cancellation":
		# A cancellation is an audit snapshot, not a new formal accounting voucher.
		voucher.sequence_number = 0
		voucher.statutory_number = None
		voucher.voucher_key = f"cancellation|{voucher.source_key}"
		return
	from china_finance.services.opening_identity import is_opening_source

	if is_opening_source(voucher.source_doctype, voucher.source_name, voucher.company):
		voucher.sequence_number = 0
		voucher.statutory_number = None
		voucher.voucher_key = f"opening|{voucher.source_key}"
		return
	if voucher.source_doctype not in FORMAL_VOUCHER_SOURCES:
		# Business-document snapshots remain available for ledger tracing, but
		# must not consume the formal accounting-voucher sequence.
		voucher.sequence_number = 0
		voucher.statutory_number = None
		voucher.voucher_key = f"business|{voucher.company}|{voucher.source_doctype}|{voucher.source_name}|{voucher.source_event}"
		return
	settings = get_company_settings(voucher.company)
	if not settings:
		frappe.throw(_("公司 {0} 未启用中国财务设置").format(voucher.company))
	sequence_key = _formal_sequence_key(voucher)
	sequence_row = _get_or_create_formal_sequence(voucher, sequence_key)
	revision = cint(sequence_row.get("numbering_revision"))
	inherited = _get_inherited_formal_number(voucher, revision)
	if inherited and inherited.statutory_number:
		voucher.sequence_number = inherited.sequence_number
		voucher.statutory_number = inherited.statutory_number
		voucher.voucher_key = _organized_voucher_key(voucher, revision) if revision else _build_amendment_voucher_key(voucher)
		return

	sequence = _next_formal_sequence(
		sequence_row.get("current_value"), _get_formal_high_water_mark(voucher, active_only=bool(revision))
	)
	voucher.sequence_number = sequence
	voucher.statutory_number = f"{voucher.voucher_word}{sequence}"
	voucher.voucher_key = _organized_voucher_key(voucher, revision) if revision else f"{sequence_key}|{sequence:08d}"
	frappe.db.set_value(
		"China Voucher Sequence",
		sequence_row["name"],
		"current_value",
		sequence,
		update_modified=False,
	)


def backfill_period_closing_voucher_numbers(company=None, apply=False):
	"""Number active, unnumbered closing snapshots after each month's existing vouchers.

	The default is a read-only preview. Cancelled closings and superseded audit
	snapshots keep their history; existing formal numbers are never reassigned.
	The caller owns the transaction, including the sequence and source backlink.
	"""
	frappe.only_for(SNAPSHOT_RETRY_ROLES)
	rows = frappe.db.sql(
		"""
		SELECT v.name, v.company, v.source_name, pcv.period_end_date
		FROM `tabChina Accounting Voucher` v
		INNER JOIN `tabPeriod Closing Voucher` pcv ON pcv.name=v.source_name
		WHERE v.source_doctype='Period Closing Voucher' AND v.source_event='Posting'
			AND v.source_key=CONCAT('Posting|Period Closing Voucher|', pcv.name)
			AND v.docstatus=1 AND v.status='Posted' AND pcv.docstatus=1
			AND v.company=pcv.company AND COALESCE(v.statutory_number, '')=''
			AND (%(company)s IS NULL OR v.company=%(company)s)
		ORDER BY v.company, pcv.period_end_date, v.creation, v.name
		""",
		{"company": company},
		as_dict=True,
	)
	result = []
	for row in rows:
		if not get_company_settings(row.company):
			continue
		if not cint(apply):
			result.append(row)
			continue

		# Serialize against cancellation and another migration of the same source.
		frappe.db.sql("SELECT name FROM `tabPeriod Closing Voucher` WHERE name=%s FOR UPDATE", row.source_name)
		frappe.db.sql("SELECT name FROM `tabChina Accounting Voucher` WHERE name=%s FOR UPDATE", row.name)
		source = frappe.get_doc("Period Closing Voucher", row.source_name)
		voucher = frappe.get_doc("China Accounting Voucher", row.name)
		if (
			source.docstatus != 1 or voucher.docstatus != 1 or voucher.status != "Posted"
			or voucher.statutory_number
			or voucher.source_key != f"Posting|Period Closing Voucher|{source.name}"
		):
			continue
		if voucher.sequence_number or (voucher.voucher_key and not voucher.voucher_key.startswith("business|")):
			frappe.throw(_("期末结转快照 {0} 的原编号状态异常，请先核查").format(voucher.name))

		voucher.posting_date = get_posting_date(source)
		voucher.fiscal_year = str(voucher.posting_date.year)
		voucher.accounting_period = voucher.posting_date.strftime("%Y-%m")
		voucher.voucher_key = None
		assign_voucher_number(voucher)
		# This controlled metadata migration does not resubmit or recreate entries.
		frappe.db.set_value(
			"China Accounting Voucher", voucher.name,
			{fieldname: voucher.get(fieldname) for fieldname in (
				"posting_date", "fiscal_year", "accounting_period",
				"sequence_number", "statutory_number", "voucher_key",
			)},
			update_modified=False,
		)
		_sync_source_voucher_number(source.doctype, source.name, voucher.statutory_number)
		voucher.add_comment(
			"Info", _("按所属月份补齐期末结转凭证字号：{0}；会计分录未变更。").format(voucher.statutory_number)
		)
		result.append({**row, "accounting_period": voucher.accounting_period, "statutory_number": voucher.statutory_number})
	return result


def calculate_entries_hash(entries):
	payload = []
	for row in entries:
		getter = row.get if hasattr(row, "get") else lambda key: getattr(row, key, None)
		payload.append(
			{
				"account": getter("account"),
				"debit": str(Decimal(str(getter("debit") or 0)).quantize(Decimal("0.01"))),
				"credit": str(Decimal(str(getter("credit") or 0)).quantize(Decimal("0.01"))),
				"party_type": getter("party_type"),
				"party": getter("party"),
				"cost_center": getter("cost_center"),
				"project": getter("project"),
				"finance_book": getter("finance_book"),
			}
		)
	canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
	return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_reviewer(doctype, docname):
	if not frappe.db.exists("DocType", "Workflow Action"):
		return None
	return frappe.db.get_value(
		"Workflow Action",
		{"reference_doctype": doctype, "reference_name": docname, "status": "Completed", "completed_by": ["is", "set"]},
		"completed_by",
		order_by="modified desc",
	)


@frappe.whitelist()
def rebuild_missing_vouchers(company, from_date=None, to_date=None, limit=500):
	frappe.only_for(("System Manager", "China Finance Manager"))
	settings = get_company_settings(company)
	if not settings:
		frappe.throw(_("公司尚未启用中国财务"))
	filters = {"company": company, "is_cancelled": 0}
	filters["posting_date"] = ["between", [from_date or settings.activation_date, to_date or frappe.utils.today()]]
	rows = frappe.get_all(
		"GL Entry",
		filters=filters,
		fields=["voucher_type", "voucher_no"],
		group_by="voucher_type, voucher_no",
		limit_page_length=cint(limit),
	)
	result = {"processed": 0, "created": 0, "skipped": 0, "errors": []}
	for row in rows:
		result["processed"] += 1
		if not frappe.db.exists("DocType", row.voucher_type) or not frappe.db.exists(row.voucher_type, row.voucher_no):
			result["skipped"] += 1
			continue
		try:
			name = create_voucher_from_source(frappe.get_doc(row.voucher_type, row.voucher_no))
			result["created"] += int(bool(name))
		except Exception as exc:
			result["errors"].append({"doctype": row.voucher_type, "name": row.voucher_no, "error": str(exc)})
	return result


def backfill_enabled_company_vouchers(company, limit=200):
	"""Create missing historical snapshots without relying on activation_date."""
	frappe.only_for(SNAPSHOT_RETRY_ROLES)
	settings = get_company_settings(company)
	if not settings:
		return {"processed": 0, "created": 0, "skipped": 0, "errors": []}
	rows = frappe.db.sql(
		"""
		SELECT gl.voucher_type, gl.voucher_no
		FROM `tabGL Entry` gl
		LEFT JOIN `tabChina Accounting Voucher` voucher
			ON voucher.source_key=CONCAT('Posting|', gl.voucher_type, '|', gl.voucher_no)
		WHERE gl.company=%(company)s
			AND gl.is_cancelled=0
			AND gl.voucher_type IN %(voucher_types)s
			AND voucher.name IS NULL
		GROUP BY gl.voucher_type, gl.voucher_no
		ORDER BY MIN(gl.posting_date), gl.voucher_type, gl.voucher_no
		LIMIT %(limit)s
		""",
		{"company": company, "voucher_types": GL_SOURCE_DOCTYPES, "limit": cint(limit)},
		as_dict=True,
	)
	result = {"processed": 0, "created": 0, "skipped": 0, "errors": []}
	for row in rows:
		result["processed"] += 1
		try:
			if not frappe.db.exists(row.voucher_type, row.voucher_no):
				result["skipped"] += 1
				continue
			name = create_voucher_from_source(
				frappe.get_doc(row.voucher_type, row.voucher_no), force=True
			)
			result["created"] += int(bool(name))
		except Exception as exc:
			result["errors"].append({"doctype": row.voucher_type, "name": row.voucher_no, "error": str(exc)})
	return result
