"""Integration coverage for account editing inside China Voucher Ledger."""

import unittest
from unittest.mock import patch

import frappe

from china_finance.china_finance.report.china_voucher_ledger.china_voucher_ledger import execute
from china_finance.services import source_voucher_edit as source_edit
from china_finance.services import voucher_preparation as preparation
from china_finance.setup.china_coa_profile import CHART_TEMPLATE, get_account_by_number


class TestInlineVoucherAccountEdit(unittest.TestCase):
	"""Create an isolated CNY company so these tests do not require a developer fixture."""

	def setUp(self):
		self.user = frappe.session.user
		self.language = frappe.local.lang
		frappe.set_user("Administrator")
		frappe.local.lang = "zh"
		self.point = "inline_account_edit_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(self.point)
		self._make_company()

	def tearDown(self):
		frappe.db.rollback(save_point=self.point)
		frappe.set_user(self.user)
		frappe.local.lang = self.language

	def _make_company(self):
		suffix = frappe.generate_hash(length=5).upper()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": f"_Inline Voucher Edit {suffix}",
				"abbr": suffix,
				"country": "China",
				"default_currency": "CNY",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": CHART_TEMPLATE,
			}
		).insert()
		self.company = company.name
		self.bank = get_account_by_number(self.company, "100201").name
		self.expense_account = get_account_by_number(self.company, "660201").name
		self.replacement_expense = get_account_by_number(self.company, "660202").name
		self.cost_center = frappe.db.get_value(
			"Cost Center", {"company": self.company, "is_group": 0}, "name"
		)
		self.second_bank = self._account("Inline Second Bank", self.bank, account_type="Bank")
		self.third_bank = self._account("Inline Third Bank", self.bank, account_type="Bank")
		self.foreign_expense = self._account("Inline Foreign Expense", self.expense_account, currency="USD")
		frappe.db.set_value(
			"China Finance Settings",
			self.company,
			{
				"enabled": 1,
				"activation_date": "2026-01-01",
				"accounting_standard": "小企业会计准则",
				"taxpayer_type": "一般纳税人",
				"voucher_mode": "统一记字",
				"default_voucher_word": "记",
				"sequence_reset": "会计期间",
				"enforce_role_separation": 0,
				"enable_voucher_preparation": 1,
				"preparation_from_date": "2026-01-01",
				"report_amount_unit": "元",
				"archive_retention_years": 30,
				"reconciliation_tolerance": 0.01,
			},
		)
		frappe.clear_document_cache("China Finance Settings", self.company)

	def _account(self, label, sibling, account_type=None, currency="CNY"):
		return (
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": label,
					"parent_account": frappe.db.get_value("Account", sibling, "parent_account"),
					"company": self.company,
					"account_type": account_type,
					"account_currency": currency,
				}
			)
			.insert()
			.name
		)

	def _journal_entry(self):
		return frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": self.company,
				"posting_date": "2026-08-15",
				"voucher_type": "Journal Entry",
				"accounts": [
					{"account": self.bank, "credit_in_account_currency": 125},
					{
						"account": self.expense_account,
						"debit_in_account_currency": 125,
						"cost_center": self.cost_center,
					},
				],
			}
		).insert()

	def _mark_ready(self, doc):
		doc.db_set(
			{
				preparation.READY_FIELDS[0]: preparation.content_hash(doc),
				preparation.READY_FIELDS[1]: frappe.session.user,
				preparation.READY_FIELDS[2]: frappe.utils.now_datetime(),
			}
		)
		doc.reload()
		return doc

	def _payment_entry(self):
		return frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"company": self.company,
				"posting_date": "2026-08-15",
				"payment_type": "Internal Transfer",
				"paid_from": self.bank,
				"paid_to": self.second_bank,
				"paid_amount": 125,
				"received_amount": 125,
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
				"reference_no": "INLINE-TRANSFER",
				"reference_date": "2026-08-15",
			}
		).insert()

	def _bank_transaction(self):
		suffix = frappe.generate_hash(length=6).upper()
		bank = frappe.get_doc({"doctype": "Bank", "bank_name": f"Inline Edit Bank {suffix}"}).insert()
		bank_account = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": f"Inline Edit Account {suffix}",
				"bank": bank.name,
				"account": self.bank,
				"company": self.company,
				"is_company_account": 1,
				"bank_account_no": suffix,
			}
		).insert()
		transaction = frappe.get_doc(
			{
				"doctype": "Bank Transaction",
				"date": "2026-08-15",
				"description": f"Inline account correction {suffix}",
				"withdrawal": 125,
				"currency": "CNY",
				"bank_account": bank_account.name,
				"transaction_id": suffix,
			}
		)
		transaction.flags.china_receipt_explicit_voucher = True
		transaction.insert()
		transaction.flags.china_receipt_explicit_voucher = True
		transaction.submit()
		return transaction

	def _reconciled_journal_entry(self, transaction):
		doc = self._journal_entry()
		doc.custom_china_bank_transaction = transaction.name
		doc.save()
		self._mark_ready(doc).submit()
		doc.reload()
		transaction.reload()
		self.assertEqual(transaction.status, "Reconciled")
		self.assertEqual(len(transaction.payment_entries), 1)
		return doc

	def _ledger_rows(self, name, status):
		return execute(
			{
				"company": self.company,
				"from_date": "2026-08-01",
				"to_date": "2026-08-31",
				"source_name": name,
				"voucher_status": status,
			}
		)[1]

	def test_draft_change_is_versioned_clears_review_and_has_no_posting_side_effect(self):
		doc = self._mark_ready(self._journal_entry())
		row = next(
			row for row in self._ledger_rows(doc.name, "待记账") if row["account"] == self.expense_account
		)
		self.assertTrue(row["editable_account"])
		old_modified = str(doc.modified)
		with self.assertRaisesRegex(frappe.ValidationError, "刷新报表"):
			source_edit.preview_inline_account_changes(
				"Journal Entry",
				doc.name,
				old_modified,
				[{"edit_key": "je:client-forged-field", "account": self.replacement_expense}],
			)
		result = source_edit.apply_inline_account_changes(
			"Journal Entry",
			doc.name,
			old_modified,
			[{"edit_key": row["edit_key"], "account": self.replacement_expense}],
		)
		self.assertEqual(result["mode"], "draft")
		doc.reload()
		self.assertIn(self.replacement_expense, [item.account for item in doc.accounts])
		self.assertFalse(doc.custom_china_ready_hash)
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": doc.name}))
		with self.assertRaises(frappe.TimestampMismatchError):
			source_edit.apply_inline_account_changes(
				"Journal Entry",
				doc.name,
				old_modified,
				[{"edit_key": row["edit_key"], "account": self.expense_account}],
			)

	def test_draft_summary_change_updates_exact_journal_row_and_clears_review(self):
		doc = self._journal_entry()
		doc.accounts[0].user_remark = "支付测试费用"
		doc.accounts[1].user_remark = "原费用摘要"
		doc.save()
		self._mark_ready(doc)
		rows = self._ledger_rows(doc.name, "待记账")
		expense_row = next(row for row in rows if row["account"] == self.expense_account)
		bank_row = next(row for row in rows if row["account"] == self.bank)
		self.assertTrue(expense_row["editable_summary"])
		self.assertNotEqual(expense_row["summary_edit_key"], bank_row["summary_edit_key"])

		preview = source_edit.preview_inline_voucher_changes(
			"Journal Entry",
			doc.name,
			str(doc.modified),
			[
				{
					"field": "summary",
					"edit_key": expense_row["summary_edit_key"],
					"value": "修改后的费用摘要",
				}
			],
		)
		self.assertEqual(preview["changes"][0]["field"], "summary")
		result = source_edit.apply_inline_voucher_changes(
			"Journal Entry",
			doc.name,
			str(doc.modified),
			[
				{
					"field": "summary",
					"edit_key": expense_row["summary_edit_key"],
					"value": "修改后的费用摘要",
				}
			],
		)
		self.assertEqual(result["mode"], "draft")
		doc.reload()
		self.assertEqual(doc.accounts[0].user_remark, "支付测试费用")
		self.assertEqual(doc.accounts[1].user_remark, "修改后的费用摘要")
		self.assertFalse(doc.custom_china_ready_hash)
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": doc.name}))

		with self.assertRaisesRegex(frappe.ValidationError, "摘要不能为空"):
			source_edit.preview_inline_voucher_changes(
				"Journal Entry",
				doc.name,
				str(doc.modified),
				[
					{
						"field": "summary",
						"edit_key": expense_row["summary_edit_key"],
						"value": " ",
					}
				],
			)

	def _amount_changes(self, doc, amount):
		return [
			{"field": "credit", "edit_key": f"je:{doc.accounts[0].name}", "value": amount},
			{"field": "debit", "edit_key": f"je:{doc.accounts[1].name}", "value": amount},
		]

	def _three_line_delete_voucher(self):
		doc = self._journal_entry()
		doc.accounts[1].debit_in_account_currency = 75
		doc.append("accounts", {"account": self.replacement_expense, "debit_in_account_currency": 50,
			"cost_center": self.cost_center, "user_remark": "Delete test"})
		doc.save()
		return doc

	def _delete_changes(self, doc, deleted=2, remaining=1, amount=125):
		return [
			{"field": "delete", "edit_key": f"je:{doc.accounts[deleted].name}", "value": True},
			{"field": "debit", "edit_key": f"je:{doc.accounts[remaining].name}", "value": amount},
		]

	def test_delete_draft_row_with_balancing_edit_reindexes_and_clears_review(self):
		doc = self._mark_ready(self._three_line_delete_voucher())
		self.assertTrue(all(row["editable_delete"] for row in self._ledger_rows(doc.name, "待记账")))
		changes = self._delete_changes(doc, deleted=1, remaining=2)
		preview = source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		self.assertFalse(preview["requires_confirmation"])
		source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		self.assertEqual(len(doc.accounts), 2)
		self.assertEqual([row.idx for row in doc.accounts], [1, 2])
		self.assertEqual(doc.accounts[1].account, self.replacement_expense)
		self.assertEqual(doc.accounts[1].debit_in_account_currency, 125)
		self.assertFalse(doc.get(preparation.READY_FIELDS[0]))
		self.assertEqual(doc.total_debit, doc.total_credit)

	def test_delete_posted_row_restores_bank_reconciliation_and_preserves_original(self):
		transaction = self._bank_transaction()
		doc = self._three_line_delete_voucher()
		doc.custom_china_bank_transaction = transaction.name
		doc.save()
		self._mark_ready(doc).submit()
		doc.reload()
		number = doc.custom_china_voucher_number
		result = source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), self._delete_changes(doc))
		doc.reload()
		amended = frappe.get_doc(doc.doctype, result["source_name"])
		self.assertEqual(doc.docstatus, 2)
		self.assertEqual(len(doc.accounts), 3)
		self.assertEqual(amended.docstatus, 1)
		self.assertEqual(len(amended.accounts), 2)
		self.assertEqual(amended.accounts[1].debit_in_account_currency, 125)
		self.assertEqual(amended.custom_china_voucher_number, number)
		transaction.reload()
		self.assertEqual(transaction.status, "Reconciled")
		self.assertEqual(transaction.payment_entries[0].payment_entry, amended.name)
		self.assertEqual(transaction.payment_entries[0].allocated_amount, 125)
		comments = frappe.get_all("Comment", filters={"reference_doctype": amended.doctype,
			"reference_name": amended.name}, pluck="content")
		self.assertTrue(any("删除行" in text and "Delete test" in text for text in comments))

	def test_delete_rejects_unbalanced_insufficient_stale_and_conflicting_changes(self):
		doc = self._three_line_delete_voucher()
		changes = self._delete_changes(doc)
		with self.assertRaisesRegex(frappe.ValidationError, "借贷不平衡"):
			source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes[:1])
		with self.assertRaises(frappe.TimestampMismatchError):
			source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, "stale", changes)
		with self.assertRaisesRegex(frappe.ValidationError, "待删除分录"):
			source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified),
				[*changes, {"field": "debit", "edit_key": changes[0]["edit_key"], "value": 25}])
		with self.assertRaisesRegex(frappe.ValidationError, "至少保留两条"):
			source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified),
				[changes[0], {"field": "delete", "edit_key": f"je:{doc.accounts[1].name}", "value": True}])
		for value in (False, 1, "true", {}, None):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				source_edit._parse_inline_changes([{**changes[0], "value": value}])
		doc.reload()
		self.assertEqual(len(doc.accounts), 3)

	def test_delete_rejects_linked_foreign_rows_and_payment_entries(self):
		doc = self._three_line_delete_voucher()
		doc.accounts[2].reference_type = "Sales Invoice"
		with self.assertRaisesRegex(frappe.ValidationError, "关联业务单据"):
			source_edit._resolve_inline_changes(doc, self._delete_changes(doc))
		doc.accounts[2].reference_type = None
		doc.multi_currency = 1
		with self.assertRaisesRegex(frappe.ValidationError, "外币"):
			source_edit._resolve_inline_changes(doc, self._delete_changes(doc))
		payment = self._payment_entry()
		with self.assertRaisesRegex(frappe.ValidationError, "本位币记账凭证"):
			source_edit.preview_inline_voucher_changes(payment.doctype, payment.name, str(payment.modified), self._delete_changes(doc))
		minimal = self._journal_entry()
		with self.assertRaisesRegex(frappe.ValidationError, "至少保留两条"):
			source_edit._resolve_inline_changes(minimal, [{"field": "delete", "edit_key": f"je:{minimal.accounts[1].name}", "value": True}])

	def test_multiple_delete_rows_apply_other_edits_before_reindexing(self):
		doc = self._three_line_delete_voucher()
		doc.accounts[1].debit_in_account_currency = 50
		doc.append("accounts", {"account": self.expense_account, "debit_in_account_currency": 25,
			"cost_center": self.cost_center})
		doc.save()
		changes = [{"field": "delete", "edit_key": f"je:{doc.accounts[index].name}", "value": True} for index in (1, 2)]
		changes.append({"field": "debit", "edit_key": f"je:{doc.accounts[3].name}", "value": 125})
		source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		self.assertEqual([row.idx for row in doc.accounts], [1, 2])
		self.assertEqual(doc.accounts[1].debit_in_account_currency, 125)

	def test_delete_cannot_change_linked_bank_amount(self):
		transaction = self._bank_transaction()
		doc = self._three_line_delete_voucher()
		doc.custom_china_bank_transaction = transaction.name
		doc.save()
		changes = [
			{"field": "delete", "edit_key": f"je:{doc.accounts[0].name}", "value": True},
			{"field": "debit", "edit_key": f"je:{doc.accounts[2].name}", "value": 0},
			{"field": "credit", "edit_key": f"je:{doc.accounts[2].name}", "value": 75},
		]
		with self.assertRaisesRegex(frappe.ValidationError, "银行收付款金额不能"):
			source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		self.assertEqual(len(doc.accounts), 3)
		self.assertEqual(doc.accounts[0].credit_in_account_currency, 125)

	def test_failed_delete_bank_restoration_rolls_back_rows_and_original(self):
		transaction = self._bank_transaction()
		doc = self._three_line_delete_voucher()
		doc.custom_china_bank_transaction = transaction.name
		doc.save()
		self._mark_ready(doc).submit()
		doc.reload()
		with patch.object(source_edit, "_restore_quick_reconciliation", side_effect=frappe.ValidationError("forced delete failure")):
			with self.assertRaisesRegex(frappe.ValidationError, "forced delete failure"):
				source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), self._delete_changes(doc))
		doc.reload()
		transaction.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(len(doc.accounts), 3)
		self.assertEqual(transaction.status, "Reconciled")
		self.assertEqual(transaction.payment_entries[0].payment_entry, doc.name)
		self.assertFalse(frappe.db.exists(doc.doctype, {"amended_from": doc.name}))

	def test_delete_large_source_limits_match_metadata_preview_and_amendment(self):
		for count in (100, 101):
			with self.subTest(count=count):
				doc = frappe.copy_doc(self._journal_entry()).insert(
					set_name="_InlineDeleteSize-" + frappe.generate_hash(length=12))
				doc.accounts[1].debit_in_account_currency = 127 - count
				for _index in range(count - 2):
					doc.append("accounts", {"account": self.replacement_expense,
						"debit_in_account_currency": 1, "cost_center": self.cost_center})
				doc.save()
				changes = self._delete_changes(doc, deleted=count - 1, amount=128 - count)
				def metadata():
					return source_edit.annotate_inline_edit_rows([frappe._dict(
						source_doctype=doc.doctype, source_name=doc.name,
						voucher_detail_no=doc.accounts[1].name, account=doc.accounts[1].account,
						debit=doc.accounts[1].debit, credit=0,
					)])[0]
				self.assertTrue(metadata()["editable_delete"])
				self.assertFalse(source_edit.preview_inline_voucher_changes(
					doc.doctype, doc.name, str(doc.modified), changes)["requires_confirmation"])
				# Run the native background-worker submission path synchronously for this rollback-only fixture.
				self._mark_ready(doc)._submit()
				doc.reload()
				self.assertEqual(doc.docstatus, 1)
				self.assertEqual(len(doc.accounts), count)
				self.assertEqual(metadata()["editable_delete"], count == 100)
				if count == 100:
					self.assertTrue(source_edit.preview_inline_voucher_changes(
						doc.doctype, doc.name, str(doc.modified), changes)["requires_confirmation"])
				else:
					for operation in (source_edit.preview_inline_voucher_changes, source_edit.apply_inline_voucher_changes):
						with self.assertRaisesRegex(frappe.ValidationError, "超过100行"):
							operation(doc.doctype, doc.name, str(doc.modified), changes)
					with self.assertRaisesRegex(frappe.ValidationError, "超过100行"):
						source_edit.prepare_source_voucher_edit(doc.doctype, doc.name)
					doc.reload()
					self.assertEqual(doc.docstatus, 1)
					self.assertEqual(len(doc.accounts), 101)
					self.assertFalse(frappe.db.exists(doc.doctype, {"amended_from": doc.name}))

	def _add_changes(self, doc, amount=118811.88, side="credit", key="new:1", index=0):
		return [{"field": "add", "edit_key": key, "value": {
			"source_row": f"je:{doc.accounts[index].name}", "account": self.replacement_expense,
			"side": side, "amount": amount, "summary": "Independent addition",
		}}]

	def test_add_larger_amount_preserves_bank_row_and_saves_with_balancing_edit(self):
		transaction = self._bank_transaction()
		doc = self._journal_entry()
		doc.custom_china_bank_transaction = transaction.name
		doc.save()
		changes = self._add_changes(doc)
		changes += [{"field": "debit", "edit_key": f"je:{doc.accounts[1].name}", "value": 118936.88}]
		self.assertFalse(source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)["requires_confirmation"])
		source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		self.assertEqual(doc.accounts[0].credit_in_account_currency, 125)
		self.assertEqual(len(doc.accounts), 3)
		self.assertEqual(doc.accounts[2].credit_in_account_currency, 118811.88)
		self.assertEqual(doc.accounts[2].debit_in_account_currency, 0)
		self.assertEqual(doc.total_debit, doc.total_credit)

	def test_multiple_additions_use_same_reference_and_allow_signed_amounts(self):
		doc = self._journal_entry()
		changes = self._add_changes(doc, -118811.88, "debit", "new:1", index=1)
		changes += self._add_changes(doc, -118811.88, "credit", "new:2", index=1)
		source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		self.assertEqual(len(doc.accounts), 4)
		self.assertEqual(doc.accounts[1].debit_in_account_currency, 125)
		self.assertEqual(doc.accounts[2].debit_in_account_currency, -118811.88)
		self.assertEqual(doc.accounts[2].cost_center, self.cost_center)
		self.assertEqual(doc.accounts[3].credit_in_account_currency, -118811.88)

	def test_posted_independent_additions_restore_bank_allocation(self):
		transaction = self._bank_transaction()
		doc = self._reconciled_journal_entry(transaction)
		changes = self._add_changes(doc, 125, "debit") + self._add_changes(doc, 125, "credit", "new:2")
		result = source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		amended = frappe.get_doc(doc.doctype, result["source_name"])
		self.assertEqual(amended.docstatus, 1)
		self.assertEqual(len(amended.accounts), 4)
		self.assertEqual(amended.accounts[0].credit_in_account_currency, 125)
		transaction.reload()
		self.assertEqual(transaction.status, "Reconciled")
		self.assertEqual(transaction.payment_entries[0].payment_entry, amended.name)

	def test_add_rejects_invalid_or_unbalanced_changes_and_preserves_source(self):
		doc = self._journal_entry()
		with self.assertRaisesRegex(frappe.ValidationError, "借贷不平衡"):
			source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), self._add_changes(doc))
		for amount in (0, "NaN", "Infinity", "bad", "0.123456789"):
			with self.subTest(amount=amount), self.assertRaises(frappe.ValidationError):
				source_edit._resolve_inline_changes(doc, self._add_changes(doc, amount))
		for side in ("bad", None, [], {}):
			with self.subTest(side=side), self.assertRaisesRegex(frappe.ValidationError, "借方或贷方"):
				source_edit._resolve_inline_changes(doc, self._add_changes(doc, side=side))
		changes = self._add_changes(doc)
		changes[0]["value"]["company"] = "foreign"
		with self.assertRaises(frappe.ValidationError):
			source_edit._resolve_inline_changes(doc, changes)
		with self.assertRaises(frappe.TimestampMismatchError):
			source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, "stale", self._add_changes(doc))
		with patch.object(source_edit, "MAX_INLINE_ENTRY_ROWS", 3), self.assertRaisesRegex(frappe.ValidationError, "不能超过"):
			source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified),
				self._add_changes(doc, 1, "debit") + self._add_changes(doc, 1, "credit", "new:2"))
		doc.reload()
		self.assertEqual(len(doc.accounts), 2)

	def test_independent_addition_failure_rolls_back_amendment_and_added_rows(self):
		transaction = self._bank_transaction()
		doc = self._reconciled_journal_entry(transaction)
		changes = self._add_changes(doc, 125, "debit") + self._add_changes(doc, 125, "credit", "new:2")
		with patch.object(source_edit, "_restore_quick_reconciliation", side_effect=frappe.ValidationError("forced add failure")):
			with self.assertRaisesRegex(frappe.ValidationError, "forced add failure"):
				source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		transaction.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(len(doc.accounts), 2)
		self.assertEqual(transaction.payment_entries[0].payment_entry, doc.name)
		self.assertEqual(transaction.status, "Reconciled")
		self.assertFalse(frappe.db.exists(doc.doctype, {"amended_from": doc.name}))

	def test_obsolete_split_requests_are_rejected_without_changing_source(self):
		doc = self._journal_entry()
		changes = [{"field": "split", "edit_key": f"je:{doc.accounts[1].name}",
			"value": {"account": self.replacement_expense, "amount": 50, "summary": "obsolete"}}]
		for operation in (source_edit.preview_inline_voucher_changes, source_edit.apply_inline_voucher_changes):
			with self.assertRaisesRegex(frappe.ValidationError, "不支持修改该凭证字段"):
				operation(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		self.assertEqual(len(doc.accounts), 2)
		self.assertEqual(doc.accounts[1].debit_in_account_currency, 125)

	def test_add_rejects_foreign_accounts_referenced_rows_and_non_journal_sources(self):
		doc = self._journal_entry()
		changes = self._add_changes(doc)
		changes[0]["value"]["account"] = self.foreign_expense
		with self.assertRaises(frappe.ValidationError):
			source_edit._resolve_inline_changes(doc, changes)
		doc.accounts[0].reference_type = "Sales Invoice"
		with self.assertRaisesRegex(frappe.ValidationError, "关联业务单据"):
			source_edit._resolve_inline_changes(doc, self._add_changes(doc))
		doc.accounts[0].reference_type = None
		doc.multi_currency = 1
		with self.assertRaisesRegex(frappe.ValidationError, "外币"):
			source_edit._resolve_inline_changes(doc, self._add_changes(doc))
		payment = self._payment_entry()
		with self.assertRaisesRegex(frappe.ValidationError, "本位币记账凭证"):
			source_edit.preview_inline_voucher_changes(payment.doctype, payment.name, str(payment.modified), self._add_changes(doc))

	def test_draft_amounts_are_saved_together_clear_review_and_do_not_post(self):
		doc = self._mark_ready(self._journal_entry())
		rows = self._ledger_rows(doc.name, "待记账")
		self.assertTrue(all(row["editable_amount"] for row in rows))
		changes = self._amount_changes(doc, "120.35")
		preview = source_edit.preview_inline_voucher_changes(
			doc.doctype, doc.name, str(doc.modified), changes
		)
		self.assertFalse(preview["requires_confirmation"])
		result = source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		self.assertFalse(result["posted"])
		self.assertEqual(doc.accounts[0].credit_in_account_currency, 120.35)
		self.assertEqual(doc.accounts[1].debit_in_account_currency, 120.35)
		self.assertEqual(doc.total_debit, 120.35)
		self.assertEqual(doc.total_credit, 120.35)
		self.assertFalse(doc.custom_china_ready_hash)
		self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": doc.name}))

	def test_invalid_amounts_are_rejected_before_saving(self):
		doc = self._journal_entry()
		changes = self._amount_changes(doc, 100)
		with self.assertRaisesRegex(frappe.ValidationError, "借贷不平衡"):
			source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes[:1])
		for value in (None, True, "NaN", "Infinity", "-Infinity", "not-a-number", "1000000000000"):
			with self.subTest(value=value), self.assertRaisesRegex(frappe.ValidationError, "有效金额"):
				source_edit.preview_inline_voucher_changes(
					doc.doctype, doc.name, str(doc.modified), self._amount_changes(doc, value)
				)
		with self.assertRaisesRegex(frappe.ValidationError, "位小数"):
			source_edit.preview_inline_voucher_changes(
				doc.doctype, doc.name, str(doc.modified), self._amount_changes(doc, "100.123456789")
			)
		with self.assertRaisesRegex(frappe.ValidationError, "同时为零"):
			source_edit.preview_inline_voucher_changes(
				doc.doctype, doc.name, str(doc.modified), self._amount_changes(doc, 0)
			)
		with self.assertRaisesRegex(frappe.ValidationError, "同时填写"):
			source_edit.preview_inline_voucher_changes(
				doc.doctype,
				doc.name,
				str(doc.modified),
				[{"field": "debit", "edit_key": f"je:{doc.accounts[0].name}", "value": 10}],
			)
		doc.reload()
		self.assertEqual(doc.total_debit, 125)

	def test_amount_keys_types_permissions_and_versions_are_checked(self):
		doc = self._journal_entry()
		with self.assertRaisesRegex(frappe.ValidationError, "刷新报表"):
			source_edit.preview_inline_voucher_changes(
				doc.doctype, doc.name, str(doc.modified),
				[{"field": "debit", "edit_key": "je:forged-row", "value": 100}],
			)
		with self.assertRaises(frappe.TimestampMismatchError):
			source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, "stale", self._amount_changes(doc, 100))
		payment = self._payment_entry()
		self.assertTrue(all(not row["editable_amount"] for row in self._ledger_rows(payment.name, "未记账")))
		with self.assertRaisesRegex(frappe.ValidationError, "由系统计算"):
			source_edit.preview_inline_voucher_changes(
				payment.doctype, payment.name, str(payment.modified),
				[{"field": "debit", "edit_key": "pe:field:paid_to", "value": 100}],
			)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				source_edit.preview_inline_voucher_changes(
					doc.doctype, doc.name, str(doc.modified), self._amount_changes(doc, 100)
				)
		finally:
			frappe.set_user("Administrator")
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-08-31")
		with self.assertRaisesRegex(frappe.ValidationError, "冻结"):
			source_edit.preview_inline_voucher_changes(
				doc.doctype, doc.name, str(doc.modified), self._amount_changes(doc, 100)
			)

	def test_posted_amount_account_and_summary_changes_share_one_amendment(self):
		doc = self._mark_ready(self._journal_entry())
		doc.submit()
		doc.reload()
		old_number = doc.custom_china_voucher_number
		changes = self._amount_changes(doc, 100)
		changes.extend([
			{"field": "account", "edit_key": f"je:{doc.accounts[1].name}", "value": self.replacement_expense},
			{"field": "summary", "edit_key": f"je-summary:{doc.accounts[1].name}", "value": "Corrected expense"},
		])
		preview = source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		self.assertTrue(preview["requires_confirmation"])
		result = source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		amended = frappe.get_doc(doc.doctype, result["source_name"])
		self.assertEqual(doc.docstatus, 2)
		self.assertEqual(amended.docstatus, 1)
		self.assertEqual(amended.total_debit, 100)
		self.assertEqual(amended.total_credit, 100)
		self.assertEqual(amended.custom_china_voucher_number, old_number)
		self.assertEqual(amended.accounts[1].account, self.replacement_expense)
		self.assertEqual(amended.accounts[1].user_remark, "Corrected expense")
		self.assertEqual(frappe.db.count(doc.doctype, {"amended_from": doc.name}), 1)
		comments = frappe.get_all("Comment", filters={
			"reference_doctype": doc.doctype, "reference_name": amended.name, "comment_type": "Comment",
		}, pluck="content")
		self.assertTrue(any("125.0" in text and "100.0" in text and "借方" in text for text in comments))
		with self.assertRaisesRegex(frappe.ValidationError, "已取消"):
			source_edit.preview_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)

	def test_amount_posting_failure_rolls_back_original_and_amendment(self):
		doc = self._mark_ready(self._journal_entry())
		doc.submit()
		doc.reload()
		with (
			patch.object(source_edit, "complete_source_voucher_edit", side_effect=frappe.ValidationError("forced failure")),
			self.assertRaisesRegex(frappe.ValidationError, "forced failure"),
		):
			source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), self._amount_changes(doc, 100))
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.total_debit, 125)
		self.assertFalse(frappe.db.exists(doc.doctype, {"amended_from": doc.name}))

	def test_linked_bank_amount_cannot_change_even_before_reconciliation(self):
		transaction = self._bank_transaction()
		doc = self._journal_entry()
		doc.custom_china_bank_transaction = transaction.name
		doc.save()
		for posted in (False, True):
			if posted:
				self._mark_ready(doc).submit()
				doc.reload()
			with self.subTest(posted=posted), self.assertRaisesRegex(frappe.ValidationError, "银行收付款金额不能"):
				source_edit.apply_inline_voucher_changes(
					doc.doctype, doc.name, str(doc.modified), self._amount_changes(doc, 100)
				)
			doc.reload()
			self.assertEqual(doc.docstatus, int(posted))
			self.assertEqual(doc.total_debit, 125)
			self.assertFalse(frappe.db.exists(doc.doctype, {"amended_from": doc.name}))

	def test_reconciled_expense_reallocation_preserves_bank_amount_and_allocation(self):
		transaction = self._bank_transaction()
		doc = self._journal_entry()
		doc.accounts[1].debit_in_account_currency = 75
		doc.append("accounts", {
			"account": self.replacement_expense, "debit_in_account_currency": 50, "cost_center": self.cost_center,
		})
		doc.custom_china_bank_transaction = transaction.name
		doc.save()
		self._mark_ready(doc).submit()
		doc.reload()
		changes = [
			{"field": "debit", "edit_key": f"je:{doc.accounts[1].name}", "value": 80},
			{"field": "debit", "edit_key": f"je:{doc.accounts[2].name}", "value": 45},
		]
		result = source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		amended = frappe.get_doc(doc.doctype, result["source_name"])
		self.assertEqual(amended.accounts[0].credit_in_account_currency, 125)
		self.assertEqual(amended.accounts[1].debit_in_account_currency, 80)
		self.assertEqual(amended.accounts[2].debit_in_account_currency, 45)
		transaction.reload()
		self.assertEqual(transaction.status, "Reconciled")
		self.assertEqual(len(transaction.payment_entries), 1)
		self.assertEqual(transaction.payment_entries[0].payment_entry, amended.name)
		self.assertEqual(transaction.payment_entries[0].allocated_amount, 125)

	def test_negative_debit_offset_is_not_converted_to_credit(self):
		doc = self._journal_entry()
		doc.accounts[0].credit_in_account_currency = 0
		doc.accounts[0].debit_in_account_currency = 125
		doc.accounts[1].debit_in_account_currency = -125
		doc.save()
		changes = [
			{"field": "debit", "edit_key": f"je:{doc.accounts[0].name}", "value": "100.35"},
			{"field": "debit", "edit_key": f"je:{doc.accounts[1].name}", "value": "-100.35"},
		]
		source_edit.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), changes)
		doc.reload()
		self.assertEqual(doc.accounts[1].debit_in_account_currency, -100.35)
		self.assertEqual(doc.accounts[1].credit_in_account_currency, 0)

	def test_merged_and_foreign_amounts_are_read_only(self):
		doc = self._journal_entry()
		row = next(row for row in self._ledger_rows(doc.name, "未记账") if row["account"] == self.expense_account)
		mapping = source_edit.get_inline_row_edit_metadata(doc, row)
		row["debit"] = 100
		self.assertFalse(source_edit.get_inline_amount_edit_metadata(doc, row, mapping)["editable"])
		doc.multi_currency = 1
		with self.assertRaisesRegex(frappe.ValidationError, "汇率换算"):
			source_edit._resolve_inline_changes(doc, self._amount_changes(doc, 100))

	def test_posted_change_reuses_controlled_amendment_and_statutory_number(self):
		doc = self._mark_ready(self._journal_entry())
		doc.submit()
		doc.reload()
		row = next(
			row for row in self._ledger_rows(doc.name, "已记账") if row["account"] == self.expense_account
		)
		preview = source_edit.preview_inline_account_changes(
			"Journal Entry",
			doc.name,
			str(doc.modified),
			[{"edit_key": row["edit_key"], "account": self.replacement_expense}],
		)
		self.assertTrue(preview["requires_confirmation"])
		result = source_edit.apply_inline_account_changes(
			"Journal Entry",
			doc.name,
			str(doc.modified),
			[{"edit_key": row["edit_key"], "account": self.replacement_expense}],
		)
		self.assertTrue(result["posted"], result)
		doc.reload()
		amended = frappe.get_doc("Journal Entry", result["source_name"])
		self.assertEqual(doc.docstatus, 2)
		self.assertEqual(amended.docstatus, 1)
		self.assertEqual(amended.amended_from, doc.name)
		self.assertIn(self.replacement_expense, [item.account for item in amended.accounts])
		old_snapshot = frappe.db.get_value(
			"China Accounting Voucher",
			{"source_doctype": doc.doctype, "source_name": doc.name, "source_event": "Posting"},
			["status", "statutory_number"],
			as_dict=True,
		)
		new_snapshot = frappe.db.get_value(
			"China Accounting Voucher",
			{"source_doctype": amended.doctype, "source_name": amended.name, "source_event": "Posting"},
			["status", "statutory_number"],
			as_dict=True,
		)
		self.assertEqual(old_snapshot.status, "Reversed")
		self.assertEqual(new_snapshot.status, "Posted")
		self.assertEqual(old_snapshot.statutory_number, new_snapshot.statutory_number)
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Journal Entry",
				"reference_name": ["in", [doc.name, amended.name]],
				"comment_type": "Comment",
			},
			pluck="content",
		)
		audit_comments = [content for content in comments if "查凭证表内科目更正" in content]
		self.assertEqual(len(audit_comments), 2)
		self.assertTrue(all(self.expense_account in content for content in audit_comments))
		self.assertTrue(all(self.replacement_expense in content for content in audit_comments))
		reversed_rows = self._ledger_rows(doc.name, "已冲销")
		self.assertTrue(reversed_rows)
		self.assertTrue(all(not row["editable_account"] for row in reversed_rows))

	def test_posted_account_and_summary_changes_share_one_amendment(self):
		doc = self._journal_entry()
		doc.accounts[1].user_remark = "原费用摘要"
		doc.save()
		self._mark_ready(doc).submit()
		doc.reload()
		row = next(
			row for row in self._ledger_rows(doc.name, "已记账") if row["account"] == self.expense_account
		)
		result = source_edit.apply_inline_voucher_changes(
			"Journal Entry",
			doc.name,
			str(doc.modified),
			[
				{"field": "account", "edit_key": row["edit_key"], "value": self.replacement_expense},
				{
					"field": "summary",
					"edit_key": row["summary_edit_key"],
					"value": "修改科目后的费用摘要",
				},
			],
		)
		amended = frappe.get_doc("Journal Entry", result["source_name"])
		changed_row = next(item for item in amended.accounts if item.account == self.replacement_expense)
		self.assertEqual(changed_row.user_remark, "修改科目后的费用摘要")
		self.assertEqual(
			frappe.db.count("Journal Entry", {"amended_from": doc.name}),
			1,
		)
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Journal Entry",
				"reference_name": amended.name,
				"comment_type": "Comment",
			},
			pluck="content",
		)
		self.assertTrue(any("查凭证表内科目和摘要修改" in content for content in comments))
		self.assertTrue(any("修改科目后的费用摘要" in content for content in comments))

	def test_user_without_source_read_permission_cannot_preview_changes(self):
		doc = self._journal_entry()
		row = next(
			row for row in self._ledger_rows(doc.name, "未记账") if row["account"] == self.expense_account
		)
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				source_edit.preview_inline_account_changes(
					"Journal Entry",
					doc.name,
					str(doc.modified),
					[{"edit_key": row["edit_key"], "account": self.replacement_expense}],
				)
		finally:
			frappe.set_user("Administrator")

	def test_payment_entry_rows_map_main_deduction_and_tax_accounts(self):
		doc = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"company": self.company,
				"payment_type": "Internal Transfer",
				"paid_from": self.bank,
				"paid_to": self.second_bank,
				"deductions": [{"account": self.expense_account, "amount": 5}],
				"taxes": [{"account_head": self.replacement_expense}],
			}
		)
		doc.name = "PE-INLINE-MAPPING"
		doc.deductions[0].name = "PE-DED-1"
		doc.taxes[0].name = "PE-TAX-1"
		main = source_edit.get_inline_row_edit_metadata(
			doc, frappe._dict(account=self.second_bank, voucher_detail_no=doc.name)
		)
		deduction = source_edit.get_inline_row_edit_metadata(
			doc, frappe._dict(account=self.expense_account, voucher_detail_no="PE-DED-1")
		)
		tax = source_edit.get_inline_row_edit_metadata(
			doc, frappe._dict(account=self.replacement_expense, voucher_detail_no="PE-TAX-1")
		)
		self.assertEqual(main["edit_key"], "pe:field:paid_to")
		self.assertEqual(deduction["edit_key"], "pe:deductions:PE-DED-1")
		self.assertEqual(tax["edit_key"], "pe:taxes:PE-TAX-1")

	def test_payment_entry_main_account_is_saved_through_the_source_field(self):
		doc = self._payment_entry()
		row = next(row for row in self._ledger_rows(doc.name, "未记账") if row["account"] == self.second_bank)
		self.assertEqual(row["edit_key"], "pe:field:paid_to")
		result = source_edit.apply_inline_account_changes(
			"Payment Entry",
			doc.name,
			str(doc.modified),
			[{"edit_key": row["edit_key"], "account": self.third_bank}],
		)
		self.assertEqual(result["mode"], "draft")
		doc.reload()
		self.assertEqual(doc.paid_to, self.third_bank)
		self.assertEqual(doc.paid_to_account_currency, "CNY")

	def test_payment_entry_summary_is_shared_and_saved_as_custom_remarks(self):
		doc = self._payment_entry()
		rows = self._ledger_rows(doc.name, "未记账")
		self.assertGreaterEqual(len(rows), 2)
		self.assertTrue(all(row["editable_summary"] for row in rows))
		self.assertEqual({row["summary_edit_key"] for row in rows}, {"pe-summary:remarks"})

		result = source_edit.apply_inline_voucher_changes(
			"Payment Entry",
			doc.name,
			str(doc.modified),
			[
				{
					"field": "summary",
					"edit_key": "pe-summary:remarks",
					"value": "银行账户内部转账",
				}
			],
		)
		self.assertEqual(result["mode"], "draft")
		doc.reload()
		self.assertEqual(doc.remarks, "银行账户内部转账")
		self.assertEqual(doc.custom_remarks, 1)

	def test_posted_payment_entry_main_account_is_cancelled_amended_and_reposted(self):
		doc = self._payment_entry()
		doc.submit()
		doc.reload()
		row = next(row for row in self._ledger_rows(doc.name, "已记账") if row["account"] == self.second_bank)
		result = source_edit.apply_inline_account_changes(
			"Payment Entry",
			doc.name,
			str(doc.modified),
			[{"edit_key": row["edit_key"], "account": self.third_bank}],
		)
		doc.reload()
		amended = frappe.get_doc("Payment Entry", result["source_name"])
		self.assertEqual(doc.docstatus, 2)
		self.assertEqual(amended.docstatus, 1)
		self.assertEqual(amended.amended_from, doc.name)
		self.assertEqual(amended.paid_to, self.third_bank)

	def test_posted_failure_rolls_back_cancel_and_amendment(self):
		doc = self._mark_ready(self._journal_entry())
		doc.submit()
		doc.reload()
		row = next(
			row for row in self._ledger_rows(doc.name, "已记账") if row["account"] == self.expense_account
		)
		with (
			patch.object(
				source_edit,
				"complete_source_voucher_edit",
				side_effect=frappe.ValidationError("forced posting failure"),
			),
			self.assertRaisesRegex(frappe.ValidationError, "forced posting failure"),
		):
			source_edit.apply_inline_account_changes(
				"Journal Entry",
				doc.name,
				str(doc.modified),
				[{"edit_key": row["edit_key"], "account": self.replacement_expense}],
			)
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertFalse(frappe.db.exists("Journal Entry", {"amended_from": doc.name}))
		self.assertEqual(
			frappe.db.get_value(
				"China Accounting Voucher",
				{"source_doctype": doc.doctype, "source_name": doc.name, "source_event": "Posting"},
				"status",
			),
			"Posted",
		)

	def test_full_bank_reconciliation_is_removed_and_restored_to_amendment(self):
		transaction = self._bank_transaction()
		doc = self._reconciled_journal_entry(transaction)
		row = next(
			row for row in self._ledger_rows(doc.name, "已记账") if row["account"] == self.expense_account
		)
		preview = source_edit.preview_inline_account_changes(
			"Journal Entry",
			doc.name,
			str(doc.modified),
			[{"edit_key": row["edit_key"], "account": self.replacement_expense}],
		)
		self.assertEqual(preview["bank_action"], "unreconcile_and_restore")
		result = source_edit.apply_inline_account_changes(
			"Journal Entry",
			doc.name,
			str(doc.modified),
			[{"edit_key": row["edit_key"], "account": self.replacement_expense}],
		)
		transaction.reload()
		self.assertEqual(transaction.status, "Reconciled")
		self.assertEqual(len(transaction.payment_entries), 1)
		self.assertEqual(transaction.payment_entries[0].payment_entry, result["source_name"])
		self.assertEqual(transaction.payment_entries[0].allocated_amount, 125)

	def test_partial_bank_reconciliation_is_read_only_with_transaction_locator(self):
		transaction = self._bank_transaction()
		doc = self._reconciled_journal_entry(transaction)
		frappe.db.set_value(
			"Bank Transaction Payments",
			transaction.payment_entries[0].name,
			"allocated_amount",
			100,
			update_modified=False,
		)
		frappe.db.set_value(
			"Bank Transaction",
			transaction.name,
			{"allocated_amount": 100, "unallocated_amount": 25, "status": "Unreconciled"},
			update_modified=False,
		)
		row = next(
			row for row in self._ledger_rows(doc.name, "已记账") if row["account"] == self.expense_account
		)
		self.assertFalse(
			row["editable_account"],
			{
				"status": source_edit._build_status(frappe.get_doc(doc.doctype, doc.name)),
				"payments": frappe.get_all(
					"Bank Transaction Payments",
					filters={"parent": transaction.name},
					fields=["payment_document", "payment_entry", "allocated_amount"],
				),
			},
		)
		self.assertEqual(row["inline_bank_transaction"], transaction.name)
		self.assertIn(transaction.name, row["inline_edit_reason"])

	def test_account_query_filters_compatibility_and_closed_period_is_rejected(self):
		doc = self._journal_entry()
		row = next(
			row for row in self._ledger_rows(doc.name, "未记账") if row["account"] == self.expense_account
		)
		query_filters = {
			"source_doctype": "Journal Entry",
			"source_name": doc.name,
			"edit_key": row["edit_key"],
		}
		compatible = source_edit.get_compatible_accounts("Account", "660202", "name", 0, 20, query_filters)
		self.assertIn(self.replacement_expense, [result[0] for result in compatible])
		foreign = source_edit.get_compatible_accounts(
			"Account", "Inline Foreign", "name", 0, 20, query_filters
		)
		self.assertNotIn(self.foreign_expense, [result[0] for result in foreign])
		receivable = get_account_by_number(self.company, "1122").name
		receivables = source_edit.get_compatible_accounts("Account", "1122", "name", 0, 20, query_filters)
		self.assertNotIn(receivable, [result[0] for result in receivables])
		with self.assertRaisesRegex(frappe.ValidationError, "币种"):
			source_edit.preview_inline_account_changes(
				"Journal Entry",
				doc.name,
				str(doc.modified),
				[{"edit_key": row["edit_key"], "account": self.foreign_expense}],
			)
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-08-31")
		with self.assertRaisesRegex(frappe.ValidationError, "冻结"):
			source_edit.preview_inline_account_changes(
				"Journal Entry",
				doc.name,
				str(doc.modified),
				[{"edit_key": row["edit_key"], "account": self.replacement_expense}],
			)
