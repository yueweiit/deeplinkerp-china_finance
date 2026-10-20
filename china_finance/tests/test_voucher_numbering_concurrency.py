"""Real two-connection numbering regressions with isolated, cleaned-up fixtures."""

import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import frappe

from china_finance.services import source_voucher_edit as edits
from china_finance.services import voucher as vouchers
from china_finance.services.month_end import lock_company
from china_finance.services.voucher_number_organization import organize_monthly_voucher_numbers
from china_finance.tests import test_voucher_numbering as numbering_fixtures

TEST_SITES = ("dev.localhost", "po-grid-qa.localhost")


class TestVoucherNumberingConcurrency(unittest.TestCase):
	def setUp(self):
		if frappe.local.site not in TEST_SITES or frappe.db.db_type != "mariadb":
			self.skipTest("Committed concurrent fixtures are restricted to local MariaDB test sites")

	def run_scenario(self, scenario):
		# Fixture commits belong to this child process, never to the runner's
		# transaction. The child removes only its randomly named fixture chain.
		code = """
import sys
import frappe
frappe.init(site=sys.argv[1], sites_path=sys.argv[2])
frappe.connect()
frappe.flags.in_test = True
try:
    from china_finance.tests.test_voucher_numbering_concurrency import run_concurrency_scenario
    run_concurrency_scenario(sys.argv[3])
finally:
    frappe.db.rollback()
    frappe.destroy()
"""
		result = subprocess.run(
			[sys.executable, "-c", code, frappe.local.site, str(frappe.local.sites_path), scenario],
			capture_output=True, text=True, timeout=45,
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

	def test_prepare_waits_for_company_before_locking_source(self):
		self.run_scenario("prepare")

	def test_inline_save_waits_for_company_before_locking_source(self):
		self.run_scenario("inline")

	def test_complete_waits_for_company_before_locking_source(self):
		self.run_scenario("complete")

	def test_native_save_waits_for_company_before_locking_source(self):
		self.run_scenario("native-save")

	def test_native_cancel_waits_for_company_before_locking_source(self):
		self.run_scenario("native-cancel")

	def test_amendment_reads_organized_number_after_old_consistent_read(self):
		self.run_scenario("inherit")

	def test_organization_does_not_lock_another_companys_snapshots(self):
		self.run_scenario("unrelated")


def _wait(event):
	if not event.wait(10):
		raise AssertionError("Timed out waiting for the other database connection")


def _connection_action(site, sites_path, action):
	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	frappe.flags.in_test = True
	frappe.set_user("Administrator")
	try:
		frappe.db.sql("SET SESSION innodb_lock_wait_timeout=5")
		try:
			return action()
		except frappe.QueryDeadlockError as exc:
			# MariaDB's optional snapshot-isolation check rejects a current
			# read whose old read view predates a committed change (1020).
			# A real lock-order deadlock (1213) must still fail this test.
			if not exc.__cause__ or exc.__cause__.args[0] != 1020:
				raise
			frappe.db.rollback()
			return action(retrying=True)
	finally:
		frappe.db.rollback()
		frappe.destroy()


def _cleanup(company):
	frappe.db.rollback()
	snapshots = frappe.get_all("China Accounting Voucher", filters={"company": company}, pluck="name")
	sequences = frappe.get_all("China Voucher Sequence", filters={"company": company}, pluck="name")
	for doctype in ("Journal Entry", "Payment Entry"):
		names = frappe.get_all(doctype, filters={"company": company}, pluck="name")
		if names:
			frappe.db.delete("Comment", {"reference_doctype": doctype, "reference_name": ["in", names]})
		frappe.db.delete(doctype, {"company": company})
	for doctype, names in (("China Accounting Voucher", snapshots), ("China Voucher Sequence", sequences)):
		if names:
			frappe.db.delete("Comment", {"reference_doctype": doctype, "reference_name": ["in", names]})
			frappe.db.delete("Version", {"ref_doctype": doctype, "docname": ["in", names]})
	if snapshots:
		frappe.db.delete("China Accounting Voucher Entry", {"parent": ["in", snapshots]})
	frappe.db.delete("China Accounting Voucher", {"company": company})
	frappe.db.delete("China Voucher Sequence", {"company": company})
	frappe.db.delete("Company", {"name": company})
	frappe.db.commit()


def run_concurrency_scenario(scenario):
	if frappe.local.site not in TEST_SITES:
		raise AssertionError("Concurrent fixtures are not permitted on this site")
	frappe.set_user("Administrator")
	frappe.db.sql("SET SESSION innodb_lock_wait_timeout=5")
	assert frappe.db.sql("SELECT @@tx_isolation")[0][0] == "REPEATABLE-READ"
	prefix = "_VNO_CONC_" + frappe.generate_hash(length=12)
	company = prefix + " Company"
	other_company = None
	site, sites_path = frappe.local.site, str(frappe.local.sites_path)
	try:
		with patch("china_finance.services.voucher.get_company_settings",
				return_value=frappe._dict(company=company, enabled=1)), patch(
			"china_finance.china_finance.doctype.china_accounting_voucher.china_accounting_voucher.ChinaAccountingVoucher.autoname",
			lambda doc: setattr(doc, "name", prefix + frappe.generate_hash(length=8)), create=True,
		):
			frappe.get_doc({"doctype": "Company", "name": company, "company_name": company,
				"abbr": frappe.generate_hash(length=5), "default_currency": "CNY"}).db_insert()
			fixtures = numbering_fixtures.TestClosingVoucherNumberingIntegration()
			fixtures.company = company
			source, snapshot = fixtures.make_voucher("Journal Entry", "2026-07-20")
			vouchers._sync_source_voucher_number(source.doctype, source.name, snapshot.statutory_number)
			fixtures.make_voucher("Payment Entry", "2026-07-01")
			amendment = frappe.get_doc({"doctype": "Journal Entry", "name": prefix + " Amendment",
				"company": company, "docstatus": 0, "posting_date": "2026-07-20", "amended_from": source.name})
			amendment.db_insert()
			if scenario == "unrelated":
				other_company = prefix + " Other Company"
				frappe.get_doc({"doctype": "Company", "name": other_company, "company_name": other_company,
					"abbr": frappe.generate_hash(length=5), "default_currency": "CNY"}).db_insert()
				fixtures.company = other_company
				_, other_snapshot = fixtures.make_voucher("Journal Entry", "2026-07-20")
			frappe.db.commit()
			preview = organize_monthly_voucher_numbers(company, "2026-07")
			assert preview["can_apply"], preview
			started, organized = threading.Event(), threading.Event()

			def reader(retrying=False):
				if scenario == "unrelated":
					frappe.get_doc("China Accounting Voucher", other_snapshot.name, for_update=True)
					started.set()
					_wait(organized)
					return
				if scenario == "inherit":
					if not retrying:
						assert frappe.db.get_value("China Accounting Voucher", snapshot.name, "statutory_number") == "记1"
						started.set()
						_wait(organized)
					scope = frappe._dict(company=company, fiscal_year="2026", accounting_period="2026-07",
						voucher_word="记", source_doctype="Journal Entry", source_name=amendment.name)
					sequence = vouchers._get_or_create_formal_sequence(scope, vouchers._formal_sequence_key(scope))
					assert sequence.numbering_revision == 1
					inherited = vouchers._get_inherited_formal_number(scope, sequence.numbering_revision)
					assert inherited and inherited.statutory_number == "记2", inherited
					return

				def wait_for_company(value):
					started.set()
					lock_company(value)

				class LockedSourceObserved(Exception):
					pass

				def inspect_source(doc, *args):
					# Even though this transaction read the old source before
					# waiting, the locked document must contain its new number.
					assert doc.custom_china_voucher_number == "记2", doc.custom_china_voucher_number
					raise LockedSourceObserved

				if scenario in ("native-save", "native-cancel"):
					doc = frappe.get_doc("Journal Entry", source.name)
					assert doc.custom_china_voucher_number == ("记2" if retrying else "记1")
					# A retried browser save can still carry its pre-organization number.
					doc.custom_china_voucher_number = "记1"
					with patch("china_finance.services.month_end.lock_company", side_effect=wait_for_company), \
							patch.object(doc, "_validate_links"), \
							patch.object(doc, "run_before_save_methods", side_effect=lambda: inspect_source(doc)):
						try:
							if scenario == "native-cancel":
								doc.cancel()
							else:
								doc.save()
						except LockedSourceObserved:
							return
						raise AssertionError("Native save did not reach validation with the organized number")

				original_locked = edits._get_locked_source_document

				def inspect_locked(*args):
					inspect_source(original_locked(*args))

				with patch.object(edits, "lock_company", side_effect=wait_for_company), \
						patch.object(edits, "_get_locked_source_document", side_effect=inspect_locked):
					try:
						if scenario == "prepare":
							edits.prepare_source_voucher_edit("Journal Entry", source.name)
						elif scenario == "complete":
							edits.complete_source_voucher_edit("Journal Entry", source.name)
						else:
							edits.apply_inline_voucher_changes("Journal Entry", source.name, str(source.modified), [])
					except LockedSourceObserved:
						return
					raise AssertionError("The edit did not reach locked source validation")

			if scenario != "inherit":
				lock_company(company)
			with ThreadPoolExecutor(max_workers=1) as executor:
				future = executor.submit(_connection_action, site, sites_path, reader)
				try:
					if not started.wait(10):
						if future.done():
							future.result()
						raise AssertionError("Reader did not reach the company lock")
					result = organize_monthly_voucher_numbers(company, "2026-07", apply=1,
						expected_plan_hash=preview["plan_hash"], reason="并发测试整理编号")
					assert result["applied"], result
					frappe.db.commit()
					organized.set()
					future.result(timeout=15)
				finally:
					frappe.db.rollback()
					organized.set()
	finally:
		if other_company:
			_cleanup(other_company)
		_cleanup(company)
