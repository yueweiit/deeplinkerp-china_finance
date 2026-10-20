"""Native GL/report boundaries for opening balances, rolled back per test."""

import json
import unittest

import frappe

from china_finance.china_finance.report.china_financial_statements import china_financial_statements as report
from china_finance.services import voucher_preparation as preparation
from china_finance.tests import test_expense_turnover as turnover_fixture
from china_finance.tests import test_opening_balance as opening_fixture


class TestOpeningReportBoundary(unittest.TestCase):
	setUp = turnover_fixture.TestExpenseTurnover.setUp
	insert = turnover_fixture.TestExpenseTurnover.insert
	account = turnover_fixture.TestExpenseTurnover.account
	post = turnover_fixture.TestExpenseTurnover.post

	def opening(self, date="2026-01-01"):
		self.post(self.bank, debit=151.77, is_opening="Yes", posting_date=date)
		self.post(self.profit, credit=151.77, is_opening="Yes", posting_date=date)

	def test_cutover_on_report_start_is_opening_not_period_turnover(self):
		self.opening()
		filters = frappe._dict(
			company=self.company, fiscal_year=self.year, from_date="2026-01-01", to_date="2026-01-31",
			statement_type="Trial Balance", expand_party=0,
		)
		rows = report.execute(filters)[1]
		bank = next(row for row in rows if row.get("acc_number") == "1002")
		self.assertAlmostEqual(bank["opening_debit"], 151.77)
		self.assertAlmostEqual(bank["debit"], 0)
		self.assertAlmostEqual(bank["credit"], 0)
		self.assertAlmostEqual(bank["closing_debit"], 151.77)

	def test_future_cutover_does_not_appear_before_activation_date(self):
		self.opening("2026-02-01")
		rows = report.execute(frappe._dict(
			company=self.company, fiscal_year=self.year, from_date="2026-01-01", to_date="2026-01-31",
			statement_type="Trial Balance", expand_party=0,
		))[1]
		bank = next((row for row in rows if row.get("acc_number") == "1002"), {})
		self.assertAlmostEqual(bank.get("opening_debit", 0), 0)
		self.assertAlmostEqual(bank.get("closing_debit", 0), 0)

	def test_preparation_trial_uses_same_opening_classification(self):
		self.opening()
		rows = preparation.trial_balance(self.company, "2026-01-01", "2026-01-31")["rows"]
		bank = next(row for row in rows if row["account"] == self.bank)
		self.assertAlmostEqual(bank["opening"], 151.77)
		self.assertAlmostEqual(bank["posted_debit"], 0)
		self.assertAlmostEqual(bank["expected_balance"], 151.77)

	def test_future_opening_removal_preserves_opposite_balance_and_native_filters(self):
		# Native Trial Balance has already netted each leaf before our projection.
		self.post(self.profit, credit=100, is_opening="Yes", posting_date="2026-02-01")
		self.post(self.profit, debit=150, posting_date="2026-01-15")
		for extra in ({}, {"cost_center": self.cost_root}, {"finance_book": self.book}, {"project": self.project}):
			for expanded in (0, 1):
				with self.subTest(extra=extra, expanded=expanded):
					rows = report.execute(frappe._dict(
						company=self.company, fiscal_year=self.year, from_date="2026-01-01", to_date="2026-01-31",
						statement_type="Trial Balance", expand_party=expanded, **extra,
					))[1]
					bank = next(row for row in rows if row.get("acc_number") == "1002")
					self.assertAlmostEqual(bank["opening_debit"], 0)
					self.assertAlmostEqual(bank["closing_credit"], 150)
					total = next(row for row in rows if row.get("warn_if_negative"))
					self.assertAlmostEqual(total["closing_debit"], 150)
					self.assertAlmostEqual(total["closing_credit"], 150)

	def test_party_details_include_opening_within_period_without_turnover(self):
		self.post(self.profit, credit=100, is_opening="Yes", posting_date="2026-02-01")
		rows = report.execute(frappe._dict(
			company=self.company, fiscal_year=self.year, from_date="2026-01-01", to_date="2026-02-28",
			statement_type="Trial Balance", expand_party=1,
		))[1]
		bank = next(row for row in rows if row.get("acc_number") == "1002")
		detail = rows[rows.index(bank) + 1]
		self.assertEqual(detail["account"], "")
		self.assertAlmostEqual(detail["opening_debit"], 100)
		self.assertAlmostEqual(detail["debit"], 0)
		self.assertAlmostEqual(detail["closing_debit"], 100)

	def test_future_opening_cannot_hide_an_existing_balance_by_netting_to_zero(self):
		self.post(self.profit, debit=100, posting_date="2026-01-15")
		self.post(self.profit, credit=100, is_opening="Yes", posting_date="2026-03-01")
		rows = report.execute(frappe._dict(
			company=self.company, fiscal_year=self.year, from_date="2026-02-01", to_date="2026-02-28",
			statement_type="Trial Balance", expand_party=0,
		))[1]
		bank = next((row for row in rows if row.get("acc_number") == "1002"), {})
		self.assertAlmostEqual(bank.get("opening_credit", 0), 100)
		self.assertAlmostEqual(bank.get("closing_credit", 0), 100)

	def test_period_spanning_cutover_is_consistent_in_all_report_paths(self):
		self.opening("2026-02-01")
		for statement in ("Trial Balance", "Account Activity and Balance"):
			for expanded in (0, 1):
				with self.subTest(statement=statement, expanded=expanded):
					rows = report.execute(frappe._dict(
						company=self.company, fiscal_year=self.year, from_date="2026-01-01", to_date="2026-02-28",
						statement_type=statement, expand_party=expanded,
					))[1]
					bank = next(row for row in rows if (row.get("acc_number") or row.get("account_number")) == "1002")
					self.assertAlmostEqual(bank["opening_debit"], 151.77)
					self.assertAlmostEqual(bank.get("debit", bank.get("period_debit", 0)), 0)
					self.assertAlmostEqual(bank["closing_debit"], 151.77)


class TestOpeningBusinessSeparation(unittest.TestCase):
	setUp = opening_fixture.TestOpeningNative.setUp
	tearDown = opening_fixture.TestOpeningNative.tearDown
	rows = opening_fixture.TestOpeningNative.rows
	save = opening_fixture.TestOpeningNative.save
	activate = opening_fixture.TestOpeningNative.activate

	def test_opening_keeps_audit_coverage_without_formal_voucher_or_daily_row(self):
		from china_finance.china_finance.report.china_voucher_ledger.china_voucher_ledger import execute
		from china_finance.services.closing import count_missing_vouchers
		from china_finance.services.opening_identity import get_opening_sources
		from china_finance.services.voucher import rebuild_missing_vouchers
		from china_finance.services.voucher_number_organization import organize_monthly_voucher_numbers
		from china_finance.services.voucher_number_repair import reorder_monthly_voucher_numbers

		active = self.activate(self.save()["batch"])["batch"]
		source = json.loads(active["receipt_json"])["source_names"][0]
		self.assertIn(source, get_opening_sources(self.company))
		snapshot = frappe.db.get_value("China Accounting Voucher", {"source_name": source},
			["name", "statutory_number", "sequence_number"], as_dict=True)
		self.assertFalse(snapshot.statutory_number)
		self.assertEqual(snapshot.sequence_number, 0)
		self.assertEqual(count_missing_vouchers(self.company, "2026-01-01", "2026-01-31"), 0)
		rows = execute(frappe._dict(company=self.company, from_date="2026-01-01", to_date="2026-01-31"))[1]
		self.assertFalse(any(row.get("source_name") == source for row in rows))
		for organize in (organize_monthly_voucher_numbers, reorder_monthly_voucher_numbers):
			self.assertEqual(organize(self.company, "2026-01")["blockers"], [])
		count = frappe.db.count("China Accounting Voucher", {"company": self.company})
		rebuild_missing_vouchers(self.company, "2026-01-01", "2026-01-31")
		self.assertEqual(frappe.db.count("China Accounting Voucher", {"company": self.company}), count)
		self.assertFalse(frappe.db.get_value("China Accounting Voucher", snapshot.name, "statutory_number"))

	def test_opening_draft_is_excluded_from_month_end_and_preparation_counts(self):
		from china_finance.services.month_end import _drafts

		journal = frappe.get_doc({
			"doctype": "Journal Entry", "company": self.company, "voucher_type": "Opening Entry",
			"is_opening": "Yes", "posting_date": "2026-01-01", "accounts": [
				{"account": self.bank, "debit_in_account_currency": 10},
				{"account": self.equity, "credit_in_account_currency": 10},
			],
		}).insert()
		self.assertEqual(preparation.draft_documents(self.company, "2026-01-01", "2026-01-31"), [])
		self.assertEqual(_drafts(frappe._dict(company=self.company, from_date="2026-01-01", to_date="2026-01-31")), [])
		trial = preparation.trial_balance(self.company, "2026-01-01", "2026-01-31")
		self.assertEqual(trial["draft_count"], 0)
		self.assertEqual(journal.docstatus, 0)
