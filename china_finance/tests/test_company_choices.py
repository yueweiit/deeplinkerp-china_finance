"""Site-free permission scope and pagination contract tests for company choices."""
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class Row(dict):
	__getattr__ = dict.get


class CompanyChoicesTests(unittest.TestCase):
	def setUp(self):
		self.frappe = types.ModuleType('frappe')
		self.frappe.whitelist = lambda **kwargs: lambda fn: fn
		self.frappe.parse_json = json.loads
		self.frappe.get_list = Mock(return_value=[])
		self.frappe.get_all = Mock(return_value=[])
		utils = types.ModuleType('frappe.utils')
		utils.cint = lambda value: int(value or 0)
		self.modules = patch.dict(sys.modules, {'frappe': self.frappe, 'frappe.utils': utils})
		self.modules.start()
		self.addCleanup(self.modules.stop)
		spec = importlib.util.spec_from_file_location('company_choices_test', Path(__file__).resolve().parents[1] / 'services/company_context.py')
		self.module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.module)

	def test_recent_companies_and_status_are_limited_to_permission_aware_results(self):
		self.frappe.get_list.side_effect = [[Row(name='Allowed', abbr='A')], [Row(name='Allowed', abbr='A')]]
		self.frappe.get_all.return_value = [Row(company='Allowed', enabled=1)]
		result = self.module.get_company_choices(recent='["Forbidden", "Allowed"]')
		self.assertEqual([r.name for r in result['recent']], ['Allowed'])
		self.assertTrue(result['companies'][0].finance_enabled)
		self.assertEqual(self.frappe.get_all.call_args.args, ('China Finance Settings',))
		self.assertEqual(self.frappe.get_all.call_args.kwargs['filters'], {'company': ['in', ['Allowed']]})
		for call in self.frappe.get_list.call_args_list:
			self.assertEqual(call.args, ('Company',))
			self.assertEqual(call.kwargs['filters']['is_group'], 0)
			self.assertNotIn('ignore_permissions', call.kwargs)

	def test_pagination_is_bounded_and_reports_more_results(self):
		self.frappe.get_list.return_value = [Row(name=str(i), abbr='') for i in range(21)]
		result = self.module.get_company_choices(txt='  Search  ', start=-100)
		self.assertEqual(len(result['companies']), 20)
		self.assertTrue(result['has_more'])
		call = self.frappe.get_list.call_args.kwargs
		self.assertEqual((call['start'], call['page_length']), (0, 21))
		self.assertEqual(call['or_filters']['abbr'], ['like', '%Search%'])

	def test_recent_input_is_capped_deduplicated_and_shape_checked(self):
		self.module.get_company_choices(recent=['A', 'A', None, 'B', 'C', 'D', 'E', 'F'])
		self.assertEqual(self.frappe.get_list.call_args.kwargs['filters']['name'][1], ['A', 'B', 'C', 'D', 'E'])
		self.frappe.get_list.reset_mock()
		self.module.get_company_choices(recent={'Forbidden': True})
		self.assertEqual(self.frappe.get_list.call_count, 1)

	def test_no_access_does_not_query_finance_settings(self):
		result = self.module.get_company_choices()
		self.assertEqual(result, {'companies': [], 'recent': [], 'has_more': False})
		self.frappe.get_all.assert_not_called()

	def test_permission_denial_is_propagated(self):
		self.frappe.get_list.side_effect = PermissionError('No Company access')
		with self.assertRaises(PermissionError):
			self.module.get_company_choices()
		self.frappe.get_all.assert_not_called()

	def test_link_search_supports_abbreviations_and_keeps_permission_scope(self):
		self.frappe.get_list.return_value = [Row(name="Allowed", abbr="AL")]
		result = self.module.search_companies("Company", "AL", "name", -1, 999, {"is_group": 1})
		self.assertEqual(result, [["Allowed", "AL"]])
		call = self.frappe.get_list.call_args.kwargs
		self.assertEqual(call["filters"], {"is_group": 0})
		self.assertEqual(call["or_filters"]["abbr"], ["like", "%AL%"])
		self.assertEqual((call["start"], call["page_length"]), (0, 20))
		self.frappe.get_all.assert_not_called()
