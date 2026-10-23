import unittest
from unittest.mock import patch

import frappe
from china_finance.services.archive import _validate_archive_reference


class TestArchiveReferenceValidation(unittest.TestCase):
    def test_cross_company_reference_is_a_validation_error(self):
        with self.assertRaises(frappe.ValidationError):
            _validate_archive_reference('Company A', 'Company', 'Company B')

    def test_unsupported_type_is_a_validation_error(self):
        with self.assertRaises(frappe.ValidationError):
            _validate_archive_reference('Company A', 'User', 'Administrator')
