"""Company opening baselines: editable staging, explicit atomic native activation.

No draft operation writes a Journal Entry or GL Entry. Historical takeover only
reads source accounting evidence. Activated rows are immutable; adjustments hold
full targets and post their exact change through the normal native workflow.
"""

# Chinese user-facing messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

import json
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime

from china_finance.services import opening_balance_validation as exact

BATCH = "China Opening Balance"
LINE = "China Opening Balance Line"
ACTIVATE_ROLES = ("System Manager", "Accounts Manager", "China Finance Manager")
ROW_FIELDS = (
	"row_id", "account", "party_type", "party", "account_currency", "exchange_rate",
	"debit", "credit", "base_debit", "base_credit", "remarks", "dimensions_json",
)
_editing = ContextVar("china_opening_service_edit", default=False)
_native = ContextVar("china_opening_native_preparation", default=None)


@contextmanager
def internal_edit():
	token = _editing.set(True)
	try:
		yield
	finally:
		_editing.reset(token)


def protect_opening_document():
	if not _editing.get():
		frappe.throw(_("期初余额只能通过期初余额页面保存、启用或调整"), frappe.PermissionError)


def _save(doc, insert=False):
	if doc.doctype != BATCH:
		frappe.throw(_("期初暂存只能保存专用批次"), frappe.PermissionError)
	# Stage every supplied value for one consolidated preview, including broken
	# child links. Company/root/predecessor identity is already server-validated.
	# The exception is local to this batch and never reaches a native JE.
	previous = getattr(doc.flags, "ignore_links", None)
	doc.flags.ignore_links = True
	try:
		with internal_edit():
			return doc.insert() if insert else doc.save(ignore_version=False)
	finally:
		doc.flags.ignore_links = previous


def _json(value, default):
	if value in (None, ""):
		return default
	try:
		return json.loads(value) if isinstance(value, str) else value
	except (ValueError, TypeError):
		frappe.throw(_("数据格式无效，请刷新后重试"))


def _dump(value):
	return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def _rows(doc):
	return [{field: row.get(field) for field in ROW_FIELDS} for row in doc.get("rows", [])]


def _source_names(doc):
	values = _json(doc.get("source_names_json"), [])
	if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
		frappe.throw(_("历史来源列表无效"))
	if len(values) != len(set(values)):
		frappe.throw(_("历史来源不能重复选择"))
	return sorted(values)


def _precision(currency=None, lock=False):
	# Native accounting precision is an explicit validation boundary; never
	# silently round or manufacture an unexplained balancing amount.
	precision = (frappe.db.get_singles_dict("System Settings", for_update=True, cast=True).get("currency_precision") if lock else frappe.db.get_single_value("System Settings", "currency_precision"))
	return min(9, max(0, cint(precision))) if precision not in (None, "") else 2


def _company_context(company, lock=False):
	if not isinstance(company, str) or not company:
		frappe.throw(_("请选择公司"))
	doc = frappe.get_doc("Company", company, for_update=lock)
	doc.check_permission("read")
	return doc


def _lock_company(company):
	from china_finance.services.month_end import lock_company

	lock_company(company)


def _find_base(company, lock=False):
	name = frappe.db.get_value(BATCH, {"initialization_key": company}, "name", for_update=lock)
	return frappe.get_doc(BATCH, name, for_update=lock) if name else None


def _check_version(doc, expected):
	try:
		valid = expected not in (None, "") and int(expected) == int(doc.get("version") or 0)
	except (TypeError, ValueError):
		valid = False
	if not valid:
		frappe.throw(_("期初余额版本已变化，请刷新并重新核对"), frappe.TimestampMismatchError)


def _get_batch(name, write=False, lock=False):
	doc = frappe.get_doc(BATCH, name)
	doc.check_permission("write" if write else "read")
	_company_context(doc.company)
	if lock:
		# Global order: company, root/current batch, then native sources sorted.
		_lock_company(doc.company)
		if doc.get("root_batch") and doc.root_batch != doc.name:
			frappe.db.get_value(BATCH, doc.root_batch, "name", for_update=True)
		doc = frappe.get_doc(BATCH, name, for_update=True)
		doc.check_permission("write" if write else "read")
	return doc


def _evidence_rows(doctype, filters=None, fields=None, order_by="name", lock=False):
	# Bare name sorts ASC in get_all compatibility mode but DESC in
	# db.get_values. Explicit directions keep immutable row evidence stable.
	order_by = ", ".join(part.strip() if part.strip().lower().endswith((" asc", " desc")) else part.strip() + " asc" for part in order_by.split(","))
	if lock:
		# Frappe v16 get_all/get_list does not expose for_update. The database
		# API does and returns current, locked rows even after an earlier RR read.
		# A None filter means Single/name lookup in get_values and returns
		# None for ordinary DocTypes; {} explicitly requests the full range.
		return frappe.db.get_values(doctype, filters={} if filters is None else filters, fieldname=fields or ["name"], as_dict=True, order_by=order_by, for_update=True)
	return frappe.get_all(doctype, filters=filters, fields=fields or ["name"], order_by=order_by)


def _dimension_definitions(company, lock=False):
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions

	if lock:
		configured = _evidence_rows("Accounting Dimension", fields=["name", "fieldname", "disabled"], order_by="name", lock=True)
		dimensions = [row.fieldname for row in configured if not row.disabled]
	else:
		dimensions = get_accounting_dimensions() or []
	fields = ["cost_center", "project"] + list(dimensions)
	meta = frappe.get_meta("Journal Entry Account")
	return {field: meta.get_field(field) for field in dict.fromkeys(fields) if meta.get_field(field)}


def _dimension_rules(company, lock=False):
	return _evidence_rows(
		"Accounting Dimension Detail", filters={"company": company},
		fields=["parent", "mandatory_for_bs"], lock=lock,
	) if frappe.db.exists("DocType", "Accounting Dimension Detail") else []


def _validation_errors(doc, rows=None, lock=False):
	rows = _rows(doc) if rows is None else rows
	errors = []
	company = _company_context(doc.get("company"), lock=lock)
	precision = _precision(company.default_currency, lock=lock)
	definitions = _dimension_definitions(company.name, lock=lock)
	rules = _dimension_rules(company.name, lock=lock)
	required = set()
	for rule in rules:
		if rule.get("mandatory_for_bs"):
			field = frappe.db.get_value("Accounting Dimension", rule.parent, "fieldname", for_update=lock)
			if field:
				required.add(field)

	def error(row_id, field, message):
		errors.append({"row_id": row_id, "field": field, "message": str(message)})

	if not doc.get("opening_date"):
		error(None, "opening_date", _("请填写期初日期"))
	if doc.get("currency") != company.default_currency:
		error(None, "currency", _("期初本位币必须与公司币种一致"))
	if not rows:
		error(None, "rows", _("请填写期初余额分录"))
	seen = set()
	for index, row in enumerate(rows, 1):
		row_id = row.get("row_id") or str(index)
		if row_id in seen:
			error(row_id, "row_id", _("行标识重复"))
		seen.add(row_id)
		for message in exact.validate_amounts(row, company.default_currency, precision, precision):
			error(row_id, "amount", message)
		account = frappe.db.get_value("Account", row.get("account"), ["name", "company", "is_group", "disabled", "report_type", "account_type", "account_currency"], as_dict=True, for_update=lock) if row.get("account") else None
		if not account or account.company != company.name or account.is_group or account.disabled:
			error(row_id, "account", _("请选择本公司启用的末级科目"))
		elif account.report_type != "Balance Sheet":
			error(row_id, "account", _("期初余额只允许资产负债表科目"))
		elif row.get("account_currency") != (account.account_currency or company.default_currency):
			error(row_id, "account_currency", _("科目币种与原生科目设置不一致"))
		party_type, party = row.get("party_type"), row.get("party")
		if bool(party_type) != bool(party):
			error(row_id, "party", _("往来类型与往来单位必须同时填写"))
		if account and account.account_type in ("Receivable", "Payable"):
			expected_type = "Customer" if account.account_type == "Receivable" else "Supplier"
			if party_type != expected_type or not party:
				error(row_id, "party", _("应收或应付科目必须选择对应的客户或供应商"))
		if party_type and party:
			if not frappe.db.get_value("Party Type", party_type, "name", for_update=lock) or not frappe.db.get_value(party_type, party, "name", for_update=lock):
				error(row_id, "party", _("往来类型或往来单位不存在"))
			else:
				party_doc = frappe.get_doc(party_type, party, for_update=lock)
				if not party_doc.has_permission("read") or party_doc.get("disabled"):
					error(row_id, "party", _("无权读取或已停用的往来单位"))
				if party_doc.get("company") and party_doc.company != company.name:
					error(row_id, "party", _("往来单位属于其他公司"))
		try:
			dimensions = exact.dimensions(row)
		except ValueError as exc:
			error(row_id, "dimensions_json", str(exc))
			continue
		for field in sorted(set(dimensions) | required):
			value = dimensions.get(field)
			definition = definitions.get(field)
			if not definition:
				error(row_id, field, _("会计维度未启用或不属于原生分录字段"))
				continue
			if not value:
				error(row_id, field, _("缺少资产负债表必填会计维度"))
				continue
			if not frappe.db.get_value(definition.options, value, "name", for_update=lock):
				error(row_id, field, _("会计维度值不存在"))
				continue
			dimension = frappe.get_doc(definition.options, value, for_update=lock)
			if not dimension.has_permission("read") or dimension.get("disabled") or dimension.get("is_group"):
				error(row_id, field, _("会计维度值无权读取、已停用或不是末级记录"))
			if dimension.get("company") and dimension.company != company.name:
				error(row_id, field, _("会计维度属于其他公司"))
	try:
		if exact.decimal(exact.totals(rows, precision)["difference"]):
			error(None, "totals", _("期初本位币借贷不平衡，请核实真实来源，系统不会自动补平"))
	except ValueError as exc:
		error(None, "totals", str(exc))
	return errors


def _normalise_rows(values, company, apply_defaults=True):
	if not isinstance(values, list) or len(values) > 5000:
		frappe.throw(_("期初分录必须为列表，且最多 5000 行"))
	result = []
	definitions = _dimension_definitions(company.name)
	precision = _precision(company.default_currency)
	for index, value in enumerate(values, 1):
		if not isinstance(value, dict):
			frappe.throw(_("期初分录格式无效"))
		row = {field: value.get(field) for field in ROW_FIELDS}
		row["row_id"] = str(row.get("row_id") or frappe.generate_hash(length=12))
		row["account_currency"] = row.get("account_currency") or frappe.db.get_value("Account", row.get("account"), "account_currency") or company.default_currency
		row["exchange_rate"] = str(row.get("exchange_rate") or ("1" if row["account_currency"] == company.default_currency else "0"))
		for field in exact.AMOUNTS:
			if row.get(field) in (None, ""):
				if field.startswith("base_") and row["account_currency"] == company.default_currency:
					row[field] = row.get(field[5:]) or "0"
				else:
					row[field] = "0"
			try:
				# Reject values that native Float conversion could hide at save time.
				exact.amount(row[field], precision)
			except ValueError as exc:
				frappe.throw(_("第 {0} 行：{1}").format(index, str(exc)))
		try:
			exact.amount(row["exchange_rate"], 9)
			dimensions = exact.dimensions(row)
		except ValueError as exc:
			frappe.throw(_("第 {0} 行：{1}").format(index, str(exc)))
		for field in definitions:
			if value.get(field):
				dimensions[field] = value[field]
		# Native Journal Entry defaults company cost center on balance-sheet
		# rows too. Persist it before preview so the approved target is explicit.
		if apply_defaults and not dimensions.get("cost_center") and company.cost_center:
			dimensions["cost_center"] = company.cost_center
		row["dimensions_json"] = _dump(dimensions)
		row["remarks"] = value.get("remarks") or ""
		result.append(row)
	return result


def _as_opening_row(row, dimensions, *, source=False, header=None):
	values = {
		"row_id": row.get("name") or row.get("row_id"), "account": row.get("account"),
		"party_type": row.get("party_type"), "party": row.get("party"),
		"account_currency": row.get("account_currency"), "exchange_rate": row.get("exchange_rate") or "1",
		"debit": row.get("debit_in_account_currency") or 0, "credit": row.get("credit_in_account_currency") or 0,
		"base_debit": row.get("debit") or 0, "base_credit": row.get("credit") or 0,
		"remarks": row.get("user_remark") if source else row.get("remarks"),
	}
	if source and values["remarks"] is None:
		values["remarks"] = (header or {}).get("user_remark") or (header or {}).get("remark") or ""
	data = exact.dimensions(row)
	for field in dimensions:
		if row.get(field):
			data[field] = row.get(field)
	values["dimensions_json"] = _dump(data)
	return values


def _source_evidence(name, company, opening_date, lock=False):
	if lock:
		frappe.db.get_value("Journal Entry", name, "name", for_update=True)
	source = frappe.get_doc("Journal Entry", name, for_update=lock)
	source.check_permission("read")
	fields = list(_dimension_definitions(company, lock=lock))
	gl_fields = [
		"name", "voucher_type", "voucher_no", "company", "posting_date", "is_opening", "is_cancelled",
		"account", "account_currency", "debit", "credit", "debit_in_account_currency", "credit_in_account_currency",
		"party_type", "party", "finance_book", "against_voucher_type", "against_voucher", "remarks",
	] + fields
	gl = _evidence_rows("GL Entry", filters={"voucher_type": "Journal Entry", "voucher_no": name}, fields=list(dict.fromkeys(gl_fields)), order_by="name", lock=lock)
	for message in exact.validate_source_identity(source, gl, company, opening_date):
		frappe.throw(_("来源 {0}：{1}").format(name, message))
	if any(row.get("reference_type") or row.get("reference_name") for row in source.accounts):
		frappe.throw(_("来源 {0} 包含业务单据引用，不能接管为纯期初余额").format(name))
	if any(row.get("against_voucher_type") or row.get("against_voucher") for row in gl):
		frappe.throw(_("来源 {0} 总账包含业务单据引用，不能接管为纯期初余额").format(name))
	rows = [_as_opening_row(row, fields, source=True, header=source) for row in source.accounts]
	ledger_rows = [_as_opening_row(row, fields) for row in gl]
	if exact.accounting_totals(rows) != exact.accounting_totals(ledger_rows):
		frappe.throw(_("来源 {0} 的原生分录与总账在科目、币种、往来、维度或金额上不一致").format(name))
	# Every existing technical snapshot must agree; never repair historical
	# evidence as a side effect of adopting it into the opening workbench.
	snapshots = _evidence_rows("China Accounting Voucher", filters={"source_doctype": "Journal Entry", "source_name": name}, fields=["name"], order_by="name", lock=lock)
	snapshot_hashes = []
	for item in snapshots:
		if lock:
			frappe.db.get_value("China Accounting Voucher", item.name, "name", for_update=True)
		snapshot = frappe.get_doc("China Accounting Voucher", item.name, for_update=lock)
		snapshot.check_permission("read")
		if snapshot.docstatus != 1 or snapshot.status != "Posted" or snapshot.source_event != "Posting" or snapshot.company != company or getdate(snapshot.posting_date) != getdate(opening_date) or snapshot.get("reversed_by"):
			frappe.throw(_("来源 {0} 的会计快照状态或公司日期不一致").format(name))
		from china_finance.services.voucher import calculate_entries_hash
		if snapshot.source_hash != calculate_entries_hash(snapshot.entries):
			frappe.throw(_("来源 {0} 的会计快照校验值不一致").format(name))
		snapshot_rows = [_as_opening_row(row, fields) for row in snapshot.entries]
		if exact.accounting_totals(ledger_rows) != exact.accounting_totals(snapshot_rows):
			frappe.throw(_("来源 {0} 的总账与会计快照分录不一致").format(name))
		if exact.decimal(snapshot.total_debit) != sum((exact.decimal(row["base_debit"]) for row in snapshot_rows), Decimal(0)) or exact.decimal(snapshot.total_credit) != sum((exact.decimal(row["base_credit"]) for row in snapshot_rows), Decimal(0)):
			frappe.throw(_("来源 {0} 的会计快照汇总与分录不一致").format(name))
		snapshot_hashes.append({"name": snapshot.name, "hash": snapshot.source_hash})
	if exact.decimal(exact.totals(rows, _precision())["difference"]):
		frappe.throw(_("来源 {0} 借贷不平衡").format(name))
	if source.get("finance_book") or any(row.get("finance_book") for row in gl):
		frappe.throw(_("期初余额目前只支持公司默认账簿；来源 {0} 使用了独立账簿").format(name))
	return {
		"name": name, "company": company, "opening_date": str(opening_date), "rows": rows,
		"hash": exact.fingerprint({"source": {"name": source.name, "modified": source.modified, "company": source.company, "posting_date": source.posting_date, "docstatus": source.docstatus, "voucher_type": source.voucher_type, "is_opening": source.is_opening, "rows": rows}, "gl": gl, "snapshots": snapshot_hashes}),
	}


def _history_candidates(company):
	result = []
	for item in frappe.get_list("Journal Entry", filters={"company": company, "docstatus": 1, "is_opening": "Yes"}, fields=["name", "posting_date"], order_by="posting_date asc, name asc", limit_page_length=0):
		try:
			if _linked_active_batch(item.name, company=company):
				continue
			evidence = _source_evidence(item.name, company, item.posting_date)
			result.append({"name": item.name, "label": item.name, "opening_date": str(item.posting_date), "rows": evidence["rows"], "source_names": [item.name], "mode": "Takeover", "errors": []})
		except (frappe.ValidationError, frappe.PermissionError, ValueError):
			continue
	return result


def _envelope(doc=None, company=None, history=False):
	company = company or doc.company
	context = _company_context(company)
	accounts = frappe.get_list("Account", filters={"company": company, "is_group": 0, "disabled": 0, "report_type": "Balance Sheet"}, fields=["name", "account_number", "account_name", "account_currency", "account_type"], order_by="lft", limit_page_length=0)
	for account in accounts:
		account.account_currency = account.account_currency or context.default_currency
	batch = doc.as_dict() if doc else None
	if batch:
		batch["source_names"] = _source_names(doc)
		for row in batch.get("rows", []):
			row.update(exact.dimensions(row))
	return {
		"batch": batch, "company": company, "currency": context.default_currency, "precision": _precision(context.default_currency),
		"opening_date": str(doc.opening_date) if doc and doc.opening_date else None,
		"accounts": accounts, "dimensions": list(_dimension_definitions(company)),
		"history_candidates": _history_candidates(company) if history else [],
		"permissions": {"summary": bool(set(frappe.get_roles()) & set(ACTIVATE_ROLES)), "write": bool(frappe.has_permission(BATCH, "write", doc=doc)) and bool(frappe.has_permission(BATCH, "create")), "activate": bool(set(frappe.get_roles()) & set(ACTIVATE_ROLES)) and bool(frappe.has_permission("Journal Entry", "create")) and bool(frappe.has_permission("Journal Entry", "submit"))},
	}


@frappe.whitelist()
def get_opening(company):
	_company_context(company)
	if not frappe.has_permission(BATCH, "read"):
		frappe.throw(_("没有读取期初余额的权限"), frappe.PermissionError)
	base = _find_base(company)
	doc = base
	if base and base.get("current_active"):
		active = frappe.get_doc(BATCH, base.current_active)
		pending = frappe.db.get_value(BATCH, {"previous_batch": active.name, "status": "Draft"}, "name")
		doc = frappe.get_doc(BATCH, pending) if pending else active
	if doc:
		doc.check_permission("read")
	return _envelope(doc, company, history=not base)


@frappe.whitelist(methods=["POST"])
def save_opening(payload, expected_version=None):
	payload = _json(payload, {})
	if not isinstance(payload, dict):
		frappe.throw(_("期初余额数据格式无效"))
	company = _company_context(payload.get("company"))
	_lock_company(company.name)
	name = payload.get("name")
	if name:
		doc = _get_batch(name, write=True, lock=True)
		_check_version(doc, expected_version)
		if doc.status != "Draft" or doc.company != company.name:
			frappe.throw(_("已启用的期初版本不可修改，公司也不可变更，请新建调整"))
	else:
		if not frappe.has_permission(BATCH, "create"):
			frappe.throw(_("没有创建期初余额的权限"), frappe.PermissionError)
		if _find_base(company.name, lock=True):
			frappe.throw(_("本公司已存在期初初始化，请刷新现有批次"))
		doc = frappe.new_doc(BATCH)
		doc.company = company.name
		doc.status = "Draft"
		doc.revision = 1
		doc.initialization_key = company.name
		doc.version = 0
	mode = payload.get("mode") or doc.get("mode") or "New"
	if mode not in ("New", "Takeover", "Adjustment"):
		frappe.throw(_("期初模式无效"))
	if bool(doc.get("previous_batch")) != (mode == "Adjustment"):
		frappe.throw(_("调整必须从已启用期初版本发起"))
	if doc.get("previous_batch"):
		previous = frappe.get_doc(BATCH, doc.previous_batch)
		if getdate(payload.get("opening_date")) != getdate(previous.opening_date):
			frappe.throw(_("调整必须沿用原期初日期；系统不会解除冻结或结账"))
	doc.mode = mode
	doc.opening_date = getdate(payload.get("opening_date")) if payload.get("opening_date") else None
	doc.currency = company.default_currency
	doc.source_names_json = _dump(payload.get("source_names") or []) if mode == "Takeover" else "[]"
	_source_names(doc)
	doc.set("rows", _normalise_rows(payload.get("rows", []), company, apply_defaults=mode != "Takeover"))
	doc.version = cint(doc.version) + 1
	_save(doc, insert=not name)
	return _envelope(doc)


def _saved_payload(doc):
	payload = {
		"name": doc.get("name"), "company": doc.get("company"), "opening_date": str(doc.get("opening_date")),
		"currency": doc.get("currency"), "mode": doc.get("mode"), "version": doc.get("version"),
		"revision": doc.get("revision"), "previous_batch": doc.get("previous_batch"), "root_batch": doc.get("root_batch"),
		"rows": _rows(doc), "source_names": _source_names(doc),
	}
	# Empty fields added by this release must not invalidate existing receipts.
	for field in ("summary_customer", "summary_supplier"):
		if doc.get(field):
			payload[field] = doc.get(field)
	return payload


def _opening_evidence(doc, lock=False):
	if doc.get("mode") == "Takeover":
		names = _source_names(doc)
		if not names:
			frappe.throw(_("请明确选择至少一张历史期初来源"))
		evidence = [_source_evidence(name, doc.get("company"), doc.get("opening_date"), lock) for name in names]
		for name in names:
			linked = _linked_active_batch(name, company=doc.get("company"))
			if linked and linked != doc.get("name"):
				frappe.throw(_("历史来源 {0} 已被其他期初批次接管").format(name))
		expected = [row for source in evidence for row in source["rows"]]
		if exact.accounting_totals(_rows(doc)) != exact.accounting_totals(expected):
			frappe.throw(_("接管明细必须与历史来源的科目、原币、本位币、往来和会计维度完全一致"))
		# Preserve row-level historical text as well as the accounting identity.
		if not exact.historical_rows_match(_rows(doc), expected):
			frappe.throw(_("接管明细必须保留原始摘要，包括尚未建立往来档案的文字"))
		return evidence
	if doc.get("previous_batch"):
		previous = frappe.get_doc(BATCH, doc.get("previous_batch"), for_update=lock)
		previous.check_permission("read")
		if previous.status != "Active" or previous.company != doc.get("company") or getdate(previous.opening_date) != getdate(doc.get("opening_date")):
			frappe.throw(_("调整的原期初版本已失效或公司日期不一致"))
		root = frappe.get_doc(BATCH, doc.get("root_batch") or previous.name, for_update=lock)
		if root.get("current_active") != previous.name:
			frappe.throw(_("原期初已存在更新版本，请刷新"), frappe.TimestampMismatchError)
		_revalidate_receipt(previous, lock=lock)
		return [{"previous_batch": previous.name, "fingerprint": previous.activation_fingerprint, "rows_hash": exact.fingerprint(_rows(previous))}]
	return []


def _semantic_master(value):
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	if isinstance(value, dict):
		volatile = {"creation", "modified", "modified_by", "owner", "parent", "parenttype", "parentfield", "docstatus", "doctype", "idx", "name"}
		return {key: _semantic_master(item) for key, item in value.items() if key not in volatile and not key.startswith("__")}
	if isinstance(value, (list, tuple)):
		return [_semantic_master(item) for item in value]
	return value


def _master_evidence(doc, lock=False):
	"""Hash the accounting configuration, not mutable operational counters.

	Rebuild this inside the activation gate. Receipts retain its hash only as
	provenance; later legitimate master maintenance never rewrites old ledgers.
	"""
	company = doc.get("company")
	definitions = _dimension_definitions(company, lock=lock)
	records = {}
	common = ("name", "company", "disabled", "is_group", "is_active", "currency", "account_currency", "default_currency", "is_frozen")
	fields = {
		"Company": ("name", "default_currency", "accounts_frozen_till_date", "cost_center", "default_finance_book", "default_receivable_account", "default_payable_account", "country", "parent_company", "is_group"),
		"Account": (*common, "modified", "account_name", "account_number", "parent_account", "report_type", "root_type", "account_type", "freeze_account", "balance_must_be"),
		"Currency": ("name", "modified", "enabled", "fraction", "fraction_units", "smallest_currency_fraction_value", "number_format"),
		"Accounts Settings": ("acc_frozen_upto", "frozen_accounts_modifier", "allow_cost_center_in_entry_of_bs_account", "credit_controller", "maintain_same_rate"),
		"System Settings": ("currency_precision", "float_precision", "number_format"),
		"China Finance Settings": ("modified", "enabled", "activation_date", "enable_voucher_preparation", "preparation_from_date", "enforce_role_separation", "voucher_mode", "default_voucher_word", "accounting_standard", "taxpayer_type", "sequence_reset", "coa_integrity_status", "cash_flow_assignment_activation_date"),
		"Fiscal Year": ("name", "disabled", "year_start_date", "year_end_date", "companies"),
		"Period Closing Voucher": ("name", "company", "docstatus", "period_start_date", "period_end_date", "closing_account_head"),
		"China Closing Run": ("name", "company", "docstatus", "from_date", "to_date", "closing_type", "status", "preparation_state"),
		"Accounting Period": ("name", "company", "start_date", "end_date", "disabled", "exempted_role", "closed_documents"),
	}

	def record(doctype, name):
		key = doctype + "|" + str(name)
		if key in records:
			return
		if not name or not frappe.db.get_value(doctype, name, "name", for_update=lock):
			records[key] = None
			return
		master = frappe.get_doc(doctype, name, for_update=lock)
		selected = fields.get(doctype, (*common, "customer_name", "supplier_name", "accounts", "parent_cost_center", "cost_center_name", "project_name"))
		records[key] = {field: _semantic_master(master.get(field)) for field in selected}

	record("Company", company)
	record("Currency", doc.get("currency"))
	for row in _rows(doc):
		record("Account", row.get("account"))
		record("Currency", row.get("account_currency"))
		if row.get("party_type") and row.get("party") and frappe.db.get_value("Party Type", row["party_type"], "name", for_update=lock):
			record(row["party_type"], row["party"])
		try:
			values = exact.dimensions(row)
		except ValueError:
			values = {}
		for field, value in values.items():
			if field in definitions:
				record(definitions[field].options, value)
	for doctype in ("Accounts Settings", "System Settings"):
		if frappe.db.exists("DocType", doctype):
			master = frappe.db.get_singles_dict(doctype, for_update=lock, cast=True)
			records[doctype] = {field: master.get(field) for field in fields[doctype]}
	if frappe.db.get_value("China Finance Settings", company, "name", for_update=lock):
		record("China Finance Settings", company)
	for item in _evidence_rows("Accounting Dimension", fields=["name"], order_by="name", lock=lock):
		master = frappe.get_doc("Accounting Dimension", item.name, for_update=lock)
		records["Accounting Dimension|" + item.name] = _semantic_master(master.as_dict())
	for item in _evidence_rows("Workflow", filters={"document_type": "Journal Entry", "is_active": 1}, fields=["name"], order_by="name", lock=lock):
		workflow = frappe.get_doc("Workflow", item.name, for_update=lock)
		records["Workflow|" + item.name] = {field: _semantic_master(workflow.get(field)) for field in ("document_type", "is_active", "workflow_state_field", "states", "transitions")}
	for item in _evidence_rows("Fiscal Year", fields=["name"], order_by="name", lock=lock):
		record("Fiscal Year", item.name)
	for doctype in ("Period Closing Voucher", "China Closing Run", "Accounting Period"):
		if frappe.db.exists("DocType", doctype):
			for item in _evidence_rows(doctype, filters={"company": company}, fields=["name"], order_by="name", lock=lock):
				record(doctype, item.name)
	return {"records": records, "dimensions": {field: {"options": definition.options, "reqd": definition.reqd} for field, definition in definitions.items()}}


def _historical_party_warnings(doc, lock=False):
	parties = {"Customer": doc.get("summary_customer"), "Supplier": doc.get("summary_supplier")}
	summary_warnings = [{"row_id": row.get("row_id"), "field": "party", "message": _("第 {0} 行：历史汇总余额尚未分配到真实客户或供应商；该汇总档案不代表真实往来明细。").format(index)}
		for index, row in enumerate(_rows(doc), 1)
		if row.get("party") and row.get("party") == parties.get(row.get("party_type"))]
	if doc.get("mode") not in ("Takeover", "Adjustment"):
		return summary_warnings
	rows = _rows(doc)
	accounts = {name: frappe.db.get_value("Account", {"name": name, "company": doc.get("company")}, ["account_name", "account_type"], as_dict=True, for_update=lock) for name in sorted({row.get("account") for row in rows if row.get("account")})}
	return summary_warnings + exact.historical_party_warnings(rows, accounts)


def _preview(doc, lock=False):
	errors = _validation_errors(doc, lock=lock)
	evidence = []
	delta = _rows(doc)
	try:
		evidence = _opening_evidence(doc, lock=lock)
		if doc.get("mode") == "Adjustment":
			previous = frappe.get_doc(BATCH, doc.previous_batch, for_update=lock)
			delta = exact.delta_rows(_rows(doc), _rows(previous), _precision())
			errors.extend(_validation_errors(doc, delta, lock=lock) if delta else [])
		elif doc.get("mode") == "New":
			# Existing native opening GL must be adopted explicitly rather than
			# silently posting a second independent company initialization.
			if frappe.db.get_value("GL Entry", {"company": doc.company, "is_opening": "Yes", "is_cancelled": 0}, "name", for_update=lock):
				errors.append({"row_id": None, "field": "mode", "message": _("公司已有原生期初总账，请核对并选择历史接管，避免重复初始化")})
	except (frappe.ValidationError, frappe.PermissionError, ValueError) as exc:
		errors.append({"row_id": None, "field": "source_names", "message": str(exc)})
	payload = _saved_payload(doc)
	payload["evidence"] = evidence
	master_fingerprint = exact.fingerprint(_master_evidence(doc, lock=lock))
	payload["master_fingerprint"] = master_fingerprint
	try:
		totals = exact.totals(_rows(doc), _precision())
	except ValueError:
		totals = {"debit": "0.00", "credit": "0.00", "difference": "0.00"}
	return {
		"fingerprint": exact.fingerprint(payload), "errors": errors, "warnings": _historical_party_warnings(doc, lock=lock), "totals": totals,
		"actions": {"creates_journal_entry": doc.get("mode") != "Takeover" and bool(delta), "historical_takeover": doc.get("mode") == "Takeover", "delta_rows": len(delta), "can_activate": not errors},
		"delta_rows": delta if doc.get("mode") == "Adjustment" else [], "version": doc.get("version"),
		"master_fingerprint": master_fingerprint,
	}


@frappe.whitelist()
def preview_opening(name):
	doc = _get_batch(name)
	return _preview(doc)


def _ensure_native_snapshot(journal):
	from china_finance.services.voucher import create_voucher_from_source, get_company_settings

	if not get_company_settings(journal.company):
		frappe.throw(_("请先启用本公司的中国财务设置，期初启用必须保留可核对的技术会计快照"))
	# Opening dates often precede ordinary voucher activation. Retain the same
	# native-source audit snapshot explicitly while keeping its opening identity.
	name = create_voucher_from_source(journal, force=True)
	if not name or not frappe.db.exists("China Accounting Voucher", {
		"name": name, "company": journal.company, "source_doctype": "Journal Entry", "source_name": journal.name,
		"source_event": "Posting", "docstatus": 1, "status": "Posted",
	}):
		frappe.throw(_("原生期初凭证缺少可信的技术会计快照，启用已回滚"))
	return name


def _post_native(doc, rows):
	from china_finance.services import voucher_preparation as preparation
	from china_finance.services.voucher import _complete_voucher_workflow

	journal = frappe.new_doc("Journal Entry")
	journal.company = doc.company
	journal.posting_date = doc.opening_date
	journal.voucher_type = "Opening Entry"
	journal.is_opening = "Yes"
	journal.multi_currency = int(any(row.get("account_currency") != doc.currency for row in rows))
	journal.user_remark = _("期初余额 {0}，版本 {1}").format(doc.name, doc.revision)
	for row in rows:
		values = {
			"account": row.get("account"), "party_type": row.get("party_type"), "party": row.get("party"),
			"account_currency": row.get("account_currency"), "exchange_rate": row.get("exchange_rate"),
			"debit_in_account_currency": row.get("debit"), "credit_in_account_currency": row.get("credit"),
			"debit": row.get("base_debit"), "credit": row.get("base_credit"), "user_remark": row.get("remarks"),
		}
		values.update(exact.dimensions(row))
		journal.append("accounts", values)
	journal.check_permission("create")
	# Native validation, workflow, period locks and freeze checks all remain in force.
	journal.insert()
	journal.check_permission("submit")
	preparation.validate_draft(journal)
	journal.reload()
	native_hash = preparation.content_hash(journal)
	trusted = {"batch": doc.name, "batch_hash": exact.fingerprint(_saved_payload(doc)), "source": journal.name, "hash": native_hash}
	token = _native.set(trusted)
	try:
		journal.db_set({preparation.READY_FIELDS[0]: native_hash, preparation.READY_FIELDS[1]: frappe.session.user, preparation.READY_FIELDS[2]: now_datetime()})
		journal.reload()
		if not validate_native_opening_preparation(journal):
			frappe.throw(_("期初来源核对版本已变化"))
		journal = _complete_voucher_workflow(journal)
	finally:
		_native.reset(token)
	if journal.docstatus != 1:
		frappe.throw(_("原生期初凭证未完成提交"))
	_ensure_native_snapshot(journal)
	return journal.name


def validate_native_opening_preparation(doc):
	trusted = _native.get()
	if not trusted:
		return False
	if doc.get("doctype") != "Journal Entry" or doc.get("name") != trusted["source"] or doc.get("voucher_type") != "Opening Entry" or doc.get("is_opening") != "Yes":
		frappe.throw(_("期初凭证核对上下文无效"))
	batch = frappe.get_doc(BATCH, trusted["batch"])
	if batch.status != "Draft" or exact.fingerprint(_saved_payload(batch)) != trusted["batch_hash"]:
		frappe.throw(_("保存的期初版本已变化，请重新核对"), frappe.TimestampMismatchError)
	from china_finance.services import voucher_preparation as preparation
	if preparation.content_hash(doc) != trusted["hash"] or not preparation.is_ready(doc):
		frappe.throw(_("原生期初凭证内容已变化，请重新核对"), frappe.TimestampMismatchError)
	return True


def _linked_active_batch(source_name, company=None, lock=False):
	# Current locking reads are required after the company gate: a stale normal
	# read view must never permit cancellation of a just-activated baseline.
	if lock:
		items = frappe.db.sql(
			"SELECT name, receipt_json FROM `tabChina Opening Balance` "
			"WHERE company=%s AND status='Active' ORDER BY name FOR UPDATE",
			(company,), as_dict=True,
		)
	else:
		filters = {"status": "Active"}
		if company:
			filters["company"] = company
		items = frappe.get_all(BATCH, filters=filters, fields=["name", "receipt_json"])
	for item in items:
		receipt = _json(item.receipt_json, {})
		if source_name in receipt.get("source_names", []):
			return item.name
	return None


def guard_linked_opening_source(doc, method=None):
	if doc.get("doctype") != "Journal Entry" or not doc.get("name"):
		return
	company = doc.get("company")
	if company:
		_lock_company(company)
	if _linked_active_batch(doc.get("name"), company=company, lock=bool(company)):
		frappe.throw(_("该原生凭证已关联启用的期初余额，不能直接取消或修改。请在期初余额中发起受控调整。"))


def _revalidate_receipt(doc, lock=False):
	receipt = _json(doc.get("receipt_json"), {})
	if not isinstance(receipt, dict) or not receipt.get("batch_hash") or receipt.get("batch_hash") != exact.fingerprint(_saved_payload(doc)):
		frappe.throw(_("已启用期初的保存内容与启用回执不一致"))
	stored = receipt.get("evidence") or []
	current = [_source_evidence(name, doc.get("company"), doc.get("opening_date"), lock=lock) for name in sorted(receipt.get("source_names") or [])]
	if [{"name": item["name"], "hash": item["hash"]} for item in current] != stored:
		frappe.throw(_("已启用期初的原生凭证、总账或会计快照证据已变化，请先核查"))
	if doc.get("previous_batch"):
		previous = frappe.get_doc(BATCH, doc.get("previous_batch"), for_update=lock)
		_revalidate_receipt(previous, lock=lock)
	return receipt


def _activate_locked(doc, fingerprint):
	preview = _preview(doc, lock=True)
	if not fingerprint or fingerprint != preview["fingerprint"]:
		frappe.throw(_("期初核对预览已变化，请重新预览后启用"), frappe.TimestampMismatchError)
	if preview["errors"]:
		frappe.throw("；".join(item["message"] for item in preview["errors"]))
	if doc.mode == "Takeover":
		names = _source_names(doc)
	else:
		rows = preview["delta_rows"] if doc.mode == "Adjustment" else _rows(doc)
		names = [_post_native(doc, rows)] if rows else []
	evidence = [_source_evidence(name, doc.company, doc.opening_date, lock=True) for name in sorted(names)]
	if doc.mode != "Takeover":
		actual = [row for item in evidence for row in item["rows"]]
		if exact.accounting_totals(rows) != exact.accounting_totals(actual):
			frappe.throw(_("原生期初凭证或总账结果与已核对的期初目标不一致，启用已回滚"))
	if preview.get("master_fingerprint") and preview["master_fingerprint"] != exact.fingerprint(_master_evidence(doc, lock=True)):
		frappe.throw(_("启用期间会计主数据或期间设置已变化，请重新预览"), frappe.TimestampMismatchError)
	receipt = {
		"batch": doc.name, "mode": doc.mode, "revision": doc.revision, "version": doc.version,
		"source_names": sorted(names), "created_journal_entries": names if doc.mode != "Takeover" else [],
		"evidence": [{"name": item["name"], "hash": item["hash"]} for item in evidence],
		"batch_hash": exact.fingerprint(_saved_payload(doc)), "fingerprint": fingerprint,
		"master_fingerprint": preview.get("master_fingerprint"),
		"activated_by": frappe.session.user, "activated_on": str(now_datetime()), "zero_delta": doc.mode == "Adjustment" and not names,
	}
	doc.status = "Active"
	doc.activation_fingerprint = fingerprint
	doc.activated_by = frappe.session.user
	doc.activated_on = now_datetime()
	doc.receipt_json = _dump(receipt)
	_save(doc)
	root = frappe.get_doc(BATCH, doc.get("root_batch") or doc.name, for_update=True)
	root.db_set("current_active", doc.name)
	if root.name == doc.name:
		doc.reload()
	return receipt


@frappe.whitelist(methods=["POST"])
def activate_opening(name, fingerprint):
	frappe.only_for(ACTIVATE_ROLES)
	if not frappe.has_permission("Journal Entry", "create") or not frappe.has_permission("Journal Entry", "submit"):
		frappe.throw(_("启用期初余额需要原生记账凭证的创建和提交权限"), frappe.PermissionError)
	doc = _get_batch(name, write=True, lock=True)
	point = "opening_activation"
	frappe.db.savepoint(point)
	try:
		if doc.get("status") == "Active":
			if fingerprint != doc.get("activation_fingerprint"):
				frappe.throw(_("启用请求与已完成回执版本不一致"), frappe.TimestampMismatchError)
			receipt = _revalidate_receipt(doc, lock=True)
		else:
			receipt = _activate_locked(doc, fingerprint)
		result = _envelope(doc)
		result["receipt"] = receipt
		return result
	except frappe.QueryDeadlockError:
		# MariaDB has already rolled back the entire transaction and removed
		# this savepoint. Preserve the real concurrency error for a clean retry.
		raise
	except Exception:
		frappe.db.rollback(save_point=point)
		raise


@frappe.whitelist(methods=["POST"])
def start_adjustment(name, expected_version):
	doc = _get_batch(name, write=True, lock=True)
	_check_version(doc, expected_version)
	if not frappe.has_permission(BATCH, "create"):
		frappe.throw(_("没有创建期初调整的权限"), frappe.PermissionError)
	if doc.status != "Active":
		frappe.throw(_("请先启用当前期初版本"))
	root = frappe.get_doc(BATCH, doc.get("root_batch") or doc.name, for_update=True)
	if root.current_active != doc.name:
		frappe.throw(_("该期初版本已被更新，请刷新"), frappe.TimestampMismatchError)
	_revalidate_receipt(doc, lock=True)
	existing = frappe.db.get_value(BATCH, {"adjustment_key": doc.name}, "name", for_update=True)
	if existing:
		adjustment = frappe.get_doc(BATCH, existing, for_update=True)
		adjustment.check_permission("write")
		return _envelope(adjustment)
	adjustment = frappe.new_doc(BATCH)
	adjustment.company = doc.company
	adjustment.currency = doc.currency
	adjustment.opening_date = doc.opening_date
	adjustment.mode = "Adjustment"
	adjustment.status = "Draft"
	adjustment.version = 1
	adjustment.revision = cint(doc.revision) + 1
	adjustment.previous_batch = doc.name
	adjustment.root_batch = root.name
	adjustment.adjustment_key = doc.name
	adjustment.source_names_json = "[]"
	adjustment.summary_customer = doc.get("summary_customer")
	adjustment.summary_supplier = doc.get("summary_supplier")
	adjustment.set("rows", _rows(doc))
	_save(adjustment, insert=True)
	return _envelope(adjustment)


@frappe.whitelist()
def preview_import(company, text=None, file_url=None):
	context = _company_context(company)
	if not frappe.has_permission(BATCH, "write"):
		frappe.throw(_("没有导入期初余额的权限"), frappe.PermissionError)
	from china_finance.services.opening_balance_import import parse_import
	if bool(text) == bool(file_url):
		frappe.throw(_("请提供粘贴表格或一个已上传文件"))
	if file_url:
		file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
		if not file_name:
			frappe.throw(_("文件不存在"))
		file = frappe.get_doc("File", file_name)
		file.check_permission("read")
		if file.get("attached_to_doctype") == BATCH and file.get("attached_to_name"):
			attached = _get_batch(file.attached_to_name)
			if attached.company != company:
				frappe.throw(_("上传文件属于其他公司"), frappe.PermissionError)
		result = parse_import(content=file.get_content(), filename=file.file_name)
	else:
		result = parse_import(text=text)
	if not result["errors"]:
		try:
			result["rows"] = _normalise_rows(result["rows"], context)
		except (frappe.ValidationError, ValueError) as exc:
			result["errors"].append({"row_id": None, "field": "rows", "message": str(exc)})
	return result


@frappe.whitelist()
def download_template():
	if not frappe.has_permission(BATCH, "read"):
		frappe.throw(_("没有读取期初余额模板的权限"), frappe.PermissionError)
	from china_finance.services.opening_balance_import import download_template_bytes
	frappe.response["filename"] = "opening_balance_template.xlsx"
	frappe.response["filecontent"] = download_template_bytes()
	frappe.response["type"] = "binary"
	frappe.response["display_content_as"] = "attachment"
	frappe.response["content_type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
