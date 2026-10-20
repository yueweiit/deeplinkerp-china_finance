"""Opening classification and integration rules; no database writes."""

import importlib
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestOpeningIdentity(unittest.TestCase):
	def identity(self):
		self.assertIsNotNone(
			importlib.util.find_spec("china_finance.services.opening_identity"),
			"Opening classification must have one shared implementation",
		)
		return importlib.import_module("china_finance.services.opening_identity")

	def test_declared_opening_requires_journal_and_exact_yes(self):
		identity = self.identity()
		self.assertTrue(identity.is_opening_draft(frappe._dict(doctype="Journal Entry", is_opening="Yes")))
		for doctype, flag in (("Journal Entry", "No"), ("Journal Entry", None), ("Payment Entry", "Yes")):
			self.assertFalse(identity.is_opening_draft(frappe._dict(doctype=doctype, is_opening=flag)))

	def test_posted_opening_uses_native_evidence_not_remarks(self):
		identity = self.identity()
		with patch.object(identity, "get_opening_sources", return_value={"OPEN-1"}) as query:
			self.assertTrue(identity.is_opening_source("Journal Entry", "OPEN-1", "Company A"))
			self.assertFalse(identity.is_opening_source("Journal Entry", "ordinary", "Company A"))
			self.assertFalse(identity.is_opening_source("Payment Entry", "OPEN-1", "Company A"))
		self.assertEqual(query.call_count, 2)

	def test_no_sources_does_not_construct_an_empty_sql_in_list(self):
		identity = self.identity()
		with patch.object(identity.frappe, "db", new=MagicMock()) as database:
			self.assertEqual(identity.get_opening_sources("Company A", []), set())
			database.sql.assert_not_called()

	def test_cash_plan_never_requires_classification_for_opening_cash(self):
		self.identity()
		from china_finance.services import voucher_preparation as preparation
		with patch("china_finance.services.cash_flow_assignment.is_direct_assignment_required") as required:
			result = preparation.cash_plan(frappe._dict(doctype="Journal Entry", is_opening="Yes"))
		self.assertEqual(result, {"rows": [], "options": [], "error": ""})
		required.assert_not_called()

	def test_new_opening_snapshot_does_not_consume_normal_number(self):
		self.identity()
		from china_finance.services import voucher
		doc = frappe._dict(
			company="Company A", source_doctype="Journal Entry", source_name="OPEN-1",
			source_event="Posting", source_key="Posting|Journal Entry|OPEN-1", voucher_key=None,
		)
		with patch("china_finance.services.opening_identity.is_opening_source", return_value=True), \
				patch.object(voucher, "_get_or_create_formal_sequence") as sequence:
			voucher.assign_voucher_number(doc)
		self.assertEqual(doc.sequence_number, 0)
		self.assertIsNone(doc.statutory_number)
		self.assertEqual(doc.voucher_key, "opening|Posting|Journal Entry|OPEN-1")
		sequence.assert_not_called()

	def test_existing_opening_number_is_preserved(self):
		self.identity()
		from china_finance.services.voucher import assign_voucher_number
		doc = frappe._dict(voucher_key="old|00000001", statutory_number="记1", sequence_number=1)
		assign_voucher_number(doc)
		self.assertEqual((doc.statutory_number, doc.sequence_number), ("记1", 1))

	def test_opening_uses_batch_adjustment_instead_of_regular_inline_edit(self):
		from china_finance.services.source_voucher_edit import _permission_blockers
		doc = frappe._dict(doctype="Journal Entry", is_opening="Yes", docstatus=1,
			has_permission=lambda permission: True)
		with patch.object(frappe, "has_permission", return_value=True):
			blockers = _permission_blockers(doc)
		self.assertTrue(any("期初" in item for item in blockers))

	def test_opening_guard_preserves_existing_cancellation_audit_and_bank_hooks(self):
		from china_finance import hooks

		callbacks = hooks.doc_events["Journal Entry"]["before_cancel"]
		for callback in (
			"china_finance.services.opening_balance.guard_linked_opening_source",
			"china_finance.services.voucher.prepare_source_cancellation",
			"china_finance.services.bank_receipt_import.allow_voucher_cancellation",
		):
			self.assertIn(callback, callbacks)
