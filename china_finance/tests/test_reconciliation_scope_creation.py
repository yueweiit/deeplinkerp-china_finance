"""Real insert regression for derived Dynamic Link targets and field-based naming."""
import unittest

import frappe
from china_finance.tests import test_inline_voucher_account_edit as fixtures


class TestReconciliationScopeCreation(unittest.TestCase):
    def test_native_insert_derives_identity_before_links_and_naming(self):
        point = 'scope_creation_' + frappe.generate_hash(length=8)
        frappe.db.savepoint(point)
        try:
            fixture = fixtures.TestInlineVoucherAccountEdit()
            fixture._make_company()
            transaction = fixture._bank_transaction()
            values = {'doctype': 'China Reconciliation Scope', 'company': fixture.company,
                      'scope_type': 'Bank', 'reference_name': transaction.bank_account,
                      'effective_from': '2026-01-01', 'confirmation_method': 'Bank Statement',
                      'amount_tolerance': .01}
            doc = frappe.get_doc(values).insert()
            self.assertEqual(doc.reference_doctype, 'Bank Account')
            self.assertEqual(doc.name, f'{fixture.company}|Bank|{transaction.bank_account}')
            self.assertEqual(doc.scope_key, doc.name)
            with self.assertRaises(frappe.DuplicateEntryError):
                frappe.get_doc(values).insert()
        finally:
            frappe.db.rollback(save_point=point)
