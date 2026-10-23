"""Site-free permission, repair and missing-snapshot visibility contracts."""
import importlib.util
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch


class Row(dict):
	__getattr__ = dict.get


class PostingStatusTests(unittest.TestCase):
	def setUp(self):
		self.frappe = types.ModuleType('frappe')
		self.frappe._ = lambda text: text
		self.frappe._dict = Row
		self.frappe.whitelist = lambda **kwargs: lambda fn: fn
		self.frappe.get_roles = Mock(return_value=['China Finance Manager'])
		self.frappe.has_permission = Mock(return_value=True)
		self.frappe.only_for = Mock()
		self.frappe.throw = Mock(side_effect=lambda text, *args: (_ for _ in ()).throw(ValueError(text)))
		self.frappe.get_cached_value = Mock(return_value='CNY')
		self.frappe.get_list = Mock(return_value=[])
		self.frappe.get_all = Mock(return_value=[])
		self.frappe.db = types.SimpleNamespace(exists=Mock(return_value=True),get_value=Mock(return_value=None))
		self.doc = types.SimpleNamespace(doctype='Journal Entry', name='JE1', company='A', docstatus=1, posting_date='2026-07-17', add_comment=Mock(), submit=Mock())
		self.company = types.SimpleNamespace(check_permission=Mock())
		self.frappe.get_doc = Mock(return_value=self.company)
		self.voucher = types.ModuleType('china_finance.services.voucher')
		self.voucher.SNAPSHOT_RETRY_ROLES = ('System Manager','China Finance Manager')
		self.voucher._get_source_document = Mock(return_value=self.doc)
		self.voucher.get_source_snapshot_status = Mock(return_value={'snapshot_ready':False})
		self.voucher.get_company_settings = Mock(return_value=types.SimpleNamespace(activation_date='2026-01-01'))
		self.voucher.get_posting_date = lambda doc: doc.posting_date
		self.voucher.create_voucher_from_source = Mock(return_value='CAV1')
		utils = types.ModuleType('frappe.utils')
		utils.getdate = lambda value: date.fromisoformat(str(value))
		self.edit = types.ModuleType('china_finance.services.source_voucher_edit')
		self.edit._get_locked_source_document = Mock(return_value=self.doc)
		self.edit._get_closing_blockers = Mock(return_value=[])
		prep = types.ModuleType('china_finance.services.voucher_preparation')
		prep.mode_enabled = Mock(return_value=True)
		prep.is_ready = Mock(return_value=True)
		self.prep = prep
		self.lifecycle = types.ModuleType('china_finance.services.closing_lifecycle')
		self.lifecycle.ensure_no_reversal = Mock()
		from china_finance import services
		modules = {'frappe':self.frappe,'frappe.utils':utils,'china_finance.services.voucher':self.voucher,
				   'china_finance.services.source_voucher_edit':self.edit,'china_finance.services.voucher_preparation':prep,
				   'china_finance.services.closing_lifecycle':self.lifecycle}
		self.patch = patch.dict(sys.modules, modules)
		self.patch.start(); self.addCleanup(self.patch.stop)
		self.parent = patch.object(services, 'voucher', self.voucher, create=True)
		self.parent.start(); self.addCleanup(self.parent.stop)
		spec = importlib.util.spec_from_file_location('posting_feedback_test',Path(__file__).resolve().parents[1]/'services/voucher_posting_status.py')
		self.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)

	def filters(self, **changes):
		return Row(company='A',from_date='2026-07-01',to_date='2026-07-31',**changes)

	def gl(self, **changes):
		return Row(dict(name='GL1',voucher_no='JE1',posting_date='2026-07-17',account='Bank',account_currency='CNY',
						party_type=None,party=None,cost_center=None,project=None,finance_book=None,remarks='Capital',debit=50,credit=0),**changes)

	def test_state_distinguishes_source_posting_snapshot_and_repair_permissions(self):
		state=self.module.get_posting_state('Journal Entry','JE1')
		self.assertTrue(state['posted']); self.assertTrue(state['has_gl']); self.assertTrue(state['can_repair'])
		self.company.check_permission.assert_called_with('read')
		self.frappe.get_roles.return_value=['Accounts User']
		self.assertFalse(self.module.get_posting_state('Journal Entry','JE1')['can_repair'])
		self.frappe.db.exists.return_value=False
		self.assertIn('总账',self.module.get_posting_state('Journal Entry','JE1')['reason'])

	def test_disabled_or_pre_activation_never_offers_automatic_repair(self):
		self.voucher.get_company_settings.return_value=None
		self.assertFalse(self.module.get_posting_state('Journal Entry','JE1')['can_repair'])
		self.voucher.get_company_settings.return_value=types.SimpleNamespace(activation_date='2026-08-01')
		state=self.module.get_posting_state('Journal Entry','JE1')
		self.assertFalse(state['can_repair']); self.assertIn('启用日期',state['reason'])

	def test_read_failure_propagates_without_enumerating_gl(self):
		self.voucher._get_source_document.side_effect=PermissionError('Forbidden')
		with self.assertRaises(PermissionError):self.module.get_posting_state('Journal Entry','JE1')
		self.frappe.db.exists.assert_not_called()

	def test_draft_reports_current_review_readiness_and_no_posted_gl(self):
		self.doc.docstatus=0
		state=self.module.get_posting_state('Journal Entry','JE1')
		self.assertTrue(state['preparation_enabled']); self.assertTrue(state['ready'])
		self.assertFalse(state['posted']); self.assertFalse(state['has_gl']); self.assertFalse(state['can_repair'])

	def test_repair_reuses_company_first_source_lock_and_never_submits_source(self):
		with patch.object(self.module,'get_posting_state',side_effect=[{'can_repair':True},{'snapshot_ready':True}]):
			result=self.module.repair_posting_snapshot('Journal Entry','JE1')
		self.assertTrue(result['snapshot_ready'])
		self.edit._get_locked_source_document.assert_called_once_with('Journal Entry','JE1')
		self.voucher.create_voucher_from_source.assert_called_once_with(self.doc)
		self.doc.submit.assert_not_called(); self.doc.add_comment.assert_called_once()

	def test_repair_is_idempotent_and_rechecks_role_before_source_access(self):
		with patch.object(self.module,'get_posting_state',return_value={'snapshot_ready':True}):
			self.module.repair_posting_snapshot('Journal Entry','JE1')
		self.voucher.create_voucher_from_source.assert_not_called(); self.doc.add_comment.assert_not_called()
		self.frappe.only_for.side_effect=PermissionError('No manager role')
		self.edit._get_locked_source_document.reset_mock()
		with self.assertRaises(PermissionError):self.module.repair_posting_snapshot('Journal Entry','JE1')
		self.edit._get_locked_source_document.assert_not_called()

	def test_repair_rejects_canceled_unposted_or_closed_period_and_invalid_snapshot(self):
		with patch.object(self.module,'get_posting_state',return_value={'can_repair':False}):
			with self.assertRaises(ValueError):self.module.repair_posting_snapshot('Journal Entry','JE1')
		self.edit._get_closing_blockers.return_value=['Closed']
		with patch.object(self.module,'get_posting_state',return_value={'can_repair':True}):
			with self.assertRaisesRegex(ValueError,'Closed'):self.module.repair_posting_snapshot('Journal Entry','JE1')
		self.voucher.create_voucher_from_source.assert_not_called()
		self.edit._get_closing_blockers.return_value=[]
		with patch.object(self.module,'get_posting_state',side_effect=[{'can_repair':True},{'snapshot_ready':False}]):
			with self.assertRaises(ValueError):self.module.repair_posting_snapshot('Journal Entry','JE1')
		self.doc.add_comment.assert_not_called()

	def test_missing_snapshot_uses_readable_sources_and_actual_gl_without_inventing_number(self):
		self.frappe.get_list.side_effect=[['JE1','JE2'],[self.gl(),self.gl(name='GL2',credit=50,debit=0)]]
		self.frappe.get_all.return_value=['JE2']
		rows=self.module.get_pending_ledger_rows(self.filters(source_doctype='Journal Entry',voucher_status='已记账'))
		self.assertEqual(len(rows),2); self.assertTrue(all(row.snapshot_pending and row.voucher_status==1 for row in rows))
		self.assertFalse(rows[0].editable_account); self.assertFalse(rows[0].editable_amount)
		self.assertEqual(rows[0].statutory_number,'待补齐 · JE1')
		query=self.frappe.get_list.call_args_list[0].kwargs['filters']
		self.assertEqual(query['company'],'A'); self.assertEqual(query['docstatus'],1)
		query=self.frappe.get_list.call_args_list[1].kwargs['filters']
		self.assertEqual(query['voucher_no'],['in',['JE1']]); self.assertEqual(query['is_cancelled'],0)
		self.assertEqual([row.entry_idx for row in rows],[1,2])

	def test_existing_snapshot_cannot_be_replaced_to_bypass_snapshot_permissions(self):
		self.frappe.get_list.return_value=['JE1']; self.frappe.get_all.return_value=['JE1']
		self.assertEqual(self.module.get_pending_ledger_rows(self.filters(source_doctype='Journal Entry')),[])
		self.assertEqual(self.frappe.get_list.call_count,1)
		self.frappe.has_permission.side_effect=lambda dt,*args,**kwargs:dt!='China Accounting Voucher'
		self.frappe.get_list.reset_mock()
		self.assertEqual(self.module.get_pending_ledger_rows(self.filters()),[])
		self.frappe.get_list.assert_not_called()

	def test_pending_rows_obey_draft_reversal_word_period_search_and_source_filters(self):
		for changes in ({'voucher_status':'未记账'},{'voucher_status':'待记账'},{'voucher_status':'已冲销'},
						{'voucher_word':'记1'},{'source_event':'Cancellation'}, {'source_name':'JE1','voucher_number':'JE2'}):
			self.assertEqual(self.module.get_pending_ledger_rows(self.filters(**changes)),[])
		for changes in ({'accounting_period':'2026-06'},{'search_text':'not found'}):
			self.frappe.get_list.side_effect=[['JE1'],[self.gl()]]
			self.assertEqual(self.module.get_pending_ledger_rows(self.filters(source_doctype='Journal Entry',**changes)),[])
		self.frappe.get_list.side_effect=[['JE1'],[self.gl()]]
		result=self.module.get_pending_ledger_rows(self.filters(source_doctype='Journal Entry',account='Bank',party_type='Customer',party='C1'))
		self.assertEqual(len(result),1)
		self.assertEqual(self.frappe.get_list.call_args.kwargs['filters']['party'],'C1')

	def test_unsupported_sources_cannot_use_repair(self):
		with self.assertRaises(ValueError):self.module.repair_posting_snapshot('Sales Invoice','SI1')
		self.edit._get_locked_source_document.assert_not_called()

	def test_running_or_reversing_closing_blocks_snapshot_writes(self):
		with patch.object(self.module,'get_posting_state',return_value={'can_repair':True}):
			self.frappe.db.get_value.return_value='RUN1'
			with self.assertRaises(ValueError):self.module.repair_posting_snapshot('Journal Entry','JE1')
			self.frappe.db.get_value.return_value=None
			self.lifecycle.ensure_no_reversal.side_effect=ValueError('Reopening')
			with self.assertRaisesRegex(ValueError,'Reopening'):self.module.repair_posting_snapshot('Journal Entry','JE1')
		self.voucher.create_voucher_from_source.assert_not_called()
