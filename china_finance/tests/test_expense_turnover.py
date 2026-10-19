"""Regression coverage for signed expense turnover, using rolled-back GL fixtures."""

import io
import unittest
from unittest.mock import patch

import frappe
import openpyxl
from frappe import _

from china_finance.china_finance.report.china_financial_statements import china_financial_statements as report
from china_finance.services.expense_turnover import get_expense_turnover_offsets


class TestExpenseTurnover(unittest.TestCase):
	def setUp(self):
		# These legacy report fixtures identify the native English total row.
		language = frappe.local.lang
		frappe.local.lang = "en"
		self.addCleanup(setattr, frappe.local, "lang", language)
		self.prefix = "CF_Turnover_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(self.prefix)
		self.addCleanup(frappe.db.rollback, save_point=self.prefix)
		self.company = self.insert("Company", "Company", company_name=self.prefix, abbr=self.prefix[-5:], default_currency="CNY")
		frappe.get_doc({
			"doctype": "China Finance Settings", "name": self.company, "company": self.company,
			"accounting_standard": "小企业会计准则", "enabled": 1,
		}).db_insert()
		self.year = self.insert("Fiscal Year", "Year", year=self.prefix, year_start_date="2026-01-01", year_end_date="2026-12-31")
		self.party = self.insert("Customer", "Customer", customer_name=self.prefix, customer_type="Company")
		self.project = self.insert("Project", "Project", project_name=self.prefix, company=self.company)
		self.book = self.insert("Finance Book", "Book", finance_book_name=self.prefix)
		self.other_book = self.insert("Finance Book", "Other Book", finance_book_name=self.prefix + " Other")
		frappe.db.set_value("Company", self.company, "default_finance_book", self.book, update_modified=False)
		self.cost_root = self.insert("Cost Center", "Cost Root", company=self.company, cost_center_name="Root", is_group=1, lft=1, rgt=6)
		self.cost_center = self.insert("Cost Center", "Cost Center", company=self.company, cost_center_name="Child", parent_cost_center=self.cost_root, lft=2, rgt=3)
		self.other_cost = self.insert("Cost Center", "Other Cost", company=self.company, cost_center_name="Other", parent_cost_center=self.cost_root, lft=4, rgt=5)
		self.expenses = self.account("Expenses", "", "Expense", None, 1, 8)
		self.finance = self.account("Finance", "6603", "Expense", self.expenses, 2, 7)
		self.interest = self.account("Interest", "660302", "Expense", self.finance, 3, 4)
		self.fees = self.account("Fees", "660303", "Expense", self.finance, 5, 6)
		assets = self.account("Assets", "", "Asset", None, 9, 12)
		self.bank = self.account("Bank", "1002", "Asset", assets, 10, 11)
		equity = self.account("Equity", "", "Equity", None, 13, 16)
		self.profit = self.account("Profit", "4103", "Equity", equity, 14, 15)
		self.post(self.fees, debit=104.87)
		self.post(self.interest, credit=0.48)
		self.post(self.fees, credit=104.87, closing=True)
		self.post(self.interest, debit=0.48, closing=True)

	def insert(self, doctype, label, **values):
		name = f"{self.prefix}-{label}"
		frappe.get_doc({"doctype": doctype, "name": name, **values}).db_insert()
		return name

	def account(self, label, number, root_type, parent, lft, rgt):
		return self.insert(
			"Account", label, company=self.company, account_name=label, account_number=number,
			root_type=root_type, report_type="Profit and Loss" if root_type == "Expense" else "Balance Sheet",
			parent_account=parent, is_group=int(rgt - lft > 1), lft=lft, rgt=rgt, account_currency="CNY",
		)

	def post(self, account, debit=0, credit=0, closing=False, **overrides):
		values = {
			"doctype": "GL Entry", "company": self.company, "posting_date": "2026-06-30" if closing else "2026-06-21",
			"fiscal_year": self.year, "voucher_type": "Period Closing Voucher" if closing else "Journal Entry",
			"voucher_no": self.prefix, "is_opening": "No", "is_cancelled": 0,
			"account_currency": "CNY", "cost_center": self.cost_center, "project": self.project,
			"party_type": "Customer", "party": self.party, "finance_book": None,
			**overrides,
		}
		for target, dr, cr in ((account, debit, credit), (self.profit if closing else self.bank, credit, debit)):
			frappe.get_doc({
				**values, "name": frappe.generate_hash(length=10), "account": target,
				"debit": dr, "credit": cr, "debit_in_account_currency": dr, "credit_in_account_currency": cr,
			}).db_insert()

	def filters(self, **overrides):
		return frappe._dict(
			company=self.company, fiscal_year=self.year, from_date="2026-06-01", to_date="2026-06-30",
			statement_type="Account Activity and Balance", expand_party=1,
			**overrides,
		)

	def test_signed_expenses_always_include_closing_with_or_without_party_details(self):
		for statement_type in ("Account Activity and Balance", "Trial Balance"):
			for closing in (0, 1):
				for expand in (0, 1):
					with self.subTest(statement=statement_type, closing=closing, expand=expand):
						filters = self.filters()
						filters.update(statement_type=statement_type, include_period_closing_entries=closing, expand_party=expand)
						rows = report.execute(filters)[1]
						by_number = {row.get("acc_number"): row for row in rows if row.get("acc_number")}
						finance, interest = by_number["6603"], by_number["660302"]
						self.assertAlmostEqual(finance["debit"], 104.39)
						self.assertAlmostEqual(finance["credit"], 104.39)
						self.assertAlmostEqual(finance["closing_debit"], 0)
						self.assertAlmostEqual(interest["debit"], -0.48)
						self.assertAlmostEqual(interest["credit"], -0.48)
						self.assertAlmostEqual(interest["closing_credit"], 0)
						self.assertAlmostEqual(by_number["1002"]["debit"], 0.48)
						self.assertAlmostEqual(by_number["1002"]["credit"], 104.87)
						total = next(row for row in rows if row.get("account") == f"'{_('Total')}'")
						self.assertAlmostEqual(total["debit"], total["credit"])
						self.assertAlmostEqual(total["debit"], 209.74)
						if expand:
							interest_detail = rows[rows.index(interest) + 1]
							self.assertEqual(interest_detail["account"], "")
							self.assertAlmostEqual(interest_detail["debit"], -0.48)
							self.assertAlmostEqual(interest_detail["credit"], -0.48)
		# The report must not rewrite either the ordinary credit or the closing debit.
		values = frappe.db.sql(
			"SELECT SUM(debit), SUM(credit) FROM `tabGL Entry` WHERE company=%s AND account=%s",
			(self.company, self.interest),
		)[0]
		self.assertEqual(tuple(values), (0.48, 0.48))

	def test_offsets_respect_period_cancellation_opening_and_company(self):
		self.post(self.interest, credit=10, posting_date="2026-05-31")
		self.post(self.interest, credit=20, posting_date="2026-07-01")
		self.post(self.interest, credit=30, is_cancelled=1)
		self.post(self.interest, credit=40, is_opening="Yes")
		other_company = self.insert("Company", "Other Company", company_name="Other", default_currency="CNY")
		self.post(self.interest, credit=50, company=other_company)
		filters = self.filters()
		filters.update(include_default_book_entries=1, with_period_closing_entry_for_current_period=1)
		offsets = get_expense_turnover_offsets(filters, [self.interest, self.bank])
		self.assertEqual({row.account for row in offsets}, {self.interest})
		self.assertAlmostEqual(sum(row.offset for row in offsets), 0.96)
		filters.with_period_closing_entry_for_current_period = 0
		self.assertAlmostEqual(sum(row.offset for row in get_expense_turnover_offsets(filters, [self.interest])), 0.48)

	def test_dimensions_and_default_finance_book_match_party_rows(self):
		self.post(self.interest, credit=0.12, finance_book=self.book)
		self.post(self.interest, credit=1, finance_book=self.other_book)
		self.post(self.interest, credit=2, cost_center=self.other_cost)
		other_project = self.insert("Project", "Other Project", project_name="Other", company=self.company)
		self.post(self.interest, credit=4, project=other_project)
		filters = self.filters()
		filters.update(cost_center=self.cost_center, project=self.project, finance_book=self.book)
		rows = report.execute(filters)[1]
		interest = next(row for row in rows if row.get("acc_number") == "660302")
		self.assertAlmostEqual(interest["debit"], -0.60)
		self.assertAlmostEqual(rows[rows.index(interest) + 1]["debit"], -0.60)
		filters.cost_center = self.cost_root
		rows = report.execute(filters)[1]
		interest = next(row for row in rows if row.get("acc_number") == "660302")
		self.assertAlmostEqual(interest["debit"], -2.60)
		self.assertAlmostEqual(rows[rows.index(interest) + 1]["debit"], -2.60)

	def test_prior_period_closing_is_not_moved_into_current_turnover(self):
		filters = self.filters()
		filters.update(from_date="2026-06-25")
		rows = report.execute(filters)[1]
		interest = next(row for row in rows if row.get("acc_number") == "660302")
		self.assertAlmostEqual(interest["opening_credit"], 0.48)
		self.assertAlmostEqual(interest["debit"], 0)
		self.assertAlmostEqual(interest["credit"], -0.48)
		self.assertAlmostEqual(interest["closing_credit"], 0)
		filters.update(from_date="2026-07-01", to_date="2026-07-31", show_zero_values=1)
		rows = report.execute(filters)[1]
		interest = next(row for row in rows if row.get("acc_number") == "660302")
		for field in ("opening_debit", "opening_credit", "debit", "credit", "closing_debit", "closing_credit"):
			self.assertAlmostEqual(interest[field], 0)

	def test_excel_and_pdf_use_the_same_signed_values(self):
		with patch.object(report, "save_file", return_value=frappe._dict(file_url="test.xlsx")) as save:
			report.export_current_report_xlsx(self.filters())
			workbook = openpyxl.load_workbook(io.BytesIO(save.call_args.args[1]), data_only=True)
			values = list(workbook.active.values)
			headers = values[3]
			debit, credit = headers.index(_("本期借方")), headers.index(_("本期贷方"))
			finance = next(row for row in values[4:] if str(row[0]).startswith("6603 -"))
			interest = next(row for row in values[4:] if str(row[0]).startswith("660302 -"))
			self.assertAlmostEqual(finance[debit], 104.39)
			self.assertAlmostEqual(finance[credit], 104.39)
			self.assertAlmostEqual(interest[debit], -0.48)
			self.assertAlmostEqual(interest[credit], -0.48)
		with patch.object(report, "get_pdf", return_value=b"pdf") as pdf, \
			patch.object(report, "save_file", return_value=frappe._dict(file_url="test.pdf")):
			report.export_current_report_pdf(self.filters())
			self.assertIn("104.39", pdf.call_args.args[0])
			self.assertIn("-0.48", pdf.call_args.args[0])
