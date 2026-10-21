import json
import unittest
from pathlib import Path
from unittest.mock import patch

import frappe

from china_finance.services.financial_statement import get_statement_source_accounts, render_rows
from china_finance.services.statement_mapping_repair import corrected_cash_flow_classification, repair_cash_flow_mappings
from china_finance.setup.templates import classify_account_number
from china_finance.china_finance.report.china_financial_statements.china_financial_statements import (
	build_balance_sheet_rows,
	execute_native_trial_balance,
	execute_account_activity_balance,
)


def account(number, name):
	return frappe._dict(account_number=number, account_name=name, account_type="Expense", root_type="Expense")


def mapping(account_name, code, **kwargs):
	return frappe._dict(account=account_name, row_code=code, **kwargs)


def row(code, indent=0, formula=None):
	return frappe._dict(row_code=code, label=code, row_type="Formula" if formula else "Mapped Accounts",
		indent=indent, formula=formula, show_zero=1, bold=0)


class TestStatementReconciliation(unittest.TestCase):
	def test_balance_sheet_headings_are_blank_but_zero_net_assets_are_preserved(self):
		rows = [
			{"row_code": "FIXED_ASSETS_HEADING", "label": "Fixed assets", "row_type": "Heading", "amount": 0, "opening_amount": 0},
			{"row_code": "FIXED_ASSETS_NET", "label": "Net fixed assets", "row_type": "Formula", "amount": 0, "opening_amount": 3510.91},
			{"row_code": "TOTAL_ASSETS", "label": "Total assets", "row_type": "Formula", "amount": 0, "opening_amount": 3510.91},
			{"row_code": "LIABILITIES", "label": "Liabilities", "row_type": "Heading", "amount": 0, "opening_amount": 0},
			{"row_code": "TOTAL_LIABILITIES_EQUITY", "label": "Total", "row_type": "Formula", "amount": 0, "opening_amount": 3510.91},
		]
		paired = build_balance_sheet_rows(rows)
		for side in ("asset", "liability_equity"):
			for field in ("opening_amount", "amount", "comparison_amount"):
				self.assertIsNone(paired[0][f"{side}_{field}"])
		self.assertEqual(paired[1]["asset_opening_amount"], 3510.91)
		self.assertEqual(paired[1]["asset_amount"], 0)
		self.assertEqual(paired[1]["liability_equity_opening_amount"], 3510.91)
		self.assertEqual(paired[1]["liability_equity_amount"], 0)

	def test_closing_filter_is_removed_from_both_definitions(self):
		from china_finance.setup.install import CHINA_FINANCIAL_STATEMENT_REPORT_FILTERS
		definition = Path(__file__).resolve().parents[1] / "china_finance/report/china_financial_statements/china_financial_statements.json"
		for filters in (CHINA_FINANCIAL_STATEMENT_REPORT_FILTERS, json.loads(definition.read_text())["filters"]):
			self.assertNotIn("include_period_closing_entries", {f["fieldname"] for f in filters})

	def test_payroll_and_employee_deductions(self):
		for number, name in [("660209", "管理费用－工资"), ("660208", "管理费用－社会保险费"),
			("660229", "管理费用－公积金"), ("122102", "其他应收款-社保"), ("122103", "其他应收款-公积金")]:
			with self.subTest(number=number):
				self.assertEqual(classify_account_number(number, "Cash Flow", account(number, name)),
					("CASH_PAID_EMPLOYEES", "OTHER_OPERATING_RECEIPTS", "CASH_PAID_EMPLOYEES"))

	def test_other_expenses_and_advances_unchanged(self):
		for number, name in [("660201", "办公费"), ("122104", "备用金"), ("122101", "验证款")]:
			self.assertEqual(classify_account_number(number, "Cash Flow", account(number, name))[2], "OTHER_OPERATING_PAYMENTS")

	def test_interest_receipts_operating_but_payments_financing(self):
		self.assertEqual(classify_account_number("660302", "Cash Flow", account("660302", "利息")),
			("CASH_PAID_DIVIDENDS_INTEREST", "OTHER_OPERATING_RECEIPTS", "CASH_PAID_DIVIDENDS_INTEREST"))
		self.assertEqual(classify_account_number("2231", "Cash Flow", account("2231", "应付利息"))[1], "OTHER_FINANCING_RECEIPTS")

	def test_tax_and_payroll_refund_rules_unchanged(self):
		self.assertEqual(classify_account_number("222112", "Cash Flow", account("222112", "个税"))[2], "CASH_PAID_TAXES")
		self.assertEqual(classify_account_number("221101", "Cash Flow", account("221101", "工资"))[1], "OTHER_OPERATING_RECEIPTS")

	def test_repair_only_matches_old_rule_and_is_idempotent(self):
		a = account("660208", "社会保险费")
		m = mapping("a", "OTHER_OPERATING_PAYMENTS", cash_inflow_row_code="OTHER_OPERATING_RECEIPTS",
			cash_outflow_row_code="OTHER_OPERATING_PAYMENTS")
		fixed = corrected_cash_flow_classification(a, m)
		self.assertEqual(fixed[2], "CASH_PAID_EMPLOYEES")
		m.row_code, m.cash_inflow_row_code, m.cash_outflow_row_code = fixed
		self.assertIsNone(corrected_cash_flow_classification(a, m))
		m.cash_outflow_row_code = "CASH_PAID_LONG_TERM_ASSETS"
		self.assertIsNone(corrected_cash_flow_classification(a, m))

	def test_migration_preserves_manual_mappings_by_default(self):
		with patch("frappe.get_all", side_effect=[["Cash Flow Template"], []]) as get_all:
			self.assertEqual(repair_cash_flow_mappings(), [])
			self.assertEqual(get_all.call_args.kwargs["filters"]["mapping_source"], "Automatic")

	def test_both_balance_paths_always_include_closing_despite_old_filters(self):
		module = "china_finance.china_finance.report.china_financial_statements.china_financial_statements"
		for run in (execute_native_trial_balance, execute_account_activity_balance):
			for value in (None, 0, 1):
				filters = frappe._dict(company="Test", from_date="2026-01-01", to_date="2026-12-31",
					include_period_closing_entries=value)
				if value is None:
					filters.pop("include_period_closing_entries")
				with patch("erpnext.accounts.report.trial_balance.trial_balance.execute", return_value=([], [])) as native, \
					patch(module + "._adjust_opening_entries_by_posting_date"), \
					patch(module + "._format_native_account_labels"), \
					patch(module + "._activity_balance_message", return_value=""):
					run(filters)
					self.assertEqual(native.call_args.args[0].with_period_closing_entry_for_current_period, 1)
					self.assertEqual(native.call_args.args[0].with_period_closing_entry_for_opening, 1)

	def test_finance_parent_and_formula_include_interest_source(self):
		template = frappe._dict(accounting_standard="小企业会计准则", statement_type="Profit and Loss",
			rows=[row("NET", formula="0-FINANCE_EXPENSES"), row("FINANCE_EXPENSES", 1), row("INTEREST_EXPENSES", 2)])
		mappings = [mapping("fees", "FINANCE_EXPENSES"), mapping("interest", "INTEREST_EXPENSES")]
		sources = get_statement_source_accounts(template, mappings)
		self.assertEqual(sources["FINANCE_EXPENSES"], {"fees", "interest"})
		self.assertEqual(sources["NET"], {"fees", "interest"})
		values = {r["row_code"]: r["amount"] for r in render_rows(template, {"FINANCE_EXPENSES":104.87, "INTEREST_EXPENSES":-.48})}
		self.assertEqual(values["FINANCE_EXPENSES"], 104.39)

	def test_supplementary_mapping_does_not_double_count(self):
		template = frappe._dict(accounting_standard="小企业会计准则", statement_type="Profit and Loss",
			rows=[row("FINANCE_EXPENSES", 1), row("INTEREST_EXPENSES", 2)])
		mappings = [mapping("interest", "FINANCE_EXPENSES", supplementary_row_code="INTEREST_EXPENSES")]
		sources = get_statement_source_accounts(template, mappings)
		self.assertEqual(sources["FINANCE_EXPENSES"], {"interest"})
		self.assertEqual(sources["INTEREST_EXPENSES"], {"interest"})
		values = render_rows(template, {"FINANCE_EXPENSES": -.48, "INTEREST_EXPENSES": -.48}, {"INTEREST_EXPENSES"})
		self.assertEqual(values[0]["amount"], -.48)

	def test_cash_flow_sources_follow_direction_not_main_row(self):
		template = frappe._dict(statement_type="Cash Flow", rows=[row("IN"), row("OUT"), row("NET", formula="IN-OUT")])
		sources = get_statement_source_accounts(template, [mapping("interest", "OUT", cash_inflow_row_code="IN", cash_outflow_row_code="OUT")])
		self.assertEqual(sources["IN"], {"interest"})
		self.assertEqual(sources["NET"], {"interest"})

	def test_sources_keep_all_mapping_revisions(self):
		template = frappe._dict(statement_type="Profit and Loss", rows=[row("A"), row("B")])
		sources = get_statement_source_accounts(template, [mapping("old_account", "A"), mapping("new_account", "A"), mapping("old_account", "B")])
		self.assertEqual(sources["A"], {"old_account", "new_account"})
		self.assertEqual(sources["B"], {"old_account"})
