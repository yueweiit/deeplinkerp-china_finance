"""Receipt staging and explicit accounting decisions, separate from statement import."""

from frappe import _


import hashlib
import json
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path

import frappe
from frappe.utils import add_days, cint, getdate, now_datetime

from china_finance.services.bank_receipt_parser import (
	PARSER_VERSION,
	ReceiptParseError,
	get_receipt_parser,
	money,
	parse_cmb_receipts,
)
from china_finance.services.bank_receipt_social import (
	SOCIAL_ITEMS,
	housing_fund_period,
	housing_fund_suggestion,
	is_housing_fund_item,
	is_housing_fund_receipt,
	social_period,
	social_suggestion,
)

IMPORT = "China Bank Receipt Import"
RECEIPT = "China Bank Receipt"
_editing = ContextVar("bank_receipt_service_edit", default=False)


@contextmanager
def internal_edit():
	token = _editing.set(True)
	try:
		yield
	finally:
		_editing.reset(token)


def protect_service_document():
	if not _editing.get():
		frappe.throw(_("回单记录只能通过回单导入的确认操作维护"), frappe.PermissionError)


def validate_import_document(doc):
	doc.currency = "CNY"
	if _editing.get():
		return
	old = doc.get_doc_before_save()
	if old and old.source_hash:
		frappe.throw(_("已识别批次不能直接修改；请通过处理按钮操作，文件或账户错误时作废后新建批次"))
	if doc.rows or doc.source_hash or doc.parser_version or doc.parse_errors or doc.status != "待识别":
		frappe.throw(_("不能手工填写识别结果"))
	if doc.abandon_reason or doc.deposit_total or doc.withdrawal_total:
		frappe.throw(_("不能手工填写回单处理结果"))
	validate_bank_context(doc.company, doc.bank_account)


def _save(doc):
	with internal_edit():
		doc.save(ignore_permissions=True)


def _get_import(name, write=False, lock=False):
	doc = frappe.get_doc(IMPORT, name)
	doc.check_permission("write" if write else "read")
	if write:
		from china_finance.services.month_end import guard_period_write
		guard_period_write(doc)
	if lock:
		doc = frappe.get_doc(IMPORT, name, for_update=True)
		doc.check_permission("write" if write else "read")
	validate_bank_context(doc.company, doc.bank_account)
	return doc


def _get_bank_parser(bank_name):
	"""Resolve a bank parser while keeping the legacy CMB patch point usable."""
	parser = get_receipt_parser(bank_name)
	if parser and parser["parser_key"] == "cmb_text_pdf_v1":
		# ``parse_cmb_receipts`` has historically been imported here and is used
		# as the test/integration replacement point.  Keep that compatibility as
		# the registry grows to include parsers from other modules.
		parser = {**parser, "parse": parse_cmb_receipts}
	return parser


def validate_bank_context(company, bank_account):
	company_doc = frappe.get_doc("Company", company)
	company_doc.check_permission("read")
	bank = frappe.get_doc("Bank Account", bank_account)
	bank.check_permission("read")
	if bank.company != company or not bank.is_company_account or bank.disabled or not bank.account:
		frappe.throw(_("请选择本公司启用且关联银行科目的公司银行账户"))
	parser = _get_bank_parser(bank.bank)
	if not parser:
		frappe.throw(_("暂未配置银行“{0}”的回单格式，请先配置解析器").format(bank.bank or '未填写'))
	account = frappe.get_doc("Account", bank.account)
	if account.company != company or account.is_group or account.disabled or account.account_type != "Bank":
		frappe.throw(_("银行账户必须关联本公司启用的 Bank 明细科目"))
	if (
		company_doc.default_currency != parser["currency"]
		or account.account_currency != parser["currency"]
	):
		frappe.throw(_("当前银行回单解析器仅支持{0}本位币及银行账户").format(parser['currency']))
	if not bank.bank_account_no:
		frappe.throw(_("请先在银行账户填写完整银行账号，用于核对回单本方账号"))
	return bank


def _source(doc):
	# Identical uploads may share a URL but have separate File attachment records.
	file_name = frappe.db.get_value(
		"File",
		{"file_url": doc.source_file, "attached_to_doctype": IMPORT, "attached_to_name": doc.name},
		"name",
	)
	if not file_name:
		file_name = frappe.db.get_value(
			"File",
			{
				"file_url": doc.source_file,
				"attached_to_name": ["is", "not set"],
				"owner": frappe.session.user,
			},
			"name",
		)
	if not file_name:
		frappe.throw(_("未找到回单附件"))
	file = frappe.get_doc("File", file_name)
	file.check_permission("read")
	attached = file.attached_to_doctype == IMPORT and file.attached_to_name == doc.name
	owned_upload = not file.attached_to_name and file.owner == frappe.session.user
	if not file.is_private or not (attached or owned_upload):
		frappe.throw(_("请将私有 PDF 原件上传到当前回单导入批次"))
	if file.is_remote_file:
		frappe.throw(_("请上传本地 PDF 原件，暂不支持远程文件链接"))
	# File.get_content may decode text-compatible PDFs; hash and parse the original bytes.
	return Path(file.get_full_path()).read_bytes()


def protect_receipt_file(doc, method=None):
	"""Keep the original privately accessible after a batch has been parsed."""
	if not frappe.db.exists("DocType", IMPORT):
		return
	old = doc.get_doc_before_save()
	url = old.file_url if old else doc.file_url
	if not url or not frappe.db.exists(
		IMPORT, {"source_file": url, "source_hash": ["is", "set"], "status": ["!=", "已作废"]}
	):
		return
	if method == "on_trash" or (
		old
		and any(
			doc.get(f) != old.get(f)
			for f in ("file_url", "is_private", "attached_to_doctype", "attached_to_name")
		)
	):
		frappe.throw(_("该原件已用于银行回单核对，不能删除、公开或移动；错误批次请先作废"))


def allow_voucher_cancellation(doc, method=None):
	# A receipt is evidence, retained across cancellation. Normal accounting and
	# bank-reconciliation cancellation restrictions still run in existing hooks.
	existing = doc.get("ignore_linked_doctypes") or ()
	if isinstance(existing, str):
		existing = (existing,)
	doc.ignore_linked_doctypes = (*existing, RECEIPT)


def normalized_name(value):
	return "".join(unicodedata.normalize("NFKC", value or "").split())


def validate_receipt_context(row, bank, company):
	configured_account = "".join(bank.bank_account_no.split())
	payer_account = "".join(str(row.get("payer_account") or "").split())
	payee_account = "".join(str(row.get("payee_account") or "").split())
	if not row.get("direction"):
		payer_match = payer_account == configured_account
		payee_match = payee_account == configured_account
		if payer_match == payee_match:
			frappe.throw(_("无法根据所选银行账户确定回单收支方向"))
		row["direction"] = "支出" if payer_match else "收入"
		row["own_account"] = payer_account if payer_match else payee_account
		row["own_name"] = row.get("payer") if payer_match else row.get("payee")
		row["counterparty"] = row.get("payee") if payer_match else row.get("payer")
		row["counterparty_account"] = payee_account if payer_match else payer_account
	if row["own_account"] != configured_account:
		frappe.throw(_("回单本方账号与所选银行账户不一致"))
	legal_name = frappe.db.get_value("Company", company, "company_name") or company
	if normalized_name(row["own_name"]) != normalized_name(legal_name):
		frappe.throw(_("回单本方户名与公司名称不一致，请核对公司及银行账户"))
	if row["currency"] != "CNY":
		frappe.throw(_("回单币种与银行账户不一致"))


def receipt_summary(data):
	"""Build a concise accounting summary from structured receipt fields."""
	summary = str(data.get("summary") or "").strip()
	counterparty = str(data.get("counterparty") or "").strip()
	if summary in {"报销", "报销款"} and counterparty:
		return f"{summary}-{counterparty}"
	if data.get("direction") == "支出" and data.get("fee_details"):
		return "支付银行手续费"

	tax_details = data.get("tax_details") or []
	personal_income_tax = tax_details and all(
		str(detail.get("item") or "").strip() == "个人所得税" for detail in tax_details
	)
	housing_fund = is_housing_fund_receipt(data)
	if personal_income_tax or housing_fund:
		try:
			period = (
				social_period(tax_details, data.get("posting_date"))
				if personal_income_tax
				else housing_fund_period(data)
			)
		except (TypeError, ValueError):
			period = None
		if period:
			item = "个人所得税" if personal_income_tax else "公积金"
			return f"支付{period['label']}{item}"
	if housing_fund:
		return "支付公积金"
	return summary


@frappe.whitelist(methods=["POST"])
def parse_import(name):
	doc = _get_import(name, write=True, lock=True)
	if doc.source_hash:
		return preview_import(name)
	bank = validate_bank_context(doc.company, doc.bank_account)
	parser = _get_bank_parser(bank.bank)
	content = _source(doc)
	try:
		result = parser["parse"](content)
	except ReceiptParseError as exc:
		result = {
			"rows": [],
			"errors": [{"page": "-", "message": str(exc)}],
			"parser_version": parser["parser_version"],
		}
	errors = list(result["errors"])
	seen = {}
	for data in result["rows"]:
		previous = seen.setdefault(data["transaction_id"], data)
		if not _equivalent(data, previous):
			errors.append(
				{"page": data["page"], "message": _("同一文件存在同号但金额、日期或收支方向不同的回单")}
			)
		try:
			validate_receipt_context(data, bank, doc.company)
		except frappe.ValidationError as exc:
			errors.append({"page": data["page"], "position": data["position"], "message": str(exc)})
		accounting_summary = receipt_summary(data)
		parsed_row = doc.append(
			"rows",
			{
				**{
					key: data[key]
					for key in (
						"posting_date",
						"transaction_id",
						"receipt_number",
						"direction",
						"currency",
						"amount",
						"counterparty",
						"business_type",
						"position",
					)
				},
				"summary": accounting_summary,
				"page_number": data["page"],
				"raw_data": json.dumps(data, ensure_ascii=False),
				"status": "待处理",
			},
		)
		try:
			existing = _existing_receipt(doc, data)
			if existing and existing.voucher_name:
				parsed_row.receipt, parsed_row.status = existing.name, receipt_status(existing)
		except frappe.ValidationError as exc:
			errors.append({"page": data["page"], "message": str(exc)})
	doc.source_hash = hashlib.sha256(content).hexdigest()
	doc.parser_version = result["parser_version"]
	doc.parse_errors = json.dumps(errors, ensure_ascii=False)
	doc.deposit_total = sum(money(r["amount"]) for r in seen.values() if r["direction"] == "收入")
	doc.withdrawal_total = sum(money(r["amount"]) for r in seen.values() if r["direction"] == "支出")
	doc.status = (
		"识别失败"
		if errors or not doc.rows
		else ("处理完成" if all(r.receipt for r in doc.rows) else "待处理")
	)
	if errors:
		for row in doc.rows:
			row.receipt = None
	_save(doc)
	return preview_import(name)


def identity(bank_account, flow):
	return hashlib.sha256(json.dumps([bank_account, flow], ensure_ascii=False).encode()).hexdigest()


def _equivalent(data, other):
	return all(
		(
			str(data["posting_date"]) == str(other["posting_date"]),
			data["direction"] == other["direction"],
			data["currency"] == other["currency"],
			money(data["amount"]) == money(other["amount"]),
		)
	)


def existing_transaction(bank_account, data, lock=False):
	# A locking/current read sees imports that committed while waiting for the
	# Bank Account lock even with MariaDB's REPEATABLE READ isolation.
	names = frappe.db.sql(
		"""SELECT name FROM `tabBank Transaction`
		WHERE bank_account=%s AND docstatus!=2 AND (transaction_id=%s OR reference_number=%s)"""
		+ (" FOR UPDATE" if lock else ""),
		(bank_account, data["transaction_id"], data["transaction_id"]),
		pluck=True,
	)
	if len(names) > 1:
		frappe.throw(_("同一流水号存在多条银行交易，请先核对重复数据"))
	if not names:
		return None
	bt = frappe.get_doc("Bank Transaction", names[0], for_update=lock)
	bt.check_permission("read")
	other = {
		"posting_date": bt.date,
		"direction": "支出" if bt.withdrawal else "收入",
		"currency": bt.currency,
		"amount": bt.withdrawal or bt.deposit,
	}
	if (bt.withdrawal and bt.deposit) or not _equivalent(data, other):
		frappe.throw(_("银行交易 {0} 与回单同号但日期、金额、方向或币种不一致，已停止处理").format(bt.name))
	return bt


def _existing_receipt(doc, data, lock=False):
	key = identity(doc.bank_account, data["transaction_id"])
	if not frappe.db.get_value(RECEIPT, key, "name", for_update=lock):
		return None
	receipt = frappe.get_doc(RECEIPT, key, for_update=lock)
	if receipt.company != doc.company or not _equivalent(data, receipt.as_dict()):
		frappe.throw(_("该流水号已有回单，但关键数据冲突，不能覆盖"))
	return receipt


def suggest_account(company, data):
	text = " ".join(
		[
			data["summary"],
			data["counterparty"],
			json.dumps(data["fee_details"] + data["tax_details"], ensure_ascii=False),
		]
	)
	rules = frappe.get_all(
		"China Bank Receipt Rule",
		filters={"company": company, "enabled": 1, "direction": data["direction"]},
		fields=[
			"name",
			"priority",
			"business_type",
			"keyword",
			"account",
			"notes",
			"rule_type",
			"personal_account",
			"accrual_account",
			"effective_from",
			"effective_to",
			"housing_fund_company_percent",
			*SOCIAL_ITEMS.values(),
		],
		order_by="priority, name",
	)
	matches = [
		r
		for r in rules
		if (not r.business_type or r.business_type == data["business_type"])
		and (not r.keyword or r.keyword in text)
		and (not r.get("effective_from") or getdate(data["posting_date"]) >= getdate(r.effective_from))
		and (not r.get("effective_to") or getdate(data["posting_date"]) <= getdate(r.effective_to))
		and (
			r.get("rule_type") != "社保分摊"
			or (data["tax_details"] and all(t.get("item") in SOCIAL_ITEMS for t in data["tax_details"]))
		)
		and (
			r.get("rule_type") != "公积金分摊"
			or is_housing_fund_receipt(data)
		)
	]
	if matches:
		best = [r for r in matches if r.priority == matches[0].priority]
		if (
			len(
				{
					(
						r.account,
						r.get("rule_type"),
						r.get("personal_account"),
						r.get("accrual_account"),
						r.get("housing_fund_company_percent"),
						*(r.get(f) for f in SOCIAL_ITEMS.values()),
					)
					for r in best
				}
			)
			!= 1
		):
			return {"account": None, "reason": "同优先级规则冲突，需人工选择"}
		if best[0].get("rule_type") == "社保分摊":
			return social_suggestion(best[0], data)
		if best[0].get("rule_type") == "公积金分摊":
			return housing_fund_suggestion(best[0], data)
		return {
			"account": best[0].account,
			"reason": "公司规则：" + (best[0].notes or best[0].name),
			"rule": best[0].name,
		}
	code, reason = None, "回单不足以确定会计科目，请结合业务单据选择"
	if data["direction"] == "支出":
		if data["business_type"] in ("企业银行收费", "代发付费") or data["fee_details"]:
			code, reason = "660303", "回单明确为银行收费；优先于摘要中的工资关键词"
		elif data["tax_details"] and all(r["item"] == "个人所得税" for r in data["tax_details"]):
			code, reason = "222112", "税费明细明确为个人所得税，请核对已计提余额"
		elif "工资" in data["summary"] or data["business_type"] == "自助代发付款":
			code, reason = "221101", "工资支付建议；请核对已计提及已付款记录"
		elif data["tax_details"] and all(is_housing_fund_item(r.get("item")) for r in data["tax_details"]):
			reason = "公积金明细已识别，请先设置该公司的公积金分摊规则"
		elif is_housing_fund_receipt(data):
			code, reason = "221104", "公积金支付建议；个人与公司承担金额需结合业务明细核对"
		elif data["tax_details"] and all("保险费" in r["item"] for r in data["tax_details"]):
			reason = "社保明细已识别，个人及公司承担部分需核对，不能自动按单一费用入账"
	elif "利息" in data["summary"]:
		code, reason = "660302", "利息收入按财务费用负借方冲减"
	account = (
		frappe.db.get_value(
			"Account", {"company": company, "account_number": code, "is_group": 0, "disabled": 0}, "name"
		)
		if code
		else None
	)
	if code or (
		data["tax_details"]
		and (
			all("保险费" in r["item"] for r in data["tax_details"])
			or all(is_housing_fund_item(r.get("item")) for r in data["tax_details"])
		)
	):
		return {"account": account, "reason": reason}

	# Use the same reviewed summary mapping as the XLSX bank-statement flow for
	# ordinary receipts. Structured receipt rules above remain authoritative for
	# fees, payroll, taxes and social-insurance allocations.
	from china_finance.services.bank_reconciliation import _resolve_account

	account = _resolve_account(
		data["summary"],
		company,
		reference_number=data["transaction_id"],
		counterparty_name=data["counterparty"],
	)
	return {
		"account": account,
		"reason": (
			"按银行流水摘要科目规则自动匹配；生成草稿后可在查凭证修改"
			if account
			else reason
		),
	}


def voucher_bank_amount(voucher, bank):
	if voucher.company != bank.company or voucher.docstatus == 2:
		frappe.throw(_("凭证公司不一致或凭证已取消"))
	if voucher.doctype == "Journal Entry":
		rows = [r for r in voucher.accounts if r.account == bank.account]
		if len(rows) != 1 or rows[0].account_currency != "CNY":
			frappe.throw(_("第一期关联要求凭证中恰有一行人民币本银行科目"))
		return Decimal(str(rows[0].debit_in_account_currency or 0)) - Decimal(
			str(rows[0].credit_in_account_currency or 0)
		)
	if voucher.doctype == "Payment Entry":
		if voucher.docstatus != 1 or voucher.payment_type == "Internal Transfer":
			frappe.throw(_("付款单须已提交；内部转账请人工对账"))
		if voucher.paid_from == bank.account and voucher.paid_from_account_currency == "CNY":
			return -Decimal(str(voucher.paid_amount))
		if voucher.paid_to == bank.account and voucher.paid_to_account_currency == "CNY":
			return Decimal(str(voucher.received_amount))
	frappe.throw(_("凭证没有匹配的人民币银行分录"))


def _validate_voucher(doctype, name, data, bank):
	if doctype not in ("Journal Entry", "Payment Entry") or not name:
		frappe.throw(_("请选择记账凭证或已提交付款单"))
	voucher = frappe.get_doc(doctype, name, for_update=True)
	voucher.check_permission("read")
	expected = money(data["amount"]) * (-1 if data["direction"] == "支出" else 1)
	if voucher_bank_amount(voucher, bank) != expected:
		frappe.throw(_("凭证的银行金额或收支方向与回单不一致，不可关联"))
	return voucher


def validate_linked_voucher(doc, method=None):
	"""Edits must not silently invalidate an already confirmed receipt match."""
	if doc.docstatus == 2 or not frappe.db.exists("DocType", RECEIPT):
		return
	for receipt in frappe.get_all(
		RECEIPT,
		filters={"voucher_type": doc.doctype, "voucher_name": doc.name},
		fields=["bank_account", "direction", "amount"],
	):
		bank = frappe.get_doc("Bank Account", receipt.bank_account)
		expected = money(receipt.amount) * (-1 if receipt.direction == "支出" else 1)
		if voucher_bank_amount(doc, bank) != expected:
			frappe.throw(_("凭证已关联银行回单，银行金额或方向不能与原回单不一致"))


def prepare_voucher_delete(doc, method=None):
	"""Detach a deleted draft while retaining its receipt and bank transaction."""
	if doc.doctype != "Journal Entry" or not frappe.db.exists("DocType", RECEIPT):
		return
	receipt_names = frappe.get_all(
		RECEIPT,
		filters={"voucher_type": doc.doctype, "voucher_name": doc.name},
		pluck="name",
	)
	bank_transaction_name = doc.get("custom_china_bank_transaction")
	if not receipt_names and not bank_transaction_name:
		return
	if doc.docstatus != 0:
		frappe.throw(_("只有未记账草稿可以直接删除；已记账或已取消凭证必须保留审计记录"))

	parents = set()
	for name in receipt_names:
		receipt = frappe.get_doc(RECEIPT, name, for_update=True)
		if (receipt.voucher_type, receipt.voucher_name) != (doc.doctype, doc.name):
			continue
		receipt.voucher_type = None
		receipt.voucher_name = None
		receipt.status = "待处理"
		receipt.process_note = f"未记账草稿 {doc.name} 已删除，可重新生成或关联凭证"
		receipt.processed_by = frappe.session.user
		receipt.processed_on = now_datetime()
		_save(receipt)
		for row in frappe.get_all(
			"China Bank Receipt Import Row",
			filters={"receipt": name},
			fields=["name", "parent"],
		):
			parents.add(row.parent)
			frappe.db.set_value(
				"China Bank Receipt Import Row",
				row.name,
				{
					"receipt": None,
					"status": "待处理",
					"message": _("原草稿 {0} 已删除，可重新生成").format(doc.name),
				},
				update_modified=False,
			)

	if bank_transaction_name and frappe.db.exists("Bank Transaction", bank_transaction_name):
		bank_transaction = frappe.get_doc("Bank Transaction", bank_transaction_name, for_update=True)
		if bank_transaction.get("custom_china_journal_entry") == doc.name:
			if any(row.allocated_amount for row in bank_transaction.payment_entries):
				frappe.throw(_("该草稿对应的银行交易已有核销记录，不能直接删除"))
			frappe.db.set_value(
				"Bank Transaction",
				bank_transaction.name,
				"custom_china_journal_entry",
				None,
				update_modified=False,
			)

	for parent in parents:
		status = frappe.db.get_value(IMPORT, parent, "status")
		if status in ("已作废", "识别失败"):
			continue
		remaining = frappe.db.count(
			"China Bank Receipt Import Row",
			{"parent": parent, "receipt": ["is", "set"]},
		)
		frappe.db.set_value(
			IMPORT,
			parent,
			"status",
			"部分处理" if remaining else "待处理",
			update_modified=False,
		)


@frappe.whitelist(methods=["POST"])
def delete_draft_vouchers(names):
	"""Delete selected Journal Entry drafts and restore receipt rows for retry."""
	if isinstance(names, str):
		names = frappe.parse_json(names)
	if not isinstance(names, list) or not 1 <= len(names) <= 300:
		frappe.throw(_("请选择 1 至 300 张凭证草稿"))

	# Keep the response order stable while avoiding duplicate delete requests.
	names = list(dict.fromkeys(str(name) for name in names if name))
	if not names:
		frappe.throw(_("请选择要删除的凭证草稿"))
	if not frappe.has_permission("Journal Entry", "delete"):
		frappe.throw(_("当前用户没有删除记账凭证的权限"), frappe.PermissionError)

	deleted, failed = [], []
	for index, name in enumerate(names):
		point = f"delete_voucher_{index}"
		frappe.db.savepoint(point)
		try:
			if not frappe.db.exists("Journal Entry", name):
				frappe.throw(_("凭证 {0} 不存在").format(name))
			voucher = frappe.get_doc("Journal Entry", name)
			voucher.check_permission("delete")
			if voucher.docstatus != 0:
				frappe.throw(_("凭证 {0} 不是未记账草稿，不能直接删除").format(name))
			frappe.delete_doc("Journal Entry", name, ignore_missing=False)
			deleted.append(name)
		except Exception as exc:
			frappe.db.rollback(save_point=point)
			failed.append({"name": name, "error": str(exc)})

	return {"deleted": deleted, "failed": failed, "deleted_count": len(deleted), "failed_count": len(failed)}


@frappe.whitelist(methods=["POST"])
def refresh_import_draft_summaries(name):
	"""Apply structured summaries to untouched drafts from an existing batch."""
	from china_finance.services.bank_reconciliation import _apply_journal_entry_summary

	doc = _get_import(name, write=True, lock=True)
	updated, skipped, seen = [], [], set()
	for row in doc.rows:
		if not row.receipt or row.receipt in seen:
			continue
		seen.add(row.receipt)
		receipt = frappe.get_doc(RECEIPT, row.receipt, for_update=True)
		if receipt.voucher_type != "Journal Entry" or not receipt.voucher_name:
			continue
		if frappe.db.get_value("Journal Entry", receipt.voucher_name, "docstatus") != 0:
			skipped.append({"voucher": receipt.voucher_name, "reason": "凭证已经记账或取消"})
			continue
		try:
			data = json.loads(receipt.raw_data or row.raw_data)
		except (TypeError, ValueError, json.JSONDecodeError):
			skipped.append({"voucher": receipt.voucher_name, "reason": "回单结构化数据无效"})
			continue
		source_summary = str(data.get("source_summary") or data.get("summary") or "").strip()
		target_summary = receipt_summary(data)
		if not target_summary or target_summary == source_summary:
			continue

		voucher = frappe.get_doc("Journal Entry", receipt.voucher_name)
		voucher.check_permission("write")
		current_summaries = {
			str(value).strip()
			for value in [
				voucher.get("remark"),
				voucher.get("user_remark"),
				*(entry.get("user_remark") for entry in voucher.accounts),
			]
			if str(value or "").strip()
		}
		if current_summaries and current_summaries <= {target_summary}:
			continue
		if current_summaries - {source_summary, target_summary}:
			skipped.append({"voucher": voucher.name, "reason": "摘要已经人工修改"})
			continue

		_apply_journal_entry_summary(voucher, target_summary)
		voucher.save()
		data["source_summary"], data["summary"] = source_summary, target_summary
		receipt.summary = target_summary
		receipt.raw_data = json.dumps(data, ensure_ascii=False)
		receipt.process_note = f"未记账草稿摘要已更新为：{target_summary}"
		receipt.processed_by, receipt.processed_on = frappe.session.user, now_datetime()
		_save(receipt)
		if receipt.bank_transaction and frappe.db.has_column("Bank Transaction", "custom_summary"):
			frappe.db.set_value(
				"Bank Transaction",
				receipt.bank_transaction,
				"custom_summary",
				target_summary,
				update_modified=False,
			)
		for same in doc.rows:
			if same.receipt == receipt.name:
				same.summary = target_summary
		updated.append({"voucher": voucher.name, "summary": target_summary})

	if updated:
		_save(doc)
	return {"updated": updated, "skipped": skipped, "updated_count": len(updated), "skipped_count": len(skipped)}


def voucher_candidates(doc, data, bank):
	# Candidates are suggestions only; a user must confirm which business they represent.
	amount = float(money(data["amount"]))
	signed = -amount if data["direction"] == "支出" else amount
	start, end = add_days(data["posting_date"], -7), add_days(data["posting_date"], 7)
	je_names = frappe.db.sql(
		"""SELECT DISTINCT je.name FROM `tabJournal Entry` je
		JOIN `tabJournal Entry Account` a ON a.parent=je.name AND a.parenttype='Journal Entry'
		WHERE je.company=%s AND je.docstatus!=2 AND a.account=%s
		AND a.debit_in_account_currency-a.credit_in_account_currency=%s
		AND (je.posting_date BETWEEN %s AND %s OR je.cheque_no=%s)
		ORDER BY je.posting_date, je.name LIMIT 51""",
		(doc.company, bank.account, signed, start, end, data["transaction_id"]),
		pluck=True,
	)
	pe_names = frappe.db.sql(
		"""SELECT name FROM `tabPayment Entry` WHERE company=%s AND docstatus=1
		AND ((paid_from=%s AND paid_amount=%s AND %s='支出') OR (paid_to=%s AND received_amount=%s AND %s='收入'))
		AND (posting_date BETWEEN %s AND %s OR reference_no=%s) ORDER BY posting_date,name LIMIT 51""",
		(
			doc.company,
			bank.account,
			amount,
			data["direction"],
			bank.account,
			amount,
			data["direction"],
			start,
			end,
			data["transaction_id"],
		),
		pluck=True,
	)
	result = []
	for doctype, names in (("Journal Entry", je_names), ("Payment Entry", pe_names)):
		for name in names:
			voucher = frappe.get_doc(doctype, name)
			if not voucher.has_permission("read"):
				# Never allow generating a duplicate just because a candidate is hidden.
				result.append(
					{
						"restricted": True,
						"blocking": True,
						"description": _("存在无权查看的疑似凭证，请财务管理员核对"),
					}
				)
				continue
			reference = (
				voucher.get("cheque_no")
				if doctype == "Journal Entry"
				else voucher.get("reference_no")
			)
			exact_reference = str(reference or "").strip() == str(data["transaction_id"]).strip()
			result.append(
				{
					"doctype": doctype,
					"name": name,
					"posting_date": str(voucher.posting_date),
					"docstatus": voucher.docstatus,
					"summary": voucher.get("user_remark") or voucher.get("remarks") or "",
					"match_type": "交易流水号" if exact_reference else "日期、金额和收支",
					"blocking": exact_reference,
				}
			)
	return result


def has_blocking_voucher_candidates(candidates):
	return any(candidate.get("blocking", True) for candidate in candidates or [])


def receipt_status(receipt):
	if not receipt.voucher_name:
		return "待处理"
	state = frappe.db.get_value(
		receipt.voucher_type, receipt.voucher_name, "docstatus", for_update=bool(receipt.flags.for_update)
	)
	if state is None or state == 2:
		return "凭证已取消或缺失"
	if state == 0:
		return "凭证待记账"
	if receipt.bank_transaction:
		bt = frappe.get_doc(
			"Bank Transaction", receipt.bank_transaction, for_update=bool(receipt.flags.for_update)
		)
		if bt.docstatus != 1:
			return "银行交易待核对"
		allocated = sum(
			Decimal(str(r.allocated_amount or 0))
			for r in bt.payment_entries
			if r.payment_document == receipt.voucher_type and r.payment_entry == receipt.voucher_name
		)
		if allocated == money(receipt.amount):
			return "已核销"
		return "已关联待核销"
	return "已补回单"


@frappe.whitelist()
def preview_import(name):
	doc = _get_import(name)
	bank = validate_bank_context(doc.company, doc.bank_account)
	rows = []
	for row in doc.rows:
		data = json.loads(row.raw_data)
		data["summary"] = receipt_summary(data)
		entry = {**row.as_dict(), "summary": data["summary"], "suggestion": suggest_account(doc.company, data), "candidates": []}
		try:
			receipt = _existing_receipt(doc, data)
			bt = existing_transaction(doc.bank_account, data)
			entry["bank_transaction"] = bt.name if bt else None
			entry["receipt_record"] = receipt.name if receipt else None
			entry["receipt"] = receipt.name if receipt and receipt.voucher_name else None
			if receipt and receipt.voucher_name:
				entry.update(
					status=receipt_status(receipt),
					voucher_type=receipt.voucher_type,
					voucher_name=receipt.voucher_name,
				)
			else:
				entry["candidates"] = voucher_candidates(doc, data, bank)
				entry["status"] = (
					"待关联确认"
					if has_blocking_voucher_candidates(entry["candidates"])
					else ("待分类" if not entry["suggestion"].get("account") else "待处理")
				)
				if row.status == "处理失败":
					# A previous attempt may have failed only because the old
					# date/amount candidate was blocking. Once it is a warning and
					# the account is known, let the batch retry without stale status.
					if has_blocking_voucher_candidates(entry["candidates"]) or not (
						entry["suggestion"].get("account") or entry["suggestion"].get("allocations")
					):
						entry["status"] = row.status
					else:
						entry["status"], entry["message"] = "待处理", None
				if bt:
					linked = [
						(r.payment_document, r.payment_entry)
						for r in bt.payment_entries
						if r.allocated_amount
					]
					if bt.get("custom_china_journal_entry"):
						linked.append(("Journal Entry", bt.custom_china_journal_entry))
					for dt, dn in linked:
						if not any(
							c.get("name") == dn and c.get("doctype") == dt for c in entry["candidates"]
						):
							v = frappe.get_doc(dt, dn)
							entry["candidates"].append(
								{"doctype": dt, "name": dn, "blocking": True, "match_type": "银行交易已关联"}
								if v.has_permission("read")
								else {"restricted": True, "blocking": True}
							)
		except frappe.ValidationError as exc:
			entry.update(status="冲突", message=str(exc))
		rows.append(entry)
	return {
		"name": doc.name,
		"mode": doc.mode,
		"status": doc.status,
		"source_file": doc.source_file,
		"company": doc.company,
		"bank_account": doc.bank_account,
		"bank_gl_account": bank.account,
		"deposit_total": doc.deposit_total,
		"withdrawal_total": doc.withdrawal_total,
		"transaction_count": len({row.transaction_id for row in doc.rows}),
		"errors": json.loads(doc.parse_errors or "[]"),
		"rows": rows,
	}


def _bank_transaction(doc, data, bank, bt):
	if bt and bt.docstatus != 1:
		frappe.throw(_("已有银行交易尚未提交，请先核对处理"))
	if bt:
		return bt
	# Permissions are checked by insert/submit; do not inherit the legacy ignore_permissions path.
	bt = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"company": doc.company,
			"bank_account": bank.name,
			"date": data["posting_date"],
			"currency": data["currency"],
			"transaction_id": data["transaction_id"],
			"reference_number": data["transaction_id"],
			"description": data["summary"],
			"transaction_type": data["business_type"],
			"bank_party_name": data["counterparty"],
			"bank_party_account_number": data["counterparty_account"],
			"withdrawal": float(money(data["amount"])) if data["direction"] == "支出" else 0,
			"deposit": float(money(data["amount"])) if data["direction"] == "收入" else 0,
		}
	)
	bt.flags.china_receipt_explicit_voucher = True
	bt.insert()
	bt.submit()
	return bt


def _create_voucher(
	doc, data, bank, bt, account, party_type=None, party=None, allocations=None, allocation_plan=None
):
	from china_finance.services.bank_reconciliation import (
		_apply_journal_entry_summary,
		_restore_interest_offset_debit,
	)

	bt.check_permission("write")
	amount = float(money(data["amount"]))
	withdrawal = data["direction"] == "支出"
	allocations = allocations or [{"account": account, "amount": data["amount"]}]
	if sum(money(line["amount"]) for line in allocations) != money(data["amount"]):
		frappe.throw(_("分录合计与银行交易金额不一致"))
	journal_lines = (
		allocation_plan["journal_lines"]
		if allocation_plan
		else [
			{
				"account": line["account"],
				"debit": line["amount"] if withdrawal else "0.00",
				"credit": "0.00" if withdrawal else line["amount"],
				"summary": data["summary"],
			}
			for line in allocations
		]
	)
	if sum(money(line["debit"]) - money(line["credit"]) for line in journal_lines) != money(
		data["amount"]
	) * (1 if withdrawal else -1):
		frappe.throw(_("计提及支付分录与银行交易金额不一致"))
	accounts = {}
	party_not_required = False
	for line in journal_lines:
		account = line["account"]
		if not account or account == bank.account:
			frappe.throw(_("请选择不同于银行科目的对方明细科目"))
		a = frappe.get_doc("Account", account)
		a.check_permission("read")
		accounts[account] = a
		if a.company != doc.company or a.disabled or a.is_group or a.account_currency != "CNY":
			frappe.throw(_("请选择本公司启用的人民币明细科目"))
		if a.account_type == "Bank":
			frappe.throw(_("银行内部转账请手工制证并核对两侧流水后关联回单"))
		# Match the bank-statement flow for the unclassified validation/refund
		# account. It intentionally carries no invented customer or supplier.
		line_party_not_required = a.account_number == "122101"
		party_not_required = party_not_required or line_party_not_required
		if a.account_type in ("Receivable", "Payable") and not (party_type and party) and not line_party_not_required:
			frappe.throw(_("往来科目必须选择真实往来单位；复杂拆分请手工制证后关联"))
	if party_type or party:
		if party_type not in ("Customer", "Supplier", "Employee", "Shareholder") or not party:
			frappe.throw(_("往来单位类型或名称不完整"))
		frappe.get_doc(party_type, party).check_permission("read")
	je = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"company": doc.company,
			"posting_date": data["posting_date"],
			"voucher_type": "Journal Entry",
			"cheque_no": data["transaction_id"],
			"cheque_date": data["posting_date"],
			"custom_china_bank_transaction": bt.name,
		}
	)
	if party_not_required:
		je.party_not_required = 1
	cost_center = frappe.db.get_value("Company", doc.company, "cost_center")
	bank_line = {
		"account": bank.account,
		"account_currency": "CNY",
		"exchange_rate": 1,
		"debit": 0 if withdrawal else amount,
		"credit": amount if withdrawal else 0,
		"debit_in_account_currency": 0 if withdrawal else amount,
		"credit_in_account_currency": amount if withdrawal else 0,
		"summary": allocation_plan["bank_summary"] if allocation_plan else data["summary"],
	}
	# Ordinary vouchers follow the accounting display convention: debit rows first.
	# Allocation plans keep their business sequence (accrual first, payment second).
	if not allocation_plan and not withdrawal:
		je.append("accounts", {k: v for k, v in bank_line.items() if k != "summary"})
	for line in journal_lines:
		debit, credit = float(money(line["debit"])), float(money(line["credit"]))
		if not debit and not credit:
			continue
		je.append(
			"accounts",
			{
				"account": line["account"],
				"account_currency": "CNY",
				"exchange_rate": 1,
				"cost_center": cost_center,
				"party_type": party_type,
				"party": party,
				"debit": debit,
				"credit": credit,
				"debit_in_account_currency": debit,
				"credit_in_account_currency": credit,
			},
		)
		if not withdrawal and accounts[line["account"]].account_number == "660302":
			_restore_interest_offset_debit(je, line["account"])
	if allocation_plan or withdrawal:
		je.append("accounts", {k: v for k, v in bank_line.items() if k != "summary"})
	_apply_journal_entry_summary(
		je, allocation_plan["voucher_summary"] if allocation_plan else data["summary"]
	)
	if allocation_plan:
		active_lines = [line for line in journal_lines if money(line["debit"]) or money(line["credit"])]
		for row, line in zip(je.accounts, [*active_lines, bank_line], strict=True):
			row.user_remark = line["summary"]
	je.insert()
	bank_transaction_updates = {"custom_china_journal_entry": je.name}
	if frappe.db.has_column("Bank Transaction", "custom_summary"):
		bank_transaction_updates["custom_summary"] = bank_line["summary"]
	frappe.db.set_value(
		"Bank Transaction", bt.name, bank_transaction_updates, update_modified=False
	)
	return je


def _check_bt_links(bt, voucher=None):
	if not bt:
		return
	for row in bt.payment_entries:
		if row.allocated_amount and (
			not voucher or (row.payment_document, row.payment_entry) != (voucher.doctype, voucher.name)
		):
			frappe.throw(_("银行交易已核销其他凭证，请先核对，不能重复制证或关联"))
	linked = bt.get("custom_china_journal_entry")
	if linked and (not voucher or voucher.doctype != "Journal Entry" or linked != voucher.name):
		frappe.throw(_("银行交易已有关联凭证，请选择原凭证，或先完成原流程的撤销核对"))


def _process_receipt(
	name,
	row_name,
	action,
	account=None,
	voucher_type=None,
	voucher_name=None,
	party_type=None,
	party=None,
	create_bank_transaction=0,
	confirmed=0,
	notes=None,
	social_not_accrued=0,
	decision_hash=None,
	allocation_not_accrued=0,
):
	if not cint(confirmed):
		frappe.throw(_("请先核对回单、业务依据及重复记账情况并确认"))
	doc = _get_import(name, write=True, lock=True)
	if not doc.source_hash or doc.status in ("识别失败", "已作废"):
		frappe.throw(_("该批次尚未成功识别或已作废"))
	if hashlib.sha256(_source(doc)).hexdigest() != doc.source_hash:
		frappe.throw(_("原件内容已变化，请作废批次后重新上传"))
	row = next((r for r in doc.rows if r.name == row_name), None)
	if not row:
		frappe.throw(_("该回单不属于当前批次"))
	bank = validate_bank_context(doc.company, doc.bank_account)
	# Account row lock is shared with legacy imports; it survives until request commit.
	frappe.db.sql("SELECT name FROM `tabBank Account` WHERE name=%s FOR UPDATE", bank.name)
	data = json.loads(row.raw_data)
	source_summary = str(data.get("source_summary") or data.get("summary") or "").strip()
	data["summary"] = receipt_summary(data)
	if data["summary"] != source_summary:
		data["source_summary"] = source_summary
	row.summary = data["summary"]
	validate_receipt_context(data, bank, doc.company)
	bt = existing_transaction(bank.name, data, lock=True)
	receipt = _existing_receipt(doc, data, lock=True)
	if receipt and receipt.voucher_name:
		if receipt_status(receipt) == "凭证已取消或缺失":
			if action != "link" or not (notes or "").strip():
				frappe.throw(_("原凭证已取消或缺失，请选择修订后的凭证并填写重新关联说明，禁止自动重复制证"))
		else:
			for same in doc.rows:
				if same.transaction_id == data["transaction_id"]:
					same.receipt, same.status = receipt.name, receipt_status(receipt)
					same.message = "复用已有回单及凭证关联"
			doc.status = "处理完成" if all(r.receipt for r in doc.rows) else "部分处理"
			_save(doc)
			return {"receipt": receipt.name, "status": row.status, "reused": True}
	allocation_action = action in ("create_social", "create_allocation")
	if action == "create" or allocation_action:
		if doc.mode != "新业务制证":
			frappe.throw(_("历史补回单模式不允许生成新凭证"))
		_check_bt_links(bt)
		if has_blocking_voucher_candidates(voucher_candidates(doc, data, bank)):
			frappe.throw(_("存在疑似已有凭证，请先关联核对；若均非本笔业务，请手工制证后关联"))
		bt = _bank_transaction(doc, data, bank, bt)
		decision = suggest_account(doc.company, data)
		if decision.get("blocked"):
			frappe.throw(decision["reason"])
		allocations = None
		if allocation_action:
			allocation_label = "公积金" if decision.get("rule_type") == "公积金分摊" else "社保"
			not_accrued = cint(allocation_not_accrued) or cint(social_not_accrued)
			if not not_accrued or not decision.get("allocations"):
				frappe.throw(_("{0}拆分需有效的公司分摊规则，并确认该所属期尚未计提").format(allocation_label))
			if decision_hash != decision.get("decision_hash"):
				frappe.throw(_("{0}分摊规则已变化，请刷新预览重新核对金额").format(allocation_label))
			allocations = decision["allocations"]
		elif decision.get("allocations"):
			frappe.throw(_("请按分摊明细核对并确认，不能忽略个人承担部分"))
		voucher = _create_voucher(
			doc,
			data,
			bank,
			bt,
			account,
			party_type,
			party,
			allocations,
			allocation_plan=decision if allocation_action else None,
		)
		data["accounting_decision"] = {
			"rule": decision.get("rule"),
			"rule_type": decision.get("rule_type"),
			"allocations": allocations or [{"account": account, "amount": data["amount"]}],
			"breakdown": decision.get("breakdown"),
			"allocation_not_accrued": bool(
				cint(allocation_not_accrued) or cint(social_not_accrued)
			),
			"social_not_accrued": bool(cint(social_not_accrued)),
			"coverage_period": decision.get("coverage_period") or decision.get("social_period"),
			"social_period": decision.get("social_period"),
			"journal_lines": decision.get("journal_lines") if allocation_action else None,
			"bank_summary": decision.get("bank_summary") if allocation_action else None,
			"bank_gl_account": bank.account,
			"bank_amount": data["amount"],
		}
	elif action == "link":
		voucher = _validate_voucher(voucher_type, voucher_name, data, bank)
		if receipt and receipt.voucher_name:
			ancestor = voucher.get("amended_from")
			seen = set()
			while ancestor and ancestor != receipt.voucher_name and ancestor not in seen:
				seen.add(ancestor)
				ancestor = frappe.db.get_value(voucher.doctype, ancestor, "amended_from")
			if voucher.doctype != receipt.voucher_type or ancestor != receipt.voucher_name:
				frappe.throw(_("重新关联仅支持原凭证修订链上的凭证，请先完成原凭证的修订"))
		if frappe.db.get_value(
			RECEIPT,
			{
				"bank_account": bank.name,
				"voucher_type": voucher.doctype,
				"voucher_name": voucher.name,
				"identity_key": ["!=", identity(bank.name, data["transaction_id"])],
			},
			"name",
			for_update=True,
		):
			frappe.throw(_("该凭证已关联另一交易回单；第一期只支持一笔银行交易对应一张凭证"))
		_check_bt_links(bt, voucher)
		if cint(create_bank_transaction):
			bt = _bank_transaction(doc, data, bank, bt)
	else:
		frappe.throw(_("未知处理方式"))
	# Failed requests roll back the transaction, including newly inserted bank entries/drafts.
	if not receipt:
		receipt = frappe.get_doc(
			{
				"doctype": RECEIPT,
				"identity_key": identity(bank.name, data["transaction_id"]),
				"company": doc.company,
				"bank_account": bank.name,
				"source_file": doc.source_file,
				"source_hash": doc.source_hash,
				"parser_version": doc.parser_version,
				**{
					key: row.get(key)
					for key in (
						"posting_date",
						"transaction_id",
						"receipt_number",
						"direction",
						"currency",
						"amount",
						"summary",
						"counterparty",
						"business_type",
						"page_number",
						"position",
						"raw_data",
					)
				},
			}
		)
	if data.get("accounting_decision"):
		receipt.raw_data = json.dumps(data, ensure_ascii=False)
	receipt.summary = data["summary"]
	receipt.bank_transaction = bt.name if bt else None
	receipt.voucher_type, receipt.voucher_name = voucher.doctype, voucher.name
	receipt.process_note = notes or (
		"确认生成凭证草稿"
		if action in ("create", "create_social", "create_allocation")
		else "确认关联已有凭证"
	)
	receipt.processed_by, receipt.processed_on = frappe.session.user, now_datetime()
	receipt.status = receipt_status(receipt)
	_save(receipt)
	for same in doc.rows:
		if same.transaction_id == data["transaction_id"]:
			same.receipt, same.status, same.message = receipt.name, receipt.status, receipt.process_note
	doc.status = "处理完成" if all(r.receipt for r in doc.rows) else "部分处理"
	_save(doc)
	return {
		"receipt": receipt.name,
		"status": receipt.status,
		"voucher_type": voucher.doctype,
		"voucher_name": voucher.name,
		"bank_transaction": receipt.bank_transaction,
	}


@frappe.whitelist(methods=["POST"])
def process_receipt(
	name,
	row_name,
	action,
	account=None,
	voucher_type=None,
	voucher_name=None,
	party_type=None,
	party=None,
	create_bank_transaction=0,
	confirmed=0,
	notes=None,
	social_not_accrued=0,
	decision_hash=None,
	allocation_not_accrued=0,
):
	_get_import(name, write=True)
	point = "receipt_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(point)
	try:
		return _process_receipt(
			name,
			row_name,
			action,
			account,
			voucher_type,
			voucher_name,
			party_type,
			party,
			create_bank_transaction,
			confirmed,
			notes,
			social_not_accrued,
			decision_hash,
			allocation_not_accrued,
		)
	except frappe.ValidationError as exc:
		frappe.db.rollback(save_point=point)
		doc = _get_import(name, write=True, lock=True)
		row = next((r for r in doc.rows if r.name == row_name), None)
		if row and doc.status not in ("已作废", "识别失败"):
			row.status, row.message = "处理失败", str(exc)
			_save(doc)
		return {"error": str(exc)}


@frappe.whitelist(methods=["POST"])
def process_import_batch(name, rows=None, confirmed=0):
	"""Create all unambiguous drafts with one batch confirmation.

	An explicit row list remains supported for older callers. The current UI
	omits it so every row is considered and exceptions remain visible.
	"""
	if not cint(confirmed):
		frappe.throw(_("请确认生成本批回单的记账凭证草稿"))
	doc = _get_import(name, write=True)
	from china_finance.services.month_end import guard_period_write
	guard_period_write(doc)
	if doc.mode != "新业务制证":
		frappe.throw(_("历史补回单模式请关联已有凭证"))
	preview_rows = preview_import(name)["rows"]
	if rows is None:
		rows = [
			{
				"name": row["name"],
				"account": row["suggestion"].get("account"),
				"decision_hash": row["suggestion"].get("decision_hash"),
				"allocation_not_accrued": 1 if row["suggestion"].get("allocations") else 0,
			}
			for row in preview_rows
		]
	else:
		rows = frappe.parse_json(rows) if isinstance(rows, str) else rows
	if not isinstance(rows, list) or not 1 <= len(rows) <= 300:
		frappe.throw(_("本批次应包含 1 至 300 笔回单"))
	preview = {r["name"]: r for r in preview_rows}
	results = []
	seen = set()
	for selected in rows:
		row = preview.get(selected.get("name"))
		if not row or row["name"] in seen:
			frappe.throw(_("回单行不存在或重复，请刷新"))
		seen.add(row["name"])
		if row.get("receipt"):
			results.append({"name": row["name"], "reused": True})
			continue
		decision = row["suggestion"]
		if has_blocking_voucher_candidates(row.get("candidates")) or row["status"] == "冲突" or decision.get("blocked"):
			results.append({"name": row["name"], "error": "存在疑似已有凭证、冲突或分类异常，未重复制证"})
			continue
		if not (decision.get("account") or decision.get("allocations")):
			results.append({"name": row["name"], "error": decision.get("reason") or "未匹配到会计科目"})
			continue
		if selected.get("account") != decision.get("account") or selected.get("decision_hash") != decision.get("decision_hash"):
			results.append({"name": row["name"], "error": "建议科目或分摊规则已变化，请刷新核对"})
			continue
		point = "receipt_batch_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(point)
		try:
			result = process_receipt(
				name,
				row["name"],
				"create_allocation" if decision.get("allocations") else "create",
				account=decision.get("account"),
				confirmed=1,
				social_not_accrued=selected.get("social_not_accrued", 0),
				allocation_not_accrued=selected.get("allocation_not_accrued", 0),
				decision_hash=selected.get("decision_hash"),
			)
			results.append({"name": row["name"], **result})
		except Exception as exc:
			frappe.db.rollback(save_point=point)
			results.append({"name": row["name"], "error": str(exc)})
	return {
		"results": results,
		"total": len(results),
		"created": sum(bool(r.get("voucher_name")) for r in results),
		"reused": sum(bool(r.get("reused")) for r in results),
		"failed": sum(bool(r.get("error")) for r in results),
	}


@frappe.whitelist(methods=["POST"])
def abandon_import(name, reason):
	doc = _get_import(name, write=True, lock=True)
	if not (reason or "").strip():
		frappe.throw(_("请填写作废原因"))
	if any(r.receipt for r in doc.rows):
		frappe.throw(_("已处理的批次不能作废；请完成剩余回单核对"))
	doc.status, doc.abandon_reason = "已作废", reason.strip()
	_save(doc)
	return {"status": doc.status}


@frappe.whitelist(methods=["POST"])
def reconcile_receipt(name):
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import reconcile_vouchers

	receipt = frappe.get_doc(RECEIPT, name)
	receipt.check_permission("read")
	bank = validate_bank_context(receipt.company, receipt.bank_account)
	frappe.db.sql("SELECT name FROM `tabBank Account` WHERE name=%s FOR UPDATE", bank.name)
	receipt = frappe.get_doc(RECEIPT, name, for_update=True)
	if not receipt.bank_transaction:
		frappe.throw(_("该回单只补充凭证附件；未创建银行交易，不需要核销"))
	data = json.loads(receipt.raw_data)
	voucher = _validate_voucher(receipt.voucher_type, receipt.voucher_name, data, bank)
	if voucher.docstatus != 1:
		frappe.throw(_("请先完成凭证审核记账"))
	bt = existing_transaction(bank.name, data, lock=True)
	if not bt or bt.name != receipt.bank_transaction or bt.docstatus != 1:
		frappe.throw(_("银行交易已变更或取消，请人工核对"))
	bt.check_permission("write")
	_check_bt_links(bt, voucher)
	if receipt_status(receipt) != "已核销":
		reconcile_vouchers(
			bt.name,
			json.dumps(
				[
					{
						"payment_doctype": voucher.doctype,
						"payment_name": voucher.name,
						"amount": float(money(data["amount"])),
					}
				]
			),
		)
	receipt.status = receipt_status(receipt)
	_save(receipt)
	return {"status": receipt.status}


def pending_receipts(company, from_date, to_date, allow_drafts=False):
	"""Include staged receipts without vouchers; ignore explicitly abandoned batches."""
	if not frappe.db.exists("DocType", IMPORT):
		return []
	rows = frappe.db.sql(
		"""SELECT r.name, r.parent, r.transaction_id, r.receipt, b.bank_account
		FROM `tabChina Bank Receipt Import Row` r JOIN `tabChina Bank Receipt Import` b ON b.name=r.parent
		WHERE b.company=%s AND b.status NOT IN ('已作废','识别失败')
		AND r.posting_date BETWEEN %s AND %s""",
		(company, from_date, to_date),
		as_dict=True,
	)
	pending = []
	for failed in frappe.get_all(IMPORT, filters={"company": company, "status": "识别失败"}, pluck="name"):
		pending.append(frappe._dict(parent=failed, transaction_id=f"{failed}：识别失败，需重新上传或作废"))
	for row in rows:
		key = row.receipt or identity(row.bank_account, row.transaction_id)
		if not frappe.db.exists(RECEIPT, key) or receipt_status(frappe.get_doc(RECEIPT, key)) not in (
			"已核销",
			"已补回单",
			"已关联待核销",
			*( ("凭证待记账",) if allow_drafts else () ),
		):
			pending.append(row)
	return pending


def reuse_receipt_for_transaction(bt):
	"""A later XLSX import must not book a historical receipt a second time."""
	if not frappe.db.exists("DocType", RECEIPT) or not bt.transaction_id:
		return False
	key = identity(bt.bank_account, bt.transaction_id)
	if not frappe.db.get_value(RECEIPT, key, "name", for_update=True):
		return False
	receipt = frappe.get_doc(RECEIPT, key, for_update=True)
	data = json.loads(receipt.raw_data)
	if receipt.company != bt.company or not _equivalent(
		data,
		{
			"posting_date": bt.date,
			"direction": "支出" if bt.withdrawal else "收入",
			"currency": bt.currency,
			"amount": bt.withdrawal or bt.deposit,
		},
	):
		frappe.throw(_("银行流水与已登记回单的交易信息冲突"))
	if receipt.bank_transaction and receipt.bank_transaction != bt.name:
		frappe.throw(_("回单已关联其他银行交易，请先核对原交易"))
	receipt.bank_transaction = bt.name
	receipt.status = receipt_status(receipt)
	_save(receipt)
	return True


def update_receipt_status(doc, method=None):
	if not frappe.db.exists("DocType", RECEIPT):
		return
	filters = (
		{"bank_transaction": doc.name}
		if doc.doctype == "Bank Transaction"
		else {"voucher_type": doc.doctype, "voucher_name": doc.name}
	)
	for name in frappe.get_all(RECEIPT, filters=filters, pluck="name"):
		receipt = frappe.get_doc(RECEIPT, name)
		status = receipt_status(receipt)
		if status != receipt.status:
			frappe.db.set_value(RECEIPT, name, "status", status, update_modified=False)


def has_structured_receipt_lines(doc):
	if doc.doctype != "Journal Entry" or not frappe.db.exists("DocType", RECEIPT):
		return False
	data = frappe.db.get_value(RECEIPT, {"voucher_type": doc.doctype, "voucher_name": doc.name}, "raw_data")
	return bool(data and json.loads(data).get("accounting_decision", {}).get("journal_lines"))


def has_social_receipt_lines(doc):
	"""Backward-compatible wrapper retained for existing integrations."""
	return has_structured_receipt_lines(doc)


@frappe.whitelist()
def get_voucher_receipts(doctype, name):
	if doctype not in ("Journal Entry", "Payment Entry"):
		frappe.throw(_("不支持的凭证类型"))
	frappe.get_doc(doctype, name).check_permission("read")
	return frappe.get_list(
		RECEIPT,
		filters={"voucher_type": doctype, "voucher_name": name},
		fields=[
			"name",
			"receipt_number",
			"posting_date",
			"amount",
			"currency",
			"source_file",
			"page_number",
			"position",
			"status",
		],
		limit_page_length=0,
	)
