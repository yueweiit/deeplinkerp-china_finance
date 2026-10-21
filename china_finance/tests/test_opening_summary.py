"""Opening summary preparation: permissions, boundaries and atomic staging."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from china_finance.tests import test_opening_balance as fixtures


class Doc(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__

	def set(self, key, value):
		self[key] = value


class TestOpeningSummary(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		fixtures.TestOpeningServiceGates.setUpClass()
		cls.opening = fixtures.TestOpeningServiceGates.service
		fake = ModuleType("frappe")
		fake._ = lambda value: value
		fake.whitelist = lambda **kwargs: lambda method: method
		utils = ModuleType("frappe.utils")
		utils.cint = lambda value: int(value or 0)
		file = Path(__file__).parents[1] / "services/opening_summary.py"
		spec = importlib.util.spec_from_file_location("_opening_summary_unit", file)
		cls.summary = importlib.util.module_from_spec(spec)
		with patch.dict(sys.modules, {"frappe": fake, "frappe.utils": utils, "china_finance.services.opening_balance": cls.opening}):
			spec.loader.exec_module(cls.summary)

	def setUp(self):
		self.frappe = MagicMock()
		self.frappe.PermissionError = PermissionError
		self.frappe.TimestampMismatchError = RuntimeError
		self.frappe.QueryDeadlockError = type("QueryDeadlockError", (Exception,), {})
		self.frappe.throw.side_effect = lambda message, exc=ValueError: (_ for _ in ()).throw(exc(message))
		for module in (self.summary, self.opening):
			patcher = patch.object(module, "frappe", self.frappe)
			patcher.start()
			self.addCleanup(patcher.stop)
		patcher = patch.object(self.summary, "opening", self.opening)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.doc = Doc(name="OPEN-1", company="Company A", mode="New", status="Draft", version=3, rows=[
			Doc(row_id="r1", account="AR", debit="100", credit="0", remarks="原文"),
			Doc(row_id="r2", account="AP", debit="0", credit="100"),
			Doc(row_id="r3", account="AR", party_type="Customer", party="Real customer"),
			Doc(row_id="r4", account="AR", party_type="Supplier"),
			Doc(row_id="r5", account="Wrong company or group"),
		])
		self.frappe.get_list.return_value = [Doc(name="AR", account_type="Receivable"), Doc(name="AP", account_type="Payable")]

	def test_candidates_respect_company_leaf_permissions_and_never_replace_existing_or_wrong_parties(self):
		self.assertEqual(self.summary.candidates(self.doc), {"Customer": [0], "Supplier": [1]})
		filters = self.frappe.get_list.call_args.kwargs["filters"]
		self.assertEqual(filters["company"], "Company A")
		self.assertEqual((filters["is_group"], filters["disabled"], filters["report_type"]), (0, 0, "Balance Sheet"))

	def test_preparation_changes_only_missing_party_links_and_saves_one_version_without_posting(self):
		before = [{key: value for key, value in row.items() if key not in ("party_type", "party")} for row in self.doc.rows]
		with patch.object(self.opening, "_get_batch", return_value=self.doc) as read, patch.object(self.opening, "_save") as save, patch.object(self.opening, "_envelope", return_value={"batch": self.doc}), patch.object(self.summary, "_party", side_effect=lambda _doc, kind, *args: kind + " aggregate"):
			result = self.summary.prepare_summary_parties("OPEN-1", 3)
		self.assertEqual(result["batch"].version, 4)
		read.assert_called_once_with("OPEN-1", write=True, lock=True)
		save.assert_called_once_with(self.doc)
		self.assertEqual(self.doc.rows[0].party, "Customer aggregate")
		self.assertEqual(self.doc.rows[1].party, "Supplier aggregate")
		self.assertEqual(self.doc.rows[2].party, "Real customer")
		self.assertEqual(self.doc.rows[3].party_type, "Supplier")
		self.assertEqual(before, [{key: value for key, value in row.items() if key not in ("party_type", "party")} for row in self.doc.rows])
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.commit.assert_not_called()

	def test_duplicate_business_row_ids_cannot_assign_a_summary_to_an_unrelated_account(self):
		self.doc.rows[4].row_id = "r1"
		with patch.object(self.opening, "_get_batch", return_value=self.doc), patch.object(self.opening, "_save"), patch.object(self.opening, "_envelope", return_value={}), patch.object(self.summary, "_party", return_value="Summary"):
			self.summary.prepare_summary_parties("OPEN-1", 3)
		self.assertEqual(self.doc.rows[0].party, "Summary")
		self.assertFalse(self.doc.rows[4].get("party"))

	def test_active_takeover_stale_version_and_missing_create_permission_never_create_parties(self):
		for changes, version, permission in (({"status": "Active"}, 3, True), ({"mode": "Takeover"}, 3, True), ({}, 2, True), ({}, 3, False)):
			with self.subTest(changes=changes, version=version, permission=permission):
				doc = Doc(self.doc, **changes)
				self.frappe.has_permission.return_value = permission
				with patch.object(self.opening, "_get_batch", return_value=doc), patch.object(self.summary, "_party") as create:
					with self.assertRaises((ValueError, RuntimeError, PermissionError)):
						self.summary.prepare_summary_parties("OPEN-1", version)
					create.assert_not_called()

	def test_native_party_insert_uses_permissions_and_actual_returned_name_not_assumed_naming(self):
		party = MagicMock()
		party.name = "CUST-007"
		self.frappe.new_doc.return_value = party
		result = self.summary._party(self.doc, "Customer", "Group", "Area")
		self.assertEqual(result, "CUST-007")
		self.assertEqual(self.doc.summary_customer, "CUST-007")
		self.assertIn("Company A", party.customer_name)
		self.assertEqual((party.customer_group, party.territory), ("Group", "Area"))
		party.insert.assert_called_once_with()
		party.check_permission.assert_called_once_with("read")

	def test_recorded_party_is_reused_but_disabled_or_unreadable_record_is_rejected(self):
		self.doc.summary_customer = "Existing aggregate"
		party = MagicMock()
		party.name = "Existing aggregate"
		party.get.return_value = 0
		self.frappe.get_doc.return_value = party
		self.assertEqual(self.summary._party(self.doc, "Customer"), "Existing aggregate")
		self.frappe.new_doc.assert_not_called()
		party.get.return_value = 1
		with self.assertRaises(ValueError):
			self.summary._party(self.doc, "Customer")
		party.check_permission.side_effect = PermissionError("unreadable")
		with self.assertRaises(PermissionError):
			self.summary._party(self.doc, "Customer")

	def test_partial_creation_failure_rolls_back_and_preserves_original_exception(self):
		with patch.object(self.opening, "_get_batch", return_value=self.doc), patch.object(self.summary, "_party", side_effect=["Customer aggregate", ValueError("supplier creation failed")]), patch.object(self.opening, "_save") as save:
			with self.assertRaisesRegex(ValueError, "supplier creation failed"):
				self.summary.prepare_summary_parties("OPEN-1", 3)
			save.assert_not_called()
		self.frappe.db.rollback.assert_called_once_with(save_point="opening_summary_parties")

	def test_deadlock_is_not_hidden_by_invalid_savepoint_rollback(self):
		error = self.frappe.QueryDeadlockError("deadlock")
		with patch.object(self.opening, "_get_batch", return_value=self.doc), patch.object(self.summary, "_party", side_effect=error):
			with self.assertRaises(self.frappe.QueryDeadlockError) as raised:
				self.summary.prepare_summary_parties("OPEN-1", 3)
		self.assertIs(raised.exception, error)
		self.frappe.db.rollback.assert_not_called()

	def test_warning_and_fingerprint_remain_bound_to_summary_identity(self):
		self.doc.summary_customer = "Summary"
		self.doc.rows[0].update(party_type="Customer", party="Summary")
		self.doc.currency = "CNY"
		self.doc.opening_date = "2026-01-01"
		self.doc.source_names_json = "[]"
		warnings = self.opening._historical_party_warnings(self.doc)
		self.assertEqual(len(warnings), 1)
		self.assertEqual(warnings[0]["row_id"], "r1")
		self.assertIn("尚未分配", warnings[0]["message"])
		self.assertEqual(self.opening._saved_payload(self.doc)["summary_customer"], "Summary")

	def test_empty_summary_fields_preserve_receipts_created_before_the_schema_change(self):
		self.doc.update(opening_date="2026-01-01", currency="CNY", revision=1, source_names_json="[]")
		legacy = {"name": "OPEN-1", "company": "Company A", "opening_date": "2026-01-01", "currency": "CNY", "mode": "New", "version": 3, "revision": 1, "previous_batch": None, "root_batch": None, "rows": self.opening._rows(self.doc), "source_names": []}
		receipt = {"batch_hash": self.opening.exact.fingerprint(legacy), "source_names": ["JE-1"], "evidence": [{"name": "JE-1", "hash": "original"}]}
		self.doc.receipt_json = json.dumps(receipt)
		for value in (None, ""):
			with self.subTest(value=value):
				self.doc.update(summary_customer=value, summary_supplier=value)
				self.assertEqual(self.opening._saved_payload(self.doc), legacy)
				with patch.object(self.opening, "_source_evidence", return_value={"name": "JE-1", "hash": "original"}):
					self.assertEqual(self.opening._revalidate_receipt(self.doc), receipt)
		self.doc.summary_customer = "new link without approval"
		with self.assertRaises(ValueError):
			self.opening._revalidate_receipt(self.doc)


try:
	import frappe as native_frappe
except ModuleNotFoundError:
	native_frappe = None


@unittest.skipUnless(native_frappe, "Requires an isolated migrated Frappe test site")
class TestOpeningSummaryNative(unittest.TestCase):
	setUp = fixtures.TestOpeningNative.setUp
	tearDown = fixtures.TestOpeningNative.tearDown
	rows = fixtures.TestOpeningNative.rows
	save = fixtures.TestOpeningNative.save
	activate = fixtures.TestOpeningNative.activate
	counts = fixtures.TestOpeningNative.counts

	def prepare(self):
		from china_finance.services import opening_summary

		ar = self._account("Summary AR", self.bank, account_type="Receivable")
		ap = self._account("Summary AP", self.bank, account_type="Payable")
		rows = self.rows()
		rows[0]["account"] = ar
		rows.extend([dict(rows[0], row_id="cash", account=self.bank, debit="60.00", base_debit="60.00"), dict(rows[1], row_id="ap", account=ap, credit="60.00", base_credit="60.00")])
		batch = self.save(rows)["batch"]
		group = native_frappe.get_doc({"doctype":"Customer Group", "customer_group_name":"Summary Group " + native_frappe.generate_hash(length=8), "parent_customer_group":native_frappe.db.get_value("Customer Group", {"is_group":1}, "name")}).insert().name
		supplier = native_frappe.get_doc({"doctype":"Supplier Group", "supplier_group_name":"Summary Group " + native_frappe.generate_hash(length=8), "parent_supplier_group":native_frappe.db.get_value("Supplier Group", {"is_group":1}, "name")}).insert().name
		territory = native_frappe.db.get_value("Territory", {}, "name")
		return opening_summary, batch, {"customer_group":group, "supplier_group":supplier, "territory":territory}

	def test_summary_activates_through_native_party_checks_and_replays_without_duplicates(self):
		module, batch, values = self.prepare()
		before = self.counts()
		prepared = module.prepare_summary_parties(batch["name"], batch["version"], **values)["batch"]
		self.assertEqual(self.counts(), before)
		self.assertTrue(prepared["summary_customer"])
		self.assertTrue(prepared["summary_supplier"])
		preview = self.service.preview_opening(batch["name"])
		self.assertEqual(preview["errors"], [])
		self.assertEqual(len(preview["warnings"]), 2)
		result = self.service.activate_opening(batch["name"], preview["fingerprint"])
		journal = native_frappe.get_doc("Journal Entry", result["receipt"]["source_names"][0])
		self.assertFalse(journal.party_not_required)
		self.assertEqual(journal.docstatus, 1)
		gl = native_frappe.get_all("GL Entry", filters={"voucher_no":journal.name, "is_cancelled":0}, fields=["party_type", "party", "debit", "credit"])
		customer = [row for row in gl if row.party_type == "Customer"]
		supplier = [row for row in gl if row.party_type == "Supplier"]
		self.assertEqual([(row.party, row.debit) for row in customer], [(prepared["summary_customer"], 100.0)])
		self.assertEqual([(row.party, row.credit) for row in supplier], [(prepared["summary_supplier"], 60.0)])
		counts = self.counts()
		self.service.activate_opening(batch["name"], preview["fingerprint"])
		self.assertEqual(self.counts(), counts)
		adjustment = self.service.start_adjustment(result["batch"]["name"], result["batch"]["version"])["batch"]
		self.assertEqual(adjustment["summary_customer"], prepared["summary_customer"])
		self.assertEqual(adjustment["summary_supplier"], prepared["summary_supplier"])

	def test_real_customer_creation_rolls_back_if_supplier_creation_fails(self):
		module, batch, values = self.prepare()
		original = module._party
		customers = native_frappe.db.count("Customer")
		suppliers = native_frappe.db.count("Supplier")

		def create(doc, doctype, *args):
			if doctype == "Supplier":
				raise ValueError("supplier failed")
			return original(doc, doctype, *args)

		with patch.object(module, "_party", side_effect=create), self.assertRaisesRegex(ValueError, "supplier failed"):
			module.prepare_summary_parties(batch["name"], batch["version"], **values)
		self.assertEqual(native_frappe.db.count("Customer"), customers)
		self.assertEqual(native_frappe.db.count("Supplier"), suppliers)
		saved = native_frappe.get_doc("China Opening Balance", batch["name"])
		self.assertFalse(saved.summary_customer)
		self.assertFalse(saved.summary_supplier)
		self.assertEqual(saved.version, batch["version"])
