"""Native Journal Entry lifecycle on whitelisted local sites; business writes roll back."""
import unittest
from unittest.mock import patch

import frappe

from china_finance.services import voucher, voucher_posting_status


class PostingFeedbackLifecycleTests(unittest.TestCase):
	def setUp(self):
		if frappe.local.site not in ("dev.localhost", "aaa"):
			self.skipTest("restricted to local test sites")
		user = frappe.session.user
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, user)
		point = "posting_feedback_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(point)
		self.addCleanup(frappe.db.rollback, save_point=point)
		prefix = "_POST_FEEDBACK_" + frappe.generate_hash(length=10)
		self.company = prefix
		self.addCleanup(frappe.clear_document_cache, "China Finance Settings", self.company)
		self.addCleanup(frappe.clear_document_cache, "Company", self.company)
		frappe.get_doc({"doctype": "Company", "name": prefix, "company_name": prefix,
			"abbr": prefix[-5:], "default_currency": "CNY", "reporting_currency": "CNY", "country": "China"}).db_insert()
		self.accounts = []
		for suffix, root in (("Asset", "Asset"), ("Capital", "Equity")):
			account = prefix + " " + suffix
			frappe.get_doc({"doctype": "Account", "name": account, "company": prefix,
				"account_name": suffix, "root_type": root, "report_type": "Balance Sheet",
				"account_currency": "CNY", "is_group": 0, "disabled": 0}).db_insert()
			self.accounts.append(account)
		frappe.get_doc({"doctype": "China Finance Settings", "name": prefix, "company": prefix,
			"enabled": 1, "activation_date": "2026-01-01", "enable_voucher_preparation": 0,
			"enforce_role_separation": 0}).db_insert()

	def manual_voucher(self):
		return frappe.get_doc({"doctype": "Journal Entry", "company": self.company,
			"posting_date": "2026-10-22", "voucher_type": "Journal Entry",
			"accounts": [{"account": self.accounts[0], "debit_in_account_currency": 50},
				{"account": self.accounts[1], "credit_in_account_currency": 50}]}).insert()

	def test_posted_missing_snapshot_remains_visible_and_repair_never_duplicates_gl(self):
		doc = self.manual_voucher()
		with patch.object(voucher, "create_voucher_from_source", return_value=None):
			doc.submit()
		before = frappe.db.count("GL Entry", {"voucher_type": "Journal Entry", "voucher_no": doc.name})
		self.assertGreater(before, 0)
		state = voucher_posting_status.get_posting_state(doc.doctype, doc.name)
		self.assertTrue(state["posted"])
		self.assertTrue(state["can_repair"])
		filters = frappe._dict(company=self.company, from_date=doc.posting_date, to_date=doc.posting_date,
			source_doctype=doc.doctype, source_name=doc.name, voucher_status="全部有效凭证")
		pending = voucher_posting_status.get_pending_ledger_rows(filters)
		self.assertEqual(len(pending), before)
		self.assertTrue(all(row.snapshot_pending and not row.editable_account for row in pending))
		from china_finance.china_finance.report.china_voucher_ledger.china_voucher_ledger import execute

		_, report_rows = execute(filters)
		self.assertEqual(len(report_rows), before)
		self.assertTrue(report_rows[0]["snapshot_pending"])
		state = voucher_posting_status.repair_posting_snapshot(doc.doctype, doc.name)
		self.assertTrue(state["snapshot_ready"])
		self.assertTrue(state["statutory_number"])
		voucher_posting_status.repair_posting_snapshot(doc.doctype, doc.name)
		self.assertEqual(frappe.db.count("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name}), before)
		self.assertEqual(frappe.db.count("China Accounting Voucher", {"source_key": f"Posting|{doc.doctype}|{doc.name}"}), 1)
		self.assertEqual(voucher_posting_status.get_pending_ledger_rows(filters), [])
		_, report_rows = execute(filters)
		self.assertEqual(len(report_rows), before)
		self.assertFalse(any(row.get("snapshot_pending") for row in report_rows))

	def test_pending_report_with_real_permission_queries(self):
		name = self.company + " JE"
		frappe.get_doc({"doctype": "Journal Entry", "name": name, "company": self.company,
			"posting_date": "2026-10-22", "voucher_type": "Journal Entry", "docstatus": 1}).db_insert()
		for index, account in enumerate(self.accounts):
			frappe.get_doc({"doctype": "GL Entry", "name": self.company + str(index), "company": self.company,
				"posting_date": "2026-10-22", "voucher_type": "Journal Entry", "voucher_no": name,
				"account": account, "account_currency": "CNY", "is_cancelled": 0,
				"debit": 50 if index == 0 else 0, "credit": 50 if index == 1 else 0}).db_insert()
		filters = frappe._dict(company=self.company, from_date="2026-10-22", to_date="2026-10-22",
			source_doctype="Journal Entry", source_name=name, voucher_status="全部有效凭证")
		pending = voucher_posting_status.get_pending_ledger_rows(filters)
		self.assertEqual(len(pending), 2)
		from china_finance.china_finance.report.china_voucher_ledger.china_voucher_ledger import execute

		_, rows = execute(filters)
		self.assertEqual(len(rows), 2)
		self.assertTrue(rows[0]["snapshot_pending"])
		self.assertFalse(rows[0]["editable_amount"])
		self.assertEqual(sum(row["debit"] for row in rows), 50)
		filters.voucher_status = "未记账"
		self.assertEqual(execute(filters)[1], [])
		state = voucher_posting_status.repair_posting_snapshot("Journal Entry", name)
		self.assertTrue(state["snapshot_ready"])
		voucher_posting_status.repair_posting_snapshot("Journal Entry", name)
		self.assertEqual(frappe.db.count("GL Entry", {"voucher_type": "Journal Entry", "voucher_no": name}), 2)
		self.assertEqual(frappe.db.count("China Accounting Voucher", {"source_key": f"Posting|Journal Entry|{name}"}), 1)
		filters.voucher_status = "全部有效凭证"
		_, rows = execute(filters)
		self.assertEqual(len(rows), 2)
		self.assertFalse(any(row.get("snapshot_pending") for row in rows))

	def report_source(self, doc):
		from china_finance.china_finance.report.china_voucher_ledger.china_voucher_ledger import execute

		return execute(dict(company=self.company, from_date=doc.posting_date, to_date=doc.posting_date,
			source_doctype=doc.doctype, source_name=doc.name, voucher_status="全部有效凭证"))[1]

	def test_merged_gl_keeps_bank_summary_bound_to_bank_entry(self):
		doc = self.manual_voucher()
		doc.set("accounts", [
			{"account": self.accounts[0], "debit_in_account_currency": 10, "user_remark": "First expense"},
			{"account": self.accounts[0], "debit_in_account_currency": 20, "user_remark": "Second expense"},
			{"account": self.accounts[1], "credit_in_account_currency": 30, "user_remark": "Bank payment"},
		])
		doc.save()
		original = frappe.get_single_value
		def setting(doctype, field, *args, **kwargs):
			return 1 if (doctype, field) == ("Accounts Settings", "merge_similar_account_heads") else original(doctype, field, *args, **kwargs)
		with patch.object(frappe, "get_single_value", side_effect=setting):
			doc.submit()
		rows = self.report_source(doc)
		self.assertEqual(len(rows), 2)
		bank = next(row for row in rows if row["account"] == self.accounts[1])
		self.assertIn("Bank payment", bank["remarks"])
		self.assertNotIn("Second expense", bank["remarks"])
		self.assertTrue(bank["editable_summary"])
		self.assertEqual(bank["summary_edit_key"], f"je-summary:{doc.accounts[2].name}")
		self.assertEqual(bank["summary_edit_value"], "Bank payment")
		expense = next(row for row in rows if row["account"] == self.accounts[0])
		self.assertFalse(expense["editable_summary"])
		from china_finance.services import source_voucher_edit as edits

		result = edits.apply_inline_voucher_changes(doc.doctype, doc.name, str(doc.modified), [
			{"field": "summary", "edit_key": bank["summary_edit_key"], "value": "Corrected bank payment"},
		])
		amended = frappe.get_doc(doc.doctype, result["source_name"])
		self.assertEqual([r.user_remark for r in amended.accounts],
			["First expense", "Second expense", "Corrected bank payment"])


	def test_negative_debit_is_identical_before_and_after_repair(self):
		doc = self.manual_voucher()
		doc.set("accounts", [
			{"account": self.accounts[0], "debit_in_account_currency": 0.31, "user_remark": "Interest"},
			{"account": self.accounts[1], "debit_in_account_currency": -0.31, "user_remark": "Interest offset"},
		])
		doc.save()
		with patch.object(voucher, "create_voucher_from_source", return_value=None):
			doc.submit()
		before = self.report_source(doc)
		voucher_posting_status.repair_posting_snapshot(doc.doctype, doc.name)
		after = self.report_source(doc)
		values = lambda rows: sorted((r["account"], r["debit"], r["credit"], r["remarks"]) for r in rows)
		self.assertEqual(values(before), values(after))
		self.assertEqual(next(r for r in before if r["account"] == self.accounts[1])["debit"], -0.31)

	def test_negative_debit_restore_respects_party_and_cost_center_filters(self):
		negative = frappe._dict(account="Expense", account_currency="CNY", debit=-1, credit=0,
			debit_in_account_currency=-1, party_type="Supplier", party="A", cost_center="CC-A", project=None,
			user_remark="Offset A")
		other = frappe._dict(account="Expense", account_currency="CNY", debit=0, credit=1,
			debit_in_account_currency=0, credit_in_account_currency=1, party_type="Supplier", party="B",
			cost_center="CC-B", project=None, remarks="Payment B")
		matching = frappe._dict(other, party="A", cost_center="CC-A")
		doc = frappe._dict(doctype="Journal Entry", accounts=[negative])
		voucher.restore_negative_journal_entry_debits(doc, [other])
		self.assertEqual((other.debit, other.credit, other.remarks), (0, 1, "Payment B"))
		voucher.restore_negative_journal_entry_debits(doc, [other, matching])
		self.assertEqual((matching.debit, matching.credit, matching.remarks), (-1, 0, "Offset A"))


	def test_cash_flow_entry_cannot_bypass_activation_or_frozen_period(self):
		doc = self.manual_voucher()
		with patch.object(voucher, "create_voucher_from_source", return_value=None):
			doc.submit()
		for activation, frozen in (("2026-11-01", None), ("2026-01-01", "2026-10-31")):
			frappe.db.set_value("China Finance Settings", self.company, "activation_date", activation)
			frappe.clear_document_cache("China Finance Settings", self.company)
			frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", frozen)
			with self.assertRaises(frappe.ValidationError):
				voucher.create_cash_flow_assignment_from_source(doc.doctype, doc.name)
			self.assertFalse(frappe.db.exists("China Accounting Voucher", {"source_key": f"Posting|{doc.doctype}|{doc.name}"}))

	def test_cash_flow_missing_snapshot_requires_manager_and_reuses_existing_snapshot(self):
		doc = self.manual_voucher()
		with patch.object(voucher, "create_voucher_from_source", return_value=None):
			doc.submit()
		from china_finance.services import cash_flow_assignment

		def allowed(roles, *args, **kwargs):
			if "Accounts User" not in roles:
				raise frappe.PermissionError("manager required")

		with patch.object(frappe, "only_for", side_effect=allowed):
			with self.assertRaises(frappe.PermissionError):
				voucher.create_cash_flow_assignment_from_source(doc.doctype, doc.name)
		self.assertFalse(frappe.db.exists("China Accounting Voucher", {"source_key": f"Posting|{doc.doctype}|{doc.name}"}))
		with patch.object(cash_flow_assignment, "create_cash_flow_assignment", return_value="TEST-ASSIGNMENT"):
			self.assertEqual(voucher.create_cash_flow_assignment_from_source(doc.doctype, doc.name)["name"], "TEST-ASSIGNMENT")
			# Once a snapshot exists, ordinary cash-flow users keep their existing entry.
			with patch.object(frappe, "only_for", side_effect=allowed):
				voucher.create_cash_flow_assignment_from_source(doc.doctype, doc.name)
		self.assertEqual(frappe.db.count("China Accounting Voucher", {"source_key": f"Posting|{doc.doctype}|{doc.name}"}), 1)
		self.assertEqual(frappe.db.count("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name}), 2)


	def test_ledger_numeric_order_preserves_whole_voucher_groups(self):
		from china_finance.china_finance.report.china_voucher_ledger.china_voucher_ledger import voucher_ledger_sort_key, build_tree_data

		rows = []
		for name, number in (("JE-A", 10), ("JE-B-amended", 2), ("JE-Z", 1)):
			for index in (2, 1):
				rows.append(frappe._dict(posting_date="2026-10-22", source_doctype="Journal Entry", source_name=name,
					voucher_snapshot=name, voucher_word="记", sequence_number=number, entry_idx=index,
					statutory_number=f"记{number}", remarks="", accounting_period="2026-10", voucher_status=1))
		result = build_tree_data(sorted(rows, key=voucher_ledger_sort_key))
		self.assertEqual([r["statutory_number"] for r in result if r["is_voucher_first_row"]], ["记1", "记2", "记10"])
		self.assertEqual([r["entry_idx"] for r in result], [1, 2, 1, 2, 1, 2])
		mixed = [frappe._dict(rows[0], sequence_number=0, voucher_snapshot="draft", source_name="AAA"),
			frappe._dict(rows[0], sequence_number=0, voucher_snapshot="pending", snapshot_pending=True),
			frappe._dict(rows[0], posting_date="2026-10-01", source_doctype="Period Closing Voucher", voucher_snapshot="closing"),
			frappe._dict(rows[0], posting_date="2026-11-01", voucher_snapshot="next-month"),
			frappe._dict(rows[0], sequence_number=1, voucher_snapshot="formal")]
		self.assertEqual([r.voucher_snapshot for r in sorted(mixed, key=voucher_ledger_sort_key)],
			["formal", "pending", "draft", "closing", "next-month"])


	def test_summary_mapping_uses_dimensions_and_ignores_reference_detail_idx(self):
		from china_finance.services.source_voucher_edit import get_inline_summary_edit_metadata

		rows = [frappe._dict(name="A", account="Expense", idx=1, party="A", user_remark="A remark"),
			frappe._dict(name="B", account="Expense", idx=2, party="B", user_remark="B remark")]
		doc = frappe._dict(doctype="Journal Entry", accounts=rows)
		line = frappe._dict(account="Expense", party="B", entry_idx=1, voucher_detail_no="A")
		self.assertEqual(get_inline_summary_edit_metadata(doc, line)["edit_key"], "je-summary:B")
		self.assertFalse(get_inline_summary_edit_metadata(doc, {"account": "Expense", "entry_idx": 1})["editable"])
		self.assertEqual(get_inline_summary_edit_metadata(doc,
			{"account": "Expense", "source_row_name": "A"})["value"], "A remark")
		self.assertFalse(get_inline_summary_edit_metadata(doc,
			{"account": "Other", "source_row_name": "A"})["editable"])





def run_local_lifecycle_tests(query_only=False):
	if frappe.local.site not in ("dev.localhost", "aaa"):
		raise RuntimeError("restricted to local test sites")
	suite = unittest.TestSuite([PostingFeedbackLifecycleTests("test_pending_report_with_real_permission_queries")]) if query_only else unittest.defaultTestLoader.loadTestsFromTestCase(PostingFeedbackLifecycleTests)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	if not result.wasSuccessful():
		raise AssertionError("posting feedback lifecycle failed")
	return {"run": result.testsRun, "skipped": len(result.skipped), "failed": len(result.failures) + len(result.errors)}
