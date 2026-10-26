"""Finance navigation stays ordered without loading a site or changing data."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class TestExpenseNavigation(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		path = Path(__file__).parents[1] / "setup/install.py"
		spec = importlib.util.spec_from_file_location("expense_navigation_install", path)
		cls.install = importlib.util.module_from_spec(spec)
		with patch.dict(sys.modules, {"frappe": types.ModuleType("frappe")}):
			spec.loader.exec_module(cls.install)

	def test_sidebar_groups_preserved_custom_links_between_banking_and_history(self):
		entry = self.install._sidebar_link("operating-expenses", "运营支出", "Page")
		rows = self.install._desired_sidebar_items([entry])
		labels = [row["label"] for row in rows]
		self.assertLess(labels.index("银行与对账"), labels.index("支出管理"))
		self.assertLess(labels.index("支出管理"), labels.index("凭证明细与历史"))
		self.assertIs(rows[labels.index("支出管理") + 1], entry)
		self.assertEqual(entry["child"], 1)
		self.assertNotIn("自定义", labels)

	def test_workspace_uses_same_group_order_and_link_count(self):
		entry = self.install._workspace_link("operating-expenses", "运营支出", "Page")
		rows = self.install._desired_workspace_links([entry])
		labels = [row["label"] for row in rows]
		self.assertLess(labels.index("银行与对账"), labels.index("支出管理"))
		self.assertLess(labels.index("支出管理"), labels.index("凭证明细与历史"))
		self.assertEqual(rows[labels.index("支出管理")]["link_count"], 1)
		self.assertIs(rows[labels.index("支出管理") + 1], entry)

	def test_no_empty_group_is_added_without_custom_links(self):
		for builder in (self.install._desired_sidebar_items, self.install._desired_workspace_links):
			self.assertNotIn("支出管理", [row["label"] for row in builder()])
