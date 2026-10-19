"""Monthly deferral must not hide missing assignments or relax report readiness."""

import unittest
from unittest.mock import patch

import frappe

from china_finance.services import cash_flow_assignment as cash
from china_finance.services import month_end


class TestCashFlowClosingPolicy(unittest.TestCase):
	def setUp(self):
		self.settings = frappe._dict(defer_cash_flow_confirmation_on_monthly_close=0)
		self.coverage = {"passed": False, "count": 10, "amount": 102464.41, "details": "10 pending"}
		settings = patch.object(cash, "get_company_settings", return_value=self.settings)
		coverage = patch.object(cash, "get_assignment_coverage", return_value=self.coverage)
		settings.start()
		coverage.start()
		self.addCleanup(settings.stop)
		self.addCleanup(coverage.stop)

	def check(self, closing_type="Monthly"):
		return cash.get_closing_assignment_check("test-company", "2026-08-01", "2026-08-31", closing_type)

	def test_default_blocks_and_restoring_enforcement_blocks_again(self):
		self.assertEqual(self.check()["severity"], "Blocking")
		self.settings.defer_cash_flow_confirmation_on_monthly_close = 1
		warning = self.check()
		self.assertEqual(warning["severity"], "Warning")
		self.assertFalse(warning["passed"])
		self.assertIn("10 pending", warning["details"])
		self.assertFalse(self.coverage["passed"])
		self.settings.defer_cash_flow_confirmation_on_monthly_close = 0
		self.assertEqual(self.check()["severity"], "Blocking")

	def test_deferral_does_not_relax_year_end_or_missing_cash_scope(self):
		self.settings.defer_cash_flow_confirmation_on_monthly_close = 1
		self.assertEqual(self.check("Year End")["severity"], "Blocking")
		self.coverage.update(count=0, details="missing cash scope")
		self.assertEqual(self.check()["severity"], "Blocking")
		self.coverage.update(passed=True, details="")
		self.assertTrue(self.check()["passed"])

	def test_old_settings_without_the_new_field_keep_enforcement(self):
		self.settings.pop("defer_cash_flow_confirmation_on_monthly_close")
		self.assertEqual(self.check()["severity"], "Blocking")

	def test_after_posting_continues_only_when_explicitly_deferred(self):
		doc = frappe._dict(
			company="test-company", from_date="2026-08-01", to_date="2026-08-31",
			closing_type="Monthly", period_closing_voucher="test-pcv",
		)
		data = {"done": []}
		with (
			patch.object(month_end, "_drafts", return_value=[]),
			patch("frappe.get_list", return_value=[]),
			patch("frappe.db.get_value", return_value=1),
			patch.object(month_end, "_store", side_effect=lambda doc, state, data, message: {
				"state": state, "checks": data["checks"], "message": message,
			}) as store,
		):
			with self.assertRaises(frappe.ValidationError):
				month_end._after_posting(doc, data)
			store.assert_not_called()
			self.settings.defer_cash_flow_confirmation_on_monthly_close = 1
			result = month_end._after_posting(doc, data)
			self.assertEqual(result["state"], "Closing")
			self.assertEqual(result["checks"][0]["severity"], "Warning")
			self.assertFalse(result["checks"][0]["passed"])
			self.assertIn("10 pending", result["message"])
