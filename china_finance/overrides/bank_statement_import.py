"""Bank statement import extensions that do not modify ERPNext core code."""

import frappe
from frappe import _
from erpnext.accounts.doctype.bank_statement_import.bank_statement_import import BankStatementImport
from erpnext.accounts.doctype.bank_statement_import_log.bank_statement_import_log import (
	BankStatementImportLog,
)


def get_full_import_preview(importer):
	"""Build the native preview payload without the generic ten-row truncation.

	Bank statement files are normally short enough to review as a whole. The
	underlying importer, field validation, and later background import remain
	fully owned by ERPNext; this only changes the preview payload.
	"""
	import_file = importer.import_file
	columns = [frappe._dict({"header_title": "Sr. No", "skip_import": True})]
	columns += [column.as_dict() for column in import_file.columns]

	for column in columns:
		if column.df:
			column.df = {
				"fieldtype": column.df.fieldtype,
				"fieldname": column.df.fieldname,
				"label": column.df.label,
				"options": column.df.options,
				"parent": column.df.parent,
				"reqd": column.df.reqd,
				"default": column.df.default,
				"read_only": column.df.read_only,
			}

	out = frappe._dict(
		data=[[row.row_number, *row.as_list()] for row in import_file.data],
		columns=columns,
		warnings=import_file.get_warnings(),
	)
	out.import_log = frappe.get_all(
		"Data Import Log",
		fields=["row_indexes", "success"],
		filters={"data_import": importer.data_import.name},
		order_by="log_index",
		limit=10,
	)
	return out


class ChinaFinanceBankStatementImport(BankStatementImport):
	@frappe.whitelist()
	def get_preview_from_template(self, import_file=None, google_sheets_url=None):
		if import_file:
			self.import_file = import_file
			self.set_delimiters_flag()

		if google_sheets_url:
			self.google_sheets_url = google_sheets_url

		if not (self.import_file or self.google_sheets_url):
			return

		return get_full_import_preview(self.get_importer())


class ChinaFinanceBankStatementImportLog(BankStatementImportLog):
	"""Make repeated statement uploads idempotent by filtering known references."""

	def get_final_transactions(self, transaction_rows):
		transactions = super().get_final_transactions(transaction_rows)
		if not self.bank_account or not transactions:
			return transactions

		references = {
			str(transaction.get("reference") or "").strip()
			for transaction in transactions
			if str(transaction.get("reference") or "").strip()
		}
		if not references:
			return transactions

		existing_references = set(
			frappe.get_all(
				"Bank Transaction",
				filters={
					"bank_account": self.bank_account,
					"reference_number": ["in", list(references)],
					"docstatus": ["!=", 2],
				},
				pluck="reference_number",
			)
		)
		# A repeated reference is reusable only when the financial identity agrees.
		# Otherwise a later XLSX could silently hide a conflict with a receipt import.
		from china_finance.services.bank_receipt_import import existing_transaction
		from frappe.utils import getdate
		for transaction in transactions:
			reference = str(transaction.get("reference") or "").strip()
			if reference in existing_references:
				if transaction.get("withdrawal") and transaction.get("deposit"):
					frappe.throw(_("银行流水不能同时存在收入和支出金额"))
				existing_transaction(self.bank_account, {
					"transaction_id": reference, "posting_date": str(getdate(transaction.get("date"))),
					"direction": "支出" if transaction.get("withdrawal") else "收入",
					"amount": transaction.get("withdrawal") or transaction.get("deposit"), "currency": self.currency,
				})
		return [
			transaction
			for transaction in transactions
			if str(transaction.get("reference") or "").strip() not in existing_references
		]

	@frappe.whitelist(methods=["POST"])
	def insert_transactions(self):
		"""Import the complete batch atomically after validating every row.

		The standard importer creates and submits transactions one at a time. That
		allowed an early row to trigger an automatic draft voucher before a later
		row failed validation. Validate the final batch first, then protect the
		whole write phase with a savepoint so an unexpected failure cannot leave a
		partial import behind.
		"""
		if self.status == "Completed":
			return super().insert_transactions()

		transactions = _get_transactions_for_import(self)
		_validate_import_batch(transactions)

		save_point = f"china_bank_import_{frappe.generate_hash(length=8)}"
		message_log = list(frappe.get_message_log())
		frappe.db.savepoint(save_point)
		try:
			return super().insert_transactions()
		except Exception:
			frappe.db.rollback(save_point=save_point)
			# Keep the actual error message but remove success messages emitted by
			# earlier rows that have now been rolled back.
			new_messages = [
				message
				for message in frappe.get_message_log()[len(message_log):]
				if not str(message.get("message") or "").startswith("已为银行流水 ")
			]
			frappe.local.message_log = message_log + new_messages
			raise
		finally:
			try:
				frappe.db.release_savepoint(save_point)
			except Exception:
				# The outer request may already have rolled back the transaction.
				pass


def _get_transactions_for_import(import_log):
	"""Build the same final transaction list used by ERPNext's importer."""
	if import_log.is_pdf():
		return import_log.get_pdf_final_transactions()

	raw_data = import_log.get_data()
	transaction_rows, _starting_index, _ending_index = import_log.get_transaction_rows(raw_data)
	return import_log.get_final_transactions(transaction_rows=transaction_rows)


def _validate_import_batch(transactions):
	"""Reject known row-level failures before any Bank Transaction is inserted."""
	seen_references = set()
	max_transaction_id_length = 140

	for row_index, transaction in enumerate(transactions, 1):
		reference = str(transaction.get("reference") or "").strip()
		if reference and len(reference) > max_transaction_id_length:
			frappe.throw(
				_("第 {0} 条银行流水的流水号超过 {1} 个字符，请检查参考号字段映射。").format(
					row_index, max_transaction_id_length
				),
				title=_("银行流水导入失败"),
			)
		if reference in seen_references:
			frappe.throw(
				_("第 {0} 条银行流水的流水号重复：{1}。").format(row_index, reference),
				title=_("银行流水导入失败"),
			)
		if reference:
			seen_references.add(reference)
