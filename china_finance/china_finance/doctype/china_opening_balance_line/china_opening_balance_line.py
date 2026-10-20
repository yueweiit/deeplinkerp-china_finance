from frappe.model.document import Document

from china_finance.services.opening_balance import protect_opening_document


class ChinaOpeningBalanceLine(Document):
	def validate(self):
		protect_opening_document()

	def on_trash(self):
		protect_opening_document()
