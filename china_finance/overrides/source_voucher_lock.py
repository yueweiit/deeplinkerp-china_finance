"""Acquire the company serialization lock before Frappe locks a formal source."""

import frappe
from frappe import _


class CompanyFirstSourceVoucherLock:
	def load_doc_before_save(self, *, raise_exception=False):
		if self.is_new() or not self.name:
			return super().load_doc_before_save(raise_exception=raise_exception)

		stored_company = frappe.db.get_value(self.doctype, self.name, "company")
		companies = sorted({company for company in (stored_company, self.company) if company})
		from china_finance.services.month_end import lock_company

		for company in companies:
			lock_company(company)

		super().load_doc_before_save(raise_exception=raise_exception)
		previous = self.get_doc_before_save()
		if previous and stored_company and previous.company != stored_company:
			frappe.throw(_("凭证已被其他人修改，请刷新后重试"), frappe.TimestampMismatchError)
		if previous and self.meta.has_field("custom_china_voucher_number"):
			self.custom_china_voucher_number = previous.get("custom_china_voucher_number")
		return previous
