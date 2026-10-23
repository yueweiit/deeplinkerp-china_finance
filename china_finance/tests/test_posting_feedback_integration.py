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


def run_local_lifecycle_tests(query_only=False):
	if frappe.local.site not in ("dev.localhost", "aaa"):
		raise RuntimeError("restricted to local test sites")
	suite = unittest.TestSuite([PostingFeedbackLifecycleTests("test_pending_report_with_real_permission_queries")]) if query_only else unittest.defaultTestLoader.loadTestsFromTestCase(PostingFeedbackLifecycleTests)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	if not result.wasSuccessful():
		raise AssertionError("posting feedback lifecycle failed")
	return {"run": result.testsRun, "skipped": len(result.skipped), "failed": len(result.failures) + len(result.errors)}
