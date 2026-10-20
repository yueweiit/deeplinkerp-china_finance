"""Exercise the actual report SQL against isolated, in-memory ledger fixtures."""

import re
import sqlite3
import unittest
from decimal import Decimal

from china_finance.services.financial_statement import _append_profit_and_loss_exclusions


class TestProfitLossClosingExclusions(unittest.TestCase):
	def setUp(self):
		self.db = sqlite3.connect(":memory:")
		self.addCleanup(self.db.close)
		self.db.executescript("""
			CREATE TABLE `tabJournal Entry` (
				name TEXT, company TEXT, docstatus INTEGER, user_remark TEXT, remark TEXT, is_opening TEXT
			);
			CREATE TABLE `tabJournal Entry Account` (
				parent TEXT, parenttype TEXT, account TEXT, debit NUMERIC, credit NUMERIC
			);
			CREATE TABLE `tabAccount` (
				name TEXT, company TEXT, root_type TEXT, is_group INTEGER
			);
			CREATE TABLE `tabChina Finance Settings` (company TEXT, profit_loss_account TEXT);
			CREATE TABLE `tabGL Entry` (
				voucher_no TEXT, voucher_type TEXT, company TEXT,
				posting_date TEXT, account TEXT, debit NUMERIC, credit NUMERIC
			);
			INSERT INTO `tabChina Finance Settings` VALUES ('Xian', 'profit');
			INSERT INTO `tabAccount` VALUES
				('profit', 'Xian', 'Equity', 0), ('fees', 'Xian', 'Expense', 0),
				('interest', 'Xian', 'Expense', 0), ('admin', 'Xian', 'Expense', 0),
				('revenue', 'Xian', 'Income', 0), ('bank', 'Xian', 'Asset', 0),
				('retained', 'Xian', 'Equity', 0), ('foreign', 'Other', 'Expense', 0);
		""")

	def add_voucher(self, name, lines, *, date="2026-06-30", company="Xian", status=1,
		remark="Imported source and original row numbers", opening="No", voucher_type="Journal Entry"):
		self.db.execute("INSERT INTO `tabJournal Entry` VALUES (?, ?, ?, ?, ?, ?)",
			(name, company, status, remark, remark, opening))
		for account, debit, credit in lines:
			self.db.execute("INSERT INTO `tabJournal Entry Account` VALUES (?, ?, ?, ?, ?)",
				(name, "Journal Entry", account, debit, credit))
			self.db.execute("INSERT INTO `tabGL Entry` VALUES (?, ?, ?, ?, ?, ?, ?)",
				(name, voucher_type, company, date, account, debit, credit))

	def visible_rows(self):
		conditions = ["gle.company=%(company)s"]
		params = {"company": "Xian"}
		_append_profit_and_loss_exclusions(conditions, params)
		query = "SELECT voucher_no, posting_date, account, debit, credit FROM `tabGL Entry` gle WHERE "
		query += " AND ".join(conditions)
		query = re.sub(r"%\((\w+)\)s", r":\1", query)
		return self.db.execute(query, params).fetchall()

	def test_imported_loss_income_and_reversed_closings_are_excluded(self):
		for name, lines in (
			("loss", [("profit", 15, 0), ("fees", 0, 15)]),
			("income", [("revenue", 15, 0), ("profit", 0, 15)]),
			("mixed", [("profit", 5, 0), ("revenue", 10, 0), ("fees", 0, 15)]),
			("reversal", [("fees", 15, 0), ("profit", 0, 15)]),
		):
			with self.subTest(name=name):
				self.add_voucher(name, lines)
				self.assertNotIn(name, {row[0] for row in self.visible_rows()})

	def test_ordinary_refunds_interest_and_reclassifications_are_preserved(self):
		for name, lines in (
			("refund", [("bank", 0.01, 0), ("fees", 0, 0.01)]),
			("interest", [("bank", 0.04, 0), ("interest", 0, 0.04)]),
			("reclass", [("admin", 15, 0), ("fees", 0, 15)]),
			("mixed_bank", [("profit", 15, 0), ("fees", 0, 10), ("bank", 0, 5)]),
			("allocation", [("retained", 15, 0), ("profit", 0, 15)]),
			("wrong_equity", [("retained", 15, 0), ("fees", 0, 15)]),
		):
			with self.subTest(name=name):
				self.add_voucher(name, lines)
				self.assertIn(name, {row[0] for row in self.visible_rows()})

	def test_incomplete_invalid_and_unposted_vouchers_are_not_classified(self):
		for name, lines, kwargs in (
			("unbalanced", [("profit", 15, 0), ("fees", 0, 14)], {}),
			("foreign_account", [("profit", 15, 0), ("foreign", 0, 15)], {}),
			("missing_account", [("profit", 15, 0), ("missing", 0, 15)], {}),
			("zero_profit", [("profit", 0, 0), ("admin", 15, 0), ("fees", 0, 15)], {}),
			("draft", [("profit", 15, 0), ("fees", 0, 15)], {"status": 0}),
			("cancelled", [("profit", 15, 0), ("fees", 0, 15)], {"status": 2}),
			("opening", [("profit", 15, 0), ("fees", 0, 15)], {"opening": "Yes"}),
		):
			with self.subTest(name=name):
				self.add_voucher(name, lines, **kwargs)
				self.assertIn(name, {row[0] for row in self.visible_rows()})

	def test_configuration_and_account_company_are_required(self):
		self.add_voucher("closing", [("profit", 15, 0), ("fees", 0, 15)])
		for settings in ("DELETE FROM `tabChina Finance Settings`",
			"INSERT INTO `tabChina Finance Settings` VALUES ('Other', 'profit')"):
			self.db.execute(settings)
			self.assertEqual(len(self.visible_rows()), 2)
		self.db.execute("INSERT INTO `tabChina Finance Settings` VALUES ('Xian', 'profit')")
		self.db.execute("UPDATE `tabAccount` SET company='Other' WHERE name='profit'")
		self.assertEqual(len(self.visible_rows()), 2)

	def test_existing_label_and_native_closing_compatibility(self):
		self.add_voucher("labelled", [("bank", 15, 0), ("fees", 0, 15)], remark="结转本期损益")
		self.add_voucher("native", [("profit", 15, 0), ("fees", 0, 15)],
			voucher_type="Period Closing Voucher")
		self.assertFalse(self.visible_rows())

	def test_xian_quarter_and_year_to_date_keep_real_one_cent_offset(self):
		fees = [15.90, 215.90, 15.90, 15.90, 15.90, 15, 15, 15, 15]
		closings = [15.90, 215.90, 15.89, 15.90, 15.90, 14.96, 15, 15]
		for month, amount in enumerate(fees, 1):
			self.add_voucher(f"fee-{month}", [("fees", amount, 0), ("bank", 0, amount)],
				date=f"2026-{month:02d}-15")
		for month, amount in enumerate(closings, 1):
			self.add_voucher(f"closing-{month}", [("profit", amount, 0), ("fees", 0, amount)],
				date=f"2026-{month:02d}-28")
		self.add_voucher("refund", [("bank", 0.01, 0), ("fees", 0, 0.01)], date="2026-03-20")
		for month in (6, 9):
			self.add_voucher(f"interest-{month}", [("bank", 0.04, 0), ("interest", 0, 0.04)],
				date=f"2026-{month:02d}-21")
		self.add_voucher("native", [("profit", 14.96, 0), ("fees", 0, 15), ("interest", 0.04, 0)],
			date="2026-09-30", voucher_type="Period Closing Voucher")
		rows = [row for row in self.visible_rows() if row[2] in {"fees", "interest"}]
		def total(selected):
			return sum((Decimal(str(row[3])) - Decimal(str(row[4])) for row in selected), Decimal(0))
		self.assertEqual(total(rows), Decimal("339.41"))
		self.assertEqual(total(row for row in rows if row[1] >= "2026-07-01"), Decimal("44.96"))
		self.assertIn("refund", {row[0] for row in rows})
		self.assertEqual(self.db.execute(
			"SELECT user_remark FROM `tabJournal Entry` WHERE name='closing-1'"
		).fetchone()[0], "Imported source and original row numbers")
