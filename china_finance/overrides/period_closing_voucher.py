"""China Finance rules for an initial mid-year period closing voucher."""

import frappe
from frappe import _
from frappe.utils import add_days, formatdate, get_first_day, get_last_day, getdate

from erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher import (
	PeriodClosingVoucher,
	get_previous_closed_period_in_current_year,
)


class ChinaFinancePeriodClosingVoucher(PeriodClosingVoucher):
	"""Allow a complete calendar month as the first closing period.

	This supports a company that starts its China Finance closing process
	mid-year. Once a closing voucher exists, the native continuous-period rule
	remains in force.
	"""

	def validate_start_and_end_date(self):
		self.fy_start_date, self.fy_end_date = frappe.db.get_value(
			"Fiscal Year", self.fiscal_year, ["year_start_date", "year_end_date"]
		)

		prev_closed_period_end_date = get_previous_closed_period_in_current_year(
			self.fiscal_year, self.company
		)
		period_start_date = getdate(self.period_start_date)
		period_end_date = getdate(self.period_end_date)
		valid_start_date = (
			add_days(prev_closed_period_end_date, 1)
			if prev_closed_period_end_date
			else getdate(self.fy_start_date)
		)

		first_month_is_allowed = (
			not prev_closed_period_end_date
			and period_start_date >= getdate(self.fy_start_date)
			and period_start_date == get_first_day(period_start_date)
			and period_end_date == get_last_day(period_start_date)
		)
		if period_start_date != valid_start_date and not first_month_is_allowed:
			frappe.throw(_("Period Start Date must be {0}").format(formatdate(valid_start_date)))

		if period_start_date > period_end_date:
			frappe.throw(_("Period Start Date cannot be greater than Period End Date"))

		if period_end_date > getdate(self.fy_end_date):
			frappe.throw(_("Period End Date cannot be greater than Fiscal Year End Date"))

	def on_cancel(self):
		linked_runs = frappe.get_all(
			"China Closing Run",
			filters={"period_closing_voucher": self.name, "docstatus": 1},
			fields=["name", "status"],
		)
		closed_runs = [run.name for run in linked_runs if run.status == "Closed"]
		if closed_runs:
			frappe.throw(
				_("请先在月末结账单 {0} 中执行反结账").format("、".join(closed_runs)),
				title=_("请先反结账"),
			)

		super().on_cancel()
		self.ignore_linked_doctypes = tuple(
			dict.fromkeys((*self.ignore_linked_doctypes, "China Closing Run"))
		)

	def cancel_gl_entries(self):
		# A closing reversal uses one durable batch job. Native process_cancellation
		# catches errors and rolls back, so it must not run inside the request that
		# changes the closing lifecycle or cancels the source documents.
		if getattr(frappe.flags, "china_closing_reversal", None):
			return
		return super().cancel_gl_entries()
