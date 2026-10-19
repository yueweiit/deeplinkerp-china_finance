"""Whole-document cash-flow list processing against isolated native Frappe records."""

import json
import unittest

import frappe

from china_finance.services import cash_flow_assignment as service


class TestCashFlowAssignmentList(unittest.TestCase):
	def setUp(self):
		self.user = frappe.session.user
		self.language = frappe.local.lang
		frappe.set_user("Administrator")
		frappe.local.lang = "zh"
		self.prefix = "CF_LIST_" + frappe.generate_hash(length=10)
		frappe.db.savepoint(self.prefix)
		self.addCleanup(frappe.set_user, self.user)
		self.addCleanup(setattr, frappe.local, "lang", self.language)
		self.addCleanup(frappe.db.rollback, save_point=self.prefix)
		self.company = self.insert(
			"Company", "company", company_name=self.prefix, abbr=self.prefix[-5:], default_currency="CNY"
		)
		self.cash = self.insert(
			"Account", "cash", company=self.company, account_name="现金测试", account_currency="CNY"
		)
		self.other = self.insert(
			"Account", "other", company=self.company, account_name="对方测试", account_currency="CNY"
		)
		self.insert(
			"China Finance Settings",
			"settings",
			name=self.company,
			company=self.company,
			enabled=1,
			accounting_standard=self.prefix,
			enforce_role_separation=0,
		)
		self.addCleanup(frappe.clear_document_cache, "China Finance Settings", self.company)
		self.template = self.insert(
			"China Financial Statement Template",
			"template",
			template_key=self.prefix,
			accounting_standard=self.prefix,
			statement_type="Cash Flow",
			version="1",
			effective_from="2026-01-01",
			is_active=1,
		)
		for idx, (code, label) in enumerate(
			(
				("CASH_RECEIVED_SALES", "销售收款测试"),
				("OTHER_OPERATING_RECEIPTS", "其他收款测试"),
				("CASH_PAID_SUPPLIERS", "采购付款测试"),
				("FX_EFFECT", "汇率测试"),
			),
			1,
		):
			self.insert(
				"China Financial Statement Row",
				code,
				parent=self.template,
				parenttype="China Financial Statement Template",
				parentfield="rows",
				idx=idx,
				row_code=code,
				label=label,
				row_type="Mapped Accounts",
			)
		self.insert(
			"China Cash Equivalent Scope",
			"scope",
			company=self.company,
			account=self.cash,
			scope_key=self.prefix,
			classification="库存现金",
			included=1,
			effective_from="2026-01-01",
		)

	def insert(self, doctype, suffix, **values):
		name = values.pop("name", self.prefix + "-" + suffix)
		frappe.get_doc({"doctype": doctype, "name": name, **values}).db_insert()
		return name

	def assignment(self, suffix="a", directions=("收款",), posting_date="2026-07-01"):
		source = self.insert(
			"Payment Entry", suffix + "-source", company=self.company, posting_date=posting_date, docstatus=1
		)
		voucher = self.insert(
			"China Accounting Voucher",
			suffix + "-voucher",
			company=self.company,
			posting_date=posting_date,
			source_doctype="Payment Entry",
			source_name=source,
			source_event="Posting",
			source_key=self.prefix + suffix,
			docstatus=1,
		)
		name = self.insert(
			"China Cash Flow Assignment",
			suffix,
			company=self.company,
			currency="CNY",
			posting_date=posting_date,
			naming_series="CCFA-.YYYY.-.#####",
			china_accounting_voucher=voucher,
			source_doctype="Payment Entry",
			source_name=source,
			assignment_key=self.prefix + "|" + suffix,
			revision=1,
			status="Draft",
			assigned_by="Administrator",
		)
		self.insert(
			"GL Entry",
			suffix + "-counterpart",
			company=self.company,
			posting_date=posting_date,
			account=self.other,
			voucher_type="Payment Entry",
			voucher_no=source,
			debit=0,
			credit=1,
			is_cancelled=0,
			is_opening="No",
		)
		for idx, direction in enumerate(directions, 1):
			gl = self.insert(
				"GL Entry",
				f"{suffix}-gl-{idx}",
				company=self.company,
				posting_date=posting_date,
				account=self.cash,
				voucher_type="Payment Entry",
				voucher_no=source,
				debit=100 if direction == "收款" else 0,
				credit=100 if direction == "付款" else 0,
				is_cancelled=0,
				is_opening="No",
			)
			self.insert(
				"China Cash Flow Assignment Item",
				f"{suffix}-row-{idx}",
				parent=name,
				parenttype="China Cash Flow Assignment",
				parentfield="items",
				idx=idx,
				gl_entry=gl,
				cash_account=self.cash,
				cash_direction=direction,
				cash_amount=100,
				counterpart_accounts=self.other,
				cash_flow_row_code="CASH_RECEIVED_SALES" if direction == "收款" else "CASH_PAID_SUPPLIERS",
				assigned_amount=100,
			)
		return frappe.get_doc("China Cash Flow Assignment", name)

	def list(self, **kwargs):
		self.assertTrue(hasattr(service, "get_cash_flow_assignment_list"), "missing whole-document list API")
		return service.get_cash_flow_assignment_list(company=self.company, **kwargs)

	def payload(self, doc):
		return {
			"name": doc.name,
			"modified": str(doc.modified),
			"remarks": doc.remarks,
			"items": [
				{
					field: row.get(field)
					for field in ("name", "gl_entry", "cash_flow_row_code", "assigned_amount", "remarks")
				}
				for row in doc.items
			],
		}

	def process(self, assignments, action="save"):
		self.assertTrue(
			hasattr(service, "process_cash_flow_assignments"), "missing whole-document processing API"
		)
		return service.process_cash_flow_assignments(assignments, action=action)["results"]

	def test_list_paginates_documents_and_direction_keeps_all_cash_rows(self):
		mixed = self.assignment("mixed", ("收款", "付款"))
		self.assignment("other")
		before = mixed.as_json()
		result = self.list(direction="付款", page_length=1)
		self.assertEqual(result["total"], 1)
		self.assertEqual(result["counts"], {"Draft": 1, "Confirmed": 0, "Cancelled": 0, "All": 1})
		self.assertEqual(result["assignments"][0]["name"], mixed.name)
		self.assertEqual(len(result["assignments"][0]["items"]), 2)
		self.assertEqual(before, frappe.get_doc("China Cash Flow Assignment", mixed.name).as_json())
		self.assertEqual(len(self.list(page_length=1)["assignments"]), 1)
		self.assertEqual(self.list(page_length=1)["total"], 2)

	def test_search_matches_child_remarks_and_options_use_document_date(self):
		doc = self.assignment()
		later_template = self.insert(
			"China Financial Statement Template",
			"later-template",
			template_key=self.prefix + "-later",
			accounting_standard=self.prefix,
			statement_type="Cash Flow",
			version="2",
			effective_from="2026-08-01",
			is_active=1,
		)
		self.insert(
			"China Financial Statement Row",
			"later-row",
			parent=later_template,
			parenttype="China Financial Statement Template",
			parentfield="rows",
			idx=1,
			row_code="CASH_RECEIVED_SALES",
			label="八月销售收款测试",
			row_type="Mapped Accounts",
		)
		frappe.db.set_value(
			"China Cash Flow Assignment Item", doc.items[0].name, "remarks", "manual evidence keyword"
		)
		result = self.list(search="evidence")
		self.assertEqual(result["total"], 1)
		options = {item["value"]: item for item in result["assignments"][0]["options"]}
		self.assertEqual(options["CASH_RECEIVED_SALES"]["direction"], "收款")
		self.assertEqual(options["CASH_PAID_SUPPLIERS"]["direction"], "付款")
		self.assertEqual(options[service.INTERNAL_TRANSFER]["direction"], "all")
		self.assertEqual(options["FX_EFFECT"]["direction"], "all")
		self.assertEqual(options["CASH_RECEIVED_SALES"]["label"], "销售收款测试")
		self.assertEqual(self.list(search=doc.source_name)["total"], 1)
		later = self.assignment("later", posting_date="2026-08-01")
		options = self.list(search=later.name)["assignments"][0]["options"]
		self.assertEqual(
			next(r["label"] for r in options if r["value"] == "CASH_RECEIVED_SALES"), "八月销售收款测试"
		)

	def test_direction_and_search_can_match_different_cash_rows_of_one_document(self):
		doc = self.assignment(directions=("收款", "付款"))
		frappe.db.set_value(
			"China Cash Flow Assignment Item", doc.items[0].name, "remarks", "receipt evidence keyword"
		)
		result = self.list(direction="付款", search="receipt evidence")
		self.assertEqual(result["total"], 1)
		self.assertEqual(len(result["assignments"][0]["items"]), 2)

	def test_voucher_summary_is_displayed_and_searchable_without_source_writes(self):
		doc = self.assignment(directions=("收款", "付款"))
		frappe.db.set_value(
			"China Accounting Voucher", doc.china_accounting_voucher, "remarks", "AUDIT SUMMARY ONLY"
		)
		frappe.db.set_value(doc.doctype, doc.name, "remarks", "assignment explanation")
		before = {
			"assignment": frappe.get_doc(doc.doctype, doc.name).as_json(),
			"voucher": frappe.get_doc("China Accounting Voucher", doc.china_accounting_voucher).as_json(),
			"source": frappe.get_doc("Payment Entry", doc.source_name).as_json(),
			"gl": frappe.get_all(
				"GL Entry",
				filters={"voucher_type": "Payment Entry", "voucher_no": doc.source_name},
				fields=["name", "account", "debit", "credit", "modified", "is_cancelled"],
				order_by="name",
			),
		}
		listed = self.list()["assignments"][0]
		self.assertEqual(listed.get("voucher_summary"), "AUDIT SUMMARY ONLY")
		self.assertEqual(listed["remarks"], "assignment explanation")
		result = self.list(search="AUDIT SUMMARY", direction="付款", page_length=1)
		self.assertEqual(result["total"], 1)
		self.assertEqual(len(result["assignments"][0]["items"]), 2)
		self.assertEqual(before["assignment"], frappe.get_doc(doc.doctype, doc.name).as_json())
		self.assertEqual(
			before["voucher"],
			frappe.get_doc("China Accounting Voucher", doc.china_accounting_voucher).as_json(),
		)
		self.assertEqual(before["source"], frappe.get_doc("Payment Entry", doc.source_name).as_json())
		self.assertEqual(
			before["gl"],
			frappe.get_all(
				"GL Entry",
				filters={"voucher_type": "Payment Entry", "voucher_no": doc.source_name},
				fields=["name", "account", "debit", "credit", "modified", "is_cancelled"],
				order_by="name",
			),
		)

	def test_voucher_summary_read_and_search_respect_voucher_read_permission(self):
		doc = self.assignment()
		secret = "RESTRICTED AUDIT SUMMARY"
		frappe.db.set_value("China Accounting Voucher", doc.china_accounting_voucher, "remarks", secret)
		frappe.db.set_value(doc.doctype, doc.name, "remarks", "public assignment explanation")
		username = self.prefix.lower() + "@example.invalid"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": username,
				"first_name": "Cash flow preparer",
				"send_welcome_email": 0,
				"roles": [{"role": "Accounts User"}],
			}
		).insert(ignore_permissions=True)
		frappe.set_user(username)
		self.assertTrue(doc.has_permission("read"))
		self.assertFalse(
			frappe.get_doc("China Accounting Voucher", doc.china_accounting_voucher).has_permission("read")
		)
		result = self.list()
		self.assertEqual(result["total"], 1)
		self.assertIn("voucher_summary", result["assignments"][0])
		self.assertFalse(result["assignments"][0]["voucher_summary"])
		self.assertNotIn(secret, json.dumps(result, default=str))
		result = self.list(search="RESTRICTED AUDIT")
		self.assertEqual(result["total"], 0)
		self.assertEqual(result["counts"]["All"], 0)
		self.assertEqual(self.list(search="public assignment")["total"], 1)

	def test_invalid_source_is_a_document_blocker_without_get_mutations(self):
		doc = self.assignment()
		frappe.db.set_value("China Accounting Voucher", doc.china_accounting_voucher, "docstatus", 0)
		before = doc.as_json()
		listed = self.list()["assignments"][0]
		self.assertTrue(listed["blocker_details"])
		self.assertEqual(listed["blocker_details"][0]["scope"], "document")
		self.assertEqual(before, frappe.get_doc(doc.doctype, doc.name).as_json())

	def test_save_split_preserves_evidence_and_manual_values_until_confirmation(self):
		doc = self.assignment()
		payload = self.payload(doc)
		payload["items"][0].update(
			cash_flow_row_code="OTHER_OPERATING_RECEIPTS", assigned_amount=40, remarks="manual first"
		)
		payload["items"].append(
			{
				"gl_entry": doc.items[0].gl_entry,
				"cash_flow_row_code": "CASH_RECEIVED_SALES",
				"assigned_amount": 60,
				"remarks": "manual second",
			}
		)
		result = self.process(json.dumps([payload]))[0]
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["document"]["status"], "Draft")
		doc.reload()
		self.assertEqual(
			[(r.assigned_amount, r.remarks) for r in doc.items], [(40, "manual first"), (60, "manual second")]
		)
		self.assertTrue(all(r.cash_amount == 100 and r.cash_account == self.cash for r in doc.items))
		result = self.process([self.payload(doc)], action="confirm")[0]
		self.assertTrue(result["ok"], result)
		doc.reload()
		self.assertEqual((doc.status, doc.docstatus), ("Confirmed", 1))
		self.assertEqual(
			[r.cash_flow_row_code for r in doc.items], ["OTHER_OPERATING_RECEIPTS", "CASH_RECEIVED_SALES"]
		)
		self.assertEqual([r.assigned_amount for r in doc.items], [40, 60])
		stamp = doc.confirmed_on
		retry = self.process([self.payload(doc)], action="confirm")[0]
		self.assertTrue(retry["ok"], retry)
		self.assertEqual(str(retry["document"]["confirmed_on"]), str(stamp))
		self.assertFalse(retry["document"]["can_write"])
		self.assertEqual(
			frappe.db.count(
				"China Cash Flow Assignment", {"china_accounting_voucher": doc.china_accounting_voucher}
			),
			1,
		)

	def test_incomplete_draft_saves_and_failed_confirmation_rolls_back_entire_document_only(self):
		first, second = self.assignment("first"), self.assignment("second")
		payload = self.payload(first)
		payload["items"][0]["assigned_amount"] = 40
		self.assertTrue(self.process([payload])[0]["ok"])
		first.reload()
		listed = self.list(search=first.name)["assignments"][0]
		self.assertTrue(any("100" in text for text in listed["blockers"]), listed)
		self.assertTrue(
			any(item["scope"] == "items" and "100" in item["message"] for item in listed["blocker_details"]),
			listed,
		)
		bad, good = self.payload(first), self.payload(second)
		bad["remarks"] = "must roll back"
		good["remarks"] = "must remain confirmed"
		results = self.process([bad, good], action="confirm")
		self.assertFalse(results[0]["ok"], results)
		self.assertTrue(results[1]["ok"], results)
		first.reload()
		second.reload()
		self.assertEqual(first.status, "Draft")
		self.assertNotEqual(first.remarks, "must roll back")
		self.assertEqual((second.status, second.remarks), ("Confirmed", "must remain confirmed"))

	def test_modified_conflict_never_overwrites_another_save(self):
		doc = self.assignment()
		stale = self.payload(doc)
		doc.remarks = "another browser"
		doc.save()
		stale["remarks"] = "stale browser"
		result = self.process([stale])[0]
		self.assertFalse(result["ok"], result)
		self.assertEqual(result["error_type"], "TimestampMismatchError")
		self.assertEqual(frappe.db.get_value(doc.doctype, doc.name, "remarks"), "another browser")

	def test_rejects_foreign_row_foreign_gl_and_evidence_mutation(self):
		doc, other = self.assignment("first"), self.assignment("other")
		for field, value in (
			("name", other.items[0].name),
			("gl_entry", other.items[0].gl_entry),
			("cash_amount", 1),
		):
			payload = self.payload(doc)
			payload["items"][0][field] = value
			result = self.process([payload])[0]
			self.assertFalse(result["ok"], (field, result))
			self.assertEqual(frappe.get_doc(doc.doctype, doc.name).items[0].cash_amount, 100)

	def test_native_separation_rule_is_kept_and_save_before_confirm_rolls_back(self):
		doc = self.assignment()
		frappe.db.set_value("China Finance Settings", self.company, "enforce_role_separation", 1)
		frappe.clear_document_cache("China Finance Settings", self.company)
		payload = self.payload(doc)
		payload["remarks"] = "failed separated submit"
		result = self.process([payload], action="confirm")[0]
		self.assertFalse(result["ok"], result)
		self.assertIn("不能为同一用户", result["error"])
		doc.reload()
		self.assertEqual(doc.status, "Draft")
		self.assertNotEqual(doc.remarks, payload["remarks"])

	def test_company_user_permission_filters_list_counts_and_process(self):
		doc = self.assignment()
		other_company = self.insert(
			"Company",
			"other-company",
			company_name=self.prefix + "other",
			abbr=self.prefix[-4:],
			default_currency="CNY",
		)
		username = self.prefix.lower() + "@example.invalid"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": username,
				"first_name": "Cash flow list test",
				"send_welcome_email": 0,
				"roles": [{"role": "China Finance Manager"}],
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": username,
				"allow": "Company",
				"for_value": other_company,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(username)
		result = self.list()
		self.assertEqual(result["total"], 0)
		self.assertEqual(result["counts"]["All"], 0)
		result = self.process([self.payload(doc)])[0]
		self.assertFalse(result["ok"], result)
		self.assertEqual(result["error_type"], "PermissionError")

	def test_auditor_sees_read_only_rows_and_cannot_process(self):
		doc = self.assignment()
		username = self.prefix.lower() + "@example.invalid"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": username,
				"first_name": "Cash flow auditor",
				"send_welcome_email": 0,
				"roles": [{"role": "China Finance Auditor"}],
			}
		).insert(ignore_permissions=True)
		frappe.set_user(username)
		listed = self.list()["assignments"][0]
		self.assertFalse(listed["can_write"])
		self.assertFalse(listed["can_confirm"])
		result = self.process([self.payload(doc)])[0]
		self.assertFalse(result["ok"], result)
		self.assertEqual(result["error_type"], "PermissionError")

	def test_native_required_fields_reject_blank_project_without_partial_save(self):
		doc = self.assignment()
		payload = self.payload(doc)
		payload["items"][0]["cash_flow_row_code"] = ""
		payload["remarks"] = "must not save"
		result = self.process([payload])[0]
		self.assertFalse(result["ok"], result)
		self.assertEqual(result["error_type"], "MandatoryError")
		doc.reload()
		self.assertEqual(doc.items[0].cash_flow_row_code, "CASH_RECEIVED_SALES")
		self.assertNotEqual(doc.remarks, payload["remarks"])

	def test_confirmation_reuses_stale_gl_repair_and_keeps_manual_assignment(self):
		doc = self.assignment()
		old_gl = doc.items[0].gl_entry
		frappe.db.set_value("GL Entry", old_gl, "is_cancelled", 1)
		new_gl = self.insert(
			"GL Entry",
			"replacement-gl",
			company=self.company,
			posting_date=doc.posting_date,
			account=self.cash,
			voucher_type="Payment Entry",
			voucher_no=doc.source_name,
			debit=100,
			credit=0,
			is_cancelled=0,
			is_opening="No",
		)
		payload = self.payload(doc)
		payload["items"][0].update(
			cash_flow_row_code="OTHER_OPERATING_RECEIPTS", remarks="preserve hand selection"
		)
		result = self.process([payload], action="confirm")[0]
		self.assertTrue(result["ok"], result)
		doc.reload()
		self.assertEqual(doc.items[0].gl_entry, new_gl)
		self.assertEqual(doc.items[0].cash_flow_row_code, "OTHER_OPERATING_RECEIPTS")
		self.assertEqual(doc.items[0].remarks, "preserve hand selection")


class TestCashFlowAssignmentListNativePosting(unittest.TestCase):
	"""One actual Journal Entry submission; reuse helpers without inheriting their tests."""

	from china_finance.tests.test_inline_voucher_account_edit import TestInlineVoucherAccountEdit as _Fixture

	setUp = _Fixture.setUp
	tearDown = _Fixture.tearDown
	_make_company = _Fixture._make_company
	_account = _Fixture._account
	_journal_entry = _Fixture._journal_entry
	_mark_ready = _Fixture._mark_ready
	payload = TestCashFlowAssignmentList.payload
	process = TestCashFlowAssignmentList.process

	def test_native_posting_then_list_split_confirmation_does_not_repost_source(self):
		from china_finance.services.cash_equivalent_scope import get_cash_scope_accounts

		if self.bank not in get_cash_scope_accounts(self.company, "2026-08-15"):
			frappe.get_doc(
				{
					"doctype": "China Cash Equivalent Scope",
					"company": self.company,
					"account": self.bank,
					"classification": "随时可用存款",
					"included": 1,
					"policy_basis": "Native list test bank account",
					"effective_from": "2026-08-15",
					"effective_to": "2026-08-15",
				}
			).insert()
		if self.expense_account not in service._suggestion_by_account(self.company, "2026-08-15"):
			frappe.get_doc(
				{
					"doctype": "China Financial Statement Mapping",
					"company": self.company,
					"template": service._get_cash_template(self.company, "2026-08-15").name,
					"account": self.expense_account,
					"row_code": "OTHER_OPERATING_PAYMENTS",
					"cash_inflow_row_code": "OTHER_OPERATING_RECEIPTS",
					"cash_outflow_row_code": "OTHER_OPERATING_PAYMENTS",
					"sign_multiplier": "1",
					"effective_from": "2026-08-15",
					"effective_to": "2026-08-15",
				}
			).insert()
		frappe.db.set_value(
			"China Finance Settings", self.company, "cash_flow_assignment_activation_date", "2026-01-01"
		)
		frappe.clear_document_cache("China Finance Settings", self.company)
		entry = self._mark_ready(self._journal_entry())
		entry.submit()
		entry.reload()
		voucher = frappe.db.get_value(
			"China Accounting Voucher",
			{
				"source_doctype": "Journal Entry",
				"source_name": entry.name,
				"source_event": "Posting",
			},
			"name",
		)
		self.assertTrue(voucher, "native posting must generate its statutory voucher snapshot")
		name = frappe.db.get_value(
			"China Cash Flow Assignment", {"china_accounting_voucher": voucher}, "name"
		)
		self.assertTrue(name, "native posting must generate its cash-flow draft")
		doc = frappe.get_doc("China Cash Flow Assignment", name)
		ledger = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Journal Entry", "voucher_no": entry.name},
			fields=["name", "account", "debit", "credit", "is_cancelled", "docstatus"],
			order_by="name",
		)
		source_before = entry.as_json()
		voucher_before = frappe.get_doc("China Accounting Voucher", voucher).as_json()
		payload = self.payload(doc)
		payload["items"][0].update(
			cash_flow_row_code="OTHER_OPERATING_PAYMENTS", assigned_amount=50, remarks="native split one"
		)
		payload["items"].append(
			{
				"gl_entry": doc.items[0].gl_entry,
				"cash_flow_row_code": "CASH_PAID_SUPPLIERS",
				"assigned_amount": 75,
				"remarks": "native split two",
			}
		)
		self.assertTrue(self.process([payload])[0]["ok"])
		doc.reload()
		result = self.process([self.payload(doc)], action="confirm")[0]
		self.assertTrue(result["ok"], result)
		self.assertEqual((result["document"]["status"], result["document"]["docstatus"]), ("Confirmed", 1))
		self.assertEqual([r["assigned_amount"] for r in result["document"]["items"]], [50, 75])
		self.assertEqual(
			ledger,
			frappe.get_all(
				"GL Entry",
				filters={"voucher_type": "Journal Entry", "voucher_no": entry.name},
				fields=["name", "account", "debit", "credit", "is_cancelled", "docstatus"],
				order_by="name",
			),
		)
		self.assertEqual(source_before, frappe.get_doc("Journal Entry", entry.name).as_json())
		self.assertEqual(voucher_before, frappe.get_doc("China Accounting Voucher", voucher).as_json())
