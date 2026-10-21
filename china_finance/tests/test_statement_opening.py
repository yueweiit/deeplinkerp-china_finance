"""Source report openings remain distinct from postings and account detail."""

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from xml.etree import ElementTree
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate
from frappe.utils.file_manager import save_file
from pypdf import PdfWriter

from china_finance.services.financial_statement import build_statement, snapshot_statement
from china_finance.services.statement_opening import BASELINE, fingerprint, get_definition
from china_finance.setup.templates import SMALL_ENTERPRISE_ROWS, build_seed_rows


class TestStatementOpening(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.prefix = "_Test Source Opening " + frappe.generate_hash(length=8)
		frappe.db.savepoint("source_opening_test")
		self.addCleanup(frappe.db.rollback, save_point="source_opening_test")
		self.company = self.insert("Company", company_name=self.prefix, abbr=self.prefix[-5:], default_currency="CNY")
		self.insert("China Finance Settings", company=self.company, enabled=1, accounting_standard=self.prefix)
		self.template = self.insert(
			"China Financial Statement Template", "template", template_key=self.prefix,
			title="Test", accounting_standard=self.prefix, statement_type="Balance Sheet", version="3.0",
			effective_from="2026-01-01", is_active=1,
		)
		for idx, row in enumerate(build_seed_rows(SMALL_ENTERPRISE_ROWS["Balance Sheet"]), 1):
			self.insert("China Financial Statement Row", row["row_code"], parent=self.template,
				parenttype="China Financial Statement Template", parentfield="rows", idx=idx, **row)
		for label, code, root, amount in (
			("original", "FIXED_ASSETS", "Asset", 100),
			("depreciation", "ACCUMULATED_DEPRECIATION", "Asset", -80),
			("capital", "PAID_IN_CAPITAL", "Equity", -20),
		):
			account = self.insert("Account", label, company=self.company, account_name=label,
				root_type=root, report_type="Balance Sheet", account_currency="CNY")
			self.insert("China Financial Statement Mapping", label, company=self.company,
				template=self.template, account=account, row_code=code, sign_multiplier="1", reviewed=1,
				effective_from="2026-01-01")
			self.insert("GL Entry", label, company=self.company, account=account, posting_date="2026-01-01",
				debit=max(amount, 0), credit=max(-amount, 0), is_opening="Yes", is_cancelled=0)
		fy = patch("china_finance.services.financial_statement.get_fiscal_year_start",
			side_effect=lambda company, date: getdate(f"{getdate(date).year}-01-01"))
		fy.start()
		self.addCleanup(fy.stop)

	def insert(self, doctype, suffix="", **values):
		name = self.prefix + (" " + suffix if suffix else "")
		frappe.get_doc({"doctype": doctype, "name": name, **values}).db_insert()
		return name

	def baseline(self):
		# These are direct filing values. Account details are deliberately absent.
		amounts = {
			"CASH": 27495.50, "ACCOUNTS_RECEIVABLE": 3039991.88, "INVENTORIES": 885631.85,
			"CURRENT_ASSETS": 3953119.23, "FIXED_ASSETS_NET": 3510.91, "NONCURRENT_ASSETS": 3510.91,
			"TOTAL_ASSETS": 3956630.14, "ACCOUNTS_PAYABLE": 210437.10, "OTHER_PAYABLES": 5047059.76,
			"CURRENT_LIABILITIES": 5257496.86, "NONCURRENT_LIABILITIES": 0, "TOTAL_LIABILITIES": 5257496.86,
			"PAID_IN_CAPITAL": 50000, "RETAINED_EARNINGS": -1350866.72, "OWNERS_EQUITY": -1300866.72,
			"TOTAL_LIABILITIES_EQUITY": 3956630.14,
		}
		doc = frappe.get_doc({"doctype": BASELINE, "company": self.company,
			"opening_date": "2026-01-01", "source_period_end": "2025-12-31", "template": self.template,
			"rows": [{"row_code": code, "source_label": code, "provided": 1, "amount": value}
				for code, value in amounts.items()]})
		doc.insert()
		pdf = PdfWriter()
		pdf.add_blank_page(width=72, height=72)
		pdf.add_metadata({"/Title": self.prefix})
		buffer = io.BytesIO()
		pdf.write(buffer)
		content = buffer.getvalue()
		file = save_file("opening.pdf", content, BASELINE, doc.name, is_private=1)
		self.pdf_content = Path(file.get_full_path()).read_bytes()
		self.addCleanup(Path(file.get_full_path()).unlink, missing_ok=True)
		doc.source_file = file.file_url
		doc.save()
		return doc

	def report(self, **filters):
		return build_statement(self.company, "Balance Sheet", "2026-01-01", "2026-09-30", **filters)

	def test_draft_does_not_override_ledger(self):
		self.baseline()
		result = self.report()
		self.assertIsNone(result["opening_source"])
		self.assertEqual(next(r for r in result["rows"] if r["row_code"] == "FIXED_ASSETS_NET")["opening_amount"], 20)

	def test_confirmed_amounts_are_direct_and_missing_details_stay_blank(self):
		doc = self.baseline()
		before = frappe.db.count("GL Entry", {"company": self.company})
		doc.submit()
		result = self.report()
		rows = {row["row_code"]: row for row in result["rows"]}
		self.assertEqual(rows["FIXED_ASSETS_NET"]["opening_amount"], 3510.91)
		self.assertEqual(rows["TOTAL_ASSETS"]["opening_amount"], 3956630.14)
		self.assertEqual(rows["RETAINED_EARNINGS"]["opening_amount"], -1350866.72)
		self.assertIsNone(rows["FIXED_ASSETS"]["opening_amount"])
		self.assertIsNone(rows["ACCUMULATED_DEPRECIATION"]["opening_amount"])
		self.assertIsNone(rows["FINISHED_GOODS"]["opening_amount"])
		self.assertEqual(rows["FIXED_ASSETS_NET"]["amount"], 20)
		self.assertEqual(frappe.db.count("GL Entry", {"company": self.company}), before)
		self.assertEqual(result["opening_source"]["fingerprint"], doc.fingerprint)
		self.assertEqual(result["opening_source"]["source_sha256"], hashlib.sha256(self.pdf_content).hexdigest())
		self.assertTrue(any(d["row_code"] == "FIXED_ASSETS_NET" and d["ledger_amount"] == 20 for d in result["opening_reconciliation"]))
		self.assertTrue(any(c["code"] == "OPENING_BASELINE_RECONCILIATION" and not c["passed"] for c in result["checks"]))

	def test_dimension_filters_do_not_use_whole_company_baseline(self):
		self.baseline().submit()
		for filters in ({"finance_book": "Other book"}, {"cost_center": "Other center"}, {"project": "Other project"}):
			with self.subTest(filters=filters):
				result = self.report(**filters)
				self.assertIsNone(result["opening_source"])
				self.assertTrue(result["warnings"])
				self.assertEqual(next(r for r in result["rows"] if r["row_code"] == "TOTAL_ASSETS")["opening_amount"], 0)

	def test_duplicate_confirmation_is_rejected_and_cancellation_allows_revision(self):
		first = self.baseline().submit()
		second = self.baseline()
		with self.assertRaises(frappe.ValidationError):
			second.submit()
		first.cancel()
		second.reload().submit()
		self.assertEqual(self.report()["opening_source"]["name"], second.name)
		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc(BASELINE, first.name)

	def test_missing_or_inconsistent_totals_cannot_be_confirmed(self):
		for value in (3956631.14, -1):
			doc = self.baseline()
			next(row for row in doc.rows if row.row_code == "TOTAL_ASSETS").amount = value
			with self.assertRaises(frappe.ValidationError):
				doc.submit()

	def test_inconsistent_supplied_formula_inputs_are_rejected(self):
		doc = self.baseline()
		next(row for row in doc.rows if row.row_code == "NONCURRENT_ASSETS").amount = 4000
		with self.assertRaises(frappe.ValidationError):
			doc.submit()

	def test_source_and_year_must_be_explicit(self):
		doc = self.baseline()
		doc.source_file = None
		with self.assertRaises(frappe.ValidationError):
			doc.submit()
		doc = self.baseline()
		doc.opening_date = "2026-02-01"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		doc = self.baseline()
		doc.source_period_end = "2024-12-31"
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_definition_contains_projects_not_account_leaves(self):
		definition = get_definition(self.company, "2026-09-30")
		self.assertEqual(definition["opening_date"], "2026-01-01")
		self.assertIn("FIXED_ASSETS_NET", {row["row_code"] for row in definition["rows"]})
		self.assertNotIn("FIXED_ASSETS_HEADING", {row["row_code"] for row in definition["rows"]})

	def test_confirmed_data_cannot_be_changed_or_silently_recomputed(self):
		doc = self.baseline().submit()
		next(row for row in doc.rows if row.row_code == "FIXED_ASSETS_NET").amount = 999
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		frappe.db.set_value("China Statement Opening Balance Line", {"parent": doc.name, "row_code": "FIXED_ASSETS_NET"}, "amount", 999)
		with self.assertRaises(frappe.ValidationError):
			self.report()

	def test_template_changes_require_review(self):
		self.baseline().submit()
		frappe.db.set_value("China Financial Statement Template", self.template, "version", "4.0")
		frappe.clear_document_cache("China Financial Statement Template", self.template)
		with self.assertRaises(frappe.ValidationError):
			self.report()

	def test_cancelled_baseline_restores_ledger_opening_without_deleting_evidence(self):
		doc = self.baseline().submit()
		doc.cancel()
		result = self.report()
		self.assertIsNone(result["opening_source"])
		self.assertTrue(frappe.db.exists(BASELINE, doc.name))
		self.assertTrue(frappe.db.get_value(BASELINE, doc.name, "source_sha256"))
		self.assertEqual(next(r for r in result["rows"] if r["row_code"] == "FIXED_ASSETS_NET")["opening_amount"], 20)

	def test_confirmed_pdf_cannot_be_deleted_or_published(self):
		doc = self.baseline().submit()
		file = frappe.get_doc("File", {"file_url": doc.source_file})
		with self.assertRaises(frappe.ValidationError):
			file.delete()
		file.is_private = 0
		with self.assertRaises(frappe.ValidationError):
			file.save()

	def test_missing_input_is_not_zero_and_unknown_project_is_rejected(self):
		doc = self.baseline()
		doc.append("rows", {"row_code": "FIXED_ASSETS", "provided": 0, "amount": 999})
		doc.save().submit()
		rows = {row["row_code"]: row for row in self.report()["rows"]}
		self.assertIsNone(rows["FIXED_ASSETS"]["opening_amount"])
		self.assertEqual(rows["FIXED_ASSETS_NET"]["opening_amount"], 3510.91)
		other = self.baseline()
		other.append("rows", {"row_code": "invented", "provided": 1, "source_label": "Unknown", "amount": 1})
		with self.assertRaises(frappe.ValidationError):
			other.save()

	def test_other_year_and_company_cannot_reuse_baseline(self):
		from china_finance.services.statement_opening import resolve_opening

		self.baseline().submit()
		template = frappe.get_doc("China Financial Statement Template", self.template)
		for company, date in ((self.company, "2025-01-01"), (self.prefix + " other", "2026-01-01")):
			self.assertEqual(resolve_opening(company, date, template, {}), (None, None, [], []))

	def test_changed_pdf_is_detected_without_falling_back_to_ledger(self):
		doc = self.baseline().submit()
		file = frappe.get_doc("File", {"file_url": doc.source_file})
		Path(file.get_full_path()).write_bytes(b"%PDF-1.4\nchanged source")
		with self.assertRaises(frappe.ValidationError):
			self.report()

	def test_snapshot_retains_confirmed_source_after_later_revision(self):
		first = self.baseline().submit()
		name = self.insert("China Closing Run", "run", company=self.company, closing_type="Monthly",
			from_date="2026-01-01", to_date="2026-01-31", status="Draft")
		run = frappe.get_doc("China Closing Run", name)
		snapshot = frappe.get_doc("China Report Snapshot", snapshot_statement(run, "Balance Sheet"))
		original = snapshot.data_json
		payload = json.loads(original)
		self.assertEqual(payload["opening_source"]["name"], first.name)
		self.assertEqual(payload["opening_source"]["fingerprint"], first.fingerprint)
		first.cancel()
		second = self.baseline().submit()
		self.assertEqual(self.report()["opening_source"]["name"], second.name)
		self.assertEqual(snapshot_statement(run, "Balance Sheet"), snapshot.name)
		self.assertEqual(snapshot.reload().data_json, original)

	def test_finance_reader_cannot_create_or_cancel_baselines(self):
		baseline = self.baseline().submit()
		user = frappe.get_doc({"doctype": "User", "email": self.prefix[-8:] + "@example.invalid",
			"first_name": "Opening reader", "send_welcome_email": 0,
			"roles": [{"role": "China Finance User"}]}).insert()
		frappe.set_user(user.name)
		try:
			frappe.get_doc(BASELINE, baseline.name).check_permission("read")
			self.assertEqual(self.report()["opening_source"]["name"], baseline.name)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(BASELINE, baseline.name).cancel()
			with self.assertRaises(frappe.PermissionError):
				get_definition(self.company, "2026-01-01")
		finally:
			frappe.set_user("Administrator")

	def test_duplicate_and_heading_projects_cannot_be_saved(self):
		for code in ("TOTAL_ASSETS", "FIXED_ASSETS_HEADING"):
			doc = self.baseline()
			doc.append("rows", {"row_code": code, "provided": 1, "source_label": "Invalid", "amount": 0})
			with self.assertRaises(frappe.ValidationError):
				doc.save()

	def test_unconfirmed_string_zero_is_not_an_approved_amount(self):
		doc = self.baseline()
		doc.append("rows", {"row_code": "FIXED_ASSETS", "provided": "0", "amount": 999})
		doc.save().submit()
		self.assertIsNone(next(r for r in self.report()["rows"] if r["row_code"] == "FIXED_ASSETS")["opening_amount"])

	def test_non_finite_source_amount_is_rejected(self):
		doc = self.baseline()
		doc.rows[0].amount = float("inf")
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_standard_submit_is_required(self):
		doc = frappe.copy_doc(self.baseline())
		doc.docstatus = 1
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_live_excel_and_pdf_keep_direct_amounts_and_blank_details(self):
		from china_finance.china_finance.report.china_financial_statements import china_financial_statements as report

		doc = self.baseline().submit()
		filters = frappe._dict(company=self.company, statement_type="Balance Sheet", fiscal_year="2026",
			from_date="2026-01-01", to_date="2026-09-30")
		with patch.object(report, "save_file", return_value=SimpleNamespace(file_url="/private/files/test.xlsx", file_name="test.xlsx")) as save:
			report.export_current_report_xlsx(filters)
			content = save.call_args.args[1]
		with zipfile.ZipFile(io.BytesIO(content)) as archive:
			ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
			strings = ["".join(node.itertext()) for node in ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))]
			table = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
			values = []
			for row in table.findall("m:sheetData/m:row", ns):
				cells = {}
				for cell in row:
					text = cell.findtext("m:v", default="", namespaces=ns)
					cells[cell.attrib["r"].rstrip("0123456789")] = strings[int(text)] if cell.attrib.get("t") == "s" else text
				values.append(cells)
			original = next(row for row in values if row.get("A") == frappe._("固定资产原价"))
			net = next(row for row in values if row.get("A") == frappe._("固定资产账面价值"))
			self.assertEqual(original.get("B", ""), "")
			self.assertEqual(float(net["B"]), 3510.91)
			self.assertEqual(float(net["C"]), 20)
			self.assertTrue(any(doc.name in value for value in strings))
		columns, rows, message, *_ = report.execute(filters)
		html = report._render_report_pdf(filters, columns, rows, message)
		self.assertIn("3,510.91", html)
		self.assertIn("<td class='num'></td>", html)
		self.assertIn(doc.name, html)
