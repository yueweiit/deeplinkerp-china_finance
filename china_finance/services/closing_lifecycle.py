"""Company-serialized closing identity and server-owned lifecycle fields."""

import frappe
from frappe import _
from frappe.utils import getdate

from china_finance.services.month_end import lock_company

RUN = "China Closing Run"
REVERSING = ("Reopening", "Reopen Failed")
PROTECTED = (
	"status", "archive_package", "previous_frozen_date", "closed_by", "closed_on",
	"reopened_by", "reopened_on", "reopen_reason", "reclosing_of", "reversal_group",
)


def protect_fields(doc):
	old = doc.get_doc_before_save()
	for field in PROTECTED:
		value = old.get(field) if old else ("Draft" if field == "status" else None)
		if old and doc.get(field) != value:
			frappe.throw(_("结账状态及审计信息只能通过结账或反结账操作更新"))
		doc.set(field, value)


def active_reversal(company):
	return frappe.db.sql(
		"""SELECT name FROM `tabChina Closing Run` WHERE company=%s AND docstatus=1
		AND status IN ('Reopening','Reopen Failed') LIMIT 1 FOR UPDATE""",
		(company,), as_dict=True,
	)


def ensure_no_reversal(company):
	rows = active_reversal(company)
	if rows:
		frappe.throw(_("公司正在反结账或撤销失败，请先处理月末结账单 {0}").format(rows[0].name))


def period_runs(doc):
	return frappe.db.sql(
		"""SELECT name, docstatus, status, creation, preparation_state FROM `tabChina Closing Run`
		WHERE company=%s AND closing_type=%s AND from_date=%s AND to_date=%s AND docstatus!=2
		ORDER BY creation, name FOR UPDATE""",
		(doc.company, doc.closing_type, doc.from_date, doc.to_date), as_dict=True,
	)


def preferred_draft(rows):
	"""Keep legacy work in progress usable; equal priorities retain DB order."""
	def priority(row):
		state = row.get("preparation_state")
		if state in ("Posting", "Checking", "Closing"):
			return 0
		if state == "Ready":
			return 1
		return 2 if state else 3

	drafts = [row for row in rows if row.docstatus == 0]
	return min(drafts, key=priority) if drafts else None


def validate_period(doc):
	if not (doc.company and doc.from_date and doc.to_date):
		return
	lock_company(doc.company)
	ensure_no_reversal(doc.company)
	rows = period_runs(doc)
	closed = [row for row in rows if row.name != doc.name and row.status == "Closed"]
	draft = preferred_draft(rows)
	conflict = closed[0] if closed else (
		draft if draft and (doc.is_new() or draft.name != doc.name) else None
	)
	if conflict:
		frappe.throw(_("本公司同期间已有结账单 {0}，请打开已有单据继续处理").format(conflict.name))
	if doc.is_new():
		previous = [row for row in rows if row.docstatus == 1 and row.status == "Reopened"]
		doc.reclosing_of = previous[-1].name if previous else None


def validate_completion(doc):
	validate_period(doc)
	later = frappe.db.sql(
		"""SELECT name FROM `tabChina Closing Run` WHERE company=%s AND docstatus=1
		AND status='Closed' AND to_date>%s LIMIT 1 FOR UPDATE""",
		(doc.company, doc.to_date), as_dict=True,
	)
	if later:
		frappe.throw(_("已有后续期间结账单 {0}，请先从后续期间反结账").format(later[0].name))
	pcv = frappe.db.get_value(
		"Period Closing Voucher", doc.period_closing_voucher,
		["company", "period_start_date", "period_end_date", "docstatus", "gle_processing_status"], as_dict=True,
	) if doc.period_closing_voucher else None
	if not pcv or pcv.docstatus != 1:
		frappe.throw(_("请先完成损益结转，再确认月末结账"))
	if (pcv.company != doc.company or getdate(pcv.period_start_date) != getdate(doc.from_date)
		or getdate(pcv.period_end_date) != getdate(doc.to_date)):
		frappe.throw(_("损益结转凭证必须与月末结账单的公司和起止日期一致，且不能是已取消状态"))
	if pcv.gle_processing_status != "Completed":
		frappe.throw(_("损益结转总账处理尚未完成"))


@frappe.whitelist(methods=["POST"])
def get_or_create_reclosing(name):
	frappe.only_for(("System Manager", "China Finance Manager"))
	source = frappe.get_doc(RUN, name)
	source.check_permission("read")
	lock_company(source.company)
	current = frappe.get_doc(RUN, name, for_update=True)
	if current.company != source.company:
		frappe.throw(_("单据已被修改，请刷新后重试"), frappe.TimestampMismatchError)
	if current.docstatus != 1 or current.status != "Reopened":
		frappe.throw(_("只有已反结账的单据可以重新结账"))
	ensure_no_reversal(current.company)
	rows = period_runs(current)
	closed = [row for row in rows if row.status == "Closed"]
	active = closed[0] if closed else preferred_draft(rows)
	if active:
		existing = frappe.get_doc(RUN, active.name)
		existing.check_permission("read")
		return {"name": existing.name, "created": False}
	doc = frappe.get_doc({"doctype": RUN, **{field: current.get(field) for field in (
		"company", "closing_type", "closing_month", "from_date", "to_date",
	)}})
	doc.insert()
	return {"name": doc.name, "created": True}
