import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_first_day, get_last_day, getdate, now_datetime

from china_finance.overrides.source_voucher_lock import CompanyFirstSourceVoucherLock
from china_finance.services.closing import create_report_snapshots, run_closing_checks


class ChinaClosingRun(CompanyFirstSourceVoucherLock, Document):
	def before_insert(self):
		if not self.amended_from:
			return
		self.status = "Draft"
		self.archive_package = None
		self.previous_frozen_date = None
		self.closed_by = None
		self.closed_on = None
		self.reopened_by = None
		self.reopened_on = None
		self.reopen_reason = None
		self.closing_month = None
		self.set("checks", [])

	def copy_attachments_from_amended_from(self):
		"""Keep the cancelled run's immutable archive attached only to that run."""
		return

	def validate(self):
		from china_finance.services.month_end import protect_run_fields
		self._sync_closing_month()
		protect_run_fields(self)
		from china_finance.services.closing_lifecycle import protect_fields, validate_period
		protect_fields(self)
		validate_period(self)
		self._remove_empty_check_rows()
		if self.from_date and self.to_date and self.from_date > self.to_date:
			frappe.throw(_("起始日期不能晚于截止日期"))
		if self.period_closing_voucher:
			pcv = frappe.db.get_value(
				"Period Closing Voucher", self.period_closing_voucher,
				["company", "period_start_date", "period_end_date", "docstatus"],
			)
			if not pcv:
				frappe.throw(_("损益结转凭证不存在，请刷新后重试"))
			company, period_start_date, period_end_date, docstatus = pcv
			if (
				company != self.company
				or getdate(period_start_date) != getdate(self.from_date)
				or getdate(period_end_date) != getdate(self.to_date)
				or docstatus == 2
			):
				frappe.throw(_("损益结转凭证必须与月末结账单的公司和起止日期一致，且不能是已取消状态"))

	def _sync_closing_month(self):
		"""Convert the user-facing month into one exact calendar-month period."""
		if self.closing_type != "Monthly":
			return

		month = (self.closing_month or "").strip()
		if month:
			if not re.fullmatch(r"\d{4}-\d{2}", month):
				frappe.throw(_("结账月份必须使用 YYYY-MM 格式，例如 2026-08"))
			year, month_number = map(int, month.split("-"))
			if year < 1 or month_number < 1 or month_number > 12:
				frappe.throw(_("结账月份无效，请选择正确的月份"))
			try:
				month_start = getdate(f"{month}-01")
			except (TypeError, ValueError):
				frappe.throw(_("结账月份无效，请选择正确的月份"))
			if month_start.strftime("%Y-%m") != month:
				frappe.throw(_("结账月份无效，请选择正确的月份"))
			month_end = get_last_day(month_start)
			if self.from_date and getdate(self.from_date) != month_start:
				frappe.throw(_("结账月份与起始日期不一致，请只选择一个自然月"))
			if self.to_date and getdate(self.to_date) != month_end:
				frappe.throw(_("结账月份与截止日期不一致，请只选择一个自然月"))
			self.from_date = month_start
			self.to_date = month_end
			return

		if not self.from_date or not self.to_date:
			frappe.throw(_("月末结账请选择结账月份"))
		from_date = getdate(self.from_date)
		to_date = getdate(self.to_date)
		if from_date != get_first_day(from_date) or to_date != get_last_day(from_date):
			frappe.throw(_("月末结账请选择完整自然月，不能跨月"))
		self.closing_month = from_date.strftime("%Y-%m")

	def _remove_empty_check_rows(self):
		"""Discard UI-created blank rows before Frappe validates child mandatory fields."""
		populated_rows = [
			row for row in self.get("checks") or []
			if any(row.get(fieldname) for fieldname in ("check_code", "description", "severity", "details"))
		]
		if len(populated_rows) != len(self.get("checks") or []):
			self.set("checks", populated_rows)

	def before_submit(self):
		from china_finance.services.month_end import lock_company
		lock_company(self.company)
		if frappe.db.count("Journal Entry", {"company": self.company, "docstatus": 0,
			"posting_date": ["between", [self.from_date, self.to_date]], "is_opening": ["!=", "Yes"]}):
			frappe.throw(_("本期还有未记账凭证，请先完成月末记账"))
		from china_finance.services.closing_lifecycle import validate_completion
		validate_completion(self)
		checks = run_closing_checks(
			self.company, self.from_date, self.to_date, self.period_closing_voucher, self.closing_type
		)
		self.set("checks", checks)
		failed = [row["description"] for row in checks if row["severity"] == "Blocking" and not row["passed"]]
		if failed:
			frappe.throw(_("以下结账检查未通过：{0}").format("；".join(failed)))
		self.previous_frozen_date = frappe.db.get_value("Company", self.company, "accounts_frozen_till_date")
		self.status = "Closed"
		self.closed_by = frappe.session.user
		self.closed_on = now_datetime()

	def before_update_after_submit(self):
		from china_finance.services.closing_lifecycle import protect_fields
		protect_fields(self)

	def before_cancel(self):
		frappe.throw(_("月末结账记录请使用反结账操作，不能直接取消"))

	def on_submit(self):
		create_report_snapshots(self)
		frappe.enqueue(
			"china_finance.services.closing.create_closing_archive",
			queue="short",
			enqueue_after_commit=True,
			closing_run_name=self.name,
		)
		settings = frappe.get_cached_doc("China Finance Settings", self.company)
		if settings.freeze_on_close:
			frappe.db.set_value("Company", self.company, "accounts_frozen_till_date",
				max(getdate(self.to_date), getdate(self.previous_frozen_date)) if self.previous_frozen_date else self.to_date,
			)
