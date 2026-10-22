"""Net fixed-asset presentation keeps source GL, native filters and exports intact."""

import copy
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
import openpyxl

from china_finance.china_finance.report.china_financial_statements import china_financial_statements as report
from china_finance.services.fixed_asset_presentation import summarize_fixed_asset_book_value
from china_finance.setup.china_coa_profile import CHART_TEMPLATE
from china_finance.tests import test_expense_turnover as fixtures


class TestFixedAssetBookValue(unittest.TestCase):
	insert = fixtures.TestExpenseTurnover.insert
	account = fixtures.TestExpenseTurnover.account
	post = fixtures.TestExpenseTurnover.post
	filters = fixtures.TestExpenseTurnover.filters

	def setUp(self):
		fixtures.TestExpenseTurnover.setUp(self)
		self.addCleanup(setattr, frappe.local, "lang", frappe.local.lang)
		frappe.local.lang = "zh"
		frappe.db.set_value("Company", self.company, "chart_of_accounts", CHART_TEMPLATE)
		for label, left, right in (("Expenses", 1, 10), ("Assets", 11, 26), ("Bank", 12, 13),
			("Equity", 27, 30), ("Profit", 28, 29)):
			frappe.db.set_value("Account", self.prefix + "-" + label, {"lft": left, "rgt": right})
		self.depreciation_expense = self.account("Depreciation Expense", "660225", "Expense", self.expenses, 8, 9)
		assets = self.prefix + "-Assets"
		self.fixed = self.account("Fixed", "1601", "Asset", assets, 14, 19)
		self.original = self.account("Original", "160104", "Asset", self.fixed, 15, 16)
		self.other_original = self.account("Other Original", "160105", "Asset", self.fixed, 17, 18)
		self.accumulated = self.account("Accumulated", "1602", "Asset", assets, 20, 23)
		self.depreciation = self.account("Depreciation", "160299", "Asset", self.accumulated, 21, 22)
		self.impairment = self.account("Impairment", "1603", "Asset", assets, 24, 25)
		for name in (self.original, self.other_original):
			frappe.db.set_value("Account", name, "account_type", "Fixed Asset")
		frappe.db.set_value("Account", self.depreciation, "account_type", "Accumulated Depreciation")
		self.movement(self.original, self.profit, 70212.61, "2026-01-01", opening=True)
		self.movement(self.profit, self.depreciation, 66701.70, "2026-01-01", opening=True)
		self.movement(self.depreciation_expense, self.depreciation, 3510.91, "2026-06-30")

	def movement(self, debit_account, credit_account, amount, date, opening=False):
		for account, debit, credit in ((debit_account, amount, 0), (credit_account, 0, amount)):
			self.insert("GL Entry", frappe.generate_hash(length=8), company=self.company,
				posting_date=date, fiscal_year=self.year, account=account, voucher_type="Journal Entry",
				voucher_no=self.prefix, debit=debit, credit=credit,
				debit_in_account_currency=debit, credit_in_account_currency=credit,
				account_currency="CNY", is_opening="Yes" if opening else "No", is_cancelled=0,
				cost_center=self.cost_center, project=self.project)

	def rows(self, **overrides):
		filters = self.filters()
		filters.update(overrides)
		return report.execute(filters)[1]

	def summary(self, rows):
		found = [row for row in rows if row.get("fixed_asset_summary")]
		self.assertEqual(len(found), 1)
		return found[0]

	def test_default_hides_original_depreciation_and_their_children(self):
		for expanded in (0, 1):
			with self.subTest(expanded=expanded):
				rows = self.rows(expand_party=expanded)
				net = self.summary(rows)
				self.assertEqual(net["account"], frappe._("固定资产账面价值"))
				self.assertAlmostEqual(net["opening_debit"], 3510.91)
				self.assertEqual(net["opening_credit"], 0)
				self.assertEqual(net["debit"], 0)
				self.assertAlmostEqual(net["credit"], 3510.91)
				self.assertAlmostEqual(net["closing_debit"], 0)
				self.assertAlmostEqual(net["closing_credit"], 0)
				self.assertFalse({"1601", "160104", "160105", "1602", "160299", "1603"} & {row.get("acc_number") for row in rows})

	def test_footer_matches_visible_leaf_rows_and_remains_balanced(self):
		rows = self.rows()
		footer = next(row for row in rows if row.get("warn_if_negative"))
		leaves = [row for row in rows if row.get("account") and not row.get("is_group_account") and not row.get("warn_if_negative")]
		for field in ("opening_debit", "opening_credit", "debit", "credit", "closing_debit", "closing_credit"):
			self.assertAlmostEqual(footer[field], sum(row.get(field, 0) for row in leaves))
		for debit, credit in (("opening_debit", "opening_credit"), ("debit", "credit"), ("closing_debit", "closing_credit")):
			self.assertAlmostEqual(footer[debit], footer[credit])

	def test_detail_option_restores_original_ledger_rows(self):
		for expanded in (0, 1):
			rows = self.rows(expand_party=expanded, show_fixed_asset_details="1")
			self.assertFalse(any(row.get("fixed_asset_summary") for row in rows))
			by_number = {row.get("acc_number"): row for row in rows if row.get("acc_number")}
			self.assertAlmostEqual(by_number["160104"]["opening_debit"], 70212.61)
			self.assertAlmostEqual(by_number["160299"]["opening_credit"], 66701.70)

	def test_legacy_trial_balance_is_unchanged(self):
		rows = self.rows(statement_type="Trial Balance")
		self.assertFalse(any(row.get("fixed_asset_summary") for row in rows))
		self.assertTrue(any(row.get("acc_number") == "160104" for row in rows))

	def test_explicit_string_zero_uses_summary_and_party_query_excludes_it(self):
		with patch.object(report, "_get_activity_party_rows", wraps=report._get_activity_party_rows) as parties:
			self.summary(self.rows(show_fixed_asset_details="0"))
			accounts = parties.call_args.args[1]
			self.assertNotIn(frappe._("固定资产账面价值"), accounts)
			self.assertNotIn(self.original, accounts)
			self.assertNotIn(self.depreciation, accounts)

	def test_acquisitions_and_depreciation_keep_gross_period_turnover(self):
		self.movement(self.original, self.bank, 1000, "2026-06-22")
		net = self.summary(self.rows())
		self.assertEqual(net["debit"], 1000)
		self.assertAlmostEqual(net["credit"], 3510.91)
		self.assertAlmostEqual(net["closing_debit"], 1000)

	def test_impairment_is_included_without_changing_source_gl(self):
		self.movement(self.profit, self.impairment, 100, "2026-01-01", opening=True)
		before = frappe.get_all("GL Entry", filters={"company": self.company}, fields=["name", "account", "debit", "credit"], order_by="name")
		net = self.summary(self.rows())
		self.assertAlmostEqual(net["opening_debit"], 3410.91)
		self.assertAlmostEqual(net["closing_credit"], 100)
		self.assertEqual(before, frappe.get_all("GL Entry", filters={"company": self.company}, fields=["name", "account", "debit", "credit"], order_by="name"))

	def test_nonprofile_chart_uses_types_and_hides_homogeneous_groups(self):
		frappe.db.set_value("Company", self.company, "chart_of_accounts", "Custom chart")
		for name, number in ((self.fixed, "9000"), (self.original, "900001"), (self.other_original, "900002"),
			(self.accumulated, "9100"), (self.depreciation, "910001")):
			frappe.db.set_value("Account", name, "account_number", number)
		rows = self.rows()
		self.assertAlmostEqual(self.summary(rows)["opening_debit"], 3510.91)
		self.assertFalse({"9000", "900001", "9100", "910001"} & {row.get("acc_number") for row in rows})

	def test_native_book_cost_center_and_project_filters_are_preserved(self):
		for filters in ({"finance_book": self.book}, {"cost_center": self.cost_center}, {"project": self.project}):
			with self.subTest(filters=filters):
				self.assertAlmostEqual(self.summary(self.rows(**filters))["opening_debit"], 3510.91)
		for filters in ({"cost_center": self.other_cost}, {"project": "unmatched-project"}):
			with self.subTest(filters=filters):
				self.assertFalse(any(row.get("fixed_asset_summary") for row in self.rows(**filters)))
		# Native Trial Balance disallows mixing another book with default-book entries.
		with self.assertRaises(frappe.ValidationError):
			self.rows(finance_book=self.other_book)

	def test_untyped_nonprofile_chart_is_not_guessed_from_numbers(self):
		frappe.db.set_value("Company", self.company, "chart_of_accounts", "Custom chart")
		for name in (self.original, self.other_original, self.depreciation):
			frappe.db.set_value("Account", name, "account_type", None)
		rows = self.rows()
		self.assertFalse(any(row.get("fixed_asset_summary") for row in rows))
		self.assertTrue(any(row.get("acc_number") == "160104" for row in rows))

	def test_actual_tree_membership_does_not_follow_number_prefix_alone(self):
		frappe.db.set_value("Account", self.bank, "account_number", "160199")
		rows = self.rows()
		self.assertAlmostEqual(self.summary(rows)["opening_debit"], 3510.91)
		self.assertTrue(any(row.get("acc_number") == "160199" for row in rows))

	def test_zero_net_without_activity_obeys_existing_show_zero_option(self):
		period = {"from_date": "2026-07-01", "to_date": "2026-07-31"}
		self.assertFalse(any(row.get("fixed_asset_summary") for row in self.rows(**period)))
		net = self.summary(self.rows(**period, show_zero_values=1))
		for field in ("opening_debit", "opening_credit", "debit", "credit", "closing_debit", "closing_credit"):
			self.assertEqual(net[field], 0)

	def test_excel_and_pdf_use_same_net_row_and_hide_details(self):
		filters = self.filters()
		with patch.object(report, "save_file", return_value=SimpleNamespace(file_url="/private/files/test.xlsx", file_name="test.xlsx")) as save:
			report.export_current_report_xlsx(filters)
			book = openpyxl.load_workbook(io.BytesIO(save.call_args.args[1]), data_only=True)
			values = list(book.active.values)
			net = next(row for row in values if row[0] == frappe._("固定资产账面价值"))
			self.assertAlmostEqual(net[2], 3510.91)
			self.assertFalse(any(str(row[0] or "").startswith(("1601", "1602", "1603")) for row in values))
		columns, rows, message, *_ = report.execute(filters)
		html = report._render_report_pdf(filters, columns, rows, message)
		self.assertIn(frappe._("固定资产账面价值"), html)
		self.assertIn("3,510.91", html)
		self.assertNotIn("160104", html)
		self.assertNotIn("160299", html)

	def test_filter_definitions_keep_default_summary_mode(self):
		from china_finance.setup.install import CHINA_FINANCIAL_STATEMENT_REPORT_FILTERS
		definition = Path(report.__file__).with_suffix(".json")
		for filters in (CHINA_FINANCIAL_STATEMENT_REPORT_FILTERS, json.loads(definition.read_text())["filters"]):
			field = next(item for item in filters if item["fieldname"] == "show_fixed_asset_details")
			self.assertEqual(int(field["default"]), 0)

	def test_mixed_currency_projection_is_rejected(self):
		rows = [
			{"account": self.original, "is_group_account": 0, "currency": "CNY", "opening_debit": 100},
			{"account": self.depreciation, "is_group_account": 0, "currency": "USD", "opening_credit": 10},
		]
		before = copy.deepcopy(rows)
		with self.assertRaises(frappe.ValidationError):
			summarize_fixed_asset_book_value(rows, self.filters())
		self.assertEqual(rows, before)
