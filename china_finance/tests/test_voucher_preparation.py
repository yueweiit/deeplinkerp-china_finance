"""Real journal/receipt lifecycle tests, isolated by transaction savepoints."""

import json
import unittest
from unittest.mock import patch

import frappe

from china_finance.services import bank_receipt_import as receipts
from china_finance.services import month_end
from china_finance.services import voucher_preparation as prep
from china_finance.services.closing import preview_reverse_closing, reverse_closing
from china_finance.tests.test_bank_receipt_import import TestReceiptIntegration


class TestVoucherPreparation(unittest.TestCase):
	batch = TestReceiptIntegration.batch
	manual_voucher = TestReceiptIntegration.manual_voucher
	tearDown = TestReceiptIntegration.tearDown

	def setUp(self):
		TestReceiptIntegration.setUp(self)
		frappe.db.set_value(
			"China Finance Settings",
			self.company,
			{
				"enable_voucher_preparation": 1,
				"preparation_from_date": "2026-01-01",
				"enforce_role_separation": 0,
			},
		)
		frappe.clear_document_cache("China Finance Settings", self.company)
		self.addCleanup(frappe.clear_document_cache, "China Finance Settings", self.company)

	def ready(self, doc):
		preview = prep.review_preview([doc.name])[0]
		result = prep.review_drafts(
			[doc.name], versions={doc.name: str(doc.modified)}, cash_plans={doc.name: preview["cash"]["rows"]}
		)
		self.assertTrue(result[0]["success"], result)
		doc.reload()
		return doc

	def run_doc(self):
		return frappe.get_doc(
			{
				"doctype": "China Closing Run",
				"company": self.company,
				"from_date": "2026-08-01",
				"to_date": "2026-08-31",
				"closing_type": "Monthly",
			}
		).insert()

	def test_closing_month_derives_full_calendar_period(self):
		doc = frappe.get_doc(
			{
				"doctype": "China Closing Run",
				"company": self.company,
				"closing_month": "2026-02",
				"closing_type": "Monthly",
			}
		).insert()

		self.assertEqual(str(doc.from_date), "2026-02-01")
		self.assertEqual(str(doc.to_date), "2026-02-28")
		self.assertEqual(doc.closing_month, "2026-02")

	def test_closing_month_rejects_inconsistent_dates(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "China Closing Run",
					"company": self.company,
					"closing_month": "2026-08",
					"from_date": "2026-08-01",
					"to_date": "2026-09-01",
					"closing_type": "Monthly",
				}
			).insert()

	def test_changed_draft_invalidates_review_and_forged_review_is_ignored(self):
		doc = self.ready(self.manual_voucher())
		self.assertTrue(prep.is_ready(doc))
		doc.accounts[0].user_remark = "修改科目摘要"
		doc.save()
		self.assertFalse(doc.custom_china_ready_hash)
		doc.custom_china_ready_hash = prep.content_hash(doc)
		doc.custom_china_ready_by = "Administrator"
		doc.save()
		self.assertFalse(doc.custom_china_ready_hash)
		with self.assertRaises(frappe.ValidationError):
			doc.submit()
		self.assertEqual(frappe.db.count("GL Entry", {"voucher_no": doc.name}), 0)

	def test_trial_draft_moves_to_posted_exactly_once(self):
		doc = self.ready(self.manual_voucher())
		before = prep.trial_balance(self.company, "2026-08-01", "2026-08-31")
		doc.submit()
		after = prep.trial_balance(self.company, "2026-08-01", "2026-08-31")
		b = {r["account"]: r for r in before["rows"]}
		a = {r["account"]: r for r in after["rows"]}
		for account in (self.bank.account, self.account):
			self.assertAlmostEqual(b[account]["expected_balance"], a[account]["expected_balance"], places=2)
		self.assertAlmostEqual(
			b[self.account]["draft_debit"] - a[self.account]["draft_debit"], 987.61, places=2
		)
		self.assertAlmostEqual(
			a[self.account]["posted_debit"] - b[self.account]["posted_debit"], 987.61, places=2
		)

	def test_period_lock_blocks_forms_and_can_pause_without_posting(self):
		doc = self.ready(self.manual_voucher())
		run = self.run_doc()
		with patch.object(
			month_end, "_preflight", return_value=[{"name": doc.name, "hash": prep.content_hash(doc)}]
		):
			month_end.start(run.name)
		doc.user_remark = "并发修改"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		month_end.pause(run.name)
		doc.reload()
		doc.user_remark = "暂停后修改"
		doc.save()
		self.assertFalse(prep.is_ready(doc))
		self.assertEqual(doc.docstatus, 0)

	def test_month_step_failure_resume_keeps_posted_voucher_and_number(self):
		doc = self.ready(self.manual_voucher())
		run = self.run_doc()
		item = {"name": doc.name, "hash": prep.content_hash(doc)}
		with patch.object(month_end, "_preflight", return_value=[item]):
			month_end.start(run.name)
		result = month_end.advance(run.name)
		self.assertEqual(result["completed"], 1, result)
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		number = doc.custom_china_voucher_number
		self.assertTrue(number)
		result = month_end.advance(run.name)
		self.assertEqual(result["state"], "Checking", result)
		with patch.object(
			month_end, "_after_posting", side_effect=frappe.ValidationError("test recoverable issue")
		):
			result = month_end.advance(run.name)
		self.assertEqual(result["state"], "Failed")
		with patch.object(month_end, "_preflight", return_value=[]):
			month_end.start(run.name)
		result = month_end.advance(run.name)
		self.assertEqual(result["state"], "Checking")
		doc.reload()
		self.assertEqual(number, doc.custom_china_voucher_number)
		self.assertEqual(frappe.db.count("China Accounting Voucher", {"source_name": doc.name}), 1)

	def test_month_failure_does_not_advance_or_leave_gl(self):
		doc = self.ready(self.manual_voucher())
		run = self.run_doc()
		with patch.object(
			month_end, "_preflight", return_value=[{"name": doc.name, "hash": prep.content_hash(doc)}]
		):
			month_end.start(run.name)
		with patch.object(
			month_end, "_verify_posted", side_effect=frappe.ValidationError("missing snapshot")
		):
			result = month_end.advance(run.name)
		self.assertEqual(result["state"], "Failed")
		self.assertEqual(result["completed"], 0)
		self.assertIn(doc.name, result["message"])
		self.assertIn("记账失败", result["message"])
		self.assertEqual(frappe.db.get_value("Journal Entry", doc.name, "docstatus"), 0)
		self.assertEqual(frappe.db.count("GL Entry", {"voucher_no": doc.name}), 0)

	def test_resume_retains_cancelled_history_without_reposting_it(self):
		doc = self.ready(self.manual_voucher())
		run = self.run_doc()
		with patch.object(
			month_end, "_preflight", return_value=[{"name": doc.name, "hash": prep.content_hash(doc)}]
		):
			month_end.start(run.name)
		self.assertEqual(month_end.advance(run.name)["completed"], 1)
		month_end.pause(run.name)
		doc.reload()
		doc.cancel()
		with patch.object(month_end, "_preflight", return_value=[]):
			result = month_end.start(run.name)
		self.assertEqual(result["completed"], 0)
		run.reload()
		self.assertEqual(json.loads(run.preparation_data)["superseded"][0]["name"], doc.name)
		self.assertEqual(month_end.advance(run.name)["state"], "Checking")

	def test_month_cannot_be_forged_through_form(self):
		run = self.run_doc()
		run.preparation_state = "Ready"
		run.preparation_data = json.dumps({"done": ["fake"]})
		run.save()
		self.assertFalse(run.preparation_state)
		with self.assertRaises(frappe.ValidationError):
			month_end.finish(run.name)

	def test_batch_creation_retry_and_draft_allowed_only_before_posting(self):
		batch = self.batch()
		row = receipts.preview_import(batch.name)["rows"][0]
		result = receipts.process_import_batch(batch.name, confirmed=1)
		self.assertEqual(result["created"], 1, result)
		retry = receipts.process_import_batch(batch.name, confirmed=1)
		self.assertEqual(retry["created"], 0)
		self.assertEqual(retry["reused"], 1)
		strict = receipts.pending_receipts(self.company, "2026-08-01", "2026-08-31")
		prepare = receipts.pending_receipts(self.company, "2026-08-01", "2026-08-31", allow_drafts=True)
		self.assertIn(row["transaction_id"], [r.transaction_id for r in strict])
		self.assertNotIn(row["transaction_id"], [r.transaction_id for r in prepare])

	def test_review_rejects_a_version_changed_after_preview(self):
		doc = self.manual_voucher()
		preview = prep.review_preview([doc.name])[0]
		doc.user_remark = "another edit"
		doc.save()
		result = prep.review_drafts(
			[doc.name],
			versions={doc.name: preview["modified"]},
			cash_plans={doc.name: preview["cash"]["rows"]},
		)
		self.assertFalse(result[0]["success"])
		doc.reload()
		self.assertFalse(prep.is_ready(doc))

	def test_formal_report_warns_that_drafts_are_excluded(self):
		from china_finance.china_finance.report.china_financial_statements.china_financial_statements import (
			execute,
		)

		self.manual_voucher()
		result = execute(
			{
				"company": self.company,
				"from_date": "2026-08-01",
				"to_date": "2026-08-31",
				"statement_type": "Account Activity and Balance",
			}
		)
		self.assertIn("未记账凭证", result[2])

	def test_draft_preparer_does_not_need_snapshot_read_permission(self):
		from china_finance.china_finance.report.china_voucher_ledger.china_voucher_ledger import execute

		doc = self.manual_voucher()
		user = "prep-" + frappe.generate_hash(length=8) + "@example.invalid"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": user,
				"first_name": "Preparation test",
				"send_welcome_email": 0,
				"roles": [{"role": "Accounts User"}],
			}
		).insert(ignore_permissions=True)
		frappe.set_user(user)
		self.assertFalse(frappe.has_permission("China Accounting Voucher", "read"))
		_columns, rows = execute(
			{
				"company": self.company,
				"from_date": "2026-08-01",
				"to_date": "2026-08-31",
				"source_name": doc.name,
				"voucher_status": "未记账",
			}
		)
		self.assertEqual(len(rows), 2)
		with self.assertRaises(frappe.PermissionError):
			prep.review_drafts([doc.name], versions={doc.name: str(doc.modified)})

	def test_cash_plan_promotes_actual_gl_and_confirms_idempotently(self):
		doc = self.ready(self.manual_voucher())
		self.assertTrue(
			json.loads(doc.custom_china_cash_flow_plan), "fixture must exercise cash classification"
		)
		doc.submit()
		prep.promote_cash_plan(doc)
		prep.promote_cash_plan(doc)
		assignments = frappe.get_all(
			"China Cash Flow Assignment", filters={"source_name": doc.name}, fields=["name", "status"]
		)
		self.assertEqual(len(assignments), 1)
		self.assertEqual(assignments[0].status, "Confirmed")
		assignment = frappe.get_doc("China Cash Flow Assignment", assignments[0].name)
		self.assertAlmostEqual(sum(r.assigned_amount for r in assignment.items), 987.61, places=2)

	def test_whole_month_pipeline_waits_for_gl_before_freezing(self):
		# Existing configuration/report readiness has its own integration suite. Here only
		# those external prerequisites are stubbed; JE/GL/snapshot/PCV/freeze run for real.
		doc = self.ready(self.manual_voucher())
		run = self.run_doc()
		frappe.db.set_single_value("Accounts Settings", "use_legacy_controller_for_pcv", 1)
		self.addCleanup(frappe.clear_document_cache, "Accounts Settings", "Accounts Settings")
		with (
			patch("china_finance.services.closing.run_closing_checks", return_value=[]),
			patch(
				"china_finance.services.cash_flow_assignment.get_assignment_coverage",
				return_value={"passed": True, "details": ""},
			),
		):
			month_end.start(run.name)
			for _ in range(8):
				result = month_end.advance(run.name)
				self.assertNotIn(result["state"], ("Failed", "Paused"), result)
				if result["state"] == "Ready":
					break
			self.assertEqual(result["state"], "Ready", result)
		run.reload()
		self.assertEqual(
			frappe.db.get_value(
				"Period Closing Voucher", run.period_closing_voucher, "gle_processing_status"
			),
			"Completed",
		)
		with (
			patch(
				"china_finance.china_finance.doctype.china_closing_run.china_closing_run.run_closing_checks",
				return_value=[],
			),
			patch(
				"china_finance.china_finance.doctype.china_closing_run.china_closing_run.create_report_snapshots"
			),
		):
			month_end.finish(run.name)
		run.reload()
		self.assertEqual(run.status, "Closed")
		self.assertEqual(
			str(frappe.db.get_value("Company", self.company, "accounts_frozen_till_date")), "2026-08-31"
		)
		self.assertEqual(frappe.db.count("China Accounting Voucher", {"source_name": doc.name}), 1)

		preview = preview_reverse_closing(run.name)
		self.assertEqual([row.name for row in preview["runs"]], [run.name])
		self.assertEqual(
			[row.name for row in preview["period_closing_vouchers"]],
			[run.period_closing_voucher],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "请先在月末结账单"):
			frappe.get_doc("Period Closing Voucher", run.period_closing_voucher).run_method("on_cancel")
		with patch("frappe.enqueue"):
			reversed_result = reverse_closing(run.name, "测试反结账")
		run.reload()
		self.assertEqual(run.status, "Reopened")
		self.assertEqual(reversed_result["cancelled_period_closing_vouchers"], [run.period_closing_voucher])
		self.assertEqual(
			frappe.db.get_value("Period Closing Voucher", run.period_closing_voucher, "docstatus"),
			2,
		)
		self.assertIsNone(frappe.db.get_value("Company", self.company, "accounts_frozen_till_date"))
