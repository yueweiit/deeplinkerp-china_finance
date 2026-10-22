"""Site-free regression tests of the real closing services with a stateful DB double.

Run: python3 -m unittest china_finance.tests.test_closing_lifecycle
Native SQL, background workers and GL posting still require isolated bench tests.
"""
import ast
import calendar
import importlib.util
import sys
import types
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]


class Row(dict):
	__getattr__ = dict.get
	__setattr__ = dict.__setitem__

	def set(self, field, value):
		self[field] = value

	def is_new(self):
		return self.get('_new', False)

	def get_doc_before_save(self):
		return self.get('_old')

	def check_permission(self, *args):
		pass


class ClosingLifecycleTests(unittest.TestCase):
	def setUp(self):
		self.rows = []
		self.pcvs = {}
		self.issues = {}
		self.trace = []
		self.frappe = types.ModuleType('frappe')
		self.frappe._ = lambda text: text
		self.frappe.whitelist = lambda **kwargs: lambda fn: fn
		self.frappe.only_for = Mock()
		self.frappe.throw = lambda text, *a, **k: (_ for _ in ()).throw(ValueError(text))
		self.frappe.TimestampMismatchError = ValueError
		self.frappe.flags = Row()
		self.frappe.session = Row(user='finance@example.test')
		self.frappe.enqueue = Mock()
		self.frappe.db = Mock()
		self.frappe.db.sql.side_effect = self.sql
		self.frappe.db.get_value.side_effect = self.get_value
		self.frappe.db.set_value.side_effect = self.set_value
		self.frappe.get_doc = self.get_doc
		utils = types.ModuleType('frappe.utils')
		utils.getdate = lambda value: date.fromisoformat(str(value)[:10])
		month = types.ModuleType('china_finance.services.month_end')
		month.lock_company = lambda company: self.trace.append(('company', company))
		voucher = types.ModuleType('china_finance.services.voucher')
		voucher.process_cancellation_snapshot_job = Mock(return_value={'status':'resolved'})
		self.patch = patch.dict(sys.modules, {'frappe': self.frappe, 'frappe.utils': utils,
			'china_finance.services.month_end': month, 'china_finance.services.voucher': voucher})
		self.patch.start()
		self.addCleanup(self.patch.stop)
		self.lifecycle = self.load('services/closing_lifecycle.py', 'china_finance.services.closing_lifecycle')
		self.reversal = self.load('services/closing_reversal.py', 'china_finance.services.closing_reversal')

	def load(self, path, name):
		spec = importlib.util.spec_from_file_location(name, BASE / path)
		module = importlib.util.module_from_spec(spec)
		sys.modules[name] = module
		spec.loader.exec_module(module)
		return module

	def doc(self, name='run', **kwargs):
		return Row(name=name, company='C', closing_type='Monthly', from_date='2026-01-01',
			to_date='2026-01-31', closing_month='2026-01', creation='2026-01-01',
			docstatus=0, status='Draft', period_closing_voucher='P', **kwargs)

	def pcv(self, **kwargs):
		self.pcvs['P'] = Row(company='C', period_start_date='2026-01-01', period_end_date='2026-01-31',
			docstatus=1, gle_processing_status='Completed', error_message=None)
		self.pcvs['P'].update(kwargs)
		return self.pcvs['P']

	def sql(self, query, args, **kwargs):
		self.trace.append(('sql', query, args))
		if 'reversal_group=%s' in query:
			return [r for r in self.rows if r.company == args[0] and r.docstatus == 1 and (r.reversal_group == args[1] or r.name == args[2])]
		if "status IN ('Reopening','Reopen Failed')" in query:
			return [r for r in self.rows if r.company == args[0] and r.docstatus == 1 and r.status in ('Reopening', 'Reopen Failed')]
		if 'closing_type=%s' in query:
			return sorted([r for r in self.rows if (r.company, r.closing_type, r.from_date, r.to_date) == tuple(args) and r.docstatus != 2], key=lambda r: (r.creation, r.name))
		if 'to_date>%s' in query:
			return [r for r in self.rows if r.company == args[0] and r.docstatus == 1 and r.status == 'Closed' and r.to_date > args[1]]
		raise AssertionError(query)

	def get_value(self, doctype, name, fields, **kwargs):
		if doctype == 'China Voucher Sync Issue': return self.issues.get(name['issue_key'])
		self.assertEqual(doctype, 'Period Closing Voucher')
		row = self.pcvs.get(name)
		return row.get(fields) if isinstance(fields, str) and row else row

	def set_value(self, doctype, name, field, value=None, **kwargs):
		if doctype == 'Period Closing Voucher':
			self.pcvs[name].update(field if isinstance(field, dict) else {field: value})
			return
		self.assertEqual(doctype, 'China Closing Run')
		row = next(r for r in self.rows if r.name == name)
		row.update(field if isinstance(field, dict) else {field: value})

	def get_doc(self, doctype, name=None, **kwargs):
		if isinstance(doctype, dict):
			self.trace.append(('insert', doctype))
			result = Row(doctype)
			result.name = 'new-run'
			result.insert = Mock(side_effect=lambda: self.rows.append(self.doc('new-run')))
			return result
		self.trace.append(('read', name, kwargs.get('for_update', False)))
		return next(r for r in self.rows if r.name == name)

	def test_duplicate_new_draft_is_rejected_under_company_lock(self):
		self.rows = [self.doc('old')]
		with self.assertRaisesRegex(ValueError, 'old'):
			self.lifecycle.validate_period(self.doc('new', _new=True))
		self.assertEqual(self.trace[0], ('company', 'C'))

	def test_legacy_duplicate_keeps_oldest_usable_and_rejects_later(self):
		first, second = self.doc('first'), self.doc('second')
		second.creation = '2026-01-02'
		self.rows = [first, second]
		self.lifecycle.validate_period(first)
		with self.assertRaisesRegex(ValueError, 'first'):
			self.lifecycle.validate_period(second)

	def test_closed_period_blocks_even_older_draft(self):
		closed = self.doc('closed')
		closed.update(docstatus=1, status='Closed', creation='2026-01-02')
		self.rows = [closed]
		with self.assertRaisesRegex(ValueError, 'closed'):
			self.lifecycle.validate_period(self.doc())

	def test_other_company_or_closing_type_does_not_conflict(self):
		other = self.doc('other'); other.company = 'Other'
		year = self.doc('year'); year.closing_type = 'Year End'
		self.rows = [other, year]
		self.lifecycle.validate_period(self.doc(_new=True))

	def test_new_round_links_to_history_without_modifying_it(self):
		old = self.doc('history'); old.update(docstatus=1, status='Reopened')
		self.rows = [old]
		new = self.doc('new', _new=True)
		self.lifecycle.validate_period(new)
		self.assertEqual(new.reclosing_of, 'history')
		self.assertEqual(old.status, 'Reopened')

	def test_pending_or_failed_reversal_blocks_new_closing(self):
		for state in ('Reopening', 'Reopen Failed'):
			run = self.doc('pending'); run.update(docstatus=1, status=state)
			self.rows = [run]
			with self.assertRaisesRegex(ValueError, 'pending'):
				self.lifecycle.validate_period(self.doc('new', _new=True))

	def test_submit_requires_native_gl_completed_in_every_mode(self):
		for phase in (None, 'Ready'):
			for state in ('In Progress', 'Failed', None):
				self.pcv(gle_processing_status=state)
				with self.assertRaisesRegex(ValueError, '尚未完成'):
					self.lifecycle.validate_completion(self.doc(preparation_state=phase))
		self.pcv()
		self.lifecycle.validate_completion(self.doc())

	def test_wrong_company_or_period_pcv_and_missing_pcv_are_rejected(self):
		for changes in ({'company': 'Other'}, {'period_end_date': '2026-02-28'}, {'docstatus': 2}):
			self.pcv(**changes)
			with self.assertRaises(ValueError): self.lifecycle.validate_completion(self.doc())
		self.pcvs.clear()
		with self.assertRaises(ValueError): self.lifecycle.validate_completion(self.doc())

	def test_earlier_close_cannot_bypass_later_closed_period(self):
		later = self.doc('later'); later.update(to_date='2026-02-28', status='Closed', docstatus=1)
		self.rows = [later]; self.pcv()
		with self.assertRaisesRegex(ValueError, 'later'):
			self.lifecycle.validate_completion(self.doc())

	def test_insert_cannot_forge_lifecycle_and_update_cannot_change_audit(self):
		doc = self.doc(); doc.status = 'Closed'; doc.archive_package = '/forged.zip'
		self.lifecycle.protect_fields(doc)
		self.assertEqual(doc.status, 'Draft'); self.assertIsNone(doc.archive_package)
		old = self.doc(); old.update(status='Closed', docstatus=1)
		for field, value in [('status', 'Reopened'), ('archive_package', '/forged.zip'), ('reopen_reason', 'forged')]:
			changed = Row(old); changed._old = old; changed[field] = value
			with self.assertRaises(ValueError): self.lifecycle.protect_fields(changed)

	def test_reclosing_returns_existing_draft_or_closed_record(self):
		history = self.doc('history'); history.update(docstatus=1, status='Reopened')
		for state in ('Draft', 'Closed'):
			active = self.doc('active'); active.update(status=state, docstatus=int(state == 'Closed'))
			self.rows = [history, active]
			result = self.lifecycle.get_or_create_reclosing('history')
			self.assertEqual(result, {'name': 'active', 'created': False})
		self.assertFalse(any(item[0] == 'insert' for item in self.trace))

	def test_repeated_reclosing_creation_returns_same_draft(self):
		history = self.doc('history'); history.update(docstatus=1, status='Reopened')
		self.rows = [history]
		self.assertTrue(self.lifecycle.get_or_create_reclosing('history')['created'])
		self.assertEqual(self.lifecycle.get_or_create_reclosing('history'), {'name': 'new-run', 'created': False})

	def batch(self):
		run = self.doc(); run.update(docstatus=1, status='Reopening', reversal_group='run')
		self.rows = [run]; self.pcv(docstatus=2, gle_processing_status='In Progress')
		return run

	def test_reversal_only_finishes_when_every_native_gl_is_completed(self):
		run = self.batch()
		self.assertEqual(self.reversal.reversal_progress('run')['status'], 'Reopening')
		self.pcvs['P'].gle_processing_status = 'Completed'
		self.assertEqual(self.reversal.reversal_progress('run')['status'], 'Reopened')
		self.assertEqual(run.status, 'Reopened')

	def test_failed_native_gl_retains_blocking_state_and_error(self):
		run = self.batch(); self.pcvs['P'].update(gle_processing_status='Failed', error_message='native failure')
		result = self.reversal.reversal_progress('run')
		self.assertEqual(run.status, 'Reopen Failed')
		self.assertEqual(result['failed'], [{'name': 'P', 'error': 'native failure'}])
		with self.assertRaises(ValueError): self.lifecycle.validate_period(self.doc('new', _new=True))

	def test_progress_never_accepts_still_submitted_or_wrong_company_pcv(self):
		for changes in ({'docstatus': 1}, {'company': 'Other'}):
			run = self.batch(); self.pcvs['P'].update(changes, gle_processing_status='Completed')
			self.assertEqual(self.reversal.reversal_progress('run')['status'], 'Reopen Failed')
			self.assertEqual(run.status, 'Reopen Failed')

	def test_audit_sync_failure_blocks_completion_after_native_gl_finished(self):
		self.batch(); self.pcvs['P'].gle_processing_status = 'Completed'
		self.issues['Cancellation|Period Closing Voucher|P'] = Row(status='Pending', last_error='snapshot failed')
		result = self.reversal.reversal_progress('run')
		self.assertEqual(result['status'], 'Reopen Failed')
		self.assertEqual(result['failed'][0]['error'], 'snapshot failed')

	def test_batch_completion_is_atomic_across_all_affected_runs(self):
		run = self.batch()
		later = self.doc('later'); later.update(docstatus=1, status='Reopening', reversal_group='run', period_closing_voucher='Q')
		self.rows.insert(0, later)
		self.pcvs['Q'] = Row(self.pcvs['P']); self.pcvs['Q'].gle_processing_status = 'Completed'
		self.assertEqual(self.reversal.reversal_progress('later')['status'], 'Reopening')
		self.assertEqual((run.status, later.status), ('Reopening', 'Reopening'))
		self.pcvs['P'].gle_processing_status = 'Completed'
		self.reversal.reversal_progress('run')
		self.assertEqual((run.status, later.status), ('Reopened', 'Reopened'))

	def test_worker_and_duplicate_delivery_use_native_reversal_once(self):
		self.batch()
		native = types.ModuleType('erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher')
		def complete(**kwargs): self.pcvs[kwargs['voucher_no']].gle_processing_status = 'Completed'
		native.process_cancellation = Mock(side_effect=complete)
		with patch.dict(sys.modules, {native.__name__: native}):
			self.reversal.process_reversal('run'); self.reversal.process_reversal('run')
		native.process_cancellation.assert_called_once_with(voucher_type='Period Closing Voucher', voucher_no='P')
		self.assertEqual(self.rows[0].status, 'Reopened')
		self.assertTrue(self.frappe.db.commit.called)

	def test_worker_preserves_native_failure_and_can_be_retried(self):
		self.batch()
		native = types.ModuleType('erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher')
		def fail(**kwargs):
			self.frappe.db.rollback()
			self.pcvs['P'].update(gle_processing_status='Failed', error_message='native rollback')
		native.process_cancellation = Mock(side_effect=fail)
		with patch.dict(sys.modules, {native.__name__: native}):
			self.reversal.process_reversal('run')
		self.assertEqual(self.rows[0].status, 'Reopen Failed')
		self.reversal.resume_reversal('run')
		self.frappe.enqueue.assert_called_once_with('china_finance.services.closing_reversal.process_reversal', queue='long', enqueue_after_commit=True, name='run')

	def selected(self, relative, names, **scope):
		"""Execute unmodified real service functions without unrelated app imports."""
		tree = ast.parse((BASE / relative).read_text())
		tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
		for node in tree.body: node.decorator_list = []
		namespace = {"frappe": self.frappe, "_": lambda text: text, **scope}
		exec(compile(tree, str(BASE / relative), 'exec'), namespace)
		return namespace

	def test_legacy_reopening_endpoint_executes_full_reversal(self):
		complete = Mock(return_value={"reopened_runs": [Row(period_closing_voucher="P")], "status": "Reopening"})
		ns = self.selected('services/closing.py', {'reopen_closing'}, reverse_closing=complete)
		result = ns['reopen_closing']('run', 'correction')
		complete.assert_called_once_with('run', 'correction')
		self.assertEqual(result['status'], 'Reopening')
		self.assertEqual(result['period_closing_vouchers'], ['P'])

	def test_submit_acquires_company_before_run_and_checks_waited_company(self):
		run = self.doc(); self.rows = [run]
		self.frappe.db.exists.return_value = True
		self.frappe.db.get_value.side_effect = lambda *args, **kwargs: 'C'
		self.frappe.db.sql.side_effect = lambda *args, **kwargs: self.trace.append(('lock-run',))
		run.submit = Mock(side_effect=lambda: run.update(docstatus=1, status='Closed'))
		ns = self.selected('services/closing.py', {'submit_closing_run'})
		ns['submit_closing_run']('run')
		self.assertEqual(self.trace[0], ('company', 'C'))
		self.assertLess(self.trace.index(('company', 'C')), self.trace.index(('lock-run',)))
		self.assertTrue(any(item[0] == 'read' and item[2] for item in self.trace))
		run.company = 'Other'
		with self.assertRaisesRegex(ValueError, '刷新'):
			ns['submit_closing_run']('run')

	def test_deadlock_is_rethrown_without_rollback_to_invalid_savepoint(self):
		class Deadlock(Exception): pass
		class Timeout(Exception): pass
		self.frappe.QueryDeadlockError = Deadlock
		self.frappe.QueryTimeoutError = Timeout
		run = self.doc(preparation_state='Posting', preparation_data='{}')
		ns = self.selected('services/month_end.py', {'advance'}, _run=lambda name: run,
			json=__import__('json'), ACTIVE=('Posting','Checking','Closing'),
			_post_one=Mock(), _store=Mock(side_effect=Deadlock('transaction rolled back')))
		with self.assertRaises(Deadlock): ns['advance']('run')
		self.frappe.db.rollback.assert_not_called()
		self.assertIsNone(self.frappe.flags.china_month_end_run)

	def test_pending_reversal_blocks_period_writes_but_allows_own_native_cancel(self):
		self.batch()
		ns = self.selected('services/month_end.py', {'guard_period_write'}, RUN='China Closing Run',
			lock_company=lambda company: self.trace.append(('company',company)), getdate=lambda value: date.fromisoformat(str(value)))
		voucher = types.ModuleType('china_finance.services.voucher')
		voucher.GL_SOURCE_DOCTYPES = ('Journal Entry','Period Closing Voucher')
		voucher.get_posting_date = lambda doc: date.fromisoformat(doc.posting_date)
		self.frappe.db.has_column.return_value = True
		self.frappe.db.sql.side_effect = lambda *a, **k: self.rows
		doc = Row(doctype='Journal Entry', company='C', posting_date='2026-01-15')
		with patch.dict(sys.modules, {voucher.__name__:voucher}):
			with self.assertRaisesRegex(ValueError, '尚未完成'): ns['guard_period_write'](doc)
			doc.posting_date = '2026-02-15'; ns['guard_period_write'](doc)
			doc.update(doctype='Period Closing Voucher', name='P', posting_date='2026-01-31')
			self.frappe.flags.china_closing_reversal = 'run'
			ns['guard_period_write'](doc)
			doc.doctype='Journal Entry'
			with self.assertRaises(ValueError): ns['guard_period_write'](doc)

	def test_submission_never_moves_company_freeze_backwards(self):
		# Load the actual controller with only its external dependencies replaced.
		model = types.ModuleType('frappe.model.document'); model.Document = Row
		closing = types.ModuleType('china_finance.services.closing')
		closing.create_report_snapshots = Mock(); closing.run_closing_checks = Mock(return_value=[])
		utils = sys.modules['frappe.utils']
		utils.get_first_day = lambda value: utils.getdate(value).replace(day=1)
		utils.get_last_day = lambda value: utils.getdate(value).replace(day=calendar.monthrange(utils.getdate(value).year, utils.getdate(value).month)[1])
		utils.now_datetime = lambda: '2026-01-31 12:00:00'
		with patch.dict(sys.modules, {model.__name__:model, closing.__name__:closing}):
			controller = self.load('china_finance/doctype/china_closing_run/china_closing_run.py', 'closing_controller_test')
		self.frappe.get_cached_doc = Mock(return_value=Row(freeze_on_close=True))
		run = controller.ChinaClosingRun(self.doc())
		run.previous_frozen_date='2026-09-30'
		self.frappe.db.set_value = Mock()
		run.on_submit()
		args = self.frappe.db.set_value.call_args.args
		self.assertEqual(args[:3], ('Company','C','accounts_frozen_till_date'))
		self.assertEqual(str(args[3]),'2026-09-30')

	def test_reversal_snapshot_waits_for_native_gl_and_failed_audit_can_retry(self):
		self.batch()
		native = types.ModuleType('erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher')
		def complete(**kwargs): self.pcvs['P'].gle_processing_status = 'Completed'
		native.process_cancellation = Mock(side_effect=complete)
		audit = sys.modules['china_finance.services.voucher'].process_cancellation_snapshot_job
		def fail_audit(*args):
			self.assertEqual(self.pcvs['P'].gle_processing_status, 'Completed')
			self.issues['Cancellation|Period Closing Voucher|P'] = Row(status='Pending', last_error='snapshot failed')
		audit.side_effect = fail_audit
		with patch.dict(sys.modules, {native.__name__:native}): self.reversal.process_reversal('run')
		self.assertEqual(self.rows[0].status, 'Reopen Failed')
		def resolve_audit(*args): self.issues['Cancellation|Period Closing Voucher|P'].status='Resolved'
		audit.side_effect = resolve_audit
		with patch.dict(sys.modules, {native.__name__:native}): self.reversal.process_reversal('run')
		self.assertEqual(self.rows[0].status, 'Reopened')
		native.process_cancellation.assert_called_once()
		self.assertEqual(audit.call_count, 2)

	def test_native_cancellation_only_defers_gl_inside_closing_batch(self):
		native = types.ModuleType('erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher')
		class Native:
			def cancel_gl_entries(self): return 'native'
		native.PeriodClosingVoucher = Native
		native.get_previous_closed_period_in_current_year = Mock()
		utils = sys.modules['frappe.utils']
		for name in ('add_days','formatdate','get_first_day','get_last_day'): setattr(utils,name,Mock())
		with patch.dict(sys.modules, {native.__name__:native}):
			module = self.load('overrides/period_closing_voucher.py','pcv_override_test')
		voucher = module.ChinaFinancePeriodClosingVoucher()
		self.assertEqual(voucher.cancel_gl_entries(),'native')
		self.frappe.flags.china_closing_reversal='run'
		self.assertIsNone(voucher.cancel_gl_entries())

	def test_reverse_requests_queue_after_commit_and_restore_internal_flag(self):
		run = self.doc(); run.update(docstatus=1,status='Closed')
		pcv = self.pcv(); pcv.name='P'; pcv.flags=Row()
		pcv.cancel=Mock()
		self.frappe.get_doc=lambda *a,**k:pcv
		self.frappe.db.sql.side_effect=lambda *a,**k:[]
		mark = Mock()
		ns=self.selected('services/closing.py',{'reverse_closing'},
			_get_closing_run_for_reverse=lambda *a,**k:run, _closing_runs_to_reverse=lambda *a,**k:[run],
			_period_closing_vouchers_to_cancel=lambda *a,**k:[pcv],_reverse_closing_runs=mark,
			_reopening_freeze_date=lambda doc:None)
		result=ns['reverse_closing']('run','correction')
		self.assertEqual(result['status'],'Reopening')
		pcv.cancel.assert_called_once()
		self.assertTrue(self.frappe.enqueue.call_args.kwargs['enqueue_after_commit'])
		self.assertIsNone(self.frappe.flags.china_closing_reversal)
		self.frappe.enqueue.reset_mock(); pcv.cancel.side_effect=ValueError('native failure')
		with self.assertRaisesRegex(ValueError,'native failure'): ns['reverse_closing']('run','correction')
		self.assertIsNone(self.frappe.flags.china_closing_reversal)
		self.frappe.enqueue.assert_not_called()

	def test_freeze_preview_and_reversal_preserve_unaffected_periods(self):
		self.frappe.db.sql.side_effect=lambda *a,**k:[['2025-12-31']]
		ns=self.selected('services/closing.py',{'_reopening_freeze_date'},getdate=lambda value:date.fromisoformat(str(value)))
		self.assertEqual(str(ns['_reopening_freeze_date'](self.doc(previous_frozen_date='2025-10-31'))),'2025-12-31')
		self.assertEqual(str(ns['_reopening_freeze_date'](self.doc(previous_frozen_date='2026-03-31'))),'2026-03-31')

	def test_existing_pcv_is_reused_before_next_period_continuity_check(self):
		preparation = types.ModuleType('china_finance.services.voucher_preparation')
		preparation.check_company = Mock()
		self.pcv(name='P',closing_account_head='Profit')
		self.frappe.get_cached_doc = Mock(return_value=Row(profit_loss_account='Profit'))
		ns=self.selected('services/closing.py',{'create_period_closing_voucher'},
			getdate=lambda value:date.fromisoformat(str(value)),
			get_first_day=lambda value:value.replace(day=1), get_last_day=lambda value:value.replace(day=31),
			get_fiscal_year=lambda *a,**k:('2026','2026-01-01','2026-12-31'))
		self.frappe.db.get_value.side_effect=lambda doctype, name, fields, **kwargs:self.pcvs['P']
		with patch.dict(sys.modules,{preparation.__name__:preparation}):
			result=ns['create_period_closing_voucher']('C','2026-01-01','2026-01-31')
		self.assertFalse(result['created']); self.assertEqual(result['name'],'P')
		preparation.check_company.assert_called_once_with('C')

	def _two_month_reversal(self):
		self.batch()
		feb = self.doc('feb')
		feb.update(docstatus=1, status='Reopening', reversal_group='run', period_closing_voucher='Q')
		self.rows.insert(0, feb)
		self.pcvs['Q'] = Row(self.pcvs['P'])
		return types.ModuleType('erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher')

	def test_multi_month_retry_reaches_failed_earlier_month(self):
		native = self._two_month_reversal()
		def first_attempt(**kwargs):
			self.pcvs[kwargs['voucher_no']].gle_processing_status = 'Failed' if kwargs['voucher_no'] == 'P' else 'Completed'
		native.process_cancellation = Mock(side_effect=first_attempt)
		with patch.dict(sys.modules, {native.__name__: native}): self.reversal.process_reversal('run')
		self.assertEqual([c.kwargs['voucher_no'] for c in native.process_cancellation.call_args_list], ['Q', 'P'])
		self.assertEqual(self.rows[0].status, 'Reopen Failed')
		native.process_cancellation.reset_mock()
		def success(**kwargs): self.pcvs[kwargs['voucher_no']].gle_processing_status = 'Completed'
		native.process_cancellation.side_effect = success
		with patch.dict(sys.modules, {native.__name__: native}): self.reversal.process_reversal('run')
		native.process_cancellation.assert_called_once_with(voucher_type='Period Closing Voucher', voucher_no='P')
		self.assertEqual([r.status for r in self.rows], ['Reopened', 'Reopened'])

	def test_retry_does_not_skip_failed_earlier_audit_or_repeat_completed_gl(self):
		native = self._two_month_reversal()
		for pcv in self.pcvs.values(): pcv.gle_processing_status = 'Completed'
		self.issues['Cancellation|Period Closing Voucher|P'] = Row(status='Pending', last_error='old failure')
		audit = sys.modules['china_finance.services.voucher'].process_cancellation_snapshot_job
		def sync(doctype, name):
			if name == 'P': self.issues['Cancellation|Period Closing Voucher|P'].status = 'Resolved'
			return {'status': 'resolved'}
		audit.side_effect = sync
		native.process_cancellation = Mock()
		with patch.dict(sys.modules, {native.__name__: native}): self.reversal.process_reversal('run')
		self.assertEqual([c.args[1] for c in audit.call_args_list], ['Q', 'P'])
		native.process_cancellation.assert_not_called()
		self.assertEqual([r.status for r in self.rows], ['Reopened', 'Reopened'])

	def test_current_month_failure_still_stops_before_older_month(self):
		native = self._two_month_reversal()
		def fail(**kwargs): self.pcvs[kwargs['voucher_no']].gle_processing_status = 'Failed'
		native.process_cancellation = Mock(side_effect=fail)
		with patch.dict(sys.modules, {native.__name__: native}): self.reversal.process_reversal('run')
		native.process_cancellation.assert_called_once_with(voucher_type='Period Closing Voucher', voucher_no='Q')
		self.assertEqual(self.pcvs['P'].gle_processing_status, 'In Progress')

	def test_worker_sets_only_scoped_reversal_context_and_always_restores_it(self):
		self.batch()
		self.frappe.flags.china_closing_reversal = 'previous-context'
		native = types.ModuleType('erpnext.accounts.doctype.period_closing_voucher.period_closing_voucher')
		def crash(**kwargs):
			self.assertEqual(self.frappe.flags.china_closing_reversal, 'run')
			raise ValueError('unexpected worker failure')
		native.process_cancellation = Mock(side_effect=crash)
		with patch.dict(sys.modules, {native.__name__: native}):
			with self.assertRaisesRegex(ValueError, 'unexpected worker failure'): self.reversal.process_reversal('run')
		self.assertEqual(self.frappe.flags.china_closing_reversal, 'previous-context')

	def _guard_harness(self):
		ns = self.selected('services/month_end.py', {'guard_period_write'}, RUN='China Closing Run',
			lock_company=lambda company: self.trace.append(('company', company)), getdate=lambda value: date.fromisoformat(str(value)))
		voucher = sys.modules['china_finance.services.voucher']
		voucher.GL_SOURCE_DOCTYPES = ('Journal Entry', 'Period Closing Voucher')
		voucher.get_posting_date = lambda doc: date.fromisoformat(doc.posting_date)
		self.frappe.db.has_column.return_value = True
		self.frappe.db.exists.return_value = False
		self.frappe.db.sql.side_effect = lambda query, args, **kwargs: [r for r in self.rows if r.company == args]
		return ns['guard_period_write']

	def test_reversal_guards_omitted_native_sources_and_any_gl_entry(self):
		run = self.batch(); guard = self._guard_harness()
		for status in ('Reopening', 'Reopen Failed'):
			run.status = status
			for doctype in ('Stock Reconciliation', 'Invoice Discounting', 'Landed Cost Voucher', 'GL Entry'):
				doc = Row(doctype=doctype, name='native', company='C', posting_date='2026-01-15', voucher_type='Future GL Source', voucher_no='future')
				with self.assertRaisesRegex(ValueError, '尚未完成'): guard(doc)
				doc.posting_date = '2026-02-15'; guard(doc)
				doc.update(company='Other', posting_date='2026-01-15'); guard(doc)

	def test_reversal_context_cannot_bypass_other_vouchers_or_batches(self):
		self.batch(); guard = self._guard_harness()
		self.frappe.flags.china_closing_reversal = 'run'
		doc = Row(doctype='GL Entry', company='C', posting_date='2026-01-15', voucher_type='Period Closing Voucher', voucher_no='P')
		guard(doc)
		doc.voucher_no = 'Unrelated PCV'
		with self.assertRaises(ValueError): guard(doc)
		doc.update(voucher_type='Journal Entry', voucher_no='P')
		with self.assertRaises(ValueError): guard(doc)
		doc.update(voucher_type='Period Closing Voucher', voucher_no='P')
		self.frappe.flags.china_closing_reversal = 'wrong-group'
		with self.assertRaises(ValueError): guard(doc)
		self.frappe.flags.china_closing_reversal = 'run'
		other = self.doc('other'); other.update(docstatus=1, status='Reopening', reversal_group='other')
		self.rows.append(other)
		with self.assertRaises(ValueError): guard(doc)

	def test_gl_guard_preserves_month_end_posting_context(self):
		run = self.doc(); run.preparation_state = 'Posting'; self.rows = [run]
		guard = self._guard_harness()
		doc = Row(doctype='GL Entry', company='C', posting_date='2026-01-15', voucher_type='Journal Entry', voucher_no='journal')
		with self.assertRaises(ValueError): guard(doc)
		self.frappe.flags.china_month_end_run = 'run'; guard(doc)

	def test_legacy_draft_with_progress_is_preferred_and_reused(self):
		old, active = self.doc('old'), self.doc('active')
		active.update(creation='2026-01-02', preparation_state='Posting')
		self.rows = [old, active]
		self.lifecycle.validate_period(active)
		with self.assertRaisesRegex(ValueError, 'active'): self.lifecycle.validate_period(old)
		with self.assertRaises(ValueError): self.lifecycle.validate_period(self.doc('new', _new=True))
		history = self.doc('history'); history.update(status='Reopened', docstatus=1)
		self.rows.append(history)
		self.assertEqual(self.lifecycle.get_or_create_reclosing('history')['name'], 'active')

	def _run_harness(self):
		original_sql = self.sql
		def query(text, args, **kwargs):
			if 'name!=%s' in text:
				return [(r.name,) for r in self.rows if r.company == args[0] and r.name != args[1] and r.preparation_state in ('Posting','Checking','Closing')]
			return original_sql(text, args, **kwargs)
		self.frappe.db.sql.side_effect = query
		def store(doc, state, data, message):
			doc.preparation_state = state
			return data
		getdate = lambda value: date.fromisoformat(str(value))
		return self.selected('services/month_end.py', {'_run', 'pause'}, RUN='China Closing Run', ROLES=('China Finance Manager',),
			preparation=Row(check_company=Mock(), mode_enabled=lambda *a: True), lock_company=lambda *a: None,
			getdate=getdate, get_first_day=lambda value: getdate(value).replace(day=1),
			get_last_day=lambda value: getdate(value).replace(day=calendar.monthrange(getdate(value).year, getdate(value).month)[1]),
			json=__import__('json'), _store=store)

	def test_multiple_active_legacy_duplicates_can_be_paused_then_resumed(self):
		first, second = self.doc('first'), self.doc('second')
		first.update(preparation_state='Posting', preparation_data='{}')
		second.update(preparation_state='Closing', creation='2026-01-02', preparation_data='{"done":[{"name":"posted-source"}]}')
		self.rows = [first, second]; ns = self._run_harness()
		with self.assertRaises(ValueError): ns['_run']('first')
		with self.assertRaises(ValueError): ns['_run']('second')
		data = ns['pause']('second')
		self.assertEqual(data['done'], [{'name':'posted-source'}])
		self.assertEqual(second.preparation_state, 'Paused')
		self.assertEqual(ns['_run']('first').name, 'first')

	def test_pausing_still_requires_draft_and_document_permission(self):
		run = self.doc(preparation_state='Posting'); self.rows = [run]; ns = self._run_harness()
		run.docstatus = 1
		with self.assertRaises(ValueError): ns['pause']('run')
		run.docstatus = 0
		with patch.object(Row, 'check_permission', side_effect=ValueError('no write permission')):
			with self.assertRaisesRegex(ValueError, 'no write permission'): ns['pause']('run')

	def test_immutable_reversal_today_still_guards_original_gl_period(self):
		self.batch(); guard = self._guard_harness()
		doc = Row(doctype='GL Entry', company='C', posting_date='2026-10-22', voucher_type='Future GL Source', voucher_no='future')
		guard(doc)
		self.frappe.db.exists.return_value = True
		with self.assertRaisesRegex(ValueError, '尚未完成'): guard(doc)
		self.frappe.db.exists.assert_called_with('GL Entry', {
			'company':'C', 'voucher_type':'Future GL Source', 'voucher_no':'future',
			'posting_date':['between',['2026-01-01','2026-01-31']],
		})


if __name__ == '__main__': unittest.main()
