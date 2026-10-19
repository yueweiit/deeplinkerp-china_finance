"""Committed, two-connection cash-flow regressions restricted to the isolated QA site."""

import json
import select
import subprocess
import sys
import unittest

import frappe

from china_finance.tests import test_cash_flow_assignment_list as fixtures


WORKER = r'''
import json, sys, time
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import frappe
from china_finance.services import cash_flow_assignment as service
frappe.init(site="cash-flow-list-qa.localhost", sites_path="/home/frappe/frappe-bench/sites")
frappe.connect()
frappe.set_user("Administrator")
frappe.local.lang = "zh"
mode, payload = sys.argv[2], json.loads(sys.argv[3])
try:
    if mode == "late-assignment":
        frappe.db.get_value("Company", payload["company"], "name")
    elif mode != "batch":
        doc = frappe.get_doc("China Cash Flow Assignment", payload["name"])
        # Establish an older REPEATABLE READ snapshot before waiting on the source.
        source_status = frappe.db.get_value(doc.source_doctype, doc.source_name, "docstatus")
    print(json.dumps({"ready": True}), flush=True)
    sys.stdin.readline()
    original = frappe.get_doc
    first = [True]
    def delayed_get_doc(*args, **kwargs):
        loaded = original(*args, **kwargs)
        if args and args[0] == "China Cash Flow Assignment" and kwargs.get("for_update") and first[0]:
            first[0] = False
            time.sleep(0.3)
        return loaded
    retries = []
    retry_details = []
    retry_codes = []
    retry_queries = []
    original_sql = frappe.db.sql
    original_get_value = frappe.db.get_value
    def guarded_get_value(*args, **kwargs):
        if mode == "late-assignment" and args and args[0] == "China Cash Flow Assignment" and kwargs.get("for_update"):
            raise AssertionError("Assignment lock reached without a visible source identity")
        return original_get_value(*args, **kwargs)
    failing_sql = [None]
    def traced_sql(query, *args, **kwargs):
        try:
            return original_sql(query, *args, **kwargs)
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            failing_sql[0] = str(query)
            raise
    for attempt in range(3):
        try:
            if attempt and mode not in ("batch", "late-assignment"):
                doc = original("China Cash Flow Assignment", payload["name"])
            with patch("frappe.get_doc", side_effect=delayed_get_doc), patch.object(frappe.db, "sql", side_effect=traced_sql), patch.object(frappe.db, "get_value", side_effect=guarded_get_value):
                if mode == "batch":
                    result = service.process_cash_flow_assignments(payload, "save")
                elif mode == "late-assignment":
                    result = service.process_cash_flow_assignments([payload], "save")
                elif mode == "list":
                    result = service.process_cash_flow_assignments([payload], "confirm")
                elif mode == "native":
                    doc.submit()
                    result = {"status": doc.status}
                elif mode == "save":
                    doc.remarks = "older native snapshot"
                    doc.save()
                    result = {"status": doc.status}
                elif mode == "reload":
                    result = service.reload_cash_flow_assignment_suggestions(doc.name)
                elif mode == "recreate":
                    result = service.recreate_cash_flow_assignment(doc.name)
                elif mode == "cancel":
                    result = service.cancel_cash_flow_assignment(doc.name, "concurrent cancellation")
                elif mode == "public-source":
                    from china_finance.services import voucher
                    with patch.object(voucher, "create_voucher_from_source", side_effect=AssertionError("Audit creation reached before the source gate")):
                        result = voucher.create_cash_flow_assignment_from_source(doc.source_doctype, doc.source_name)
            frappe.db.commit()
            print(json.dumps({"result": result, "retries": retries, "retry_details": retry_details, "retry_codes": retry_codes, "retry_queries": retry_queries}, default=str), flush=True)
            break
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as exc:
            # This subprocess owns the whole transaction. Services must preserve
            # retryable errors; only this boundary can safely roll back/retry.
            frappe.db.rollback()
            retries.append(type(exc).__name__)
            retry_details.append(str(exc))
            cause_args = getattr(exc.__cause__, "args", ())
            retry_codes.append(cause_args[0] if cause_args else None)
            retry_queries.append(failing_sql[0])
            if attempt == 2:
                raise
except Exception as exc:
    frappe.db.rollback()
    print(json.dumps({"error": type(exc).__name__, "message": str(exc), "retries": locals().get("retries", []), "retry_details": locals().get("retry_details", []), "retry_codes": locals().get("retry_codes", []), "retry_queries": locals().get("retry_queries", [])}), flush=True)
finally:
    frappe.destroy()
'''


class TestCashFlowAssignmentConcurrency(unittest.TestCase):
	insert = fixtures.TestCashFlowAssignmentList.insert
	assignment = fixtures.TestCashFlowAssignmentList.assignment
	payload = fixtures.TestCashFlowAssignmentList.payload

	def setUp(self):
		if frappe.local.site != "cash-flow-list-qa.localhost":
			self.skipTest("Committed concurrency fixtures are restricted to cash-flow-list-qa.localhost")
		fixtures.TestCashFlowAssignmentList.setUp(self)
		self._cleanups = [cleanup for cleanup in self._cleanups if cleanup[0] != frappe.db.rollback]
		self.addCleanup(self.cleanup_fixtures)
		self.workers = []

	def cleanup_fixtures(self):
		for worker in self.workers:
			if worker.poll() is None:
				worker.terminate()
				worker.wait(timeout=5)
		frappe.db.rollback()
		names = frappe.get_all("China Cash Flow Assignment", filters={"company": self.company}, pluck="name")
		if names:
			frappe.db.delete("China Cash Flow Assignment Item", {"parent": ["in", names]})
			frappe.db.delete("Version", {"docname": ["in", names]})
			frappe.db.delete("Comment", {"reference_name": ["in", names]})
			frappe.db.delete("China Cash Flow Assignment", {"name": ["in", names]})
		pattern = self.prefix + "%"
		for doctype in ("China Accounting Voucher Entry", "China Financial Statement Row"):
			frappe.db.delete(doctype, {"parent": ["like", pattern]})
		for doctype in ("China Voucher Sync Issue", "China Accounting Voucher", "GL Entry", "Payment Entry", "China Financial Statement Mapping",
			"China Cash Equivalent Scope", "China Financial Statement Template", "China Finance Settings", "Account", "Company"):
			frappe.db.delete(doctype, {"name": ["like", pattern]})
		frappe.db.commit()

	def worker(self, mode, payload):
		worker = subprocess.Popen([sys.executable, "-c", WORKER, sys.path[0], mode, json.dumps(payload)],
			stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
		self.workers.append(worker)
		self.assertTrue(select.select([worker.stdout], [], [], 15)[0], "Worker did not become ready")
		self.assertEqual(json.loads(worker.stdout.readline()), {"ready": True})
		return worker

	def release(self, worker):
		worker.stdin.write("continue\n")
		worker.stdin.flush()

	def result(self, worker):
		output, errors = worker.communicate(timeout=20)
		self.assertEqual(worker.returncode, 0, errors)
		response = json.loads(output.strip().splitlines()[-1])
		self.assertLessEqual(len(response["retries"]), 2, response)
		self.assertTrue(all(error in ("QueryDeadlockError", "QueryTimeoutError") for error in response["retries"]), response)
		return response

	def test_reversed_batches_finish_without_deadlock_and_keep_input_order(self):
		first, second = self.assignment("first"), self.assignment("second")
		payloads = [self.payload(first), self.payload(second)]
		frappe.db.commit()
		workers = [self.worker("batch", payloads), self.worker("batch", payloads[::-1])]
		for worker in workers:
			self.release(worker)
		responses = [self.result(worker) for worker in workers]
		print("Reverse batch retry evidence: " + json.dumps([
			{"codes": response["retry_codes"], "queries": response["retry_queries"]} for response in responses
		]))
		for response, expected in zip(responses, (payloads, payloads[::-1]), strict=True):
			self.assertNotIn("error", response, response)
			# Frappe classifies CHECKREAD 1020 as QueryDeadlockError as well.
			# A whole-transaction owner may retry that stale-read conflict, but
			# native 1213 lock cycles, 1205 timeouts and unknown errors must fail.
			self.assertEqual(len(response["retry_codes"]), len(response["retries"]), response)
			self.assertTrue(all(code == 1020 for code in response["retry_codes"]), response)
			self.assertEqual([row["name"] for row in response["result"]["results"]], [row["name"] for row in expected])
		self.assertEqual(sum(row["ok"] for response in responses for row in response["result"]["results"]), 2)
		for response in responses:
			for row in response["result"]["results"]:
				if not row["ok"]:
					self.assertEqual(row["error_type"], "TimestampMismatchError", row)

	def test_old_snapshot_waits_for_source_then_rejects_cancelled_evidence_across_write_apis(self):
		for mode in ("list", "native", "save", "reload", "recreate", "public-source"):
			with self.subTest(mode=mode):
				doc = self.assignment(mode)
				if mode == "recreate":
					frappe.db.set_value(doc.doctype, doc.name, "status", "Cancelled")
					doc.reload()
				payload = self.payload(doc)
				frappe.db.commit()
				worker = self.worker(mode, payload)
				frappe.db.set_value("Payment Entry", doc.source_name, "docstatus", 2)
				frappe.db.set_value("GL Entry", {"voucher_type": "Payment Entry", "voucher_no": doc.source_name}, "is_cancelled", 1)
				self.release(worker)
				try:
					blocked = not select.select([worker.stdout], [], [], 0.6)[0]
				finally:
					frappe.db.commit()
				response = self.result(worker)
				self.assertTrue(blocked, "Write must wait for the source transaction before locking the assignment")
				if mode == "list":
					self.assertFalse(response["result"]["results"][0]["ok"], response)
				else:
					self.assertIn(response.get("error"), ("CancelledLinkError", "ValidationError"), response)
				if mode == "public-source":
					self.assertIn("仅已提交", response.get("message", ""), response)
					self.assertFalse(frappe.db.exists("China Accounting Voucher", {"source_key": "Posting|Payment Entry|" + doc.source_name}))
				self.assertEqual(frappe.db.get_value(doc.doctype, doc.name, "docstatus"), 0)

	def test_same_assignment_confirm_and_native_cancel_serialize_without_engine_failure(self):
		doc = self.assignment()
		payload = self.payload(doc)
		frappe.db.commit()
		workers = [self.worker("list", payload), self.worker("cancel", payload)]
		for worker in workers:
			self.release(worker)
		responses = [self.result(worker) for worker in workers]
		for response in responses:
			if "error" in response:
				self.assertIn(response["error"], ("TimestampMismatchError", "ValidationError"), response)
		frappe.db.rollback()
		state = frappe.db.get_value(doc.doctype, doc.name, ["status", "docstatus"])
		self.assertIn(state, (("Confirmed", 1), ("Cancelled", 0), ("Cancelled", 2)))
		self.assertEqual(frappe.db.count("GL Entry", {"voucher_type": "Payment Entry", "voucher_no": doc.source_name, "is_cancelled": 0}), 2)

	def test_public_source_reuses_voucher_committed_after_its_old_snapshot(self):
		doc = self.assignment("late-voucher")
		self.insert("China Financial Statement Mapping", "late-voucher-mapping", company=self.company,
			template=self.template, account=self.other, row_code="CASH_RECEIVED_SALES",
			cash_inflow_row_code="CASH_RECEIVED_SALES", cash_outflow_row_code="CASH_PAID_SUPPLIERS",
			effective_from="2026-01-01", sign_multiplier="1")
		frappe.db.commit()
		worker = self.worker("public-source", self.payload(doc))
		frappe.db.get_value(doc.source_doctype, doc.source_name, "name", for_update=True)
		voucher = self.insert("China Accounting Voucher", "concurrently-created-voucher",
			company=self.company, posting_date=doc.posting_date, source_doctype=doc.source_doctype,
			source_name=doc.source_name, source_event="Posting", docstatus=1,
			source_key=f"Posting|{doc.source_doctype}|{doc.source_name}")
		self.release(worker)
		try:
			blocked = not select.select([worker.stdout], [], [], 0.6)[0]
		finally:
			frappe.db.commit()
		response = self.result(worker)
		self.assertTrue(blocked, "Public creation must acquire the source before inspecting the voucher")
		self.assertNotIn("error", response, response)
		created = frappe.get_doc("China Cash Flow Assignment", response["result"]["name"])
		self.assertEqual((created.status, created.china_accounting_voucher), ("Draft", voucher))
		self.assertEqual(frappe.db.count(created.doctype, {"china_accounting_voucher": voucher}), 1)

	def test_unseen_assignment_identity_rejects_before_any_assignment_lock(self):
		frappe.db.commit()
		payload = {"name": self.prefix + "-late-assignment", "company": self.company}
		worker = self.worker("late-assignment", payload)
		doc = self.assignment("late-assignment")
		frappe.db.commit()
		self.release(worker)
		response = self.result(worker)
		self.assertEqual(response.get("error"), "TimestampMismatchError", response)
		self.assertIn("刷新", response.get("message", ""), response)
		self.assertEqual(frappe.db.get_value(doc.doctype, doc.name, "modified"), doc.modified)
