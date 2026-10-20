import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

from china_finance.services.voucher import assign_voucher_number, calculate_entries_hash


def on_doctype_update():
	# Keep monthly locking reads inside one company's numbering scope. Without
	# a usable range index, InnoDB can lock snapshots belonging to other companies.
	frappe.db.add_index(
		"China Accounting Voucher",
		["company", "voucher_word", "accounting_period", "posting_date"],
		"company_voucher_period_date",
	)


class ChinaAccountingVoucher(Document):
	def validate(self):
		self.posting_date = getdate(self.posting_date)
		self.fiscal_year = str(self.posting_date.year)
		self.accounting_period = self.posting_date.strftime("%Y-%m")
		self.total_debit = sum(flt(row.debit, 2) for row in self.entries)
		self.total_credit = sum(flt(row.credit, 2) for row in self.entries)
		if not self.entries:
			frappe.throw(_("记账凭证至少需要一条分录"))
		if abs(self.total_debit - self.total_credit) > 0.005:
			frappe.throw(_("记账凭证借贷不平衡：借方 {0}，贷方 {1}").format(self.total_debit, self.total_credit))
		self.source_hash = calculate_entries_hash(self.entries)

	def before_submit(self):
		assign_voucher_number(self)
		self.status = "Posted"
		self.posted_by = self.posted_by or frappe.session.user

	def before_update_after_submit(self):
		old = self.get_doc_before_save()
		if not old:
			return
		protected = (
			"company", "posting_date", "fiscal_year", "accounting_period", "voucher_word",
			"sequence_number", "statutory_number", "voucher_key", "source_doctype",
			"source_name", "source_event", "source_key", "source_hash", "total_debit", "total_credit",
		)
		for fieldname in protected:
			if self.get(fieldname) != old.get(fieldname):
				frappe.throw(_("已记账凭证不允许修改字段：{0}").format(self.meta.get_label(fieldname)))
