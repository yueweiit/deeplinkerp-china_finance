"""Company-default regressions using real chart rules and an isolated account store."""

import operator
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from china_finance.setup import china_coa_profile as coa


class TestCompanyDefaultAccounts(unittest.TestCase):
	def setUp(self):
		self._patch(frappe, "local", SimpleNamespace(flags=frappe._dict(allow_unverified_charts=0)))
		self._patch(coa, "_", lambda message: message)
		self.rules = coa.get_profile_accounts()
		self.accounts = {
			rule["account_number"]: frappe._dict(
				**rule, name=f"{rule['account_number']} - Test", company="Test Company",
				account_currency="CNY", disabled=0, lft=1, rgt=2,
				report_type="Balance Sheet" if rule["root_type"] in {"Asset", "Liability", "Equity"} else "Profit and Loss",
			)
			for rule in self.rules
		}
		self.company = frappe._dict(name="Test Company", default_currency="CNY")
		for fieldname, number in coa.COMPANY_DEFAULT_ACCOUNTS.items():
			self.company[fieldname] = self.accounts[number].name
		self.company.db_set = Mock(side_effect=lambda field, value, **kwargs: self.company.update({field: value}))
		self.candidates = []
		self._patch(coa, "is_profile_company", Mock(return_value=True))
		self._patch(coa, "get_company_profile", Mock(return_value={
			"accounts": self.rules, "template": coa.CHART_TEMPLATE, "version": "1.0", "hash": "test-hash",
		}))
		self._patch(coa, "get_company_accounts_by_number", Mock(return_value=(self.accounts, {})))
		self._patch(coa, "get_company_account_type_overrides", Mock(return_value={}))
		self._patch(coa, "get_company_default_accounts", Mock(return_value=coa.COMPANY_DEFAULT_ACCOUNTS))
		self._patch(frappe, "get_cached_doc", Mock(return_value=self.company))
		self._patch(frappe, "get_doc", Mock(return_value=self.company))
		self._patch(frappe, "clear_document_cache", Mock())
		self._patch(frappe, "get_all", Mock(side_effect=lambda *args, **kwargs: [
			frappe._dict(parent_account=row.name) for row in self.accounts.values() if row.is_group
		]))
		self._patch(frappe, "db", SimpleNamespace(exists=self._exists))

	def _patch(self, owner, name, value):
		patcher = patch.object(owner, name, value)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _exists(self, doctype, filters):
		self.assertEqual(doctype, "Account")
		comparisons = {"=": operator.eq, ">": operator.gt, "<": operator.lt}
		for row in [*self.accounts.values(), *self.candidates]:
			if all(
				comparisons[value[0]](row.get(field), value[1]) if isinstance(value, list)
				else comparisons["="](row.get(field), value)
				for field, value in filters.items()
			):
				return row.name
		return None

	def _group_default(self, fieldname="default_receivable_account"):
		account = self.accounts[coa.COMPANY_DEFAULT_ACCOUNTS[fieldname]]
		account.update(is_group=1, lft=100, rgt=200)
		# Group types can be cleared by ERPNext; the template supplies the leaf type.
		candidate = frappe._dict(account)
		candidate.update(name="Selected leaf", account_number="custom", is_group=0, lft=120, rgt=121,
			parent_account="Intermediate group", account_type=next(
				rule["account_type"] for rule in self.rules if rule["account_number"] == account.account_number
			))
		account.account_type = None
		self.candidates = [candidate]
		self.company[fieldname] = candidate.name
		return account, candidate

	def test_every_grouped_default_accepts_a_valid_descendant(self):
		for fieldname in coa.COMPANY_DEFAULT_ACCOUNTS:
			with self.subTest(fieldname=fieldname):
				account, _candidate = self._group_default(fieldname)
				result = coa.validate_profile(self.company.name)
				self.assertEqual(result["errors"], [])
				self.assertEqual(result["default_differences"], [])
				account.is_group = 0
				account.account_type = next(
					rule["account_type"] for rule in self.rules if rule["account_number"] == account.account_number
				)
				self.company[fieldname] = account.name

	def test_grouped_default_rejects_unusable_or_unrelated_accounts(self):
		cases = [
			{"company": "Other Company"}, {"disabled": 1}, {"is_group": 1},
			{"account_type": "Payable"}, {"root_type": "Liability"}, {"account_currency": "USD"},
			# A matching number prefix outside the actual subtree is insufficient.
			{"account_number": "112201", "lft": 210, "rgt": 211},
			{"lft": 90, "rgt": 220}, {"lft": 120, "rgt": 201},
		]
		for changes in cases:
			with self.subTest(changes=changes):
				_account, candidate = self._group_default()
				candidate.update(changes)
				result = coa._company_default_readiness(self.company.name)
				self.assertFalse(result["passed"])
				self.assertEqual(result["severity"], "Blocking")
				self.assertIn("明细科目", result["details"])

	def test_group_or_missing_selection_still_blocks(self):
		account, _candidate = self._group_default()
		for selected in (None, "Missing Account", account.name):
			with self.subTest(selected=selected):
				self.company.default_receivable_account = selected
				self.assertFalse(coa._company_default_readiness(self.company.name)["passed"])

	def test_disabled_group_still_blocks(self):
		account, _candidate = self._group_default()
		account.disabled = 1
		self.assertFalse(coa._company_default_readiness(self.company.name)["passed"])

	def test_leaf_default_keeps_exact_template_selection(self):
		self.assertTrue(coa._company_default_readiness(self.company.name)["passed"])
		self.company.default_receivable_account = "Other Receivable"
		result = coa._company_default_readiness(self.company.name)
		self.assertFalse(result["passed"])
		self.assertIn("default_receivable_account 应为科目编号 1122", result["details"])

	def test_template_account_type_override_is_used_for_descendants(self):
		_account, candidate = self._group_default()
		candidate.account_type = "Payable"
		coa.get_company_account_type_overrides.return_value = {"1122": "Payable"}
		self.assertTrue(coa._company_default_readiness(self.company.name)["passed"])

	def test_auto_configuration_never_writes_any_group_or_chooses_a_child(self):
		for fieldname in coa.COMPANY_DEFAULT_ACCOUNTS:
			account, _candidate = self._group_default(fieldname)
			for selected in (None, "Selected leaf", account.name):
				for repair in (False, True):
					with self.subTest(fieldname=fieldname, selected=selected, repair=repair):
						self.company[fieldname] = selected
						self.assertEqual(coa.apply_company_defaults(self.company.name, repair=repair)["updated"], 0)
						self.assertEqual(self.company[fieldname], selected)
			account.is_group = 0
			self.company[fieldname] = account.name
		self.company.db_set.assert_not_called()

	def test_auto_configuration_keeps_leaf_fill_and_explicit_restore_behavior(self):
		self.company.default_receivable_account = None
		self.assertEqual(coa.apply_company_defaults(self.company.name)["updated"], 1)
		self.company.default_receivable_account = "Finance user's choice"
		self.assertEqual(coa.apply_company_defaults(self.company.name)["updated"], 0)
		self.assertEqual(coa.apply_company_defaults(self.company.name, repair=True)["updated"], 1)
		self.assertEqual(self.company.default_receivable_account, self.accounts["1122"].name)
