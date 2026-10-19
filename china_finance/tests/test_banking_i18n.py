import unittest
from unittest.mock import patch

import frappe
from bs4 import BeautifulSoup

from china_finance.www import china_banking_embedded


class BankingTranslationTests(unittest.TestCase):
	def test_session_dictionary_is_initialized_before_deferred_module_scripts(self):
		html = '''<!doctype html><html><head>
		<script type="module" src="/assets/erpnext/banking/assets/current.js"></script>
		</head><body><div id="root"></div>
		<script>frappe.boot = {__messages: {Banking: "Banca"}};</script></body></html>'''
		result = china_banking_embedded.prepare_banking_html(html)
		page = BeautifulSoup(result, "html.parser")
		self.assertEqual(page.select_one('script[type="module"]')["src"],
			"/assets/erpnext/banking/assets/current.js")
		scripts = page.body.find_all("script")
		self.assertIn("frappe.boot =", scripts[0].string)
		self.assertEqual(scripts[-2].string, "frappe._messages = frappe.boot.__messages || {};")
		self.assertEqual(scripts[-1]["src"], "/assets/china_finance/js/banking_i18n.js?v=1")
		self.assertIsNotNone(page.select_one("#root"))

	def test_incompatible_template_is_reported(self):
		with self.assertRaisesRegex(ValueError, "must contain a body"):
			china_banking_embedded.prepare_banking_html("<div>No Banking document</div>")

	def test_guest_cannot_load_the_embedded_session(self):
		with patch.object(frappe, "session", frappe._dict(user="Guest")):
			with self.assertRaises(frappe.PermissionError):
				china_banking_embedded.get_context(frappe._dict())
