import frappe
from frappe import _
from frappe.model.document import Document

from china_finance.services.statement_opening import cancel_document, confirm_document, validate_document


class ChinaStatementOpeningBalance(Document):
	def validate(self):
		validate_document(self)

	def before_submit(self):
		confirm_document(self)

	def before_cancel(self):
		cancel_document(self)

	def on_trash(self):
		if self.confirmed_on:
			frappe.throw(_("已确认的报表年初基准必须保留审计历史，不能删除"))
