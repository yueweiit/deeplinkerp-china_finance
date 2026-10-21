"""Empty-month cancellation needs an audited outcome, not a fictitious reversal."""

import unittest
from unittest.mock import patch

import frappe

from china_finance.services.voucher import (
	_ensure_cancellation_sync_issue,
	get_pending_cancellation_sync_issues,
	process_cancellation_snapshot,
	retry_cancellation_snapshot,
)


class TestEmptyPeriodClosingCancellation(unittest.TestCase):
	def setUp(self):
		self.prefix = "CF_EMPTY_PCV_" + frappe.generate_hash(length=10)
		frappe.db.savepoint(self.prefix)
		self.addCleanup(frappe.db.rollback, save_point=self.prefix)
		user = frappe.session.user
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, user)
		self.company = self.insert("Company", company_name=self.prefix, abbr=self.prefix[-5:], default_currency="CNY")
		self.source = self.insert(
			"Period Closing Voucher", company=self.company, period_start_date="2026-05-01",
			period_end_date="2026-05-31", docstatus=2, gle_processing_status="Completed",
		)
		self.issue = _ensure_cancellation_sync_issue(frappe.get_doc("Period Closing Voucher", self.source)).name

	def insert(self, doctype, **values):
		doc = frappe.get_doc({"doctype": doctype, "name": self.prefix + "-" + frappe.generate_hash(length=8), **values})
		doc.db_insert()
		return doc.name

	def process(self):
		with patch("china_finance.services.voucher.get_company_settings", return_value=frappe._dict(activation_date="2026-01-01")):
			return process_cancellation_snapshot("Period Closing Voucher", self.source, self.issue)

	def assert_pending(self):
		result = self.process()
		self.assertEqual(result["status"], "pending", result)
		self.assertEqual(frappe.db.get_value("China Voucher Sync Issue", self.issue, "status"), "Pending")
		self.assertFalse(frappe.db.exists("Comment", {"reference_doctype": "China Voucher Sync Issue", "reference_name": self.issue}))
		return result

	def source_gl(self, **values):
		return self.insert("GL Entry", **{
			"company": self.company, "posting_date": "2026-05-31", "is_cancelled": 1,
			"voucher_type": "Period Closing Voucher", "voucher_no": self.source, **values,
		})

	def test_empty_month_resolves_once_without_posting_or_number_allocation(self):
		counts = {name: frappe.db.count(name) for name in ("GL Entry", "China Accounting Voucher", "China Voucher Sequence")}
		self.assertEqual(len(get_pending_cancellation_sync_issues(self.company, "2026-05-01", "2026-05-31")), 1)
		first = retry_cancellation_snapshot(self.issue)
		self.assertEqual(first["status"], "resolved", first)
		self.assertEqual(first["reason"], "empty_period_closing")
		self.assertIsNone(first["voucher"])
		self.assertEqual(self.process(), first)
		self.assertEqual(retry_cancellation_snapshot(self.issue)["issue"], self.issue)
		issue = frappe.get_doc("China Voucher Sync Issue", self.issue)
		self.assertEqual(issue.retry_count, 1)
		self.assertTrue(issue.resolved_on)
		self.assertIsNone(issue.last_error)
		self.assertFalse(issue.cancellation_voucher)
		comments = frappe.get_all("Comment", filters={"reference_doctype": issue.doctype, "reference_name": issue.name}, pluck="content")
		self.assertEqual(len(comments), 1)
		self.assertIn(self.source, comments[0])
		self.assertIn("2026-05-01", comments[0])
		self.assertEqual(get_pending_cancellation_sync_issues(self.company, "2026-05-01", "2026-05-31"), [])
		for name, count in counts.items():
			self.assertEqual(frappe.db.count(name), count, name)
		self.assertEqual(frappe.db.get_value("Period Closing Voucher", self.source, "docstatus"), 2)

	def test_submitted_or_draft_closing_cannot_resolve(self):
		for status in (0, 1):
			with self.subTest(status=status):
				frappe.db.set_value("Period Closing Voucher", self.source, "docstatus", status)
				self.assert_pending()

	def test_failed_running_or_unfinished_native_cancellation_stays_pending(self):
		for status in ("Failed", "In Progress", None):
			with self.subTest(status=status):
				frappe.db.set_value("Period Closing Voucher", self.source, "gle_processing_status", status)
				self.assert_pending()

	def test_processing_error_stays_pending(self):
		frappe.db.set_value("Period Closing Voucher", self.source, "error_message", "Cancellation failed")
		self.assert_pending()

	def test_only_a_complete_calendar_month_qualifies(self):
		for start, end in ((None, "2026-05-31"), ("2026-05-02", "2026-05-31"), ("2026-05-01", "2026-06-30")):
			with self.subTest(start=start, end=end):
				frappe.db.set_value("Period Closing Voucher", self.source, {"period_start_date": start, "period_end_date": end})
				self.assert_pending()

	def test_any_source_gl_blocks_even_zero_cancelled_rows_outside_period(self):
		self.source_gl(posting_date="2026-04-30", debit=0, credit=0)
		self.assert_pending()

	def test_month_with_offsetting_cancelled_business_gl_is_not_empty(self):
		for debit, credit in ((100, 0), (0, 100)):
			self.source_gl(voucher_type="Journal Entry", voucher_no=self.prefix, debit=debit, credit=credit)
		self.assert_pending()

	def test_month_with_active_business_gl_is_not_empty(self):
		self.source_gl(voucher_type="Journal Entry", voucher_no=self.prefix, is_cancelled=0, debit=1)
		self.assert_pending()

	def test_outside_period_and_other_company_business_do_not_block(self):
		self.source_gl(voucher_type="Journal Entry", voucher_no=self.prefix, posting_date="2026-04-30", debit=100)
		other = self.insert("Company", company_name=self.prefix + "Other", abbr="CFO", default_currency="CNY")
		self.source_gl(voucher_type="Journal Entry", voucher_no=self.prefix, company=other, debit=100)
		self.assertEqual(self.process()["status"], "resolved")

	def test_any_snapshot_blocks_empty_resolution_even_cancelled_or_draft(self):
		for status in (0, 1, 2):
			with self.subTest(status=status):
				name = self.insert("China Accounting Voucher", company=self.company, docstatus=status,
					source_doctype="Period Closing Voucher", source_name=self.source, source_event="Posting")
				self.assert_pending()
				frappe.db.delete("China Accounting Voucher", {"name": name})

	def test_mismatched_snapshot_with_matching_source_key_stays_pending(self):
		self.insert("China Accounting Voucher", company=self.company, source_doctype="Journal Entry",
			source_name=self.prefix, source_key=f"Posting|Period Closing Voucher|{self.source}")
		self.assert_pending()

	def test_existing_cancellation_reference_stays_pending(self):
		frappe.db.set_value("China Voucher Sync Issue", self.issue, "cancellation_voucher", self.prefix + "-missing")
		self.assert_pending()

	def test_any_cash_flow_assignment_requires_normal_audit(self):
		name = self.insert("China Cash Flow Assignment", company=self.company,
			source_doctype="Period Closing Voucher", source_name=self.source, status="Cancelled")
		self.assert_pending()
		self.assertEqual(frappe.db.get_value("China Cash Flow Assignment", name, "status"), "Cancelled")

	def test_closing_balance_residue_stays_pending(self):
		self.insert("Account Closing Balance", company=self.company, period_closing_voucher=self.source)
		self.assert_pending()

	def test_native_processing_must_be_cancelled_without_p_l_balances(self):
		name = self.insert("Process Period Closing Voucher", parent_pcv=self.source, docstatus=1, status="Running")
		self.assert_pending()
		frappe.db.set_value("Process Period Closing Voucher", name, {"docstatus": 2, "status": "Cancelled", "p_l_closing_balance": '{"account":100}'})
		self.assert_pending()
		frappe.db.set_value("Process Period Closing Voucher", name, "p_l_closing_balance", "{}")
		self.assertEqual(self.process()["status"], "resolved")

	def test_running_native_processing_detail_stays_pending(self):
		name = self.insert("Process Period Closing Voucher", parent_pcv=self.source, docstatus=2, status="Cancelled")
		self.insert("Process Period Closing Voucher Detail", parent=name, parenttype="Process Period Closing Voucher",
			parentfield="normal_balances", status="Running", processing_date="2026-05-01")
		self.assert_pending()

	def test_native_detail_requires_known_status_and_no_profit_loss_evidence(self):
		name = self.insert("Process Period Closing Voucher", parent_pcv=self.source, docstatus=2, status="Cancelled")
		detail = self.insert("Process Period Closing Voucher Detail", parent=name, parenttype="Process Period Closing Voucher",
			parentfield="normal_balances", status=None, report_type="Profit and Loss", processing_date="2026-05-01")
		self.assert_pending()
		frappe.db.set_value("Process Period Closing Voucher Detail", detail, {"status": "Completed", "closing_balance": '[{"debit": 100}]'})
		self.assert_pending()
		frappe.db.set_value("Process Period Closing Voucher Detail", detail, "closing_balance", "[]")
		self.assertEqual(self.process()["status"], "resolved")

	def test_audit_comment_failure_rolls_back_resolution(self):
		with patch("frappe.model.document.Document.add_comment", side_effect=frappe.ValidationError("audit write failed")):
			result = self.assert_pending()
		self.assertIn("audit write failed", result["error"])

	def test_empty_payment_without_original_still_requires_a_snapshot(self):
		name = self.insert("Payment Entry", company=self.company, posting_date="2026-05-31", docstatus=2)
		with patch("china_finance.services.voucher.get_company_settings", return_value=frappe._dict(activation_date="2026-01-01")):
			result = process_cancellation_snapshot("Payment Entry", name)
		self.assertEqual(result["status"], "pending", result)
		self.assertIn("issue", result)

	def test_retry_retains_finance_manager_permission_check(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			retry_cancellation_snapshot(self.issue)
		self.assertEqual(frappe.db.get_value("China Voucher Sync Issue", self.issue, "status"), "Pending")
