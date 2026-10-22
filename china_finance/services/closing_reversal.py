"""Durable native GL reversal and completion of a month-end reversal batch."""

import frappe
from frappe import _

from china_finance.services.closing_lifecycle import REVERSING, RUN
from china_finance.services.month_end import lock_company


def _batch(name):
	seed = frappe.get_doc(RUN, name)
	company = seed.company
	lock_company(company)
	seed = frappe.get_doc(RUN, name, for_update=True)
	if seed.company != company:
		frappe.throw(_("单据已被修改，请刷新后重试"), frappe.TimestampMismatchError)
	root = seed.get("reversal_group") or seed.name
	runs = frappe.db.sql(
		"""SELECT name, status, period_closing_voucher FROM `tabChina Closing Run`
		WHERE company=%s AND docstatus=1 AND (reversal_group=%s OR name=%s)
		ORDER BY to_date DESC, creation DESC FOR UPDATE""", (company, root, root), as_dict=True,
	)
	return seed, runs


def _progress(seed, runs):
	pending, failed = [], []
	for name in dict.fromkeys(row.period_closing_voucher for row in runs if row.period_closing_voucher):
		pcv = frappe.db.get_value("Period Closing Voucher", name,
			["company", "docstatus", "gle_processing_status", "error_message"], as_dict=True, for_update=True)
		if not pcv or pcv.company != seed.company or pcv.docstatus != 2 or pcv.gle_processing_status == "Failed":
			failed.append({"name": name, "error": (pcv.error_message or _("损益结转凭证状态不正确，请检查原单")) if pcv else _("损益结转凭证不存在，请刷新后重试")})
		elif pcv.gle_processing_status != "Completed":
			pending.append(name)
		else:
			issue = frappe.db.get_value("China Voucher Sync Issue",
				{"issue_key": f"Cancellation|Period Closing Voucher|{name}"}, ["status", "last_error"], as_dict=True)
			if issue and issue.status != "Resolved":
				failed.append({"name": name, "error": issue.last_error or _("冲销审计快照或来源凭证状态不完整，不能标记同步完成")})
	state = "Reopen Failed" if failed else "Reopening" if pending else "Reopened"
	for row in runs:
		if row.status in REVERSING:
			frappe.db.set_value(RUN, row.name, "status", state)
	return {"name": seed.name, "status": state, "pending_period_closing_vouchers": pending, "failed": failed}


@frappe.whitelist(methods=["POST"])
def reversal_progress(name):
	frappe.only_for(("System Manager", "China Finance Manager"))
	frappe.get_doc(RUN, name).check_permission("write")
	seed, runs = _batch(name)
	if seed.status not in (*REVERSING, "Reopened"):
		frappe.throw(_("该结账单没有进行中的反结账任务"))
	return _progress(seed, runs)


@frappe.whitelist(methods=["POST"])
def resume_reversal(name):
	result = reversal_progress(name)
	if result["status"] == "Reopened":
		return result
	frappe.enqueue("china_finance.services.closing_reversal.process_reversal",
		queue="long", enqueue_after_commit=True, name=name)
	return result


def process_reversal(name):
	"""Background-only: native cancellation commits separately from the request.

	Each completed voucher is committed before the next, preserving progress even
	when the native worker rolls back a failed voucher. The company lock and native
	is_cancelled predicate make duplicate job delivery safe.
	"""
	from erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher import process_cancellation
	from china_finance.services.voucher import process_cancellation_snapshot_job

	seed, runs = _batch(name)
	if seed.status not in REVERSING:
		return
	names = list(dict.fromkeys(row.period_closing_voucher for row in runs if row.period_closing_voucher))
	for voucher_name in names:
		# Reacquire after each commit or a native rollback, then read current state.
		seed, runs = _batch(name)
		if seed.status not in REVERSING:
			return
		pcv = frappe.db.get_value("Period Closing Voucher", voucher_name,
			["company", "docstatus", "gle_processing_status"], as_dict=True, for_update=True)
		if not pcv or pcv.company != seed.company or pcv.docstatus != 2:
			_progress(seed, runs)
			frappe.db.commit()
			return
		for row in runs:
			if row.status in REVERSING:
				frappe.db.set_value(RUN, row.name, "status", "Reopening")
		if pcv.gle_processing_status != "Completed":
			previous = getattr(frappe.flags, "china_closing_reversal", None)
			frappe.flags.china_closing_reversal = seed.get("reversal_group") or seed.name
			try:
				process_cancellation(voucher_type="Period Closing Voucher", voucher_no=voucher_name)
			finally:
				frappe.flags.china_closing_reversal = previous
		# Native processing can roll back its transaction on failure.
		seed, runs = _batch(name)
		completed = frappe.db.get_value(
			"Period Closing Voucher", voucher_name, "gle_processing_status", for_update=True,
		) == "Completed"
		audit = None
		if completed:
			frappe.db.set_value("Period Closing Voucher", voucher_name, "error_message", None)
			audit = process_cancellation_snapshot_job("Period Closing Voucher", voucher_name)
		seed, runs = _batch(name)
		_progress(seed, runs)
		frappe.db.commit()
		# A later, unvisited voucher may still carry its previous failure.
		# Stop only on this voucher's failure, so retries can reach the others.
		if not completed or not audit or audit.get("status") != "resolved":
			return
	seed, runs = _batch(name)
	_progress(seed, runs)
	frappe.db.commit()
