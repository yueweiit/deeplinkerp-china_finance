"""China Finance balance validation for Journal Entry saves."""

from erpnext.accounts.doctype.journal_entry.journal_entry import JournalEntry


class ChinaFinanceJournalEntry(JournalEntry):
	"""Require balanced debit and credit totals before saving any voucher."""

	def submit(self):
		from china_finance.services.opening_balance import validate_native_opening_preparation

		if validate_native_opening_preparation(self):
			# ERPNext queues Journal Entries above 100 rows. A confirmed opening
			# batch must finish native submission and audit evidence atomically in
			# its activation transaction, retaining every native submit hook.
			return self._submit()
		return super().submit()

	def validate(self):
		super().validate()
		# ERPNext normally calls this only from before_submit. Run it here too so
		# a draft cannot be saved with an unbalanced accounting entry.
		self.validate_total_debit_and_credit()

	def build_gl_map(self):
		from china_finance.services.bank_receipt_import import has_structured_receipt_lines

		gl_map = super().build_gl_map()
		if has_structured_receipt_lines(self):
			# Accrual and payment need separate audit lines even when the site's
			# "merge similar account heads" option is enabled.
			for entry in gl_map:
				entry._skip_merge = True
		return gl_map
