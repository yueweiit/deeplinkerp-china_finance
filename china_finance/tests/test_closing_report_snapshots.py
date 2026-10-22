"""Closing snapshots use isolated, rolled-back company and template records."""

import hashlib
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate

from china_finance.services.closing import (
	preview_reverse_closing,
	reverse_closing,
	run_closing_checks,
	submit_closing_run,
)
from china_finance.services.closing_reversal import reversal_progress
from china_finance.services.financial_statement import build_statement, snapshot_statement
from china_finance.services.statutory_reporting import generate_statutory_report_package
from china_finance.setup.templates import SMALL_ENTERPRISE_ROWS, build_seed_rows


class TestClosingReportSnapshots(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		language = frappe.local.lang
		frappe.local.lang = "zh"
		self.addCleanup(setattr, frappe.local, "lang", language)
		self.prefix = "_Test Closing Reports " + frappe.generate_hash(length=8)
		frappe.db.savepoint("closing_report_test")
		self.addCleanup(frappe.db.rollback, save_point="closing_report_test")
		self.company = self.insert(
			"Company", company_name=self.prefix, abbr=self.prefix[-5:], default_currency="CNY"
		)
		self.cash = self.insert(
			"Account", "cash", company=self.company, account_name="Cash", root_type="Asset",
			report_type="Balance Sheet", account_type="Cash", account_currency="CNY",
		)
		self.capital = self.insert(
			"Account", "capital", company=self.company, account_name="Capital", root_type="Equity",
			report_type="Balance Sheet", account_currency="CNY",
		)
		# A private standard isolates template selection from installed shared templates.
		self.insert(
			"China Finance Settings", company=self.company, enabled=1, accounting_standard=self.prefix,
			activation_date="2026-01-01", reconciliation_tolerance=0.01, freeze_on_close=1,
			profit_loss_account=self.capital, retained_earnings_account=self.capital,
			require_customer_reconciliation=0, require_supplier_reconciliation=0,
			require_bank_reconciliation=0, enforce_role_separation=0,
		)
		self.templates = {}
		for kind in ("Balance Sheet", "Profit and Loss", "Cash Flow", "Changes in Equity"):
			name = self.insert(
				"China Financial Statement Template", kind, template_key=self.prefix + "|" + kind,
				title=kind, accounting_standard=self.prefix, statement_type=kind, version="3.0",
				effective_from="2026-01-01", is_active=1,
			)
			self.templates[kind] = name
			codes = {
				"Balance Sheet": ("TOTAL_ASSETS", "TOTAL_LIABILITIES_EQUITY"),
				"Profit and Loss": ("NET_PROFIT",),
				"Cash Flow": ("CASH_RECONCILIATION_DIFFERENCE",),
				"Changes in Equity": ("NET_PROFIT",),
			}[kind]
			for idx, code in enumerate(codes, 1):
				self.insert(
					"China Financial Statement Row", kind + code, parent=name, parenttype="China Financial Statement Template",
					parentfield="rows", idx=idx, row_code=code, label=code, row_type="Mapped Accounts",
					balance_direction="Credit Positive" if code == "TOTAL_LIABILITIES_EQUITY" else "Debit Positive",
					show_zero=1,
				)
		for label, account, code in (
			("cash", self.cash, "TOTAL_ASSETS"), ("capital", self.capital, "TOTAL_LIABILITIES_EQUITY")
		):
			self.insert(
				"China Financial Statement Mapping", label, company=self.company,
				template=self.templates["Balance Sheet"], account=account, row_code=code,
				effective_from="2026-01-01", sign_multiplier="1", reviewed=1,
			)
		self.pcv = self.insert(
			"Period Closing Voucher", company=self.company, period_start_date="2026-01-01",
			period_end_date="2026-01-31", docstatus=1, gle_processing_status="Completed",
		)
		name = self.insert(
			"China Closing Run", company=self.company, closing_type="Monthly", from_date="2026-01-01",
			to_date="2026-01-31", period_closing_voucher=self.pcv, status="Draft", naming_series="CNC-.YYYY.-.#####",
		)
		self.run = frappe.get_doc("China Closing Run", name)
		fiscal_year = patch(
			"china_finance.services.financial_statement.get_fiscal_year_start",
			side_effect=lambda company, date: getdate(f"{getdate(date).year}-01-01"),
		)
		fiscal_year.start()
		self.addCleanup(fiscal_year.stop)

	def insert(self, doctype, suffix="", **values):
		name = self.prefix + (" " + suffix if suffix else "")
		frappe.get_doc({"doctype": doctype, "name": name, **values}).db_insert()
		return name

	def make_snapshot(self, kind="Balance Sheet"):
		name = snapshot_statement(self.run, kind)
		doc = frappe.get_doc("China Report Snapshot", name)
		return doc, json.loads(doc.data_json)

	def add_balance(self, label, date, amount, **dimensions):
		for side, account in (("debit", self.cash), ("credit", self.capital)):
			self.insert(
				"GL Entry", label + side, company=self.company, account=account, posting_date=date,
				debit=amount if side == "debit" else 0, credit=amount if side == "credit" else 0,
				is_opening=dimensions.get("is_opening", "No"), is_cancelled=dimensions.get("is_cancelled", 0),
				**{key: value for key, value in dimensions.items() if key not in {"is_opening", "is_cancelled"}},
			)

	def test_year_start_opening_entries_are_in_opening_and_closing_once(self):
		self.add_balance("prior opening", "2025-12-31", 20, is_opening="Yes")
		self.add_balance("opening", "2026-01-01", 100, is_opening="Yes")
		self.add_balance("first day business", "2026-01-01", 7)
		self.add_balance("later opening", "2026-02-01", 50, is_opening="Yes")
		self.add_balance("cancelled opening", "2026-01-01", 1000, is_opening="Yes", is_cancelled=1)
		for start, end, closing in (
			("2026-01-01", "2026-01-31", 127),
			("2026-07-01", "2026-09-30", 177),
			("2026-01-01", "2026-12-31", 177),
		):
			with self.subTest(start=start, end=end):
				result = build_statement(self.company, "Balance Sheet", start, end)
				self.assertTrue(all(row["opening_amount"] == 120 for row in result["rows"]))
				self.assertTrue(all(row["amount"] == closing for row in result["rows"]))

	def test_supplemented_full_opening_without_history_is_not_added_twice(self):
		# A late initialization represents the full opening balance, not current-year income.
		self.add_balance("supplemented full opening", "2026-01-01", 100, is_opening="Yes")
		self.add_balance("current year business", "2026-01-01", 7)
		for start, end in (("2026-01-01", "2026-01-31"), ("2026-07-01", "2026-09-30")):
			with self.subTest(start=start, end=end):
				result = build_statement(self.company, "Balance Sheet", start, end)
				self.assertEqual(len(result["rows"]), 2)
				self.assertTrue(all(row["opening_amount"] == 100 for row in result["rows"]))
				self.assertTrue(all(row["amount"] == 107 for row in result["rows"]))

	def test_fixed_asset_opening_net_and_current_depreciation_use_distinct_periods(self):
		frappe.db.set_value("China Financial Statement Template", self.templates["Balance Sheet"], "is_active", 0)
		template = self.insert(
			"China Financial Statement Template", "fixed assets", template_key=self.prefix + "|fixed assets",
			title="Balance Sheet", accounting_standard=self.prefix, statement_type="Balance Sheet", version="3.0",
			effective_from="2026-01-01", is_active=1,
		)
		for idx, definition in enumerate(build_seed_rows(SMALL_ENTERPRISE_ROWS["Balance Sheet"]), 1):
			self.insert(
				"China Financial Statement Row", definition["row_code"], parent=template,
				parenttype="China Financial Statement Template", parentfield="rows", idx=idx, **definition,
			)
		fixed = self.insert(
			"Account", "fixed", company=self.company, account_name="Fixed assets", root_type="Asset",
			report_type="Balance Sheet", account_type="Fixed Asset", account_currency="CNY",
		)
		accumulated = self.insert(
			"Account", "accumulated", company=self.company, account_name="Accumulated depreciation",
			root_type="Asset", report_type="Balance Sheet", account_type="Accumulated Depreciation", account_currency="CNY",
		)
		expense = self.insert(
			"Account", "expense", company=self.company, account_name="Depreciation expense", root_type="Expense",
			report_type="Profit and Loss", account_currency="CNY",
		)
		for label, account, code in (
			("fixed mapping", fixed, "FIXED_ASSETS"),
			("accumulated mapping", accumulated, "ACCUMULATED_DEPRECIATION"),
			("capital mapping", self.capital, "PAID_IN_CAPITAL"),
		):
			self.insert(
				"China Financial Statement Mapping", label, company=self.company, template=template, account=account,
				row_code=code, effective_from="2026-01-01", sign_multiplier="1", reviewed=1,
			)
		for label, account, debit, credit in (
			("original", fixed, 70212.61, 0), ("accumulated opening", accumulated, 0, 66701.70),
			("opening equity", self.capital, 0, 3510.91),
		):
			self.insert(
				"GL Entry", label, company=self.company, account=account, posting_date="2026-01-01",
				debit=debit, credit=credit, is_opening="Yes", is_cancelled=0,
			)
		for date, amount in (
			("2026-01-31", 967.92), ("2026-02-28", 967.92),
			("2026-03-31", 967.92), ("2026-04-30", 607.15),
		):
			for side, account in (("debit", expense), ("credit", accumulated)):
				self.insert(
					"GL Entry", date + side, company=self.company, account=account, posting_date=date,
					debit=amount if side == "debit" else 0, credit=amount if side == "credit" else 0,
					is_opening="No", is_cancelled=0,
				)
		for start, end, depreciation, net in (
			("2026-01-01", "2026-01-31", 67669.62, 2542.99),
			("2026-04-01", "2026-04-30", 70212.61, 0),
			("2026-01-01", "2026-12-31", 70212.61, 0),
		):
			with self.subTest(end=end):
				result = build_statement(self.company, "Balance Sheet", start, end)
				rows = {row["row_code"]: row for row in result["rows"]}
				self.assertEqual(rows["FIXED_ASSETS"]["opening_amount"], 70212.61)
				self.assertEqual(rows["ACCUMULATED_DEPRECIATION"]["opening_amount"], 66701.70)
				self.assertEqual(rows["FIXED_ASSETS_NET"]["opening_amount"], 3510.91)
				self.assertEqual(rows["NONCURRENT_ASSETS"]["opening_amount"], 3510.91)
				self.assertEqual(rows["FIXED_ASSETS"]["amount"], 70212.61)
				self.assertEqual(rows["ACCUMULATED_DEPRECIATION"]["amount"], depreciation)
				self.assertEqual(rows["FIXED_ASSETS_NET"]["amount"], net)
				self.assertEqual(rows["NONCURRENT_ASSETS"]["amount"], net)
				self.assertEqual(rows["TOTAL_ASSETS"]["amount"], net)
				self.assertEqual(rows["TOTAL_LIABILITIES_EQUITY"]["amount"], net)

	def test_opening_entries_keep_dimension_filters(self):
		for label, amount, book, center, project in (
			("selected", 100, "Book A", "Center A", "Project A"),
			("other book", 200, "Book B", "Center A", "Project A"),
			("other center", 300, "Book A", "Center B", "Project A"),
			("other project", 400, "Book A", "Center A", "Project B"),
		):
			self.add_balance(
				label, "2026-01-01", amount, is_opening="Yes",
				finance_book=book, cost_center=center, project=project,
			)
		result = build_statement(
			self.company, "Balance Sheet", "2026-01-01", "2026-01-31",
			finance_book="Book A", cost_center="Center A", project="Project A",
		)
		self.assertTrue(all(row["opening_amount"] == 100 for row in result["rows"]))
		self.assertTrue(all(row["amount"] == 100 for row in result["rows"]))

	def test_non_calendar_year_uses_fiscal_start_not_january(self):
		self.add_balance("prior business", "2026-06-30", 20)
		self.add_balance("july opening", "2026-07-01", 100, is_opening="Yes")
		self.add_balance("july business", "2026-07-01", 7)
		with patch(
			"china_finance.services.financial_statement.get_fiscal_year_start", return_value=getdate("2026-07-01")
		):
			result = build_statement(self.company, "Balance Sheet", "2026-10-01", "2026-12-31")
		self.assertTrue(all(row["opening_amount"] == 120 for row in result["rows"]))
		self.assertTrue(all(row["amount"] == 127 for row in result["rows"]))

	def test_snapshot_keeps_prior_year_comparison_separate_from_opening(self):
		frappe.db.set_value("China Financial Statement Template", self.templates["Balance Sheet"], "effective_from", "2025-01-01")
		for label in ("cash", "capital"):
			frappe.db.set_value(
				"China Financial Statement Mapping", self.prefix + " " + label, "effective_from", "2025-01-01"
			)
		self.add_balance("prior balance", "2025-12-31", 20)
		self.add_balance("new year opening", "2026-01-01", 100, is_opening="Yes")
		self.add_balance("new year business", "2026-01-01", 7)
		_doc, payload = self.make_snapshot()
		self.assertTrue(all(row["opening_amount"] == 120 for row in payload["rows"]))
		self.assertTrue(all(row["comparison_amount"] == 20 for row in payload["rows"]))
		self.assertTrue(all(row["amount"] == 127 for row in payload["rows"]))

	def test_monthly_missing_comparison_keeps_current_amounts_and_blank_comparatives(self):
		for label, account, debit, credit in (("cash", self.cash, 123, 0), ("capital", self.capital, 0, 123)):
			self.insert(
				"GL Entry", label, company=self.company, account=account, posting_date="2026-01-15",
				debit=debit, credit=credit, is_cancelled=0, is_opening="No",
			)
		doc, payload = self.make_snapshot()
		self.assertEqual(doc.report_status, "草表")
		self.assertEqual(payload["report_status"], "草表")
		self.assertEqual(payload["comparison_status"], "Missing Template")
		self.assertEqual(payload["comparison_to_date"], "2025-12-31")
		self.assertTrue(all(row["amount"] == 123 for row in payload["rows"]))
		self.assertTrue(all(row["comparison_amount"] is None for row in payload["rows"]))
		self.assertIn("2025-12-31", payload["comparison_details"])
		self.assertEqual(doc.sha256, hashlib.sha256(doc.data_json.encode()).hexdigest())

	def test_missing_comparison_is_reported_in_monthly_checks_as_warning(self):
		checks = {row["check_code"]: row for row in run_closing_checks(
			self.company, self.run.from_date, self.run.to_date, self.pcv, "Monthly"
		)}
		self.assertTrue(checks["REPORT_TEMPLATE_COVERAGE"]["passed"])
		self.assertFalse(checks["COMPARISON_TEMPLATE_COVERAGE"]["passed"])
		self.assertEqual(checks["COMPARISON_TEMPLATE_COVERAGE"]["severity"], "Warning")
		self.assertIn("2025-12-31", checks["COMPARISON_TEMPLATE_COVERAGE"]["details"])

	def test_monthly_submit_creates_four_draft_snapshots_and_freezes_period(self):
		with patch("frappe.enqueue") as enqueue:
			result = submit_closing_run(self.run.name)
		self.assertEqual(result["status"], "Closed")
		self.assertEqual(result["docstatus"], 1)
		snapshots = frappe.get_all(
			"China Report Snapshot", filters={"closing_run": self.run.name}, fields=["report_status", "data_json"]
		)
		self.assertEqual(len(snapshots), 4)
		self.assertTrue(all(row.report_status == "草表" for row in snapshots))
		self.assertTrue(all(json.loads(row.data_json)["comparison_status"] == "Missing Template" for row in snapshots))
		self.assertEqual(getdate(frappe.db.get_value("Company", self.company, "accounts_frozen_till_date")), getdate("2026-01-31"))
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])

	def test_deferred_monthly_close_records_warning_and_retains_draft_cash_flow(self):
		frappe.db.set_value("China Finance Settings", self.company,
			"defer_cash_flow_confirmation_on_monthly_close", 1)
		frappe.clear_document_cache("China Finance Settings", self.company)
		self.addCleanup(frappe.clear_document_cache, "China Finance Settings", self.company)
		for template in self.templates.values():
			frappe.db.set_value("China Financial Statement Template", template, "effective_from", "2025-01-01")
		pending = {"passed": False, "count": 10, "amount": 102464.41, "details": "10 pending"}
		with (
			patch("china_finance.services.cash_flow_assignment.get_assignment_coverage", return_value=pending),
			patch("frappe.enqueue"),
		):
			result = submit_closing_run(self.run.name)
			self.assertEqual(result["status"], "Closed")
			self.run.reload()
			check = next(row for row in self.run.checks if row.check_code == "CASH_FLOW_ASSIGNMENT")
			self.assertFalse(check.passed)
			self.assertEqual(check.severity, "Warning")
			self.assertIn("10 pending", check.details)
			name = frappe.db.get_value("China Report Snapshot", {
				"closing_run": self.run.name, "statement_type": "Cash Flow",
			})
			snapshot = frappe.get_doc("China Report Snapshot", name)
			self.assertEqual(snapshot.report_status, "草表")
			payload = json.loads(snapshot.data_json)
			self.assertEqual(payload["comparison_status"], "Available")
			self.assertEqual(payload["cash_flow_assignment_coverage"]["count"], 10)
			self.assertTrue(any(row["code"] == "CASH_FLOW_ASSIGNMENT" and not row["passed"]
				for row in payload["checks"]))
			frappe.db.set_value("China Finance Settings", self.company,
				"defer_cash_flow_confirmation_on_monthly_close", 0)
			frappe.clear_document_cache("China Finance Settings", self.company)
			checks = run_closing_checks(self.company, self.run.from_date, self.run.to_date, self.pcv)
			self.assertEqual(next(row for row in checks
				if row["check_code"] == "CASH_FLOW_ASSIGNMENT")["severity"], "Blocking")

	def test_cash_flow_policy_cannot_change_during_active_monthly_close(self):
		frappe.db.set_value("China Closing Run", self.run.name, "preparation_state", "Checking")
		settings = frappe.get_doc("China Finance Settings", self.company)
		settings.load_doc_before_save()
		# This fixture uses a private standard; validate only the policy-change guard.
		settings.defer_cash_flow_confirmation_on_monthly_close = 1
		with self.assertRaises(frappe.ValidationError):
			settings.run_method("validate")
		frappe.db.set_value("China Closing Run", self.run.name, "preparation_state", "Paused")
		settings.run_method("validate")

	def test_reverse_closing_includes_later_periods_and_cancels_latest_first(self):
		frappe.db.set_value(
			"China Closing Run",
			self.run.name,
			{"docstatus": 1, "status": "Closed", "previous_frozen_date": "2025-12-31"},
		)
		later_pcv = self.insert(
			"Period Closing Voucher",
			"February closing",
			company=self.company,
			period_start_date="2026-02-01",
			period_end_date="2026-02-28",
			docstatus=1, gle_processing_status="Completed",
		)
		later_run = self.insert(
			"China Closing Run",
			"February run",
			company=self.company,
			closing_type="Monthly",
			from_date="2026-02-01",
			to_date="2026-02-28",
			period_closing_voucher=later_pcv,
			status="Closed",
			docstatus=1,
		)
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-02-28")

		preview = preview_reverse_closing(self.run.name)
		self.assertEqual([row.name for row in preview["runs"]], [later_run, self.run.name])

		cancelled = []

		def cancel(voucher):
			cancelled.append(voucher.name)
			frappe.db.set_value("Period Closing Voucher", voucher.name, {"docstatus": 2, "gle_processing_status": "In Progress"})

		with patch(
			"china_finance.overrides.period_closing_voucher.ChinaFinancePeriodClosingVoucher.cancel",
			autospec=True,
			side_effect=cancel,
		), patch("frappe.enqueue") as enqueue:
			result = reverse_closing(self.run.name, "补录一月凭证")

		self.assertEqual(result["status"], "Reopening")
		enqueue.assert_called_once()
		self.assertEqual(cancelled, [later_pcv, self.pcv])
		self.assertEqual([row.name for row in result["reopened_runs"]], [later_run, self.run.name])
		self.assertEqual(
			frappe.get_all(
				"China Closing Run",
				filters={"name": ["in", [self.run.name, later_run]]},
				pluck="status",
			),
			["Reopening", "Reopening"],
		)
		self.assertEqual(
			getdate(frappe.db.get_value("Company", self.company, "accounts_frozen_till_date")),
			getdate("2025-12-31"),
		)

		# Source cancellation alone cannot complete the business transition.
		self.assertEqual(reversal_progress(self.run.name)["status"], "Reopening")
		for pcv in (self.pcv, later_pcv):
			frappe.db.set_value("Period Closing Voucher", pcv, "gle_processing_status", "Completed")
		self.assertEqual(reversal_progress(self.run.name)["status"], "Reopened")
		self.assertEqual(frappe.db.get_value("China Closing Run", later_run, "status"), "Reopened")

	def test_duplicate_period_insert_is_rejected_without_creating_second_draft(self):
		draft = frappe.get_doc({
			"doctype": "China Closing Run", "company": self.company, "closing_type": "Monthly",
			"closing_month": "2026-01", "naming_series": "CNC-.YYYY.-.#####",
		})
		with self.assertRaises(frappe.ValidationError):
			draft.insert()
		self.assertEqual(frappe.db.count("China Closing Run", {
			"company": self.company, "from_date": "2026-01-01", "docstatus": 0,
		}), 1)

	def test_native_gl_insert_is_blocked_during_reversal_for_unlisted_source(self):
		frappe.db.set_value("China Closing Run", self.run.name, {
			"docstatus": 1, "status": "Reopening", "reversal_group": self.run.name,
		})
		source = self.insert("Invoice Discounting", "Unlisted GL source", company=self.company, posting_date="2026-01-15", docstatus=1)
		entry = frappe.get_doc({
			"doctype": "GL Entry", "company": self.company, "posting_date": "2026-01-15",
			"account": self.cash, "voucher_type": "Invoice Discounting", "voucher_no": source,
			"debit": 1, "debit_in_account_currency": 1,
		})
		# Native make_entry also sets ignore_permissions; the period guard still runs.
		entry.flags.ignore_permissions = True
		with self.assertRaisesRegex(frappe.ValidationError, "总账撤销尚未完成"):
			entry.submit()
		self.assertFalse(frappe.db.exists("GL Entry", {
			"company": self.company, "voucher_type": "Invoice Discounting", "voucher_no": source,
		}))

	def test_legacy_confirmation_waits_for_native_gl_completion(self):
		frappe.db.set_value("Period Closing Voucher", self.pcv, "gle_processing_status", "In Progress")
		with self.assertRaises(frappe.ValidationError):
			submit_closing_run(self.run.name)
		self.assertEqual(frappe.db.get_value("China Closing Run", self.run.name, "docstatus"), 0)
		self.assertFalse(frappe.db.get_value("Company", self.company, "accounts_frozen_till_date"))

	def test_year_end_still_rejects_missing_comparison(self):
		self.run.closing_type = "Year End"
		with self.assertRaises(frappe.ValidationError):
			self.make_snapshot()
		self.assertFalse(frappe.db.exists("China Report Snapshot", {"closing_run": self.run.name}))

	def test_current_period_template_is_still_required(self):
		frappe.db.set_value("China Financial Statement Template", self.templates["Balance Sheet"], "is_active", 0)
		with self.assertRaises(frappe.ValidationError):
			self.make_snapshot()
		checks = {row["check_code"]: row for row in run_closing_checks(
			self.company, self.run.from_date, self.run.to_date, self.pcv, "Monthly"
		)}
		self.assertFalse(checks["REPORT_TEMPLATE_COVERAGE"]["passed"])
		self.assertEqual(checks["REPORT_TEMPLATE_COVERAGE"]["severity"], "Blocking")

	def test_available_comparison_preserves_formal_snapshot_behavior(self):
		frappe.db.set_value("China Financial Statement Template", self.templates["Balance Sheet"], "effective_from", "2025-01-01")
		doc, payload = self.make_snapshot()
		self.assertEqual(doc.report_status, "正式")
		self.assertEqual(payload["comparison_status"], "Available")
		self.assertTrue(all(row["comparison_amount"] == 0 for row in payload["rows"]))

	def test_comparison_calculation_errors_are_not_suppressed(self):
		from china_finance.services.financial_statement import build_statement

		frappe.db.set_value("China Financial Statement Template", self.templates["Balance Sheet"], "effective_from", "2025-01-01")
		def calculate(company, kind, from_date, to_date, **kwargs):
			if getdate(to_date).year == 2025:
				raise RuntimeError("comparison calculation failed")
			return build_statement(company, kind, from_date, to_date, **kwargs)
		with patch("china_finance.services.financial_statement.build_statement", side_effect=calculate):
			with self.assertRaisesRegex(RuntimeError, "comparison calculation failed"):
				self.make_snapshot()

	def test_formal_export_rejects_draft_snapshots_even_after_readiness_is_fixed(self):
		with patch("frappe.enqueue"):
			submit_closing_run(self.run.name)
		with patch("china_finance.services.statutory_reporting.get_statutory_report_readiness_data", return_value={"passed": True}):
			with self.assertRaisesRegex(frappe.ValidationError, "草表快照"):
				generate_statutory_report_package(self.run.name, formats=["Excel"])

	def test_draft_snapshot_is_immutable_and_repeat_does_not_overwrite_it(self):
		doc, _payload = self.make_snapshot()
		frappe.db.set_value("China Financial Statement Template", self.templates["Balance Sheet"], "effective_from", "2025-01-01")
		repeated, _ = self.make_snapshot()
		self.assertEqual(repeated.name, doc.name)
		self.assertEqual(repeated.data_json, doc.data_json)
		doc.report_status = "正式"
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
