"""Resumable month-end steps. Each request commits at most one source voucher."""

import json

import frappe
from frappe import _
from frappe.utils import get_first_day, get_last_day, getdate, now_datetime

from china_finance.services import voucher_preparation as preparation

RUN = "China Closing Run"
ACTIVE = ("Posting", "Checking", "Closing")
FIELDS = ("preparation_state", "preparation_data", "preparation_message")
ROLES = ("System Manager", "China Finance Manager")


def lock_company(company):
	frappe.db.sql("SELECT name FROM `tabCompany` WHERE name=%s FOR UPDATE", (company,))


def guard_period_write(doc, method=None):
	from china_finance.services.voucher import GL_SOURCE_DOCTYPES, get_posting_date

	if doc.doctype not in (
		*GL_SOURCE_DOCTYPES,
		"China Bank Receipt Import",
		"Bank Transaction",
		"Stock Reconciliation",
		"Invoice Discounting",
		"Landed Cost Voucher",
		"GL Entry",
	) or not doc.get("company"):
		return
	companies = {doc.company}
	old = doc.get_doc_before_save()
	if old and old.get("company"):
		companies.add(old.company)
	for company in sorted(companies):
		lock_company(company)
		if not frappe.db.has_column(RUN, "preparation_state"):
			continue
		active = frappe.db.sql(
			"""SELECT name, from_date, to_date, status, reversal_group, period_closing_voucher FROM `tabChina Closing Run`
			WHERE company=%s AND ((docstatus=0 AND preparation_state IN ('Posting','Checking','Closing'))
			OR (docstatus=1 AND status IN ('Reopening','Reopen Failed')))
			FOR UPDATE""",
			company,
			as_dict=True,
		)
		source_type = doc.get("voucher_type") if doc.doctype == "GL Entry" else doc.doctype
		source_name = doc.get("voucher_no") if doc.doctype == "GL Entry" else doc.name
		group = getattr(frappe.flags, "china_closing_reversal", None)
		own_reversal = source_type == "Period Closing Voucher" and group and any(
			run.reversal_group == group and run.period_closing_voucher == source_name for run in active
		)
		for run in active:
			if own_reversal and run.reversal_group == group and run.status in ("Reopening", "Reopen Failed"):
				continue
			if getattr(frappe.flags, "china_month_end_run", None) == run.name:
				continue
			dates = [] if doc.doctype == "China Bank Receipt Import" else [get_posting_date(doc)]
			if doc.doctype == "Bank Transaction":
				dates = [getdate(doc.date)]
			if old:
				dates.append(
					getdate(old.date) if doc.doctype == "Bank Transaction" else get_posting_date(old)
				)
			in_period = (
				doc.doctype == "China Bank Receipt Import"
				or not dates
				or any(getdate(run.from_date) <= date <= getdate(run.to_date) for date in dates)
			)
			if not in_period and doc.doctype == "GL Entry" and source_type and source_name:
				# Immutable-ledger cancellation posts its reversal on today's date.
				# Also guard the source's original period, including cancelled GL.
				in_period = frappe.db.exists("GL Entry", {
					"company": company, "voucher_type": source_type, "voucher_no": source_name,
					"posting_date": ["between", [run.from_date, run.to_date]],
				})
			if in_period:
				if run.status in ("Reopening", "Reopen Failed"):
					frappe.throw(_("本期总账撤销尚未完成，请先处理月末结账单 {0}").format(run.name))
				frappe.throw(
					_("本期正在进行月末记账（{0}），请先在月末结账中暂停，再修改或导入。").format(run.name)
				)


def protect_run_fields(doc):
	old = doc.get_doc_before_save()
	for field in FIELDS:
		doc.set(field, old.get(field) if old else None)
	if old and old.get("preparation_state"):
		if any(doc.get(f) != old.get(f) for f in ("company", "from_date", "to_date", "closing_type")):
			frappe.throw(_("月末结账已开始，不能修改公司或期间，请新建正确期间的结账单"))


def _run(name, *, for_pause=False):
	frappe.only_for(ROLES)
	doc = frappe.get_doc(RUN, name)
	doc.check_permission("write")
	preparation.check_company(doc.company)
	lock_company(doc.company)
	company = doc.company
	doc = frappe.get_doc(RUN, name, for_update=True)
	if doc.company != company:
		frappe.throw(_("单据已被修改，请刷新后重试"), frappe.TimestampMismatchError)
	if doc.docstatus != 0:
		frappe.throw(_("该期间已经结账或作废"))
	# Pausing is a recovery action, including for non-canonical legacy duplicates.
	# It still requires role, company/document permission, locks and draft status.
	if for_pause:
		return doc
	from china_finance.services.closing_lifecycle import validate_period
	validate_period(doc)
	if not preparation.mode_enabled(doc.company, doc.from_date):
		frappe.throw(_("请先启用凭证准备模式，且本期须在生效日期之后"))
	if (
		doc.closing_type != "Monthly"
		or getdate(doc.from_date) != get_first_day(doc.from_date)
		or getdate(doc.to_date) != get_last_day(doc.from_date)
	):
		frappe.throw(_("月末结账请选择完整自然月"))
	other = frappe.db.sql(
		"""SELECT name FROM `tabChina Closing Run` WHERE company=%s AND name!=%s
		AND docstatus=0 AND preparation_state IN ('Posting','Checking','Closing') FOR UPDATE""",
		(doc.company, doc.name),
	)
	if other:
		frappe.throw(_("公司还有进行中的月末结账：{0}").format(other[0][0]))
	return doc


def _store(doc, state, data, message):
	doc.db_set(
		{
			"preparation_state": state,
			"preparation_data": json.dumps(data, ensure_ascii=False, default=str),
			"preparation_message": message,
		}
	)
	return {
		"name": doc.name,
		"state": state,
		"message": message,
		"completed": len(data.get("done", [])),
		"total": len(data.get("items", [])),
		"period_closing_voucher": doc.period_closing_voucher,
		"checks": data.get("checks", []),
	}


def _drafts(doc):
	drafts = preparation.draft_documents(doc.company, doc.from_date, doc.to_date)
	count = frappe.db.count(
		"Journal Entry",
		{"company": doc.company, "docstatus": 0, "posting_date": ["between", [doc.from_date, doc.to_date]], "is_opening": ["!=", "Yes"]},
	)
	if count != len(drafts):
		frappe.throw(_("没有读取本期全部凭证的权限，不能完成月末记账"), frappe.PermissionError)
	return drafts


def _preflight(doc):
	from china_finance.services.bank_receipt_import import pending_receipts
	from china_finance.services.closing import run_closing_checks

	blocked = pending_receipts(doc.company, doc.from_date, doc.to_date, allow_drafts=True)
	if blocked:
		frappe.throw(_("回单仍有待处理项：{0}").format("、".join(r.transaction_id for r in blocked[:20])))
	checks = run_closing_checks(
		doc.company, doc.from_date, doc.to_date, doc.period_closing_voucher, doc.closing_type
	)
	# These inputs are independent of the ledgers being posted. Other checks run again afterwards.
	config_codes = {"CONFIGURATION_READINESS", "VOUCHER_HASH"}
	bad = [
		r
		for r in checks
		if not r["passed"]
		and r["severity"] == "Blocking"
		and (r["check_code"] in config_codes or "TEMPLATE" in r["check_code"])
	]
	if bad:
		frappe.throw("；".join(r["description"] + "：" + r["details"] for r in bad))
	if frappe.db.count(
		"Journal Entry", {"company": doc.company, "docstatus": 0, "posting_date": ["<", doc.from_date], "is_opening": ["!=", "Yes"]}
	):
		frappe.throw(_("存在更早期间的未记账凭证，请先核对并处理"))
	drafts = _drafts(doc)
	items = []
	for voucher in drafts:
		voucher.check_permission("submit")
		preparation.validate_draft(voucher)
		voucher.reload()
		if not preparation.is_ready(voucher):
			frappe.throw(_("{0} 尚未核对或内容已修改，请在查凭证中核对完成").format(voucher.name))
		items.append({"name": voucher.name, "hash": preparation.content_hash(voucher)})
	return items


@frappe.whitelist(methods=["POST"])
def start(name):
	doc = _run(name)
	data = json.loads(doc.preparation_data or "{}")
	if doc.preparation_state in ACTIVE:
		return _store(doc, doc.preparation_state, data, doc.preparation_message)
	point = "month_start"
	frappe.db.savepoint(point)
	items = _preflight(doc)
	# Never reuse a stale approved manifest after a pause. Already posted sources remain posted.
	done = []
	superseded = data.get("superseded", [])
	for item in data.get("done", []):
		state = frappe.db.get_value("Journal Entry", item["name"], "docstatus")
		if state == 2:
			superseded.append(item)
		elif state == 1:
			done.append(item)
		else:
			frappe.throw(_("原批次凭证 {0} 状态异常，请核对历史处理记录").format(item["name"]))
	data = {
		"items": [{"name": row["name"], "hash": row.get("hash")} for row in done] + items,
		"done": done,
		"superseded": superseded,
		"started_by": frappe.session.user,
		"started_on": str(now_datetime()),
	}
	return _store(doc, "Posting", data, _("凭证范围检查完成，开始月末记账"))


@frappe.whitelist(methods=["POST"])
def pause(name):
	doc = _run(name, for_pause=True)
	data = json.loads(doc.preparation_data or "{}")
	return _store(doc, "Paused", data, _("已暂停。已记账凭证保留，继续前将重新检查剩余草稿。"))


def _verify_posted(name):
	if frappe.db.get_value("Journal Entry", name, "docstatus") != 1 or not frappe.db.exists(
		"China Accounting Voucher",
		{"source_key": "Posting|Journal Entry|" + name, "docstatus": 1, "status": "Posted"},
	):
		frappe.throw(_("凭证 {0} 尚未记账或缺少有效会计快照").format(name))


def _post_one(doc, data):
	from china_finance.services.voucher import _complete_voucher_workflow

	item = data["items"][len(data["done"])]
	voucher = frappe.get_doc("Journal Entry", item["name"], for_update=True)
	voucher.check_permission("submit")
	if (
		voucher.docstatus != 0
		or preparation.content_hash(voucher) != item["hash"]
		or not preparation.is_ready(voucher)
	):
		frappe.throw(_("{0} 状态或核对版本已变化，请暂停后重新检查").format(voucher.name))
	_complete_voucher_workflow(voucher)
	_verify_posted(voucher.name)
	preparation.promote_cash_plan(voucher, confirm=False)
	voucher.add_comment("Comment", _("月末记账：{0}").format(doc.name))
	data["done"].append(item)
	return _store(doc, "Posting", data, _("已记账 {0}").format(voucher.name))


def _after_posting(doc, data):
	from china_finance.services.bank_receipt_import import receipt_status, reconcile_receipt
	from china_finance.services.cash_flow_assignment import get_closing_assignment_check
	from china_finance.services.closing import create_period_closing_voucher

	if _drafts(doc):
		frappe.throw(_("本期仍有未记账草稿，请暂停后重新检查"))
	for item in data["done"]:
		_verify_posted(item["name"])
		preparation.promote_cash_plan(frappe.get_doc("Journal Entry", item["name"]))
	for row in frappe.get_list(
		"China Bank Receipt",
		filters={"company": doc.company, "posting_date": ["between", [doc.from_date, doc.to_date]]},
		pluck="name",
		limit_page_length=0,
	):
		receipt = frappe.get_doc("China Bank Receipt", row)
		if receipt_status(receipt) == "已关联待核销":
			reconcile_receipt(receipt.name)
	cash_check = get_closing_assignment_check(doc.company, doc.from_date, doc.to_date, doc.closing_type)
	if not cash_check["passed"] and cash_check["severity"] == "Blocking":
		frappe.throw(_("现金流项目需要核对：{0}").format(cash_check["details"]))
	data["checks"] = [r for r in data.get("checks", []) if r["check_code"] != "CASH_FLOW_ASSIGNMENT"]
	data["checks"].append(cash_check)
	if (
		doc.period_closing_voucher
		and frappe.db.get_value("Period Closing Voucher", doc.period_closing_voucher, "docstatus") == 2
	):
		doc.db_set("period_closing_voucher", None)
	if not doc.period_closing_voucher:
		result = create_period_closing_voucher(
			doc.company, doc.from_date, doc.to_date, allow_first_period=True
		)
		doc.db_set("period_closing_voucher", result["name"])
	message = _("凭证已记账，开始损益结转")
	if not cash_check["passed"]:
		message += "；" + cash_check["details"]
	return _store(doc, "Closing", data, message)


def _close_profit(doc, data):
	from china_finance.services.closing import run_closing_checks, save_and_complete_period_closing_voucher
	from china_finance.services.voucher import create_voucher_from_source

	pcv = frappe.get_doc("Period Closing Voucher", doc.period_closing_voucher, for_update=True)
	pcv.check_permission("submit")
	if pcv.docstatus == 0:
		from frappe.model.workflow import get_workflow_name

		if get_workflow_name(pcv.doctype):
			save_and_complete_period_closing_voucher(pcv.name)
		else:
			pcv.submit()
		return _store(doc, "Closing", data, _("损益结转已提交，等待总账处理完成"))
	pcv.reload()
	if pcv.docstatus != 1 or pcv.gle_processing_status == "Failed":
		frappe.throw(_("损益结转失败，请打开结转凭证处理错误后继续"))
	if pcv.gle_processing_status != "Completed":
		return _store(doc, "Closing", data, _("损益结转总账处理中，请稍后继续"))
	create_voucher_from_source(pcv)
	checks = run_closing_checks(doc.company, doc.from_date, doc.to_date, pcv.name, doc.closing_type)
	data["checks"] = checks
	bad = [r for r in checks if r["severity"] == "Blocking" and not r["passed"]]
	if bad:
		return _store(doc, "Paused", data, _("结账检查未通过，请处理下方问题后继续"))
	return _store(doc, "Ready", data, _("本期记账和损益结转已完成，请核对财务报表后确认结账"))


@frappe.whitelist(methods=["POST"])
def advance(name):
	doc = _run(name)
	data = json.loads(doc.preparation_data or "{}")
	if doc.preparation_state not in ACTIVE:
		frappe.throw(_("请先检查并开始，或继续上次处理"))
	point = "month_step"
	frappe.db.savepoint(point)
	previous = getattr(frappe.flags, "china_month_end_run", None)
	frappe.flags.china_month_end_run = doc.name
	try:
		if doc.preparation_state == "Posting":
			if len(data.get("done", [])) < len(data.get("items", [])):
				return _post_one(doc, data)
			return _store(doc, "Checking", data, _("凭证记账完成，检查核销和现金流"))
		if doc.preparation_state == "Checking":
			return _after_posting(doc, data)
		return _close_profit(doc, data)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		raise
	except Exception as exc:
		frappe.db.rollback(save_point=point)
		doc.reload()
		data = json.loads(doc.preparation_data or "{}")
		return _store(doc, "Failed", data, str(exc))
	finally:
		frappe.flags.china_month_end_run = previous


@frappe.whitelist(methods=["POST"])
def finish(name):
	from china_finance.services.closing import submit_closing_run

	frappe.only_for(ROLES)
	existing = frappe.get_doc(RUN, name)
	existing.check_permission("submit")
	preparation.check_company(existing.company)
	if existing.docstatus == 1 and existing.status == "Closed":
		return {
			"name": name,
			"status": "Closed",
			"already_submitted": True,
			"archive_package": existing.archive_package,
		}
	doc = _run(name)
	if doc.preparation_state != "Ready":
		frappe.throw(_("请先完成本期记账和损益结转"))
	if _drafts(doc):
		frappe.throw(_("本期仍有未记账凭证"))
	if (
		frappe.db.get_value("Period Closing Voucher", doc.period_closing_voucher, "gle_processing_status")
		!= "Completed"
	):
		frappe.throw(_("损益结转总账尚未完成"))
	result = submit_closing_run(name)
	return result


@frappe.whitelist(methods=["POST"])
def retry_archive(name):
	frappe.only_for(ROLES)
	doc = frappe.get_doc(RUN, name)
	doc.check_permission("submit")
	preparation.check_company(doc.company)
	if doc.docstatus != 1 or doc.status != "Closed":
		frappe.throw(_("请先完成结账"))
	if doc.archive_package:
		return {"archive_package": doc.archive_package}
	frappe.enqueue(
		"china_finance.services.closing.create_closing_archive",
		closing_run_name=name,
		queue="short",
		enqueue_after_commit=True,
		job_id="china-closing-archive:" + name,
		deduplicate=True,
	)
	return {"message": _("归档任务已加入队列，请稍后刷新")}
