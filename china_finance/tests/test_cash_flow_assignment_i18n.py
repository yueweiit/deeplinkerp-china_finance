"""Cash-flow editor resource coverage, independent of a Frappe site."""

import csv
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


class TestCashFlowAssignmentI18n(unittest.TestCase):
	def test_all_editor_messages_have_english_and_spanish_resources(self):
		source = (PACKAGE / "public/js/china_cash_flow_assignment_list.js").read_text(encoding="utf-8")
		keys = set(re.findall(r'__\("([^"]+)"', source))
		self.assertGreater(len(keys), 100)
		for language in ("en", "es"):
			with (PACKAGE / "translations" / f"{language}.csv").open(encoding="utf-8", newline="") as handle:
				translations = {row[0]: row[1] for row in csv.reader(handle) if len(row) >= 2}
			for key in keys:
				with self.subTest(language=language, key=key):
					self.assertTrue(translations.get(key), f"Missing {language} translation: {key}")
					self.assertEqual(
						sorted(re.findall(r"\{\d+\}", key)),
						sorted(re.findall(r"\{\d+\}", translations[key])),
					)
