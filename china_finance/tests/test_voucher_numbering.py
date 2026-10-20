from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from china_finance.services.closing import _get_period_closing_next_states
from china_finance.services.voucher import (
	_build_amendment_voucher_key, _next_formal_sequence, get_posting_date,
	assign_voucher_number, backfill_period_closing_voucher_numbers,
)
from china_finance.china_finance.report.china_voucher_ledger.china_voucher_ledger import execute as voucher_ledger
from china_finance.services.voucher_number_repair import reorder_monthly_voucher_numbers
from china_finance.services.voucher_number_organization import organize_monthly_voucher_numbers
from china_finance.services import voucher as voucher_service
from china_finance.services import source_voucher_edit as source_edit
from china_finance.services import voucher_number_organization as number_organization
from china_finance.services.voucher_batch import sort_voucher_names
from china_finance.overrides.source_voucher_lock import CompanyFirstSourceVoucherLock


class TestVoucherNumbering(UnitTestCase):
	def test_native_source_locks_companies_before_loading_previous_document(self):
		events = []
		previous = frappe._dict(company="Company A", custom_china_voucher_number="记2")

		class NativeSource:
			def is_new(self):
				return False

			def load_doc_before_save(self, **kwargs):
				events.append("source")
				self.previous = previous

			def get_doc_before_save(self):
				return self.previous

		class Source(CompanyFirstSourceVoucherLock, NativeSource):
			doctype, name, company = "Journal Entry", "JE-1", "Company B"
			custom_china_voucher_number = "记1"
			meta = frappe._dict(has_field=lambda field: True)

		with patch.object(frappe.db, "get_value", return_value="Company A"), \
				patch("china_finance.services.month_end.lock_company", side_effect=events.append):
			doc = Source()
			doc.load_doc_before_save(raise_exception=True)
		self.assertEqual(events, ["Company A", "Company B", "source"])
		self.assertEqual(doc.custom_china_voucher_number, "记2")

	def test_native_source_company_change_is_rejected_after_waiting(self):
		class NativeSource:
			def is_new(self):
				return False

			def load_doc_before_save(self, **kwargs):
				pass

			def get_doc_before_save(self):
				return frappe._dict(company="Company B")

		class Source(CompanyFirstSourceVoucherLock, NativeSource):
			doctype, name, company = "Journal Entry", "JE-1", "Company A"

		with patch.object(frappe.db, "get_value", return_value="Company A"), \
				patch("china_finance.services.month_end.lock_company") as lock:
			with self.assertRaises(frappe.TimestampMismatchError):
				Source().load_doc_before_save(raise_exception=True)
		lock.assert_called_once_with("Company A")

	def test_inline_deadlock_keeps_original_error_after_database_rollback(self):
		error = frappe.QueryDeadlockError("database rolled back transaction")
		with patch.object(frappe.db, "savepoint"), patch.object(frappe.db, "rollback") as rollback, \
				patch.object(source_edit, "_apply_inline_voucher_changes", side_effect=error):
			with self.assertRaises(frappe.QueryDeadlockError) as raised:
				source_edit.apply_inline_voucher_changes("Journal Entry", "JE-1", "version", [])
		self.assertIs(raised.exception, error)
		rollback.assert_not_called()

	def test_number_organization_deadlock_keeps_original_error_after_database_rollback(self):
		error = frappe.QueryDeadlockError("database rolled back transaction")
		with patch.object(frappe.db, "savepoint"), patch.object(frappe.db, "rollback") as rollback, \
				patch.object(number_organization, "lock_company", side_effect=error):
			with self.assertRaises(frappe.QueryDeadlockError) as raised:
				organize_monthly_voucher_numbers("Test Company", "2026-07", apply=1,
					expected_plan_hash="preview", reason="test")
		self.assertIs(raised.exception, error)
		rollback.assert_not_called()

	def test_amendment_inherits_current_number_instead_of_old_read_view(self):
		voucher = frappe._dict(company="Test Company", fiscal_year="2026", accounting_period="2026-07",
			voucher_word="记", source_doctype="Journal Entry", source_name="JE-Amended")
		old = frappe._dict(company=voucher.company, fiscal_year=voucher.fiscal_year,
			accounting_period=voucher.accounting_period, voucher_word="记", sequence_number=3,
			statutory_number="记3", source_key="Posting|Journal Entry|JE-Original", voucher_key="old-key")
		current = frappe._dict(old, sequence_number=1, statutory_number="记1")
		current.voucher_key = voucher_service._organized_voucher_key(current, 1)

		def read(doctype, *args, **kwargs):
			if doctype == "Journal Entry":
				return "JE-Original"
			return current if kwargs.get("for_update") else old

		with patch.object(frappe.db, "has_column", return_value=True), \
				patch.object(frappe.db, "get_value", side_effect=read) as get_value:
			inherited = voucher_service._get_inherited_formal_number(voucher, 1)
		self.assertEqual(inherited.statutory_number, "记1")
		self.assertTrue(all(call.kwargs.get("for_update") for call in get_value.call_args_list))

	def test_source_company_change_while_waiting_requires_refresh(self):
		with patch.object(source_edit, "_get_source_document", side_effect=[
			frappe._dict(company="Company A"), frappe._dict(company="Company B"),
		]) as read, patch.object(source_edit, "lock_company") as lock:
			with self.assertRaises(frappe.TimestampMismatchError):
				source_edit._get_locked_source_document("Journal Entry", "JE-1")
		lock.assert_called_once_with("Company A")
		self.assertTrue(read.call_args.kwargs["for_update"])

	def test_next_sequence_uses_the_larger_high_water_mark(self):
		self.assertEqual(_next_formal_sequence(15, 15), 16)
		self.assertEqual(_next_formal_sequence(15, 17), 18)

	def test_next_sequence_does_not_reuse_a_previous_number(self):
		self.assertEqual(_next_formal_sequence(0, 3), 4)

	def test_amendment_uses_a_distinct_snapshot_key(self):
		voucher = frappe._dict(source_key="Posting|Journal Entry|ACC-JV-2026-00382-1")
		self.assertEqual(
			_build_amendment_voucher_key(voucher),
			"amendment|Posting|Journal Entry|ACC-JV-2026-00382-1",
		)

	def test_period_closing_uses_period_end_date_for_snapshot(self):
		doc = frappe._dict(
			doctype="Period Closing Voucher",
			period_end_date="2026-05-31",
			transaction_date="2026-09-17",
		)
		self.assertEqual(str(get_posting_date(doc)), "2026-05-31")

	def test_period_closing_shortcut_only_runs_remaining_workflow_states(self):
		self.assertEqual(
			_get_period_closing_next_states("Draft"),
			("Pending Review", "Approved", "Posted"),
		)
		self.assertEqual(_get_period_closing_next_states("Pending Review"), ("Approved", "Posted"))
		self.assertEqual(_get_period_closing_next_states("Approved"), ("Posted",))
		self.assertEqual(_get_period_closing_next_states("Posted"), ())

	def test_closing_cancellation_does_not_consume_a_number(self):
		voucher = frappe._dict(
			source_doctype="Period Closing Voucher", source_name="PCV-1",
			source_event="Cancellation", source_key="Cancellation|Period Closing Voucher|PCV-1",
		)
		assign_voucher_number(voucher)
		self.assertEqual(voucher.sequence_number, 0)
		self.assertIsNone(voucher.statutory_number)
		self.assertEqual(voucher.voucher_key, "cancellation|Cancellation|Period Closing Voucher|PCV-1")


class TestClosingVoucherNumberingIntegration(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.company = f"_Test Closing Number {frappe.generate_hash(length=8)}"
		# Minimal database fixtures isolate numbering/reporting from GL creation,
		# which is independently owned by ERPNext's posting controllers.
		frappe.get_doc({
			"doctype": "Company", "name": self.company, "company_name": self.company,
			"abbr": frappe.generate_hash(length=5), "default_currency": "CNY",
		}).db_insert()
		settings = patch(
			"china_finance.services.voucher.get_company_settings",
			return_value=frappe._dict(company=self.company, enabled=1),
		)
		settings.start()
		self.addCleanup(settings.stop)

	def make_voucher(self, source_doctype, posting_date="2026-07-31", amended_from=None, legacy=False):
		source = frappe.get_doc({
			"doctype": source_doctype, "name": f"TEST-{frappe.generate_hash(length=10)}",
			"company": self.company, "docstatus": 1, "posting_date": posting_date,
			"transaction_date": "2026-09-19", "period_start_date": posting_date[:8] + "01",
			"period_end_date": posting_date, "amended_from": amended_from,
		})
		source.db_insert()
		voucher = frappe.get_doc({
			"doctype": "China Accounting Voucher", "company": self.company,
			"posting_date": posting_date, "voucher_word": "记", "currency": "CNY",
			"source_doctype": source.doctype, "source_name": source.name,
			"source_event": "Posting", "source_key": f"Posting|{source.doctype}|{source.name}",
			"entries": [
				{"account": "Test Expense", "account_currency": "CNY", "debit": 100, "credit": 0,
					"debit_in_account_currency": 100},
				{"account": "Test Profit", "account_currency": "CNY", "debit": 0, "credit": 100,
					"credit_in_account_currency": 100},
			],
		})
		if legacy:
			voucher.voucher_key = f"business|{self.company}|{source.doctype}|{source.name}|Posting"
		voucher.flags.ignore_links = True
		voucher.flags.ignore_permissions = True
		voucher.insert()
		voucher.submit()
		return source, voucher

	def report(self, **filters):
		return voucher_ledger({
			"company": self.company, "from_date": "2026-07-01", "to_date": "2026-08-31", **filters,
		})[1]

	def test_closing_is_numbered_after_monthly_sources_and_sorted_last(self):
		self.make_voucher("Journal Entry")
		self.make_voucher("Payment Entry", "2026-07-30")
		source, closing = self.make_voucher("Period Closing Voucher")
		self.assertEqual(closing.statutory_number, "记3")
		self.assertEqual(closing.accounting_period, "2026-07")
		self.assertEqual(self.report()[-2]["statutory_number"], "记3")
		self.assertEqual(self.report()[-2]["source_name"], source.name)
		self.assertEqual(len(self.report(voucher_word="记3")), 2)
		self.assertEqual(self.report(voucher_word="记3")[0]["source_name"], source.name)
		_, next_month = self.make_voucher("Journal Entry", "2026-08-01")
		self.assertEqual(next_month.statutory_number, "记1")
		self.assertEqual(self.report()[-2]["accounting_period"], "2026-08")

	def test_reclosing_takes_new_tail_number_but_journal_amendment_keeps_number(self):
		original_source, original_closing = self.make_voucher("Period Closing Voucher")
		frappe.db.set_value(original_source.doctype, original_source.name, "docstatus", 2)
		frappe.db.set_value(original_closing.doctype, original_closing.name, "status", "Reversed")
		journal, original = self.make_voucher("Journal Entry")
		_, amended = self.make_voucher("Journal Entry", amended_from=journal.name)
		self.assertEqual(amended.statutory_number, original.statutory_number)
		_, closing = self.make_voucher("Period Closing Voucher", amended_from=original_source.name)
		self.assertEqual(closing.statutory_number, "记3")
		self.assertEqual(self.report()[-2]["voucher_snapshot"], closing.name)

	def test_backfill_is_idempotent_and_only_numbers_current_posting_snapshots(self):
		_, ordinary = self.make_voucher("Journal Entry")
		source, active = self.make_voucher("Period Closing Voucher", legacy=True)
		cancelled_source, cancelled = self.make_voucher("Period Closing Voucher", legacy=True)
		frappe.db.set_value(cancelled_source.doctype, cancelled_source.name, "docstatus", 2)
		frappe.db.set_value(cancelled.doctype, cancelled.name, "status", "Reversed")
		_, superseded = self.make_voucher("Period Closing Voucher", legacy=True)
		frappe.db.set_value(superseded.doctype, superseded.name, "source_key", "legacy-reused|" + superseded.name)
		before_hash = active.source_hash
		preview = backfill_period_closing_voucher_numbers(self.company)
		self.assertEqual([row.name for row in preview], [active.name])
		active.reload()
		self.assertFalse(active.statutory_number)
		result = backfill_period_closing_voucher_numbers(self.company, apply=True)
		self.assertEqual(result[0]["statutory_number"], "记2")
		active.reload()
		self.assertEqual(active.source_hash, before_hash)
		self.assertEqual(active.total_debit, 100)
		ordinary.reload()
		self.assertEqual(ordinary.statutory_number, "记1")
		cancelled.reload()
		superseded.reload()
		self.assertFalse(cancelled.statutory_number)
		self.assertFalse(superseded.statutory_number)
		self.assertEqual(backfill_period_closing_voucher_numbers(self.company, apply=True), [])
		if frappe.db.has_column("Period Closing Voucher", "custom_china_voucher_number"):
			self.assertEqual(frappe.db.get_value(source.doctype, source.name, "custom_china_voucher_number"), "记2")

	def test_report_currency_is_explicit_on_every_entry_including_continuation_rows(self):
		self.make_voucher("Journal Entry")
		_, legacy = self.make_voucher("Payment Entry")
		frappe.db.set_value(legacy.doctype, legacy.name, "currency", None)
		columns, rows = voucher_ledger({"company": self.company,
			"from_date": "2026-07-01", "to_date": "2026-07-31"})
		self.assertEqual(len(rows), 4)
		self.assertTrue(all(row["currency"] == "CNY" for row in rows))
		for column in columns:
			if column["fieldtype"] == "Currency":
				self.assertEqual(column["options"], "currency")
		frappe.db.set_value(legacy.doctype, legacy.name, "currency", "EUR")
		self.assertEqual({row["currency"] for row in self.report()}, {"CNY", "EUR"})

	def test_repair_swaps_numbers_without_changing_entries_and_keeps_closing_last(self):
		late_source, late = self.make_voucher("Journal Entry", "2026-07-31")
		_, closing = self.make_voucher("Period Closing Voucher")
		early_source, early = self.make_voucher("Journal Entry", "2026-07-02")
		_, next_month = self.make_voucher("Journal Entry", "2026-08-01")
		self.assertEqual(sort_voucher_names("Journal Entry", [late_source.name, early_source.name]),
			[early_source.name, late_source.name])
		before = {doc.name: (doc.source_hash, doc.total_debit, doc.total_credit) for doc in (late, early, closing)}
		preview = reorder_monthly_voucher_numbers(self.company, "2026-07")
		self.assertTrue(preview["can_apply"], preview["blockers"])
		self.assertEqual(preview["changed_count"], 3)
		self.assertEqual(late.reload().statutory_number, "记1")
		result = reorder_monthly_voucher_numbers(self.company, "2026-07", apply=1,
			expected_plan_hash=preview["plan_hash"], reason="修复导入顺序")
		self.assertTrue(result["applied"])
		for doc, number in ((early, "记1"), (late, "记2"), (closing, "记3")):
			doc.reload()
			self.assertEqual(doc.statutory_number, number)
			self.assertEqual((doc.source_hash, doc.total_debit, doc.total_credit), before[doc.name])
			if frappe.db.has_column(doc.source_doctype, "custom_china_voucher_number"):
				self.assertEqual(frappe.db.get_value(doc.source_doctype, doc.source_name, "custom_china_voucher_number"), number)
		self.assertEqual(next_month.reload().statutory_number, "记1")
		self.assertEqual(reorder_monthly_voucher_numbers(self.company, "2026-07")["changed_count"], 0)
		_, after = self.make_voucher("Payment Entry")
		self.assertEqual(after.statutory_number, "记4")

	def test_repair_rejects_stale_preview(self):
		_, late = self.make_voucher("Journal Entry", "2026-07-31")
		self.make_voucher("Journal Entry", "2026-07-02")
		preview = reorder_monthly_voucher_numbers(self.company, "2026-07")
		self.make_voucher("Payment Entry", "2026-07-20")
		with self.assertRaises(frappe.ValidationError):
			reorder_monthly_voucher_numbers(self.company, "2026-07", apply=1,
				expected_plan_hash=preview["plan_hash"], reason="修复导入顺序")
		self.assertEqual(late.reload().statutory_number, "记1")

	def test_repair_rolls_back_number_swaps_if_source_update_fails(self):
		_, late = self.make_voucher("Journal Entry", "2026-07-31")
		_, early = self.make_voucher("Journal Entry", "2026-07-02")
		before_keys = [late.voucher_key, early.voucher_key]
		preview = reorder_monthly_voucher_numbers(self.company, "2026-07")
		with patch("china_finance.services.voucher._sync_source_voucher_number", side_effect=RuntimeError("test")):
			with self.assertRaises(RuntimeError):
				reorder_monthly_voucher_numbers(self.company, "2026-07", apply=1,
					expected_plan_hash=preview["plan_hash"], reason="修复导入顺序")
		self.assertEqual([late.reload().voucher_key, early.reload().voucher_key], before_keys)
		self.assertEqual([late.statutory_number, early.statutory_number], ["记1", "记2"])

	def test_repair_blocks_cancelled_or_amended_history(self):
		source, original = self.make_voucher("Journal Entry")
		frappe.db.set_value(source.doctype, source.name, "docstatus", 2)
		frappe.db.set_value(original.doctype, original.name, "status", "Reversed")
		self.make_voucher("Journal Entry", amended_from=source.name)
		preview = reorder_monthly_voucher_numbers(self.company, "2026-07")
		self.assertFalse(preview["can_apply"])
		self.assertTrue(preview["blockers"])
		with self.assertRaises(frappe.ValidationError):
			reorder_monthly_voucher_numbers(self.company, "2026-07", apply=1,
				expected_plan_hash=preview["plan_hash"], reason="修复导入顺序")

	def test_repair_blocks_frozen_and_archived_vouchers(self):
		_, voucher = self.make_voucher("Journal Entry")
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-07-31")
		self.assertFalse(reorder_monthly_voucher_numbers(self.company, "2026-07")["can_apply"])
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", None)
		frappe.get_doc({"doctype": "China Electronic Document", "name": frappe.generate_hash(),
			"company": self.company, "reference_doctype": "China Accounting Voucher",
			"reference_name": voucher.name, "status": "Archived"}).db_insert()
		self.assertFalse(reorder_monthly_voucher_numbers(self.company, "2026-07")["can_apply"])

	def test_repair_keeps_existing_gaps_and_sequence_high_water_mark(self):
		_, late = self.make_voucher("Journal Entry", "2026-07-31")
		frappe.db.set_value("China Voucher Sequence", {"company": self.company}, "current_value", 4)
		_, early = self.make_voucher("Journal Entry", "2026-07-02")
		frappe.db.set_value("China Voucher Sequence", {"company": self.company}, "current_value", 9)
		preview = reorder_monthly_voucher_numbers(self.company, "2026-07")
		reorder_monthly_voucher_numbers(self.company, "2026-07", apply=1,
			expected_plan_hash=preview["plan_hash"], reason="修复导入顺序")
		self.assertEqual([early.reload().statutory_number, late.reload().statutory_number], ["记1", "记5"])
		_, following = self.make_voucher("Journal Entry")
		self.assertEqual(following.statutory_number, "记10")

	def test_same_day_uses_source_creation_order(self):
		first_source, _ = self.make_voucher("Journal Entry", "2026-07-02")
		second_source, _ = self.make_voucher("Journal Entry", "2026-07-02")
		self.assertEqual(sort_voucher_names("Journal Entry", [second_source.name, first_source.name]),
			[first_source.name, second_source.name])

	def test_repair_requires_plan_and_reason(self):
		self.make_voucher("Journal Entry")
		with self.assertRaises(frappe.ValidationError):
			reorder_monthly_voucher_numbers(self.company, "2026-07", apply=1)

	def test_repair_blocks_archived_closing_even_after_reopening(self):
		self.make_voucher("Journal Entry")
		frappe.get_doc({"doctype": "China Closing Run", "name": frappe.generate_hash(),
			"company": self.company, "from_date": "2026-07-01", "to_date": "2026-07-31",
			"status": "Reopened", "archive_package": "/private/files/test-closing.zip"}).db_insert()
		self.assertFalse(reorder_monthly_voucher_numbers(self.company, "2026-07")["can_apply"])

	def cancel_for_numbering(self, source, snapshot):
		frappe.db.set_value(source.doctype, source.name, "docstatus", 2)
		cancellation = frappe.get_doc({
			"doctype": "China Accounting Voucher", "company": self.company, "currency": "CNY",
			"posting_date": snapshot.posting_date, "voucher_word": snapshot.voucher_word,
			"source_doctype": source.doctype, "source_name": source.name,
			"source_event": "Cancellation", "source_key": f"Cancellation|{source.doctype}|{source.name}",
			"reversal_of": snapshot.name, "entries": voucher_service.reverse_voucher_entries(snapshot.name),
		})
		cancellation.flags.ignore_links = True
		cancellation.flags.ignore_permissions = True
		cancellation.insert()
		cancellation.submit()
		frappe.db.set_value(snapshot.doctype, snapshot.name, {"status": "Reversed", "reversed_by": cancellation.name})
		return cancellation

	def organize(self, apply=False):
		preview = organize_monthly_voucher_numbers(self.company, "2026-07")
		if not apply:
			return preview
		return organize_monthly_voucher_numbers(self.company, "2026-07", apply=1,
			expected_plan_hash=preview["plan_hash"], reason="取消重复凭证后整理编号")

	def test_organization_fills_gaps_preserves_cancelled_history_and_next_number(self):
		source, cancelled = self.make_voucher("Journal Entry", "2026-07-01")
		_, late = self.make_voucher("Journal Entry", "2026-07-30")
		_, early = self.make_voucher("Payment Entry", "2026-07-03")
		_, august = self.make_voucher("Journal Entry", "2026-08-01")
		audit = self.cancel_for_numbering(source, cancelled)
		before = [(row.source_hash, row.total_debit, row.total_credit) for row in (late, early)]
		preview = self.organize()
		self.assertTrue(preview["can_apply"], preview["blockers"])
		self.assertEqual(preview["history_count"], 1)
		self.assertEqual(cancelled.reload().statutory_number, "记1")
		self.assertEqual(early.reload().statutory_number, "记3")
		self.assertTrue(self.organize(apply=True)["applied"])
		self.assertEqual([early.reload().statutory_number, late.reload().statutory_number], ["记1", "记2"])
		self.assertEqual(cancelled.reload().statutory_number, "记1")
		self.assertEqual(cancelled.reversed_by, audit.name)
		self.assertEqual(august.reload().statutory_number, "记1")
		self.assertEqual([(row.source_hash, row.total_debit, row.total_credit) for row in (late, early)], before)
		self.assertFalse(self.organize()["needs_update"])
		_, following = self.make_voucher("Journal Entry")
		self.assertEqual(following.statutory_number, "记3")

	def test_organization_resets_empty_month_and_old_cancelled_tail(self):
		first_source, first = self.make_voucher("Journal Entry")
		second_source, second = self.make_voucher("Journal Entry")
		self.cancel_for_numbering(first_source, first)
		self.cancel_for_numbering(second_source, second)
		result = self.organize(apply=True)
		self.assertEqual(result["next_number"], "记1")
		_, following = self.make_voucher("Journal Entry")
		self.assertEqual(following.statutory_number, "记1")
		self.assertTrue(following.voucher_key.startswith("organized|"))

	def test_organization_handles_amendments_and_does_not_reuse_old_cancelled_number(self):
		old_source, old = self.make_voucher("Journal Entry")
		self.cancel_for_numbering(old_source, old)
		live_source, live = self.make_voucher("Journal Entry")
		self.cancel_for_numbering(live_source, live)
		amend_source, amended = self.make_voucher("Journal Entry", amended_from=live_source.name)
		self.assertEqual(amended.statutory_number, "记2")
		self.assertTrue(self.organize(apply=True)["applied"])
		self.assertEqual(amended.reload().statutory_number, "记1")
		self.cancel_for_numbering(amend_source, amended)
		_, newest = self.make_voucher("Journal Entry", amended_from=amend_source.name)
		self.assertEqual(newest.statutory_number, "记1")
		_, delayed_amendment = self.make_voucher("Journal Entry", amended_from=old_source.name)
		self.assertEqual(delayed_amendment.statutory_number, "记2")
		self.assertEqual(old.reload().statutory_number, "记1")

	def test_organization_repeats_in_new_generation_and_rejects_stale_plan(self):
		_, later = self.make_voucher("Journal Entry", "2026-07-20")
		self.make_voucher("Payment Entry", "2026-07-10")
		preview = self.organize()
		self.make_voucher("Journal Entry", "2026-07-05")
		with self.assertRaises(frappe.ValidationError):
			organize_monthly_voucher_numbers(self.company, "2026-07", apply=1,
				expected_plan_hash=preview["plan_hash"], reason="整理编号")
		self.organize(apply=True)
		self.make_voucher("Journal Entry", "2026-07-01")
		self.organize(apply=True)
		self.assertEqual(later.reload().statutory_number, "记4")
		self.assertFalse(self.organize()["needs_update"])

	def test_organization_failure_rolls_back_numbers_counter_and_audit(self):
		_, late = self.make_voucher("Journal Entry", "2026-07-20")
		_, early = self.make_voucher("Journal Entry", "2026-07-02")
		keys = [late.voucher_key, early.voucher_key]
		with patch.object(voucher_service, "_sync_source_voucher_number", side_effect=RuntimeError("test")):
			with self.assertRaises(RuntimeError):
				self.organize(apply=True)
		self.assertEqual([late.reload().voucher_key, early.reload().voucher_key], keys)
		sequence = frappe.db.get_value("China Voucher Sequence", {"company": self.company},
			["current_value", "numbering_revision"], as_dict=True)
		self.assertEqual(sequence.current_value, 2)
		self.assertEqual(sequence.numbering_revision, 0)

	def test_organization_blocks_incomplete_cancellation_frozen_and_archived_months(self):
		source, snapshot = self.make_voucher("Journal Entry")
		frappe.db.set_value(source.doctype, source.name, "docstatus", 2)
		frappe.db.set_value(snapshot.doctype, snapshot.name, "status", "Reversed")
		self.assertTrue(self.organize()["blockers"])
		audit = self.cancel_for_numbering(source, snapshot)
		frappe.db.set_value(audit.doctype, audit.name, "reversal_of", None)
		self.assertTrue(self.organize()["blockers"])
		frappe.db.set_value(audit.doctype, audit.name, "reversal_of", snapshot.name)
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", "2026-07-31")
		self.assertTrue(self.organize()["blockers"])
		frappe.db.set_value("Company", self.company, "accounts_frozen_till_date", None)
		frappe.get_doc({"doctype": "China Electronic Document", "name": frappe.generate_hash(),
			"company": self.company, "reference_doctype": "China Accounting Voucher",
			"reference_name": snapshot.name, "status": "Archived"}).db_insert()
		self.assertTrue(self.organize()["blockers"])

	def test_organization_blocks_active_closing_and_requires_reason_and_permissions(self):
		self.make_voucher("Journal Entry")
		frappe.get_doc({"doctype": "China Closing Run", "name": frappe.generate_hash(),
			"company": self.company, "from_date": "2026-07-01", "to_date": "2026-07-31",
			"preparation_state": "Posting", "docstatus": 0}).db_insert()
		self.assertTrue(self.organize()["blockers"])
		with self.assertRaises(frappe.ValidationError):
			organize_monthly_voucher_numbers(self.company, "2026-07", apply=1)
		with patch("frappe.only_for", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				self.organize()
