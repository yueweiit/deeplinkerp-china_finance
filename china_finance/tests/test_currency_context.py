import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import fmt_money

from china_finance.services import currency_context, financial_statement
from china_finance.china_finance.report.china_vat_return_worksheet.china_vat_return_worksheet import execute as vat_worksheet
from china_finance.china_finance.report.china_financial_statements import china_financial_statements as statements
from china_finance.china_finance.report.china_purchase_reconciliation import china_purchase_reconciliation as purchases
from china_finance.china_finance.report.china_business_document_chain.china_business_document_chain import execute as business_chain
from china_finance.china_finance.report.china_output_invoice_reconciliation.china_output_invoice_reconciliation import execute as output_reconciliation
from china_finance.services import purchase_reconciliation, tax_reconciliation


class TestCurrencyCoverage(UnitTestCase):
	def test_every_amount_has_a_resolvable_currency_field(self):
		root = Path(frappe.get_app_path("china_finance", "china_finance", "doctype"))
		docs = {d["name"]: d for p in root.glob("*/*.json") for d in [json.loads(p.read_text())]}
		parents = {}
		for doc in docs.values():
			for field in doc["fields"]:
				if field["fieldtype"] == "Table":
					parents.setdefault(field["options"], []).append(doc)
		for doc in docs.values():
			for field in doc["fields"]:
				if field["fieldtype"] != "Currency":
					continue
				with self.subTest(doctype=doc["name"], field=field["fieldname"]):
					context = field.get("options")
					self.assertTrue(context, "Amount would inherit the unrelated system default")
					if context not in {f["fieldname"] for f in doc["fields"]}:
						self.assertTrue(parents.get(doc["name"]))
						for parent in parents[doc["name"]]:
							self.assertIn(context, {f["fieldname"] for f in parent["fields"]})

	def test_report_preserves_invoice_and_party_currencies(self):
		columns = [{"fieldname": "total", "fieldtype": "Currency"},
			{"fieldname": "outstanding", "fieldtype": "Currency", "options": "party_account_currency"}]
		rows = [{"currency": "EUR", "party_account_currency": "CNY", "total": 100, "outstanding": 780}]
		with patch.object(currency_context, "company_currency", return_value="CNY"):
			currency_context.bind_report_currency(columns, rows, "Company")
		self.assertEqual(rows[0]["currency"], "EUR")
		self.assertEqual(rows[0]["party_account_currency"], "CNY")
		self.assertEqual(rows[0]["outstanding"], 780)

	def test_financial_statements_return_company_currency_for_rows_and_chart(self):
		rows = [{"currency": "USD", "amount": 700}]
		columns = [{"fieldname": "amount", "fieldtype": "Currency"}]
		with patch.object(statements, "_execute", return_value=(columns, rows, "", {})), patch.object(
			currency_context, "company_currency", return_value="CNY"
		), patch.object(statements, "company_currency", return_value="CNY"):
			result = statements.execute({
				"company": "Company", "fiscal_year": "2026",
				"from_date": "2026-01-01", "to_date": "2026-12-31",
			})
		self.assertEqual(result[1][0]["currency"], "CNY")
		self.assertEqual(result[3]["currency"], "CNY")
		self.assertEqual(result[0][0]["options"], "currency")

	def test_statement_display_labels_translate_without_changing_identity_or_amounts(self):
		rows = [{"row_code": "CURRENT_ASSETS", "label": "流动资产", "amount": 700,
			"asset_label": "资产合计", "liability_equity_label": None,
			"source_accounts": ["BUSINESS-ACCOUNT"]}]
		with patch.object(statements, "_execute", return_value=([], rows)), patch.object(
			statements, "_", side_effect=lambda value: {"流动资产": "Current assets", "资产合计": "Total assets"}.get(value, value)
		), patch.object(statements.frappe, "has_permission", return_value=False), patch.object(
			statements, "bind_report_currency"
		):
			result = statements.execute({"company": "Company", "fiscal_year": "2026",
				"from_date": "2026-01-01", "to_date": "2026-12-31"})
		self.assertEqual(result[1][0]["label"], "Current assets")
		self.assertEqual(result[1][0]["asset_label"], "Total assets")
		self.assertEqual(result[1][0]["row_code"], "CURRENT_ASSETS")
		self.assertEqual(result[1][0]["amount"], 700)
		self.assertEqual(result[1][0]["source_accounts"], ["BUSINESS-ACCOUNT"])
		self.assertIsNone(result[1][0]["liability_equity_label"])

	def test_reclassification_warning_translates_parts_and_preserves_source(self):
		translations = {"本期": "Current period", "其他应收款": "Other receivables",
			"其他应付款": "Other payables", "{0}余额展示调整": "Presentation adjustment for {0}",
			"（科目：{0}）": " (Account: {0})",
			"其他应收款贷方余额": "Credit balance of other receivables",
			"{0}：{1}{2} {3} 已按报表展示规则列入 {4}；原始账务未改变。": "{0}: {1}{2} {3} -> {4}"}
		for reason in ("其他应收款余额展示调整", "其他应收款贷方余额"):
			item = {"period": "本期", "reason": reason, "source_account": "业务科目-ID",
				"amount": 123.45, "target_label": "其他应付款"}
			original = item.copy()
			with patch.object(financial_statement, "_", side_effect=lambda value: translations.get(value, value)):
				message = financial_statement._format_reclassification_warning(item)
			self.assertIn("Current period", message)
			self.assertIn("Other payables", message)
			self.assertIn("(Account: 业务科目-ID)", message)
			self.assertIn("123.45", message)
			self.assertNotIn("余额展示调整", message)
			self.assertEqual(item, original)

	def test_mixed_currency_totals_are_suppressed_including_party_currency(self):
		columns = [{"fieldname": "total", "fieldtype": "Currency", "options": "currency"},
			{"fieldname": "outstanding", "fieldtype": "Currency", "options": "party_account_currency"}]
		rows = [{"currency": "USD", "party_account_currency": "CNY", "total": 100, "outstanding": 700},
			{"currency": "USD", "party_account_currency": "USD", "total": 100, "outstanding": 100}]
		with patch.object(currency_context, "company_currency", return_value="CNY"):
			result = currency_context.currency_report_result(columns, rows, "Company")
			self.assertTrue(result[5])
			self.assertIn("币种", result[2])
			rows[1]["party_account_currency"] = "CNY"
			self.assertFalse(currency_context.currency_report_result(columns, rows, "Company")[5])

	def test_nonzero_foreign_amount_without_currency_is_not_relabelled(self):
		columns = [{"fieldname": "outstanding", "fieldtype": "Currency", "options": "party_account_currency"}]
		with patch.object(currency_context, "company_currency", return_value="CNY"):
			with self.assertRaises(frappe.ValidationError):
				currency_context.bind_report_currency(columns, [{"currency": "USD", "outstanding": 700}], "Company")


class TestCurrencyContextIntegration(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.company = f"_Test Currency {frappe.generate_hash(length=10)}"
		frappe.get_doc({"doctype": "Company", "name": self.company, "company_name": self.company,
			"abbr": frappe.generate_hash(length=5), "default_currency": "CNY"}).db_insert()
		frappe.local.field_currency = frappe._dict()

	def make_tax_invoice(self, currency, amount=100):
		doc = frappe.get_doc({"doctype": "China Tax Invoice", "name": frappe.generate_hash(),
			"company": self.company, "currency": currency, "invoice_date": "2026-05-10",
			"docstatus": 1, "invoice_type": "数电普通发票", "invoice_status": "蓝票",
			"direction": "销项", "deduction_status": "不适用", "net_amount": amount,
			"tax_amount": amount * .13, "gross_amount": amount * 1.13})
		doc.db_insert()
		row = doc.append("items", {"net_amount": amount, "tax_rate": 13,
			"tax_amount": amount * .13, "gross_amount": amount * 1.13})
		row.db_insert()
		return doc

	def test_voucher_base_currency_and_account_currency_format_separately(self):
		doc = frappe.get_doc({"doctype": "China Accounting Voucher", "name": frappe.generate_hash(),
			"company": self.company, "currency": "CNY", "total_debit": 700,
			"entries": [{"debit": 700, "debit_in_account_currency": 100, "account_currency": "USD"}]})
		with patch("frappe.defaults.get_defaults", return_value={"currency": "USD"}):
			self.assertEqual(doc.get_formatted("total_debit"), fmt_money(700, currency="CNY"))
			self.assertEqual(doc.entries[0].get_formatted("debit", doc), fmt_money(700, currency="CNY"))
			self.assertEqual(doc.entries[0].get_formatted("debit_in_account_currency", doc), fmt_money(100, currency="USD"))

	def test_cash_flow_child_inherits_company_currency(self):
		doc = frappe.get_doc({"doctype": "China Cash Flow Assignment", "name": frappe.generate_hash(),
			"company": self.company, "items": [{"cash_amount": 500, "assigned_amount": 500}]})
		currency_context.set_document_currency(doc)
		self.assertEqual(doc.currency, "CNY")
		self.assertEqual(doc.items[0].get_formatted("assigned_amount", doc), fmt_money(500, currency="CNY"))

	def test_settlement_children_use_transaction_currency(self):
		doc = frappe.get_doc({"doctype": "China Sales Settlement", "name": frappe.generate_hash(),
			"company": self.company, "currency": "EUR", "items": [{"settlement_amount": 100}]})
		self.assertEqual(doc.items[0].get_formatted("settlement_amount", doc), fmt_money(100, currency="EUR"))

	def test_tax_worksheet_does_not_sum_different_currencies(self):
		self.make_tax_invoice("CNY", 700)
		self.make_tax_invoice("USD", 100)
		result = vat_worksheet({"company": self.company, "from_date": "2026-05-01", "to_date": "2026-05-31"})
		columns, rows = result[:2]
		self.assertEqual({row.currency: row.net_amount for row in rows}, {"CNY": 700, "USD": 100})
		self.assertTrue(all(c["options"] == "currency" for c in columns if c["fieldtype"] == "Currency"))
		self.assertTrue(result[5], "Frappe must not add a mixed-currency total row")

	def test_source_currency_batches_reject_mixed_amounts(self):
		cny = self.make_tax_invoice("CNY")
		usd = self.make_tax_invoice("USD")
		doc = frappe.get_doc({"doctype": "China Input Tax Deduction Batch", "company": self.company,
			"items": [{"tax_invoice": cny.name}, {"tax_invoice": usd.name}]})
		with self.assertRaises(frappe.ValidationError):
			currency_context.set_document_currency(doc)
		doc.set("items", [{"tax_invoice": usd.name}])
		currency_context.set_document_currency(doc)
		self.assertEqual(doc.currency, "USD")

	def test_foreign_bank_snapshot_separates_book_base_and_bank_amounts(self):
		account = frappe.get_doc({"doctype": "Account", "name": frappe.generate_hash(),
			"company": self.company, "account_name": "USD bank", "account_currency": "USD"})
		account.db_insert()
		doc = frappe.get_doc({"doctype": "China Reconciliation Statement", "company": self.company,
			"statement_type": "Bank", "account": account.name, "closing_balance": 700,
			"calculated_bank_balance": 100, "lines": [{"line_source": "Ledger", "debit": 700},
				{"line_source": "Bank Transaction", "debit": 100}]})
		currency_context.set_document_currency(doc)
		self.assertEqual((doc.currency, doc.bank_currency), ("CNY", "USD"))
		self.assertEqual([row.currency for row in doc.lines], ["CNY", "USD"])
		self.assertEqual(doc.get_formatted("closing_balance"), fmt_money(700, currency="CNY"))
		self.assertEqual(doc.get_formatted("calculated_bank_balance"), fmt_money(100, currency="USD"))

	def test_backfill_preserves_amounts_and_existing_foreign_currency(self):
		missing = self.make_tax_invoice(None, 500)
		foreign = self.make_tax_invoice("EUR", 100)
		before = frappe.db.get_value(missing.doctype, missing.name, ["modified", "net_amount", "gross_amount"])
		with patch.object(currency_context, "CURRENCY_DOCTYPES", ("China Tax Invoice",)):
			currency_context.backfill_currency_context()
		self.assertEqual(frappe.db.get_value(missing.doctype, missing.name, "currency"), "CNY")
		self.assertEqual(frappe.db.get_value(foreign.doctype, foreign.name, "currency"), "EUR")
		self.assertEqual(frappe.db.get_value(missing.doctype, missing.name, ["modified", "net_amount", "gross_amount"]), before)

	def make_invoice(self, doctype, currency="USD"):
		doc = frappe.get_doc({"doctype": doctype, "name": frappe.generate_hash(),
			"company": self.company, "currency": currency, "party_account_currency": "CNY",
			"customer": "_Test Currency Customer", "supplier": "_Test Currency Supplier",
			"posting_date": "2026-05-10", "docstatus": 1, "grand_total": 100,
			"outstanding_amount": 700, "base_net_total": 700, "is_return": 0})
		doc.db_insert()
		return doc

	def test_sales_reports_use_invoice_party_and_tax_allocation_currencies(self):
		invoice = self.make_invoice("Sales Invoice")
		tax_invoice = self.make_tax_invoice("EUR")
		allocation = tax_invoice.append("allocations", {"reference_doctype": "Sales Invoice",
			"reference_name": invoice.name, "allocated_gross_amount": 113, "allocated_tax_amount": 13})
		allocation.db_insert()
		filters = {"company": self.company, "from_date": "2026-05-01", "to_date": "2026-05-31"}
		with patch.object(tax_reconciliation, "get_invoice_requirement", return_value={"requirement": "Manual"}):
			columns, rows = output_reconciliation(filters)[:2]
		row = rows[0]
		self.assertEqual((row.currency, row.party_account_currency, row.allocation_currency), ("USD", "CNY", "EUR"))
		self.assertEqual((row.grand_total, row.outstanding_amount, row.allocated_gross_amount), (100, 700, 113))
		self.assertEqual(row.reconciliation_status, "Blocked")
		self.assertIn(frappe._("销售发票与税票币种不一致，未进行汇率换算"), row.exception_reason)
		chain_row = business_chain(filters)[1][0]
		self.assertEqual((chain_row.currency, chain_row.party_account_currency), ("USD", "CNY"))
		request = frappe.get_doc({"doctype": "China Tax Invoice Request", "company": self.company,
			"items": [{"sales_invoice": invoice.name}]})
		request.run_method("before_validate")
		self.assertEqual(request.currency, "USD")

	def test_purchase_order_sql_preserves_context_and_rejects_mixed_invoice_sums(self):
		order = frappe.get_doc({"doctype": "Purchase Order", "name": frappe.generate_hash(),
			"company": self.company, "currency": "USD", "transaction_date": "2026-05-10", "docstatus": 1})
		order.db_insert()
		invoice = self.make_invoice("Purchase Invoice")
		invoice.append("items", {"purchase_order": order.name, "qty": 1, "base_amount": 700}).db_insert()
		filters = {"company": self.company, "from_date": "2026-05-01", "to_date": "2026-05-31", "view_mode": "Purchase Order"}
		policy = {"policy": purchase_reconciliation.POLICY_DIRECT, "quantity_tolerance": .01}
		with patch.object(purchase_reconciliation, "get_reconciliation_policy", return_value=policy):
			row = purchases.execute(filters)[1][0]
			self.assertEqual((row.currency, row.party_account_currency), ("USD", "CNY"))
			self.assertEqual((row.invoice_amount, row.outstanding_amount), (100, 700))
			second = self.make_invoice("Purchase Invoice", "EUR")
			second.append("items", {"purchase_order": order.name, "qty": 1, "base_amount": 700}).db_insert()
			with self.assertRaises(frappe.ValidationError):
				purchases.execute(filters)
		# Exercise the invoice view's real query and payment summary without changing reconciliation policy.
		filters["view_mode"] = "Purchase Invoice"
		with patch.object(purchases, "evaluate_purchase_invoice", side_effect=lambda name: {"reconciliation_status": "Ready"}):
			rows = purchases.execute(filters)[1]
		self.assertEqual({r["currency"] for r in rows}, {"USD", "EUR"})
		self.assertTrue(all(r["party_account_currency"] == "CNY" for r in rows))

	def test_single_currency_native_total_retains_currency(self):
		from frappe.desk.query_report import add_total_row

		self.make_tax_invoice("EUR")
		self.make_tax_invoice("EUR")
		result = vat_worksheet({"company": self.company, "from_date": "2026-05-01", "to_date": "2026-05-31"})
		columns, rows = result[:2]
		self.assertFalse(result[5])
		total = add_total_row(rows, columns)[-1]
		self.assertEqual(total[next(i for i, c in enumerate(columns) if c["fieldname"] == "currency")], "EUR")
		self.assertEqual(total[next(i for i, c in enumerate(columns) if c["fieldname"] == "net_amount")], 200)

	def test_voucher_print_formats_use_base_currency_and_preserve_negative_amounts(self):
		doc = frappe.get_doc({"doctype": "China Accounting Voucher", "name": frappe.generate_hash(),
			"company": self.company, "currency": "CNY", "posting_date": "2026-05-31",
			"total_debit": 104.39, "total_credit": 104.39,
			"entries": [{"account": "_Test Print Account", "debit": 104.87, "credit": 104.39},
				{"account": "_Test Print Account", "debit": -.48}]})
		root = Path(frappe.get_app_path("china_finance", "china_finance", "print_format"))
		with patch("frappe.defaults.get_defaults", return_value={"currency": "USD"}):
			for name in ("china_accounting_voucher", "china_accounting_voucher_a5_landscape"):
				html = frappe.render_template((root / name / (name + ".html")).read_text(), {"doc": doc})
				self.assertIn("CNY", html)
				self.assertIn(fmt_money(-.48, currency="CNY"), html)
				self.assertNotIn("$", html)
