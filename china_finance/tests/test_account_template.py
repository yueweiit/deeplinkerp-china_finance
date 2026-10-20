from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from china_finance.overrides.company import ChinaFinanceCompany
from china_finance.services.account_template import (
	COMPANY_TEMPLATE,
	_get_template_sync_blockers,
	build_company_template_chart,
	count_chart_accounts,
	flatten_company_template_chart,
	get_charts_for_country,
	get_company_template_chart,
	get_company_template_data,
	preview_existing_company_template_sync,
	sync_existing_company_to_template,
)
from china_finance.setup.china_coa_profile import (
	CHART_TEMPLATE,
	get_company_default_accounts,
	get_settings_accounts,
	get_tax_account_rules,
	validate_profile,
)


class TestAccountTemplate(UnitTestCase):
	def test_export_rejects_asset_under_expense_parent(self):
		rows = [
			frappe._dict(
				name="研发支出", parent_account=None, root_type="Expense", report_type="Profit and Loss"
			),
			frappe._dict(
				name="530102", parent_account="研发支出", root_type="Asset", report_type="Balance Sheet"
			),
		]
		with patch.object(frappe, "get_all", return_value=rows):
			with self.assertRaisesRegex(ValueError, "530102"):
				build_company_template_chart("Test Company")

	def test_company_hierarchy_blocks_template_conversion(self):
		for has_parent, has_child in ((True, False), (False, True)):
			with self.subTest(has_parent=has_parent, has_child=has_child):
				with (
					patch.object(frappe.db, "get_value", return_value="Parent" if has_parent else None),
					patch.object(
						frappe.db,
						"exists",
						side_effect=lambda dt, *_: has_child if dt == "Company" else False,
					),
					patch.object(frappe.db, "count", return_value=0),
				):
					blockers = _get_template_sync_blockers("New Company")
				self.assertTrue(any(row["doctype"] == "Company" for row in blockers))

	def test_same_company_name_does_not_override_a_standard_chart(self):
		with patch("china_finance.setup.china_coa_profile.uses_yuewei_company_chart", return_value=False):
			company = "悦为智能技术(东莞)有限公司"
			self.assertEqual(get_company_default_accounts(company)["depreciation_expense_account"], "660203")
			self.assertEqual(get_tax_account_rules(company)["Output"], "22210102")
			self.assertEqual(get_settings_accounts(company)["retained_earnings_account"], "410401")

	def test_source_company_template_is_available_for_china(self):
		self.assertIn(COMPANY_TEMPLATE, get_charts_for_country("China", with_standard=True))
		self.assertNotIn(COMPANY_TEMPLATE, get_charts_for_country("India", with_standard=True))

	def test_bundled_template_matches_current_source_snapshot(self):
		data = get_company_template_data()
		self.assertEqual(data["account_count"], 335)
		self.assertEqual(count_chart_accounts(get_company_template_chart()), data["account_count"])
		self.assertEqual(set(get_company_template_chart()), {"资产", "负债", "所有者权益", "收入", "费用"})
		rows = flatten_company_template_chart()
		self.assertEqual(len(rows), data["account_count"])
		self.assertEqual(len({row["account_number"] for row in rows if row["account_number"]}), 313)
		self.assertTrue(all(row["root_type"] for row in rows))
		capitalized = next(row for row in rows if row["account_number"] == "530102")
		self.assertEqual(capitalized["root_type"], "Asset")
		self.assertEqual(capitalized["report_type"], "Balance Sheet")
		self.assertEqual(capitalized["parent_key"], "资产/非流动资产/开发支出")

	def test_source_company_template_uses_bundled_chart_path(self):
		company = frappe.new_doc("Company")
		company.update(
			{
				"company_name": "模板测试公司",
				"abbr": "MTC",
				"country": "China",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": COMPANY_TEMPLATE,
			}
		)
		self.assertIsInstance(company, ChinaFinanceCompany)

		company.validate_coa_input()

		self.assertEqual(company.create_chart_of_accounts_based_on, "Standard Template")
		self.assertIsNone(company.existing_company)
		self.assertEqual(company.chart_of_accounts, CHART_TEMPLATE)
		self.assertTrue(company._use_company_account_template)


class TestAccountTemplateIntegration(IntegrationTestCase):
	def test_new_company_is_created_from_bundled_template(self):
		suffix = frappe.generate_hash(length=5).upper()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": f"_Test Bundled CoA {suffix}",
				"abbr": suffix,
				"country": "China",
				"default_currency": "CNY",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": COMPANY_TEMPLATE,
			}
		)
		# The shared test site can retain invalid Mode of Payment links from
		# unrelated ERPNext tests. They do not participate in CoA creation.
		with patch.object(ChinaFinanceCompany, "set_mode_of_payment_account"):
			company.insert()

		self.assertEqual(
			frappe.db.count("Account", {"company": company.name, "disabled": 0}),
			get_company_template_data()["account_count"],
		)
		settings = frappe.get_doc("China Finance Settings", company.name)
		self.assertEqual(settings.accounting_standard, "小企业会计准则")
		status = validate_profile(company.name)
		self.assertEqual(status["status"], "Ready", status["errors"])
		self.assertEqual(status["errors"], [])
		company.reload()
		construction = frappe.db.get_value(
			"Account", {"company": company.name, "account_number": "1604"}, ["name", "is_group"], as_dict=True
		)
		self.assertTrue(construction.is_group)
		# A grouped default needs a finance user's choice; initialization must not
		# select one of the construction accounts on the user's behalf.
		self.assertIsNone(company.capital_work_in_progress_account)
		warning = frappe._("公司默认科目 {0} 应为 {1} 下本公司已启用且类型、币种匹配的明细科目").format(
			"capital_work_in_progress_account", construction.name
		)
		self.assertEqual(status["warnings"], [warning])
		self.assertEqual(status["default_differences"], [{
			"fieldname": "capital_work_in_progress_account",
			"account_number": "1604",
			"expected": construction.name,
			"actual": None,
			"details": warning,
		}])
		for fieldname, number in get_company_default_accounts(company.name).items():
			if fieldname == "capital_work_in_progress_account":
				continue
			with self.subTest(default_account=fieldname):
				self.assertEqual(
					company.get(fieldname),
					frappe.db.get_value("Account", {"company": company.name, "account_number": number}),
				)
		self.assertEqual(settings.coa_template, COMPANY_TEMPLATE)
		self.assertEqual(settings.coa_hash, status["hash"])
		capitalized = frappe.db.get_value("Account", {"company": company.name, "account_number": "530102"})
		frappe.db.set_value("Account", capitalized, "report_type", "Profit and Loss")
		self.assertEqual(validate_profile(company.name)["status"], "Needs Attention")
		frappe.db.set_value("Account", capitalized, "report_type", "Balance Sheet")
		self.assertEqual(
			frappe.db.get_value("Account", settings.retained_earnings_account, "account_number"), "410411"
		)
		for account_number in ("2301", "2401"):
			self.assertEqual(
				frappe.db.get_value(
					"Account",
					{"company": company.name, "account_number": account_number},
					"account_name",
				),
				"递延收益",
			)

	def test_zero_ledger_standard_company_can_be_upgraded_to_bundled_template(self):
		suffix = frappe.generate_hash(length=5).upper()
		company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": f"_Test CoA Upgrade {suffix}",
				"abbr": suffix,
				"country": "China",
				"default_currency": "CNY",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": CHART_TEMPLATE,
			}
		)
		with patch.object(ChinaFinanceCompany, "set_mode_of_payment_account"):
			company.insert()

		settings = frappe.get_doc("China Finance Settings", company.name)
		settings.update(
			{
				"accounting_standard": "小企业会计准则",
				"activation_date": "2026-05-01",
				"statutory_reporting_activation_date": "2026-05-01",
				"statement_mapping_activation_date": "2026-05-01",
				"cash_flow_assignment_activation_date": "2026-05-01",
			}
		)
		settings.save()
		preview = preview_existing_company_template_sync(company.name)
		self.assertTrue(preview["can_apply"])
		self.assertGreater(preview["missing_count"], 0)
		settings = frappe.get_doc("China Finance Settings", company.name)
		self.assertEqual(
			frappe.db.get_value("Account", settings.retained_earnings_account, "account_number"), "410401"
		)
		output_template = frappe.get_doc(
			"Sales Taxes and Charges Template",
			{
				"company": company.name,
				"title": "销售税费模板（13%销项税）",
			},
		)
		self.assertEqual(
			frappe.db.get_value("Account", output_template.taxes[0].account_head, "account_number"),
			"22210102",
		)
		self.assertTrue(preview["configuration_updates"])

		with patch.object(frappe.db, "commit"):
			result = sync_existing_company_to_template(company.name, apply=True)

		self.assertTrue(result["applied"])
		self.assertGreater(result["created_count"], 0)
		self.assertEqual(
			frappe.db.count("Account", {"company": company.name, "disabled": 0}),
			get_company_template_data()["account_count"] + result["retained_extra_count"],
		)
		for account_number, account_name in (
			("2301", "递延收益"),
			("2401", "递延收益"),
			("660225", "管理费用－折旧费"),
		):
			self.assertEqual(
				frappe.db.get_value(
					"Account",
					{"company": company.name, "account_number": account_number},
					"account_name",
				),
				account_name,
			)
		self.assertEqual(
			frappe.db.get_value(
				"Account",
				frappe.db.get_value("Company", company.name, "depreciation_expense_account"),
				"account_number",
			),
			"660225",
		)
		after_sync = preview_existing_company_template_sync(company.name)
		self.assertEqual(after_sync["missing_count"], 0)
		self.assertEqual(after_sync["rename_count"], 0)
		self.assertEqual(after_sync["reparent_count"], 0)
		self.assertEqual(after_sync["configuration_updates"], [])
		self.assertEqual(validate_profile(company.name)["status"], "Ready")
		settings.reload()
		self.assertEqual(settings.accounting_standard, "小企业会计准则")
		for field in (
			"activation_date",
			"statutory_reporting_activation_date",
			"statement_mapping_activation_date",
			"cash_flow_assignment_activation_date",
		):
			self.assertEqual(str(settings.get(field)), "2026-05-01")
		self.assertEqual(
			frappe.db.get_value("Account", settings.retained_earnings_account, "account_number"), "410411"
		)
		output_template.reload()
		self.assertEqual(
			frappe.db.get_value("Account", output_template.taxes[0].account_head, "account_number"),
			"22210107",
		)
		item_template = frappe.get_doc(
			"Item Tax Template",
			{
				"company": company.name,
				"title": "物料税费模板（13%销项税）",
			},
		)
		self.assertEqual(
			frappe.db.get_value("Account", item_template.taxes[0].tax_type, "account_number"), "22210107"
		)
		output_mappings = frappe.get_all(
			"China Tax Account Mapping",
			filters={
				"company": company.name,
				"direction": "Output",
				"enabled": 1,
			},
			pluck="account",
		)
		self.assertTrue(output_mappings)
		self.assertEqual(
			{frappe.db.get_value("Account", name, "account_number") for name in output_mappings}, {"22210107"}
		)
		with patch.object(frappe.db, "commit"):
			repeat = sync_existing_company_to_template(company.name, apply=True)
		self.assertEqual(repeat["created_count"], 0)
		self.assertEqual(repeat["configuration_updates_applied"], 0)
		# Reproduce the previous bundled chart: capitalized R&D inherited Expense.
		capitalized = frappe.db.get_value("Account", {"company": company.name, "account_number": "530102"})
		expense_parent = frappe.db.get_value("Account", {"company": company.name, "account_number": "5301"})
		frappe.db.set_value(
			"Account",
			capitalized,
			{
				"parent_account": expense_parent,
				"root_type": "Expense",
				"report_type": "Profit and Loss",
			},
		)
		status = validate_profile(company.name)
		self.assertEqual(status["status"], "Needs Attention")
		self.assertTrue(any("530102" in error for error in status["errors"]))
		self.assertEqual(preview_existing_company_template_sync(company.name)["reparent_count"], 1)
		with patch.object(frappe.db, "commit"):
			sync_existing_company_to_template(company.name, apply=True)
		settings.reload()
		self.assertEqual(settings.coa_integrity_status, "Ready")
		self.assertEqual(settings.coa_template, COMPANY_TEMPLATE)

		deposit = frappe.db.get_value("Account", {"company": company.name, "account_number": "101201"})
		scopes = frappe.get_all(
			"China Cash Equivalent Scope",
			filters={"company": company.name, "account": deposit},
			fields=["restricted", "reviewed"],
		)
		self.assertTrue(scopes)
		self.assertTrue(all(not row.restricted and not row.reviewed for row in scopes))
		mapping = frappe.db.get_value("China Financial Statement Mapping", {"company": company.name})
		self.assertTrue(mapping)
		frappe.db.set_value("China Financial Statement Mapping", mapping, "reviewed", 1)
		self.assertFalse(preview_existing_company_template_sync(company.name)["can_apply"])
		frappe.db.set_value("China Financial Statement Mapping", mapping, "reviewed", 0)

		# A draft is enough to block a later conversion, even with zero GL rows.
		company.reload()
		frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": company.name,
				"posting_date": "2026-05-01",
				"accounts": [
					{"account": company.default_cash_account, "debit_in_account_currency": 1},
					{"account": company.default_bank_account, "credit_in_account_currency": 1},
				],
			}
		).insert()
		blocked = preview_existing_company_template_sync(company.name)
		self.assertEqual(blocked["general_ledger_entry_count"], 0)
		self.assertFalse(blocked["can_apply"])
		self.assertTrue(any(row["doctype"] == "Journal Entry" for row in blocked["blockers"]))
		with self.assertRaises(frappe.ValidationError):
			sync_existing_company_to_template(company.name, apply=True)
