"""Permission-aware document list and atomic per-assignment edits.

Confirmation remains the native controller's responsibility. This service accepts
only allocation fields and copies cash evidence from the locked stored document.
"""

# Chinese user-facing messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

from contextlib import contextmanager
from math import isfinite

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate

from china_finance.services import cash_flow_assignment as core

DOCTYPE = "China Cash Flow Assignment"
ITEM_DOCTYPE = "China Cash Flow Assignment Item"
VOUCHER_DOCTYPE = "China Accounting Voucher"
DOCUMENT_FIELDS = (
	"name",
	"company",
	"currency",
	"posting_date",
	"status",
	"docstatus",
	"modified",
	"china_accounting_voucher",
	"source_doctype",
	"source_name",
	"revision",
	"remarks",
	"assigned_by",
	"assigned_on",
	"confirmed_by",
	"confirmed_on",
	"cancelled_by",
	"cancelled_on",
	"cancellation_reason",
)
ITEM_FIELDS = (
	"name",
	"idx",
	"gl_entry",
	"cash_account",
	"cash_direction",
	"cash_amount",
	"counterpart_accounts",
	"suggested_row_code",
	"suggested_row_label",
	"cash_flow_row_code",
	"cash_flow_row_label",
	"assigned_amount",
	"remarks",
)
EDITABLE_ITEM_FIELDS = ("cash_flow_row_code", "assigned_amount", "remarks")
ALLOWED_ITEM_KEYS = {"name", "gl_entry", *EDITABLE_ITEM_FIELDS}
ALLOWED_DOCUMENT_KEYS = {"name", "modified", "items", "remarks"}
STATUSES = ("Draft", "Confirmed", "Cancelled")


@contextmanager
def _quiet_validation():
	# Diagnostics and per-document failures belong in the returned row. Avoid
	# Frappe opening an unrelated modal for every diagnostic in a list response.
	previous = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	try:
		yield
	finally:
		frappe.flags.mute_messages = previous


def _has_role(roles):
	return frappe.session.user == "Administrator" or bool(set(frappe.get_roles()) & set(roles))


def _has_permission(doc, ptype):
	with _quiet_validation():
		return bool(doc.has_permission(ptype))


def _confirmation_issue(doc):
	"""Diagnose using existing confirmation rules on an unpersisted copy."""
	probe = frappe.get_doc(doc.as_dict())
	probe._action = "submit"
	try:
		voucher = core._get_voucher(doc.china_accounting_voucher)
		legs = core.get_cash_legs_for_voucher(voucher)
		core._refresh_draft_cash_legs(probe, legs)
		probe._validate_links()
	except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError) as exc:
		return {"message": str(exc), "scope": "document"}
	try:
		core.prepare_cash_flow_assignment_confirmation(probe, for_update=False)
	except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError) as exc:
		message = str(exc)
		if any(row.gl_entry not in {leg.gl_entry for leg in legs} for row in probe.items):
			return {"message": message, "scope": "document"}
		# The original hook validates rows before duties. Re-run its existing row
		# validator only to label editable errors for the live client, not to
		# implement a second set of confirmation rules.
		try:
			core._validate_assignment_rows(probe)
		except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError) as row_exc:
			if str(row_exc) == message:
				return {"message": message, "scope": "items"}
		return {"message": message, "scope": "document"}
	return None


def _readable_voucher_summaries(filters):
	# Assignment access does not grant access to its linked audit voucher. Use
	# the voucher's native list permissions for both display and search, so an
	# otherwise visible assignment cannot disclose a restricted summary.
	with _quiet_validation():
		try:
			rows = frappe.get_list(VOUCHER_DOCTYPE, filters=filters, fields=["name", "remarks"], limit=0)
		except frappe.PermissionError:
			return {}
	return {row.name: row.remarks for row in rows}


def serialize_assignment(doc, voucher_summaries=None):
	result = {field: doc.get(field) for field in DOCUMENT_FIELDS}
	if voucher_summaries is None:
		voucher_summaries = _readable_voucher_summaries({"name": doc.china_accounting_voucher})
	result["voucher_summary"] = voucher_summaries.get(doc.china_accounting_voucher)
	result["currency"] = doc.currency or frappe.get_cached_value("Company", doc.company, "default_currency")
	result["items"] = [{field: row.get(field) for field in ITEM_FIELDS} for row in doc.items]
	draft = doc.status == "Draft" and doc.docstatus == 0
	result["can_write"] = bool(draft and _has_role(core.DRAFT_ROLES) and _has_permission(doc, "write"))
	result["can_confirm"] = bool(
		result["can_write"] and _has_role(core.CONFIRM_ROLES) and _has_permission(doc, "submit")
	)
	details = []
	with _quiet_validation():
		try:
			result["options"] = [
				{
					"value": row["value"],
					"label": row["label"],
					"direction": (
						"收款"
						if row["value"] in core.INFLOW_ROWS
						else "付款"
						if row["value"] in core.OUTFLOW_ROWS
						else "all"
					),
				}
				for row in core.get_cash_flow_row_options(doc.name)
			]
		except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError) as exc:
			result["options"] = []
			details.append({"message": str(exc), "scope": "document"})
		if draft:
			if not result["can_write"]:
				details.append({"message": _("无权修改该现金流量指定单"), "scope": "document"})
			elif not result["can_confirm"]:
				details.append({"message": _("无权确认该现金流量指定单"), "scope": "document"})
			elif not details and (issue := _confirmation_issue(doc)):
				details.append(issue)
	result["blocker_details"] = details
	result["blockers"] = [item["message"] for item in details]
	return result


def get_assignment_list(company, from_date, to_date, status, search, direction, start, page_length):
	status = status or "Draft"
	if status not in (*STATUSES, "All"):
		frappe.throw(_("请选择有效的现金流量指定单状态"))
	if direction and direction not in ("收款", "付款"):
		frappe.throw(_("请选择有效的收付方向"))
	if from_date and to_date and getdate(from_date) > getdate(to_date):
		frappe.throw(_("开始日期不能晚于结束日期"))
	filters = []
	if company:
		filters.append([DOCTYPE, "company", "=", company])
	if from_date:
		filters.append([DOCTYPE, "posting_date", ">=", getdate(from_date)])
	if to_date:
		filters.append([DOCTYPE, "posting_date", "<=", getdate(to_date)])
	if direction:
		matching = frappe.get_list(
			DOCTYPE,
			filters=[*filters, [ITEM_DOCTYPE, "cash_direction", "=", direction]],
			fields=["name"],
			distinct=True,
			limit=0,
		)
		if not matching:
			return {"assignments": [], "total": 0, "counts": {**dict.fromkeys(STATUSES, 0), "All": 0}}
		# Search and direction may match different cash legs of the same parent.
		filters.append([DOCTYPE, "name", "in", [row.name for row in matching]])
	or_filters = []
	if search and (term := str(search).strip()):
		for doctype, fields in (
			(DOCTYPE, ("name", "china_accounting_voucher", "source_doctype", "source_name", "remarks")),
			(
				ITEM_DOCTYPE,
				(
					"gl_entry",
					"cash_account",
					"counterpart_accounts",
					"suggested_row_code",
					"suggested_row_label",
					"cash_flow_row_code",
					"cash_flow_row_label",
					"remarks",
				),
			),
		):
			or_filters.extend([doctype, field, "like", f"%{term}%"] for field in fields)
		voucher_filters = {"remarks": ["like", f"%{term}%"]}
		if company:
			voucher_filters["company"] = company
		if matching_vouchers := _readable_voucher_summaries(voucher_filters):
			or_filters.append([DOCTYPE, "china_accounting_voucher", "in", list(matching_vouchers)])
	# get_list applies native role, sharing and Company User Permission filters.
	# DISTINCT selects parent identities before pagination: a direction/search
	# match must never truncate the other cash legs in the same document.
	rows = frappe.get_list(
		DOCTYPE,
		filters=filters,
		or_filters=or_filters,
		fields=["name", "status", "posting_date", "modified"],
		order_by="posting_date desc, modified desc, name desc",
		distinct=True,
		limit=0,
	)
	counts = dict.fromkeys(STATUSES, 0)
	for row in rows:
		if row.status in counts:
			counts[row.status] += 1
	counts["All"] = len(rows)
	selected = [row for row in rows if status == "All" or row.status == status]
	start = max(cint(start), 0)
	page_length = min(max(cint(page_length), 1), 100)
	documents = []
	for row in selected[start : start + page_length]:
		doc = frappe.get_doc(DOCTYPE, row.name)
		doc.check_permission("read")
		documents.append(doc)
	voucher_summaries = (
		_readable_voucher_summaries({"name": ["in", [doc.china_accounting_voucher for doc in documents]]})
		if documents
		else {}
	)
	assignments = [serialize_assignment(doc, voucher_summaries) for doc in documents]
	return {"assignments": assignments, "total": len(selected), "counts": counts}


def _apply_allocations(doc, payload):
	if set(payload) - ALLOWED_DOCUMENT_KEYS:
		frappe.throw(_("只能修改现金流量指定项目、金额和说明，不能修改来源证据"))
	items = payload.get("items")
	if not isinstance(items, list):
		frappe.throw(_("请提交该指定单的完整明细"))
	stored_rows = {row.name: row for row in doc.items}
	stored_gl = {row.name: row.gl_entry for row in doc.items}
	# Use the established repair before native save so historical temporary GL
	# links do not prevent submission. Editable allocations remain untouched.
	legs = core.get_assignment_cash_legs(doc)
	core._refresh_draft_cash_legs(doc, legs)
	valid_gl = {leg.gl_entry for leg in legs}
	canonical_by_gl = {}
	for row in doc.items:
		if row.gl_entry in valid_gl:
			canonical_by_gl[row.gl_entry] = row
			canonical_by_gl[stored_gl[row.name]] = row
	new_rows = []
	seen_names = set()
	for item in items:
		if not isinstance(item, dict) or set(item) - ALLOWED_ITEM_KEYS:
			frappe.throw(_("明细包含不可修改的现金分录证据字段"))
		name, gl_entry = item.get("name"), item.get("gl_entry")
		if name:
			if name not in stored_rows or name in seen_names:
				frappe.throw(_("指定明细 {0} 不属于本单或重复提交").format(name))
			seen_names.add(name)
			canonical = stored_rows[name]
			if gl_entry not in (stored_gl[name], canonical.gl_entry):
				frappe.throw(_("不能修改指定明细的现金总账分录"))
		else:
			canonical = canonical_by_gl.get(gl_entry)
			if not canonical:
				frappe.throw(_("新增拆分必须来自本单已有的合法现金总账分录"))
		if canonical.gl_entry not in valid_gl:
			frappe.throw(_("现金总账分录 {0} 不属于该凭证或已失效").format(canonical.gl_entry))
		values = {field: canonical.get(field) for field in ITEM_FIELDS if field not in ("name", "idx")}
		if name:
			values["name"] = name
		for field in EDITABLE_ITEM_FIELDS:
			if field in item:
				values[field] = item[field]
		try:
			if values.get("assigned_amount") is not None and not isfinite(float(values["assigned_amount"])):
				raise ValueError
		except (ValueError, TypeError, OverflowError):
			frappe.throw(_("指定金额必须是有效数字"))
		new_rows.append(values)
	doc.set("items", new_rows)
	if "remarks" in payload:
		doc.remarks = payload["remarks"]


def process_assignments(assignments, action):
	assignments = frappe.parse_json(assignments) if isinstance(assignments, str) else assignments
	if action not in ("save", "confirm"):
		frappe.throw(_("请选择保存草稿或确认指定"))
	if not isinstance(assignments, list) or not 1 <= len(assignments) <= 100:
		frappe.throw(_("请选择 1 至 100 张现金流量指定单"))
	names = [payload.get("name") for payload in assignments if isinstance(payload, dict) and payload.get("name")]
	if any(not isinstance(name, str) for name in names):
		frappe.throw(_("请指定有效的现金流量指定单编号"))
	if len(set(names)) != len(names):
		frappe.throw(_("同一批次不能重复提交现金流量指定单"))
	# Keep the same hierarchy as native saves and source-cancellation sync. All
	# source gates precede every assignment lock, even across multiple sources.
	core.lock_assignment_sources(names)
	for name in sorted(names):
		frappe.db.get_value(DOCTYPE, name, "name", for_update=True)
	results = []
	for payload in assignments:
		name = payload.get("name") if isinstance(payload, dict) else None
		point = "cash_flow_list_" + frappe.generate_hash(length=12)
		frappe.db.savepoint(point)
		with _quiet_validation():
			try:
				if not name:
					frappe.throw(_("请指定需要处理的现金流量指定单"))
				doc = frappe.get_doc(DOCTYPE, name, for_update=True)
				doc.check_permission("read")
				doc.check_permission("write")
				core._require_roles(core.DRAFT_ROLES)
				if action == "confirm":
					doc.check_permission("submit")
					core._require_roles(core.CONFIRM_ROLES)
				if not payload.get("modified") or get_datetime(payload["modified"]) != get_datetime(
					doc.modified
				):
					frappe.throw(
						_("现金流量指定单已被其他用户修改，请刷新后重试"), frappe.TimestampMismatchError
					)
				if action == "confirm" and doc.status == "Confirmed" and doc.docstatus == 1:
					results.append({"name": name, "ok": True, "document": serialize_assignment(doc)})
				else:
					if doc.status != "Draft" or doc.docstatus != 0:
						frappe.throw(_("只有草稿现金流量指定单可执行此操作"))
					_apply_allocations(doc, payload)
					doc.save()
					if action == "confirm":
						doc.submit()
					doc.reload()
					results.append({"name": name, "ok": True, "document": serialize_assignment(doc)})
			except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
				# Deadlock victims may already have lost all writes/savepoints. Only
				# the transaction owner can safely retry the entire operation.
				raise
			except Exception as exc:
				frappe.db.rollback(save_point=point)
				results.append(
					{"name": name, "ok": False, "error": str(exc), "error_type": type(exc).__name__}
				)
			frappe.db.release_savepoint(point)
	return {"results": results}
