"""Exercise scope synchronization and the real scope controller without a site database."""

import operator
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from china_finance import api
from china_finance.china_finance.doctype.china_cash_equivalent_scope import (
	china_cash_equivalent_scope as scope,
)
from china_finance.setup import china_coa_profile as coa


class TestCashScopeSync(unittest.TestCase):
	def setUp(self):
		self.accounts = []
		self.scopes = []
		self.company = frappe._dict(name="Test Company", default_currency="CNY",
			default_cash_account="Cash leaf", default_bank_account="Bank leaf")
		self.company.db_set = Mock(side_effect=lambda field, value, **kwargs: self.company.update({field: value}))
		self.settings = frappe._dict(company=self.company.name, activation_date="2026-09-01", accounting_standard="test")
		for name, number, kind, group, left, right in (
			("Cash group", "1001", None, 1, 10, 50),
			("Cash leaf", "100101", "Cash", 0, 12, 13),
			("Nested cash group", "100102", None, 1, 20, 40),
			("Nested cash leaf", "10010201", "Cash", 0, 22, 23),
			("Bank group", "100201", None, 1, 60, 90),
			("Bank leaf", "10020101", "Bank", 0, 62, 63),
			("Restricted group", "101201", None, 1, 100, 130),
			("Restricted bank leaf", "10120101", "Bank", 0, 102, 103),
			("Other funds", "101299", None, 0, 140, 141),
		):
			self.accounts.append(frappe._dict(name=name, account_number=number, account_name=name,
				company=self.company.name, root_type="Asset", account_type=kind, is_group=group,
				disabled=0, lft=left, rgt=right, account_currency="CNY"))
		self._patch(frappe, "get_all", Mock(side_effect=self._get_all))
		self._patch(frappe, "get_doc", Mock(side_effect=self._get_doc))
		self._patch(frappe, "get_cached_doc", Mock(return_value=self.settings))
		self._patch(frappe, "clear_document_cache", Mock())
		self._patch(frappe, "db", SimpleNamespace(exists=self._exists, get_value=self._get_value))
		self._patch(frappe, "throw", self._throw)
		self._patch(frappe, "only_for", Mock())
		self._patch(frappe, "has_permission", Mock())
		self._patch(coa, "is_profile_company", Mock(return_value=True))
		self._patch(api, "is_profile_company", Mock(return_value=True))
		self._patch(coa, "uses_yuewei_company_chart", Mock(return_value=False))
		self._patch(coa, "get_china_coa_master_data_readiness", Mock(return_value={"status": "Ready"}))
		vat_result = {key: 0 for key in ("templates_updated", "item_templates_updated", "item_templates_created",
			"vat_account_disabled", "vat_account_deleted")}
		for module in (coa, api):
			self._patch(module, "_", lambda message: message)
			self._patch(module, "ensure_tax_mappings", Mock(return_value=0))
			self._patch(module, "normalize_generic_vat_templates", Mock(return_value=vat_result))
		self._patch(scope, "_", lambda message: message)
		for name in ("create_automatic_mappings", "sync_unreviewed_automatic_mappings", "update_settings_profile"):
			self._patch(api, name, Mock(return_value=0))
		self._patch(api, "get_profile_status", Mock(return_value={"status": "Ready"}))

	def _patch(self, owner, name, value):
		patcher = patch.object(owner, name, value)
		patcher.start()
		self.addCleanup(patcher.stop)

	@staticmethod
	def _throw(message, *args, **kwargs):
		raise frappe.ValidationError(message)

	@staticmethod
	def _matches(row, filters):
		comparisons = {"=": operator.eq, "!=": operator.ne, "<": operator.lt, ">": operator.gt,
			"in": lambda actual, values: actual in values}
		return all(
			comparisons[value[0]](row.get(field), value[1]) if isinstance(value, list)
			else row.get(field) == value for field, value in filters.items()
		)

	def _get_all(self, doctype, filters=None, **kwargs):
		rows = self.accounts if doctype == "Account" else self.scopes if doctype == "China Cash Equivalent Scope" else []
		result = [frappe._dict(row) for row in rows if self._matches(row, filters or {})]
		if kwargs.get("order_by"):
			field, direction = kwargs["order_by"].split()
			result.sort(key=lambda row: str(row.get(field)), reverse=direction == "desc")
		return result

	def _exists(self, doctype, filters):
		rows = self._get_all(doctype, filters if isinstance(filters, dict) else {"name": filters})
		return rows[0].name if rows else None

	def _get_value(self, doctype, name, fields, as_dict=False, **kwargs):
		rows = self._get_all(doctype, {"name": name})
		if not rows:
			return None
		if isinstance(fields, str):
			return rows[0].get(fields)
		values = frappe._dict({field: rows[0].get(field) for field in fields})
		return values if as_dict else list(values.values())

	def _get_doc(self, doctype, name=None):
		if doctype == "Company":
			return self.company
		if doctype == "China Finance Settings":
			return self.settings
		self.assertIsInstance(doctype, dict)
		self.assertEqual(doctype["doctype"], "China Cash Equivalent Scope")
		doc = SimpleNamespace(**{"name": None, "effective_to": None, "restricted": 0,
			"restriction_reason": None, **doctype})
		for method in ("before_validate", "validate", "_validate_no_overlap", "_invalidate_review"):
			setattr(doc, method, MethodType(getattr(scope.ChinaCashEquivalentScope, method), doc))
		doc.is_new = lambda: True
		def insert(**kwargs):
			doc.before_validate()
			doc.validate()
			doc.name = f"Scope-{len(self.scopes) + 1}"
			self.scopes.append(frappe._dict({key: value for key, value in vars(doc).items() if not callable(value)}))
			return doc
		doc.insert = insert
		return doc

	def test_master_data_checks_and_repairs_accept_grouped_cash_accounts(self):
		for repair in (False, True):
			with self.subTest(repair=repair):
				self.scopes.clear()
				result = coa.sync_china_coa_master_data(self.company.name, repair=repair)
				self.assertEqual(result["cash_scope_created"], 5)
				self.assertEqual(result["company_defaults_updated"], 0)
				self.assertEqual(coa.sync_china_coa_master_data(self.company.name, repair=repair)["cash_scope_created"], 0)
				self.company.db_set.assert_not_called()

	def test_profile_sync_and_restore_accept_grouped_cash_accounts(self):
		for restore in (0, 1):
			with self.subTest(restore=restore):
				self.scopes.clear()
				result = api.sync_china_coa_profile(self.company.name, repair_defaults=restore)
				self.assertEqual(result["cash_scope_created"], 5)
				self.assertEqual(result["company_defaults_updated"], 0)
				self.company.db_set.assert_not_called()

	def test_scope_rules_preserve_exclusions_and_require_review(self):
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 5)
		by_account = {row.account: row for row in self.scopes}
		self.assertEqual(by_account["Cash leaf"].classification, "库存现金")
		self.assertEqual(by_account["Nested cash leaf"].classification, "库存现金")
		self.assertEqual(by_account["Bank leaf"].classification, "随时可用存款")
		self.assertEqual(by_account["Restricted bank leaf"].classification, "排除项")
		self.assertEqual(by_account["Restricted bank leaf"].included, 0)
		self.assertEqual(by_account["Restricted bank leaf"].restricted, 1)
		self.assertTrue(by_account["Restricted bank leaf"].restriction_reason)
		self.assertEqual(by_account["Other funds"].included, 0)
		self.assertTrue(all(row.reviewed == 0 for row in self.scopes))

	def test_unnumbered_and_foreign_currency_cash_descendants_are_supported(self):
		self.accounts.append(frappe._dict({**self.accounts[1], "name": "Unnumbered foreign cash",
			"account_number": None, "account_currency": "USD", "lft": 24, "rgt": 25}))
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 6)
		self.assertTrue(any(row.account == "Unnumbered foreign cash" for row in self.scopes))

	def test_existing_leaf_rules_keep_their_classifications(self):
		self.accounts = [row for row in self.accounts if row.account_number in {"1001", "100201", "101201", "101299"}]
		for row in self.accounts:
			row.is_group = 0
			row.account_type = "Cash" if row.account_number == "1001" else "Bank" if row.account_number == "100201" else None
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 4)
		self.assertEqual({row.account: row.classification for row in self.scopes}, {
			"Cash group": "库存现金", "Bank group": "随时可用存款",
			"Restricted group": "排除项", "Other funds": "排除项",
		})
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 0)

	def test_company_chart_retains_its_out_of_town_deposit_policy(self):
		coa.uses_yuewei_company_chart.return_value = True
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 5)
		restricted = next(row for row in self.scopes if row.account == "Restricted bank leaf")
		self.assertEqual(restricted.included, 0)
		self.assertEqual(restricted.restricted, 0)
		self.assertIn("外埠存款", restricted.restriction_reason)

	def test_exclusions_take_precedence_inside_an_included_bank_group(self):
		for account in self.accounts:
			if account.name == "Restricted group":
				account.update(lft=70, rgt=80)
			elif account.name == "Restricted bank leaf":
				account.update(lft=72, rgt=73)
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 5)
		restricted = next(row for row in self.scopes if row.account == "Restricted bank leaf")
		self.assertEqual(restricted.included, 0)
		self.assertEqual(restricted.restricted, 1)

	def test_unusable_or_unrelated_cash_children_are_not_suggested(self):
		valid = self.accounts[1]
		for name, changes in (
			("Disabled", {"disabled": 1}), ("Other company", {"company": "Other Company"}),
			("Wrong type", {"account_type": "Receivable"}), ("Wrong root", {"root_type": "Liability"}),
			("Wrong bank root", {"account_type": "Bank", "root_type": "Liability"}),
			("Prefix only", {"lft": 200, "rgt": 201}),
		):
			self.accounts.append(frappe._dict({**valid, "name": name, "account_number": "100109" + name, **changes}))
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 5)

	def test_additional_bank_accounts_still_receive_review_required_suggestions(self):
		self.accounts.append(frappe._dict({**self.accounts[5], "name": "Additional bank",
			"account_number": "custom-bank", "lft": 200, "rgt": 201}))
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 6)
		bank = next(row for row in self.scopes if row.account == "Additional bank")
		self.assertEqual(bank.classification, "随时可用存款")
		self.assertEqual(bank.reviewed, 0)
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-09-01"), 0)

	def test_existing_reviewed_scopes_and_future_periods_are_preserved(self):
		coa.ensure_cash_scope(self.company.name, "2026-09-01")
		for row in self.scopes:
			row.update(reviewed=1, included=0, classification="排除项", policy_basis="Finance decision")
		before = [dict(row) for row in self.scopes]
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-10-01"), 0)
		self.assertEqual(self.scopes, before)
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-08-01"), 5)
		new = self.scopes[len(before):]
		self.assertTrue(all(str(row.effective_to) == "2026-08-31" and row.reviewed == 0 for row in new))
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-08-01"), 0)
		self.assertEqual(coa.ensure_cash_scope(self.company.name, "2026-08-15"), 0)
