"""China Finance additions around ERPNext's native bank reconciliation tool."""

import json
import re

import frappe
from frappe import _
from frappe.utils import flt, getdate
from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
	create_journal_entry_bts,
	get_bank_transactions,
	reconcile_vouchers,
)

# Default account mapping: description keyword -> account number
# Override via China Finance Settings if needed.
DEFAULT_ACCOUNT_MAPPING = {
	"利息": "660302",
	"手续费": "660303",
	"服务费": "660303",
	"报销": "660201",
	"社保": "221103",
	"工资": "221101",
	"代发": "221101",
	# The source chart has no dedicated recruitment expense account. Keep
	# unclassified recruitment charges in Management Expense - Other.
	"招聘": "660299",
	"租金水电物业管理费": "660299",
	"租金": "660202",
	"物业水电": "660299",
	"物业": "660203",
	"水电": "660204",
	"缴税": "22210104",
	"公积金": "221104",
	"补缴": "221104",
	"投资": "4001",
	"退款": "122101",
	"退回": "122101",
	"验证": "122101",
	"实名": "122101",
}

# Only company-style counterparties are auto-filled into the accounting party
# field. Personal names such as employee reimbursement recipients stay in the
# bank statement metadata and are already included in the reimbursement summary.
COMPANY_NAME_MARKERS = ("有限公司", "有限责任公司", "股份有限公司", "集团公司", "公司")
CONFIRMED_UNLINKED_BANK_REFERENCES = {
	"C0347GZ00067CTZ",
	"C0347GZ000C7OXZ",
	"C0347GZ000F1H1Z",
	"C0347GZ000L36UZ",
	"C0347GZ000Z17JZ",
	"C0347H30010RMVZ",
	"C0347H80011WJFZ",
	"C0347H9000NWEUZ",
	"C0347H9000T824Z",
	"C0347HD0007FPMZ",
	"C0347HD0005GH0Z",
	"C0347HK0012R48Z",
}

BANK_RECONCILIATION_TOLERANCE = 0.005


@frappe.whitelist()
def get_bank_transactions_with_summary(*args, **kwargs):
	# Frappe injects the RPC command name into whitelisted calls; the native
	# ERPNext function only accepts the business arguments.
	kwargs.pop("cmd", None)
	transactions = get_bank_transactions(*args, **kwargs)
	if not transactions:
		return transactions

	names = [row.name for row in transactions]
	summaries = frappe.get_all(
		"Bank Transaction",
		filters={"name": ["in", names]},
		fields=["name", "custom_summary", "description"],
	)
	summary_map = {row.name: row.custom_summary for row in summaries}
	for row in transactions:
		stored = next((item for item in summaries if item.name == row.name), None)
		row.custom_summary = clean_bank_summary(
			(summary_map.get(row.name) or (stored.description if stored else ""))
		)
	return transactions


def clean_bank_summary(value):
	"""Keep the business summary and remove bank reference metadata from display."""
	value = (value or "").strip()
	if "｜" in value:
		value = value.split("｜", 1)[0].strip()
	value = re.split(r"\s*参考\s*#?.*$", value, maxsplit=1, flags=re.IGNORECASE)[0].strip()
	return value


def _apply_journal_entry_summary(journal_entry, summary):
	"""Put the concise business summary on the parent and every entry row."""
	summary = (summary or "").strip()
	if not summary:
		return
	if journal_entry.meta.has_field("custom_remark"):
		# ERPNext's create_remarks hook otherwise replaces ``remark`` with the
		# cheque/reference text when the Journal Entry is inserted.
		journal_entry.custom_remark = 1
	if journal_entry.meta.has_field("remark"):
		journal_entry.remark = summary
	if journal_entry.meta.has_field("user_remark"):
		journal_entry.user_remark = summary
	for entry in journal_entry.accounts:
		if entry.meta.has_field("user_remark"):
			entry.user_remark = summary


def prepare_bank_transaction(doc, method=None):
	"""Persist useful CMB metadata before the standard Bank Transaction submit hook."""
	description = str(doc.description or "")
	if frappe.db.has_column("Bank Transaction", "custom_summary") and not doc.get("custom_summary"):
		doc.custom_summary = clean_bank_summary(description)

	metadata = {
		"bank_party_name": _extract_bank_metadata(description, "对方"),
		"bank_party_account_number": _extract_bank_metadata(description, "账号"),
		"transaction_type": _extract_bank_metadata(description, "交易类型") or doc.transaction_type,
	}
	for fieldname, value in metadata.items():
		if value and frappe.get_meta("Bank Transaction").has_field(fieldname) and not doc.get(fieldname):
			setattr(doc, fieldname, value)

	# The bank statement contains the counterparty's legal name, but ERPNext's
	# native importer does not map it to party_type/party. Resolve it only when
	# an exact Customer/Supplier master already exists; never create master data
	# implicitly during a bank import.
	if not doc.get("party_type") and not doc.get("party"):
		party = _resolve_company_party(
			metadata["bank_party_name"], doc, _preferred_party_type(doc)
		)
		if party:
			doc.update(party)

	# CMB 流水号 is the stable idempotency key. Keep it in the native
	# transaction_id field as well as reference_number for reconciliation.
	if doc.reference_number and not doc.transaction_id:
		doc.transaction_id = str(doc.reference_number).strip()

	if doc.transaction_id and doc.bank_account:
		# Serialize receipt imports and statement imports against the same account.
		frappe.db.sql("SELECT name FROM `tabBank Account` WHERE name=%s FOR UPDATE", doc.bank_account)
		existing = frappe.db.get_value(
			"Bank Transaction",
			{
				"bank_account": doc.bank_account,
				"docstatus": ["!=", 2],
				"transaction_id": doc.transaction_id,
			},
			"name",
			for_update=True,
		)
		if not existing:
			existing = frappe.db.get_value(
				"Bank Transaction",
				{
					"bank_account": doc.bank_account,
					"docstatus": ["!=", 2],
					"reference_number": doc.reference_number,
				},
				"name",
				for_update=True,
			)
		if existing and existing != doc.name:
			frappe.throw(_("银行流水 {0} 已导入为 {1}，请勿重复导入。" ).format(doc.transaction_id, existing))


def _extract_bank_metadata(description, label):
	match = re.search(rf"(?:^|｜){re.escape(label)}：([^｜]*)", description or "")
	return match.group(1).strip() if match else ""


def _resolve_company_party(counterparty_name, bank_transaction, preferred_party_type=None):
	"""Resolve a company counterparty to an existing Customer or Supplier."""
	name = (counterparty_name or "").strip()
	if not name or not any(marker in name for marker in COMPANY_NAME_MARKERS):
		return {}

	# Prefer the party type accepted by the target account. In particular, the
	# verification/refund rows use the 122101 Receivable leaf account, so both
	# payment and refund rows must use Customer even though one direction is a
	# bank withdrawal.
	is_withdrawal = flt(bank_transaction.get("withdrawal")) > 0
	party_types = (preferred_party_type,) if preferred_party_type else (
		("Supplier",) if is_withdrawal else ("Customer",)
	)
	name_fields = {"Supplier": "supplier_name", "Customer": "customer_name"}
	for party_type in party_types:
		name_field = name_fields[party_type]
		party = frappe.db.get_value(
			party_type,
			{"disabled": 0, name_field: name},
			"name",
		)
		if not party:
			party = frappe.db.get_value(
				party_type,
				{"disabled": 0, "name": name},
				"name",
			)
		if party:
			return {"party_type": party_type, "party": party}

	return {}


def _preferred_party_type(bank_transaction):
	"""Return a party type compatible with the account chosen for the row."""
	account = _resolve_account(
		bank_transaction.get("description") or bank_transaction.get("custom_summary"),
		bank_transaction.get("company"),
		reference_number=bank_transaction.get("reference_number"),
		counterparty_name=bank_transaction.get("bank_party_name"),
	)
	if not account:
		return None
	account_info = frappe.db.get_value("Account", account, ["account_type"], as_dict=True)
	if account_info and account_info.account_type == "Receivable":
		return "Customer"
	if account_info and account_info.account_type == "Payable":
		return "Supplier"
	return None


@frappe.whitelist()
def create_journal_entry_with_summary(remarks=None, **kwargs):
	"""Create an editable Journal Entry draft and link it to the bank transaction."""
	kwargs.pop("cmd", None)
	kwargs.pop("allow_edit", None)
	bank_transaction_name = kwargs.get("bank_transaction_name")
	if bank_transaction_name:
		bank_transaction = frappe.get_doc("Bank Transaction", bank_transaction_name)
		_validate_bank_account_company(bank_transaction.bank_account, bank_transaction.company)
	journal_entry = create_journal_entry_bts(**kwargs, allow_edit=True)
	if _is_interest_income_account(kwargs.get("second_account")):
		_restore_interest_offset_debit(journal_entry, kwargs.get("second_account"))
	if remarks:
		_apply_journal_entry_summary(journal_entry, remarks)
	if journal_entry.meta.has_field("custom_china_bank_transaction"):
		journal_entry.custom_china_bank_transaction = kwargs["bank_transaction_name"]
	journal_entry.insert()
	if frappe.db.has_column("Bank Transaction", "custom_china_journal_entry"):
		frappe.db.set_value(
			"Bank Transaction",
			kwargs["bank_transaction_name"],
			"custom_china_journal_entry",
			journal_entry.name,
			update_modified=False,
		)
	return journal_entry


def _is_interest_income_account(account):
	return bool(account and frappe.db.get_value("Account", account, "account_number") == "660302")


def _restore_interest_offset_debit(journal_entry, account=None):
	"""Keep interest income as a negative debit in the source Journal Entry."""
	for entry in journal_entry.accounts:
		if account and entry.account != account:
			continue
		if entry.credit_in_account_currency > 0 and not entry.debit_in_account_currency:
			entry.debit_in_account_currency = -abs(entry.credit_in_account_currency)
			entry.credit_in_account_currency = 0
			entry.debit = -abs(entry.credit)
			entry.credit = 0


def _get_bank_transactions_for_journal_entry(doc):
	"""Return every bank transaction that still refers to this Journal Entry."""
	names = []
	if frappe.db.exists("DocType", "Bank Transaction Payments"):
		names.extend(
			frappe.db.get_all(
				"Bank Transaction Payments",
				filters={
					"payment_document": doc.doctype,
					"payment_entry": doc.name,
				},
				pluck="parent",
			)
			or []
		)

	if doc.meta.has_field("custom_china_bank_transaction") and doc.get("custom_china_bank_transaction"):
		names.append(doc.custom_china_bank_transaction)

	if frappe.db.has_column("Bank Transaction", "custom_china_journal_entry"):
		names.extend(
			frappe.db.get_all(
				"Bank Transaction",
				filters={"custom_china_journal_entry": doc.name, "docstatus": ["!=", 2]},
				pluck="name",
			)
			or []
		)

	return sorted(dict.fromkeys(name for name in names if name))


def prepare_journal_entry_bank_cancellation(doc):
	"""Validate bank state and detach stale pointers before cancelling a Journal Entry.

	ERPNext's unreconcile action removes ``Bank Transaction Payments`` rows, but
	the China Finance convenience link can still point to the submitted Journal
	Entry. Frappe then treats that link as a backlink and refuses cancellation.
	Only detach it after this Journal Entry has no active allocation; allocations
	belonging to other vouchers on the same bank transaction are left untouched.
	"""
	if doc.doctype != "Journal Entry":
		return []

	has_custom_link = frappe.db.has_column("Bank Transaction", "custom_china_journal_entry")
	names = _get_bank_transactions_for_journal_entry(doc)
	if not names:
		if doc.get("clearance_date"):
			frappe.throw(_("凭证已完成银行清账，请先撤销银行对账后再取消凭证"))
		return []

	for name in names:
		# Serialize source editing/cancellation with reconciliation for this row.
		if not frappe.db.sql(
			"SELECT name FROM `tabBank Transaction` WHERE name=%s FOR UPDATE",
			(name,),
		):
			continue

		fields = ["docstatus", "status", "allocated_amount"]
		if has_custom_link:
			fields.append("custom_china_journal_entry")
		bank_transaction = frappe.db.get_value(
			"Bank Transaction",
			name,
			fields,
			as_dict=True,
		)
		if not bank_transaction or bank_transaction.docstatus == 2:
			continue

		allocated_row = None
		if frappe.db.exists("DocType", "Bank Transaction Payments"):
			allocated_row = frappe.db.get_value(
				"Bank Transaction Payments",
				{
					"parent": name,
					"payment_document": doc.doctype,
					"payment_entry": doc.name,
				},
				"allocated_amount",
			)

		points_to_voucher = has_custom_link and bank_transaction.get("custom_china_journal_entry") == doc.name
		is_still_reconciled = (
			flt(allocated_row) > BANK_RECONCILIATION_TOLERANCE
			or bool(doc.get("clearance_date"))
		)
		if is_still_reconciled:
			frappe.throw(
				_("凭证已参与银行对账（银行流水 {0}），请先执行“取消银行交易流水核销”后再取消凭证").format(
					name
				)
			)

		if points_to_voucher:
			frappe.db.set_value(
				"Bank Transaction",
				name,
				"custom_china_journal_entry",
				None,
				update_modified=False,
			)

	return names


def on_journal_entry_submit(doc, method=None):
	"""Reconcile a reviewed bank-generated Journal Entry after it is submitted."""
	bank_transaction_name = doc.get("custom_china_bank_transaction")
	if not bank_transaction_name or not frappe.db.exists("Bank Transaction", bank_transaction_name):
		return

	bank_transaction = frappe.get_doc("Bank Transaction", bank_transaction_name)
	if bank_transaction.docstatus != 1 or flt(bank_transaction.allocated_amount) > 0:
		return

	if frappe.db.has_column("Bank Transaction", "custom_china_journal_entry"):
		frappe.db.set_value(
			"Bank Transaction",
			bank_transaction_name,
			"custom_china_journal_entry",
			doc.name,
			update_modified=False,
		)

	amount = flt(bank_transaction.deposit) if flt(bank_transaction.deposit) > 0 else flt(bank_transaction.withdrawal)
	if amount <= 0:
		return

	try:
		reconcile_vouchers(
			bank_transaction_name,
			json.dumps([{
				"payment_doctype": "Journal Entry",
				"payment_name": doc.name,
				"amount": amount,
			}]),
		)
	except Exception:
		frappe.log_error(
			f"Bank reconciliation after Journal Entry submit failed for {doc.name}: {frappe.get_traceback()}",
			"Bank Reconciliation After Journal Entry Submit",
		)


def auto_create_voucher_on_submit(doc, method=None):
	"""Create a draft Journal Entry when a Bank Transaction is submitted.

	Called via doc_events on_submit hook. Only creates a voucher if:
	- The bank transaction has no allocated amount yet
	- A matching bank account GL account exists
	"""
	from china_finance.services.bank_receipt_import import reuse_receipt_for_transaction

	if doc.flags.china_receipt_explicit_voucher or reuse_receipt_for_transaction(doc):
		return
	if flt(doc.allocated_amount) > 0:
		return
	if (
		frappe.db.has_column("Bank Transaction", "custom_china_journal_entry")
		and doc.get("custom_china_journal_entry")
		and frappe.db.exists("Journal Entry", doc.custom_china_journal_entry)
	):
		return

	bank_account_gl = _get_valid_bank_account_gl(doc.bank_account, doc.company)
	if not bank_account_gl:
		return

	company = doc.company
	raw_description = str(doc.description or "")
	desc = clean_bank_summary(doc.get("custom_summary") or raw_description)
	amount = flt(doc.withdrawal) if flt(doc.withdrawal) > 0 else flt(doc.deposit)
	is_withdrawal = flt(doc.withdrawal) > 0

	if amount <= 0:
		return

	expense_account = _resolve_account(
		raw_description or desc,
		company,
		reference_number=doc.reference_number,
		counterparty_name=doc.get("bank_party_name"),
	)
	if not expense_account:
		frappe.log_error(
			f"Auto voucher: no account found for bank transaction {doc.name} ({desc})",
			"Bank Auto Voucher",
		)
		return

	party_fields = _get_safe_party_fields(expense_account, doc)
	account_type = frappe.db.get_value("Account", expense_account, "account_type")

	try:
		je = frappe.new_doc("Journal Entry")
		je.posting_date = doc.date
		je.company = company
		je.voucher_type = "Journal Entry"
		je.cheque_no = doc.reference_number or doc.name
		je.cheque_date = doc.date
		if je.meta.has_field("custom_china_bank_transaction"):
			je.custom_china_bank_transaction = doc.name
		# The unclassified other-receivables leaf is intentionally used for
		# verification/refund rows, which do not have a real customer or supplier.
		# Keep those drafts/submissions valid without inventing a party.
		if account_type in ("Receivable", "Payable") and not party_fields:
			je.party_not_required = 1

		if is_withdrawal:
			expense_entry = {
				"account": expense_account,
				"debit_in_account_currency": amount,
				"credit_in_account_currency": 0,
			}
			expense_entry.update(party_fields)
			je.append("accounts", expense_entry)
			je.append("accounts", {
				"account": bank_account_gl,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": amount,
			})
		else:
			je.append("accounts", {
				"account": bank_account_gl,
				"debit_in_account_currency": amount,
				"credit_in_account_currency": 0,
			})
			expense_entry = {
				"account": expense_account,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": amount,
			}
			expense_entry.update(party_fields)
			je.append("accounts", expense_entry)
			if _is_interest_income_account(expense_account):
				_restore_interest_offset_debit(je, expense_account)

		_apply_journal_entry_summary(je, desc)

		je.insert(ignore_permissions=True)
		if frappe.db.has_column("Bank Transaction", "custom_china_journal_entry"):
			frappe.db.set_value(
				"Bank Transaction",
				doc.name,
				"custom_china_journal_entry",
				je.name,
				update_modified=False,
			)
		frappe.msgprint(
			_("已为银行流水 {0} 生成待审核记账凭证 {1}，请检查后手动提交").format(doc.name, je.name),
			alert=True,
		)

	except Exception:
		frappe.log_error(
			f"Auto voucher creation failed for {doc.name}: {frappe.get_traceback()}",
			"Bank Auto Voucher",
		)


@frappe.whitelist()
def repair_draft_bank_journal_entry_summaries(company, from_date=None, to_date=None):
	"""Backfill summaries on existing draft JEs created from bank transactions."""
	frappe.only_for(("System Manager", "China Finance Manager"))
	if not company:
		frappe.throw(_("必须选择公司"))
	if not frappe.get_meta("Bank Transaction").has_field("custom_china_journal_entry"):
		return {"processed": 0, "updated": 0, "skipped": 0}

	from_date = getdate(from_date) if from_date else None
	to_date = getdate(to_date) if to_date else None
	filters = {"company": company, "custom_china_journal_entry": ["is", "set"]}
	if from_date and to_date:
		filters["date"] = ["between", [from_date, to_date]]
	elif from_date:
		filters["date"] = [">=", from_date]
	elif to_date:
		filters["date"] = ["<=", to_date]

	rows = frappe.get_all(
		"Bank Transaction",
		filters=filters,
		fields=["name", "custom_china_journal_entry", "custom_summary", "description"],
		order_by="date, name",
	)
	processed = updated = skipped = 0
	for row in rows:
		journal_entry_name = row.custom_china_journal_entry
		if not frappe.db.exists("Journal Entry", journal_entry_name):
			skipped += 1
			continue
		journal_entry = frappe.get_doc("Journal Entry", journal_entry_name)
		from china_finance.services.bank_receipt_import import has_structured_receipt_lines

		if has_structured_receipt_lines(journal_entry):
			# Keep receipt-period accrual/payment summaries on their individual lines.
			skipped += 1
			continue
		if journal_entry.docstatus != 0:
			# Posted vouchers must be amended through the normal accounting flow.
			skipped += 1
			continue
		summary = clean_bank_summary(row.custom_summary or row.description)
		if not summary:
			skipped += 1
			continue

		before = (
			getattr(journal_entry, "custom_remark", None),
			getattr(journal_entry, "remark", None),
			getattr(journal_entry, "user_remark", None),
			[entry.user_remark for entry in journal_entry.accounts],
		)
		_apply_journal_entry_summary(journal_entry, summary)
		after = (
			getattr(journal_entry, "custom_remark", None),
			getattr(journal_entry, "remark", None),
			getattr(journal_entry, "user_remark", None),
			[entry.user_remark for entry in journal_entry.accounts],
		)
		processed += 1
		if before != after:
			values = {
				"remark": journal_entry.remark,
				"user_remark": journal_entry.user_remark,
			}
			if journal_entry.meta.has_field("custom_remark"):
				values["custom_remark"] = journal_entry.custom_remark
			frappe.db.set_value("Journal Entry", journal_entry.name, values, update_modified=False)
			for entry in journal_entry.accounts:
				frappe.db.set_value(
					"Journal Entry Account", entry.name, "user_remark", entry.user_remark, update_modified=False
				)
			updated += 1

	frappe.db.commit()
	return {"processed": processed, "updated": updated, "skipped": skipped}


@frappe.whitelist()
def repair_unlinked_bank_vouchers(company="悦为智能技术(东莞)有限公司", references=None):
	"""Repair the confirmed bank rows that previously had no draft voucher.

	This is an explicit repair operation, not part of normal bank import. It may
	create the missing Customer/Supplier masters required by the 122101 receivable
	account, then reuses the normal automatic-voucher path for the selected rows.
	"""
	frappe.only_for(("System Manager", "China Finance Manager"))
	if company != "悦为智能技术(东莞)有限公司":
		frappe.throw(_("此维护操作仅允许处理 aaa 的悦为智能技术(东莞)有限公司"))

	if isinstance(references, str):
		try:
			references = json.loads(references)
		except json.JSONDecodeError:
			references = [item.strip() for item in references.split(",") if item.strip()]
	references = set(references or CONFIRMED_UNLINKED_BANK_REFERENCES)

	rows = frappe.get_all(
		"Bank Transaction",
		filters={
			"company": company,
			"reference_number": ["in", list(references)],
			"docstatus": 1,
		},
		fields=["name", "reference_number"],
		order_by="date,reference_number",
	)
	result = {"processed": 0, "created": 0, "skipped": 0, "parties_created": [], "errors": []}
	for row in rows:
		result["processed"] += 1
		bank_transaction = frappe.get_doc("Bank Transaction", row.name)
		if bank_transaction.get("custom_china_journal_entry") and frappe.db.exists(
			"Journal Entry", bank_transaction.custom_china_journal_entry
		):
			result["skipped"] += 1
			continue

		try:
			party = _ensure_company_party(bank_transaction)
			if party:
				bank_transaction.db_set("party_type", party["party_type"], update_modified=False)
				bank_transaction.db_set("party", party["party"], update_modified=False)
			voucher_before = bank_transaction.get("custom_china_journal_entry")
			auto_create_voucher_on_submit(bank_transaction)
			voucher_after = frappe.db.get_value(
				"Bank Transaction", bank_transaction.name, "custom_china_journal_entry"
			)
			if voucher_after and voucher_after != voucher_before:
				result["created"] += 1
			else:
				result["errors"].append({"name": bank_transaction.name, "error": "未生成凭证"})
		except Exception as exc:
			result["errors"].append({"name": bank_transaction.name, "error": str(exc)})

	frappe.db.commit()
	return result


def _ensure_company_party(bank_transaction):
	"""Create a party master only for the explicit repair operation."""
	name = (bank_transaction.get("bank_party_name") or "").strip()
	if not name or not any(marker in name for marker in COMPANY_NAME_MARKERS):
		return {}

	preferred_party_type = _preferred_party_type(bank_transaction)
	party = _resolve_company_party(name, bank_transaction, preferred_party_type)
	if party:
		return party

	is_withdrawal = flt(bank_transaction.get("withdrawal")) > 0
	party_type = preferred_party_type or ("Supplier" if is_withdrawal else "Customer")
	name_field = "supplier_name" if party_type == "Supplier" else "customer_name"
	party_name = frappe.db.get_value(
		party_type, {"disabled": 0, name_field: name}, "name"
	)
	if not party_name:
		party_name = frappe.db.get_value(party_type, {"disabled": 0, "name": name}, "name")
	if party_name:
		return {"party_type": party_type, "party": party_name}

	values = {
		"doctype": party_type,
		name_field: name,
		"customer_type" if party_type == "Customer" else "supplier_type": "Company",
	}
	if party_type == "Customer":
		values.update({"customer_group": "Commercial", "territory": "China"})
	else:
		values["supplier_group"] = "Services"
	party_doc = frappe.get_doc(values)
	party_doc.insert(ignore_permissions=True)
	return {"party_type": party_type, "party": party_doc.name}


def _resolve_account(description, company, reference_number=None, counterparty_name=None):
	"""Resolve high-confidence bank summaries; leave unclassified rows for review."""
	raw_description = str(description or "")
	description = clean_bank_summary(raw_description)
	search_text = f"{description}｜{raw_description}"
	# Finance has confirmed that a combined rent/property/utilities payment is
	# intentionally booked as one line in Management Expense - Other.
	if _has_mixed_facility_terms(search_text):
		account_number = "660299"
	elif "批量代发" in search_text and any(term in search_text for term in ("付费", "费用", "手续费", "服务费")):
		account_number = "660303"
	elif "招聘" in search_text and "备用金" in search_text:
		account_number = "660299"
	elif "公众号注册退款" in search_text or "企业实名验证" in search_text or "银行账户一分钱打款验证" in search_text:
		# 1221 is a group account in the China Finance chart. Validation and
		# refund rows must use its unclassified leaf account instead.
		account_number = "122101"
	elif "个税" in search_text or "个人所得税" in search_text:
		account_number = "222112"
	elif "社保" in search_text:
		account_number = "221103"
	elif "公积金" in search_text or ("补缴" in search_text and "住房公积金" in search_text):
		account_number = "221104"
	elif "实时缴税" in search_text or "缴税" in search_text or "税单" in search_text:
		# The confirmed business rule is to use the chart's generic tax account
		# when the bank reference does not identify a tax type.
		account_number = "222199"
	elif "利息" in search_text or "interest" in search_text.lower():
		# Interest income is represented as a negative debit to the finance expense
		# account. The bank deposit remains a positive debit, so the two lines
		# offset without presenting the finance expense as a credit.
		account_number = "660302"
	elif "工资" in search_text or "代发" in search_text:
		account_number = "221101"
	elif "手续费" in search_text or "服务费" in search_text:
		account_number = "660303"
	elif "投资" in search_text or (
		reference_number == "C0347H30010RMVZ"
		and (counterparty_name or "").strip() == "周悦"
	):
		account_number = "4001"
	elif "办公" in search_text and "报销" in search_text:
		account_number = "660201"
	elif "报销" in search_text and (
		_extract_bank_metadata(raw_description, "对方") or (counterparty_name or "").strip()
	):
		# The CMB statement identifies these rows as employee reimbursements;
		# receipt imports carry the same counterparty in a structured field.
		# The reviewed aaa policy books both sources to Management Expense - Office.
		account_number = "660201"
	elif ("租金" in search_text or "房租" in search_text) and not any(term in search_text for term in ("物业", "水电", "电费")):
		account_number = "660202"
	elif ("水电" in search_text or "电费" in search_text) and "物业" not in search_text and "租金" not in search_text:
		account_number = "660204"
	elif "物业" in search_text and "租金" not in search_text:
		account_number = "660203"
	elif "验证" in search_text or "实名" in search_text:
		account_number = "122101"
	else:
		return None

	return frappe.db.get_value("Account", {"account_number": account_number, "company": company}, "name")


def _has_mixed_facility_terms(description):
	text = description or ""
	categories = (
		any(term in text for term in ("租金", "房租")),
		"物业" in text,
		any(term in text for term in ("水电", "电费")),
	)
	return sum(categories) >= 2


def _get_safe_party_fields(account, bank_transaction):
	"""Carry a bank party only when the target account type accepts it."""
	party_type = bank_transaction.get("party_type")
	party = bank_transaction.get("party")
	if not party_type or not party or not frappe.db.exists(party_type, party):
		return {}
	account_info = frappe.db.get_value(
		"Account", account, ["root_type", "account_type"], as_dict=True
	)
	if not account_info:
		return {}
	allowed = (
		party_type == "Shareholder" and account_info.root_type == "Equity"
	) or (
		party_type == "Supplier" and account_info.account_type == "Payable"
	) or (
		party_type == "Customer" and account_info.account_type == "Receivable"
	) or (
		party_type == "Employee" and account_info.account_type in {"Payable", "Receivable"}
	)
	return {"party_type": party_type, "party": party} if allowed else {}


def _get_valid_bank_account_gl(bank_account, company):
	if not bank_account or not company:
		return None
	account = frappe.db.get_value("Bank Account", bank_account, "account")
	if not account:
		return None
	account_info = frappe.db.get_value(
		"Account", account, ["company", "is_group", "disabled"], as_dict=True
	)
	if (
		not account_info
		or account_info.company != company
		or account_info.is_group
		or account_info.disabled
	):
		frappe.log_error(
			f"Bank account {bank_account} points to an invalid account {account} for company {company}",
			"Bank Account Company Mismatch",
		)
		return None
	return account


def _validate_bank_account_company(bank_account, company):
	account = _get_valid_bank_account_gl(bank_account, company)
	if not account:
		frappe.throw(_("银行账户 {0} 未正确关联公司 {1} 的银行科目").format(bank_account, company))
	return account
