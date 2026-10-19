"""Verify completion visibility with real realtime transaction callbacks and no database."""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
import frappe.realtime
from frappe.core.doctype.prepared_report import prepared_report as prepared
from frappe.utils import CallbackManager


class PreparedReportNotificationTests(unittest.TestCase):
	def test_completion_is_published_only_after_commit(self):
		self.check_notification(commit=True)

	def test_rollback_does_not_publish_completion(self):
		self.check_notification(commit=False)

	def check_notification(self, commit):
		callbacks = SimpleNamespace(after_commit=CallbackManager(), after_rollback=CallbackManager())
		instance = Mock(report_name="Example", filters="{}", owner="Administrator")
		instance.name = "test-report"
		report = SimpleNamespace(report_type="Script Report", name="Example")
		notification = Mock()
		pending, visible, emitted = [], [], []

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict):
				return notification
			return instance if doctype == "Prepared Report" else report

		def consume(event, message, room):
			self.assertEqual(visible, [{"result": [[1]]}])
			emitted.append((event, message, room))

		with ExitStack() as stack:
			for owner, name, value in (
				(frappe, "db", callbacks),
				(frappe, "local", SimpleNamespace()),
				(frappe, "session", SimpleNamespace(user="Administrator")),
				(frappe.utils, "now", lambda: "2026-10-19 12:00:00"),
				(frappe, "get_doc", get_doc),
				(prepared, "update_job_id", Mock()),
				(prepared, "add_data_to_monitor", Mock()),
				(prepared, "generate_report_result", Mock(return_value={"result": [[1]]})),
				(prepared, "create_json_gz_file", lambda *args: pending.append(args[0])),
				(frappe.realtime, "emit_via_redis", consume),
			):
				stack.enter_context(patch.object(owner, name, value))

			prepared.generate_report("test-report")
			self.assertEqual(pending, [{"result": [[1]]}])
			self.assertEqual(emitted, [])
			if commit:
				visible.extend(pending)
				callbacks.after_commit.run()
				self.assertEqual(emitted, [(
					"report_generated",
					{"report_name": "Example", "name": "test-report"},
					"user:Administrator",
				)])
			else:
				callbacks.after_rollback.run()
				callbacks.after_commit.run()
				self.assertEqual(emitted, [])
