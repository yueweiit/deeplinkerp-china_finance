from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime

from china_finance.services.financial_statement import get_mappings, get_template
from china_finance.services.voucher import get_company_settings


DRAFT_ROLES = ("Accounts User", "China Finance User", "Accounts Manager", "China Finance Manager", "System Manager")
CONFIRM_ROLES = ("China Finance Manager", "Accounts Manager", "System Manager")
EPSILON = 0.005
INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
INTERNAL_TRANSFER_LABEL = _("内部资金划转（不计入现金流量表）")
INFLOW_ROWS = {
	"CASH_RECEIVED_SALES", "TAX_REFUNDS", "OTHER_OPERATING_RECEIPTS",
	"CASH_RECEIVED_INVESTMENT_RECOVERY", "CASH_RECEIVED_INVESTMENT_INCOME",
	"CASH_RECEIVED_ASSET_DISPOSAL", "CASH_RECEIVED_SUBSIDIARY_DISPOSAL", "OTHER_INVESTING_RECEIPTS",
	"CASH_RECEIVED_INVESTMENT", "CASH_RECEIVED_BORROWINGS", "OTHER_FINANCING_RECEIPTS",
}
OUTFLOW_ROWS = {
	"CASH_PAID_SUPPLIERS", "CASH_PAID_EMPLOYEES", "CASH_PAID_TAXES", "OTHER_OPERATING_PAYMENTS",
	"CASH_PAID_LONG_TERM_ASSETS", "CASH_PAID_INVESTMENTS", "CASH_PAID_SUBSIDIARY_ACQUISITION",
	"OTHER_INVESTING_PAYMENTS", "CASH_PAID_DEBT_REPAYMENT", "CASH_PAID_DIVIDENDS_INTEREST",
	"OTHER_FINANCING_PAYMENTS",
}


def is_direct_assignment_required(company, posting_date):
	settings = get_company_settings(company)
	return bool(
		settings
		and settings.cash_flow_assignment_activation_date
		and getdate(posting_date) >= getdate(settings.cash_flow_assignment_activation_date)
	)


def _require_roles(roles):
	frappe.only_for(roles)


def lock_assignment_sources(assignments):
	"""Acquire every source gate before any assignment locks, including native saves.

	The identity lookup may use an older read view: these source identities are
	immutable. Status, voucher and GL evidence must use current reads afterwards.
	"""
	from china_finance.services.voucher import GL_SOURCE_DOCTYPES

	sources = set()
	for assignment in assignments:
		if isinstance(assignment, str):
			identity = frappe.db.get_value("China Cash Flow Assignment", assignment,
				["china_accounting_voucher", "source_doctype", "source_name"], as_dict=True)
			if not identity:
				frappe.throw(_("现金流量指定单的来源身份不可见，请刷新后重试"), frappe.TimestampMismatchError)
		else:
			identity = None
			if assignment.get("name") and not assignment.is_new():
				identity = frappe.db.get_value("China Cash Flow Assignment", assignment.name,
					["china_accounting_voucher", "source_doctype", "source_name"], as_dict=True)
				if not identity:
					frappe.throw(_("现金流量指定单的来源身份不可见，请刷新后重试"), frappe.TimestampMismatchError)
			identity = identity or assignment
		voucher_source = frappe.db.get_value("China Accounting Voucher", identity.china_accounting_voucher,
			["source_doctype", "source_name"], as_dict=True) if identity.china_accounting_voucher else None
		source = voucher_source or identity
		if source.source_doctype in GL_SOURCE_DOCTYPES and source.source_name:
			sources.add((source.source_doctype, source.source_name))
	for doctype, name in sorted(sources):
		frappe.db.get_value(doctype, name, "name", for_update=True)


def _get_voucher(voucher_name, for_update=False):
	voucher = frappe.get_doc("China Accounting Voucher", voucher_name, for_update=for_update)
	if voucher.docstatus != 1:
		frappe.throw(_("仅已提交的中国会计凭证可以指定现金流量项目"))
	return voucher


def get_assignment_cash_legs(doc, for_update=True):
	"""Read authoritative evidence after the caller owns the early source gate."""
	from china_finance.services.voucher import GL_SOURCE_DOCTYPES

	voucher = _get_voucher(doc.china_accounting_voucher, for_update=for_update)
	if voucher.source_event != "Posting" or voucher.source_doctype not in GL_SOURCE_DOCTYPES:
		frappe.throw(_("现金流量指定必须来自有效的过账凭证"))
	if any(doc.get(field) != voucher.get(field) for field in ("company", "source_doctype", "source_name")) or getdate(doc.posting_date) != getdate(voucher.posting_date):
		frappe.throw(_("现金流量指定单的来源信息与凭证不一致"))
	status = frappe.db.get_value(voucher.source_doctype, voucher.source_name, "docstatus", for_update=for_update)
	if status != 1:
		frappe.throw(_("来源单据 {0} 已取消或未提交，不能修改或确认现金流量指定").format(voucher.source_name),
			frappe.CancelledLinkError if status == 2 else frappe.ValidationError)
	return get_cash_legs_for_voucher(voucher, for_update=for_update)


def validate_cash_evidence(doc, legs, old=None):
	"""Read-only UI metadata is not a server trust boundary, including native APIs."""
	by_gl = {leg.gl_entry: leg for leg in legs}
	old_rows = {}
	stored_rows = {}
	if old:
		stored_rows = {row.name: row for row in old.items}
		previous = frappe.get_doc(old.as_dict())
		_refresh_draft_cash_legs(previous, legs)
		old_rows = {row.name: row for row in previous.items}
	for row in doc.items:
		previous = old_rows.get(row.name)
		stored = stored_rows.get(row.name)
		if stored is None and row.gl_entry not in by_gl:
			# Native split_row creates a fresh child name while copying its old GL
			# evidence. Bind that row to this document's locked historical leg only.
			candidates = [
				candidate for candidate in stored_rows.values()
				if candidate.gl_entry == row.gl_entry
				and old_rows[candidate.name].gl_entry in by_gl
				and all(row.get(field) == candidate.get(field) for field in ("cash_account", "cash_direction", "counterpart_accounts"))
				and abs(flt(row.cash_amount) - flt(candidate.cash_amount)) <= EPSILON
			]
			if len({old_rows[candidate.name].gl_entry for candidate in candidates}) == 1:
				stored = candidates[0]
				previous = old_rows[stored.name]
		# Repair only the exact stored historical link, from the uniquely repaired
		# locked snapshot. Never normalize a forged change to a valid stored GL or
		# cash evidence. Manual allocations and the native old snapshot stay intact.
		if (
			previous and stored
			and row.gl_entry == stored.gl_entry
			and stored.gl_entry not in by_gl
			and previous.gl_entry in by_gl
			and all(row.get(field) == stored.get(field) for field in ("cash_account", "cash_direction", "counterpart_accounts"))
			and abs(flt(row.cash_amount) - flt(stored.cash_amount)) <= EPSILON
		):
			for field in ("gl_entry", "cash_account", "cash_direction", "cash_amount", "counterpart_accounts", "suggested_row_code", "suggested_row_label"):
				row.set(field, previous.get(field))
		leg = by_gl.get(row.gl_entry)
		if not leg or (previous and row.gl_entry != previous.gl_entry) or any(
			row.get(field) != leg.get(field) for field in ("cash_account", "cash_direction", "counterpart_accounts")
		) or abs(flt(row.cash_amount) - flt(leg.cash_amount)) > EPSILON:
			frappe.throw(_("现金分录证据与来源总账不一致，不能修改现金科目、收付方向或发生额"))


def _get_cash_template(company, posting_date):
	return get_template(company, "Cash Flow", posting_date)


def _valid_cash_flow_rows(company, posting_date):
	template = _get_cash_template(company, posting_date)
	return {
		row.row_code
		for row in template.rows
		if row.row_type == "Mapped Accounts" and row.row_code in INFLOW_ROWS | OUTFLOW_ROWS | {"FX_EFFECT"}
	}, template


@frappe.whitelist()
def get_cash_flow_row_options(name):
	doc = frappe.get_doc("China Cash Flow Assignment", name)
	if not doc.has_permission("read"):
		frappe.throw(_("无权查看该现金流量指定单"), frappe.PermissionError)
	valid_rows, template = _valid_cash_flow_rows(doc.company, doc.posting_date)
	return [
		{"value": INTERNAL_TRANSFER, "label": INTERNAL_TRANSFER_LABEL, "description": INTERNAL_TRANSFER}
	] + [
		{"value": row.row_code, "label": row.label, "description": row.row_code}
		for row in template.rows
		if row.row_code in valid_rows
	]


def _get_voucher_gl_rows(voucher, for_update=False):
	# ERPNext can assign the final GL Entry name after a source document's submit
	# hooks have run. Prefer the source document's current GL rows over the audit
	# snapshot's historical link so a cash-flow draft never relies on a temporary
	# GL name.
	if voucher.source_event == "Posting" and voucher.source_doctype and voucher.source_name:
		source_rows = frappe.db.get_values(
			"GL Entry",
			filters={
				"voucher_type": voucher.source_doctype,
				"voucher_no": voucher.source_name,
				"is_cancelled": 0,
				"is_opening": "No",
			},
			fieldname=["name", "account", "debit", "credit", "voucher_type", "voucher_no", "remarks"],
			order_by="creation asc, name asc",
			as_dict=True,
			for_update=for_update,
		)
		if source_rows:
			return source_rows

	entry_ids = [row.gl_entry for row in voucher.entries if row.gl_entry]
	if not entry_ids:
		return []
	return frappe.db.get_values(
		"GL Entry",
		filters={"name": ["in", entry_ids], "is_cancelled": 0, "is_opening": "No"},
		fieldname=["name", "account", "debit", "credit", "voucher_type", "voucher_no", "remarks"],
		order_by="creation asc, name asc",
		as_dict=True,
		for_update=for_update,
	)


def _suggestion_by_account(company, posting_date):
	template = _get_cash_template(company, posting_date)
	return {
		row.account: row
		for row in get_mappings(company, template, getdate(posting_date))
	}


def get_cash_legs_for_voucher(voucher, for_update=False):
	"""Return eligible cash GL rows. Pure cash/bank transfers are intentionally excluded."""
	gl_rows = _get_voucher_gl_rows(voucher, for_update=for_update)
	if not gl_rows:
		return []
	from china_finance.services.cash_equivalent_scope import get_cash_scope_accounts

	cash_accounts = set(get_cash_scope_accounts(voucher.company, voucher.posting_date))
	cash_rows = [row for row in gl_rows if row.account in cash_accounts]
	counterpart_rows = [row for row in gl_rows if row.account not in cash_accounts]
	if not cash_rows or not counterpart_rows:
		return []
	suggestions = _suggestion_by_account(voucher.company, voucher.posting_date)
	counterparts = ", ".join(sorted({row.account for row in counterpart_rows}))
	primary_counterpart = max(counterpart_rows, key=lambda row: abs(flt(row.debit) - flt(row.credit)))
	mapping = suggestions.get(primary_counterpart.account)
	legs = []
	for row in cash_rows:
		movement = flt(row.debit) - flt(row.credit)
		if abs(movement) <= EPSILON:
			continue
		direction = "收款" if movement > 0 else "付款"
		suggested = mapping.cash_inflow_row_code if movement > 0 and mapping else None
		if movement < 0 and mapping:
			suggested = mapping.cash_outflow_row_code
		legs.append(
			frappe._dict(
				gl_entry=row.name,
				cash_account=row.account,
				cash_direction=direction,
				cash_amount=abs(movement),
				counterpart_accounts=counterparts,
				suggested_row_code=suggested,
				suggested_row_label=mapping.label if mapping else None,
				cash_flow_row_code=suggested,
				assigned_amount=abs(movement),
			)
		)
	return legs


def _refresh_draft_cash_legs(doc, legs):
	"""Replace stale temporary GL names without overwriting the user's assignment."""
	by_old_entry = defaultdict(list)
	for row in doc.items:
		by_old_entry[row.gl_entry].append(row)

	available = {leg.gl_entry: leg for leg in legs}
	for old_entry, rows in by_old_entry.items():
		if old_entry in available:
			continue
		snapshot = rows[0]
		matches = [
			leg
			for leg in available.values()
			if leg.cash_account == snapshot.cash_account
			and leg.cash_direction == snapshot.cash_direction
			and abs(flt(leg.cash_amount) - flt(snapshot.cash_amount)) <= EPSILON
			and leg.counterpart_accounts == snapshot.counterpart_accounts
		]
		if len(matches) != 1:
			continue
		leg = matches[0]
		for row in rows:
			row.gl_entry = leg.gl_entry
			row.cash_account = leg.cash_account
			row.cash_direction = leg.cash_direction
			row.cash_amount = leg.cash_amount
			row.counterpart_accounts = leg.counterpart_accounts
			row.suggested_row_code = leg.suggested_row_code
			row.suggested_row_label = leg.suggested_row_label


def _get_active_assignment(voucher_name, for_update=False):
	return frappe.db.get_value(
		"China Cash Flow Assignment",
		{"china_accounting_voucher": voucher_name, "status": ["in", ["Draft", "Confirmed"]]},
		"name",
		order_by="revision desc, creation desc",
		for_update=for_update,
	)


@frappe.whitelist()
def get_cash_flow_assignment_for_voucher(voucher_name):
	voucher = _get_voucher(voucher_name)
	if not voucher.has_permission("read"):
		frappe.throw(_("无权查看该中国会计凭证"), frappe.PermissionError)
	assignments = frappe.get_all(
		"China Cash Flow Assignment",
		filters={"china_accounting_voucher": voucher.name},
		fields=["name", "status", "revision"],
		order_by="revision desc, creation desc",
		limit_page_length=1,
	)
	return assignments[0] if assignments else None


def _next_revision(voucher_name):
	maximum = frappe.db.get_value("China Cash Flow Assignment", {"china_accounting_voucher": voucher_name},
		"revision", order_by="revision desc", for_update=True)
	return (flt(maximum) or 0) + 1


def _create_cash_flow_assignment(voucher, ignore_permissions=True):
	probe = frappe.get_doc({"doctype": "China Cash Flow Assignment", "company": voucher.company,
		"posting_date": voucher.posting_date, "china_accounting_voucher": voucher.name,
		"source_doctype": voucher.source_doctype, "source_name": voucher.source_name})
	lock_assignment_sources([probe])
	if _get_active_assignment(voucher.name, for_update=True):
		frappe.throw(_("该凭证已有有效的现金流量指定单"))
	legs = get_assignment_cash_legs(probe)
	if not legs:
		return None
	revision = int(_next_revision(voucher.name))
	doc = frappe.get_doc(
		{
			"doctype": "China Cash Flow Assignment",
			"company": voucher.company,
			"posting_date": voucher.posting_date,
			"china_accounting_voucher": voucher.name,
			"assignment_key": f"{voucher.name}|{revision}",
			"revision": revision,
			"source_doctype": voucher.source_doctype,
			"source_name": voucher.source_name,
			"assigned_by": voucher.prepared_by,
			"assigned_on": now_datetime(),
			"items": legs,
		}
	)
	doc.flags.ignore_permissions = ignore_permissions
	doc.insert()
	return doc.name


def create_cash_flow_assignment(voucher_name, *, source=None):
	from china_finance.services.voucher import GL_SOURCE_DOCTYPES

	# Only server callers can supply this context; the public source API obtains
	# it from its authorized, current source document. Otherwise probe immutable
	# voucher identity without locking, before any voucher/assignment locks.
	if source is None:
		identity = frappe.db.get_value("China Accounting Voucher", voucher_name,
			["source_doctype", "source_name"], as_dict=True)
		if not identity:
			frappe.throw(_("中国会计凭证的来源身份不可见，请刷新后重试"), frappe.TimestampMismatchError)
		source = (identity.source_doctype, identity.source_name)
	if source[0] not in GL_SOURCE_DOCTYPES or not source[1]:
		frappe.throw(_("现金流量指定必须来自有效的过账凭证"))
	probe = frappe.get_doc({"doctype": "China Cash Flow Assignment",
		"source_doctype": source[0], "source_name": source[1]})
	lock_assignment_sources([probe])
	voucher = _get_voucher(voucher_name, for_update=True)
	if (voucher.source_doctype, voucher.source_name) != source:
		frappe.throw(_("现金流量指定单的来源信息与凭证不一致"))
	if voucher.source_event != "Posting":
		return None
	existing = _get_active_assignment(voucher.name, for_update=True)
	if existing:
		return existing
	return _create_cash_flow_assignment(voucher)


def create_assignment_if_required(voucher_name):
	voucher = _get_voucher(voucher_name)
	if is_direct_assignment_required(voucher.company, voucher.posting_date):
		return create_cash_flow_assignment(voucher.name)
	return None


def _ensure_draft(name):
	lock_assignment_sources([name])
	doc = frappe.get_doc("China Cash Flow Assignment", name, for_update=True)
	if doc.status != "Draft" or doc.docstatus != 0:
		frappe.throw(_("只有草稿现金流量指定单可执行此操作"))
	return doc


@frappe.whitelist()
def reload_cash_flow_assignment_suggestions(name):
	_require_roles(DRAFT_ROLES)
	doc = _ensure_draft(name)
	legs = get_assignment_cash_legs(doc)
	if not legs:
		frappe.throw(_("该凭证不包含需要指定的外部现金流"))
	doc.set("items", legs)
	doc.assigned_by = frappe.session.user
	doc.assigned_on = now_datetime()
	doc.save()
	return {"name": doc.name, "item_count": len(doc.items)}


def _validate_assignment_rows(doc, legs=None):
	valid_rows, _template = _valid_cash_flow_rows(doc.company, doc.posting_date)
	legs = {row.gl_entry: row for row in (legs if legs is not None else get_cash_legs_for_voucher(_get_voucher(doc.china_accounting_voucher)))}
	allocated = defaultdict(float)
	for row in doc.items:
		if row.gl_entry not in legs:
			frappe.throw(_("现金总账分录 {0} 不属于该凭证或已失效").format(row.gl_entry))
		if row.cash_flow_row_code not in valid_rows and row.cash_flow_row_code != INTERNAL_TRANSFER:
			frappe.throw(_("现金流量项目 {0} 不是当前模板的明细项目").format(row.cash_flow_row_code))
		if row.cash_flow_row_code != INTERNAL_TRANSFER:
			if row.cash_direction == "收款" and row.cash_flow_row_code not in INFLOW_ROWS | {"FX_EFFECT"}:
				frappe.throw(_("收款分录只能指定到现金流入项目"))
			if row.cash_direction == "付款" and row.cash_flow_row_code not in OUTFLOW_ROWS | {"FX_EFFECT"}:
				frappe.throw(_("付款分录只能指定到现金流出项目"))
		if flt(row.assigned_amount) <= 0:
			frappe.throw(_("指定金额必须大于零"))
		allocated[row.gl_entry] += flt(row.assigned_amount)
	for gl_entry, leg in legs.items():
		if abs(allocated[gl_entry] - flt(leg.cash_amount)) > EPSILON:
			frappe.throw(
				_("现金分录 {0} 指定金额必须等于 {1}，当前为 {2}").format(
					gl_entry, flt(leg.cash_amount), flt(allocated[gl_entry])
				)
			)


def prepare_cash_flow_assignment_confirmation(doc, for_update=True):
	_require_roles(CONFIRM_ROLES)
	# Frappe sets docstatus=1 before calling before_submit. Validate the
	# business status here and only reject an already cancelled document.
	if doc.status != "Draft" or doc.docstatus == 2:
		frappe.throw(_("只有草稿现金流量指定单可执行此操作"))
	legs = get_assignment_cash_legs(doc, for_update=for_update)
	_refresh_draft_cash_legs(doc, legs)
	validate_cash_evidence(doc, legs)
	_validate_assignment_rows(doc, legs)
	valid_rows, template = _valid_cash_flow_rows(doc.company, doc.posting_date)
	labels = {row.row_code: row.label for row in template.rows if row.row_code in valid_rows}
	labels[INTERNAL_TRANSFER] = INTERNAL_TRANSFER_LABEL
	for row in doc.items:
		row.cash_flow_row_label = labels.get(row.cash_flow_row_code, row.cash_flow_row_code)
	settings = get_company_settings(doc.company)
	if settings and settings.enforce_role_separation and doc.assigned_by == frappe.session.user:
		frappe.throw(_("启用职责分离时，现金流量指定人与确认人不能为同一用户"))
	doc.status = "Confirmed"
	doc.confirmed_by = frappe.session.user
	doc.confirmed_on = now_datetime()
	doc.flags.ignore_cash_flow_assignment_status = True


@frappe.whitelist()
def confirm_cash_flow_assignment(name):
	"""Compatibility endpoint for older clients; use native Frappe submission."""
	doc = _ensure_draft(name)
	doc.submit()
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def cancel_cash_flow_assignment(name, reason):
	_require_roles(CONFIRM_ROLES)
	if not reason:
		frappe.throw(_("请填写作废原因"))
	lock_assignment_sources([name])
	doc = frappe.get_doc("China Cash Flow Assignment", name, for_update=True)
	doc.check_permission("read")
	doc.check_permission("write")
	if doc.status == "Cancelled":
		return {"name": doc.name, "status": doc.status}
	doc.status = "Cancelled"
	doc.cancelled_by = frappe.session.user
	doc.cancelled_on = now_datetime()
	doc.cancellation_reason = reason
	doc.flags.ignore_cash_flow_assignment_status = True
	if doc.docstatus == 0:
		doc.save()
	else:
		doc.cancel()
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def recreate_cash_flow_assignment(name):
	_require_roles(DRAFT_ROLES)
	lock_assignment_sources([name])
	previous = frappe.get_doc("China Cash Flow Assignment", name, for_update=True)
	previous.check_permission("read")
	previous.check_permission("write")
	if previous.status != "Cancelled":
		frappe.throw(_("只有已作废的现金流量指定单可以重新创建"))
	voucher = _get_voucher(previous.china_accounting_voucher, for_update=True)
	frappe.has_permission("Company", "read", voucher.company, throw=True)
	if voucher.source_event != "Posting":
		frappe.throw(_("仅过账凭证可以重新创建现金流量指定单"))
	if not frappe.db.get_value(voucher.source_doctype, {"name": voucher.source_name, "docstatus": 1}, "name", for_update=True):
		frappe.throw(_("来源单据已取消，不能重新创建现金流量指定单"))
	if _get_active_assignment(voucher.name, for_update=True):
		frappe.throw(_("该凭证已有有效的现金流量指定单"))
	assignment = _create_cash_flow_assignment(voucher, ignore_permissions=False)
	if not assignment:
		frappe.throw(_("该凭证不包含需要指定的外部现金流"))
	return {"name": assignment, "revision": frappe.db.get_value("China Cash Flow Assignment", assignment, "revision")}


def cancel_assignments_for_source(doc):
	from china_finance.services.voucher import GL_SOURCE_DOCTYPES

	# Only a persisted, cancelled GL source authorizes this audit transition.
	# Do not trust an in-memory docstatus or relax ordinary assignment saves.
	if doc.doctype not in GL_SOURCE_DOCTYPES or frappe.db.get_value(doc.doctype, doc.name, "docstatus", for_update=True) != 2:
		frappe.throw(_("来源单据尚未取消或不是支持的总账来源，不能作废现金流量指定单"))
	assignments = frappe.db.sql(
		"""SELECT name FROM `tabChina Cash Flow Assignment`
		WHERE source_doctype=%s AND source_name=%s AND status!='Cancelled' FOR UPDATE""",
		(doc.doctype, doc.name),
	)
	for (name,) in assignments:
		assignment = frappe.get_doc("China Cash Flow Assignment", name, for_update=True)
		assignment.flags.ignore_permissions = True
		reason = _("来源单据已取消：{0}").format(doc.name)
		assignment.status = "Cancelled"
		assignment.cancelled_by = frappe.session.user
		assignment.cancelled_on = now_datetime()
		assignment.cancellation_reason = reason
		assignment.flags.ignore_cash_flow_assignment_status = True
		if assignment.docstatus == 0:
			# Preserve the historical source and GL links verbatim. ERPNext may
			# remove GL rows at cancellation; drafts still validate links unlike
			# Frappe's native cancel operation for submitted assignments.
			assignment.flags.ignore_links = True
			assignment.save()
		else:
			assignment.cancel()


def get_confirmed_cash_flow_values(company, from_date, to_date, finance_book=None, cost_center=None, project=None):
	from china_finance.services.cash_equivalent_scope import get_cash_scope_accounts

	cash_accounts = get_cash_scope_accounts(company, to_date)
	if not cash_accounts:
		return defaultdict(float), set(), set()
	conditions = [
		"assignment.company=%(company)s",
		"assignment.status='Confirmed'",
		"gle.is_cancelled=0",
		"gle.is_opening='No'",
		"gle.posting_date BETWEEN %(from_date)s AND %(to_date)s",
	]
	conditions.append("gle.account IN %(cash_accounts)s")
	values = {"company": company, "from_date": from_date, "to_date": to_date, "cash_accounts": cash_accounts}
	for fieldname, value in (("finance_book", finance_book), ("cost_center", cost_center), ("project", project)):
		if value:
			conditions.append(f"gle.{fieldname}=%({fieldname})s")
			values[fieldname] = value
	rows = frappe.db.sql(
		f"""
		SELECT item.cash_flow_row_code, item.assigned_amount, item.cash_direction,
			gle.voucher_type, gle.voucher_no, item.gl_entry
		FROM `tabChina Cash Flow Assignment` assignment
		INNER JOIN `tabChina Cash Flow Assignment Item` item ON item.parent=assignment.name
		INNER JOIN `tabGL Entry` gle ON gle.name=item.gl_entry
		WHERE {' AND '.join(conditions)}
		""",
		values,
		as_dict=True,
	)
	result = defaultdict(float)
	confirmed_sources = set()
	confirmed_entries = set()
	for row in rows:
		amount = flt(row.assigned_amount)
		confirmed_sources.add((row.voucher_type, row.voucher_no))
		confirmed_entries.add(row.gl_entry)
		if row.cash_flow_row_code == INTERNAL_TRANSFER:
			continue
		result[row.cash_flow_row_code] += -amount if row.cash_flow_row_code == "FX_EFFECT" and row.cash_direction == "付款" else amount
	return result, confirmed_sources, confirmed_entries


def get_assignment_coverage(company, from_date, to_date):
	settings = get_company_settings(company)
	if not settings or not settings.cash_flow_assignment_activation_date:
		return {"passed": True, "count": 0, "amount": 0, "details": _("未配置现金流量直接指定启用日期")}
	start = max(getdate(from_date), getdate(settings.cash_flow_assignment_activation_date))
	if start > getdate(to_date):
		return {"passed": True, "count": 0, "amount": 0, "details": ""}
	from china_finance.services.cash_equivalent_scope import get_cash_scope_accounts
	cash_accounts = get_cash_scope_accounts(company, to_date)
	if not cash_accounts:
		return {"passed": False, "count": 0, "amount": 0, "details": _("尚未配置现金及现金等价物范围")}
	rows = frappe.db.sql(
		"""
		SELECT candidate.voucher_type, candidate.voucher_no, candidate.cash_amount
		FROM (
			SELECT gle.voucher_type, gle.voucher_no,
				SUM(CASE WHEN gle.account IN %(cash_accounts)s THEN ABS(gle.debit-gle.credit) ELSE 0 END) AS cash_amount,
				SUM(CASE WHEN gle.account IN %(cash_accounts)s THEN 1 ELSE 0 END) AS cash_entries,
				SUM(CASE WHEN gle.account NOT IN %(cash_accounts)s THEN 1 ELSE 0 END) AS counterpart_entries
			FROM `tabGL Entry` gle
			INNER JOIN `tabAccount` account ON account.name=gle.account
			WHERE gle.company=%(company)s AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s
				AND gle.is_cancelled=0 AND gle.is_opening='No'
			GROUP BY gle.voucher_type, gle.voucher_no
		) candidate
		LEFT JOIN `tabChina Accounting Voucher` voucher
			ON voucher.source_key=CONCAT('Posting|', candidate.voucher_type, '|', candidate.voucher_no)
			AND voucher.docstatus=1
		LEFT JOIN `tabChina Cash Flow Assignment` assignment
			ON assignment.china_accounting_voucher=voucher.name AND assignment.status='Confirmed'
		WHERE candidate.cash_entries > 0 AND candidate.counterpart_entries > 0 AND assignment.name IS NULL
		""",
		{"company": company, "from_date": start, "to_date": to_date, "cash_accounts": cash_accounts},
		as_dict=True,
	)
	amount = sum(flt(row.cash_amount) for row in rows)
	return {
		"passed": not rows,
		"count": len(rows),
		"amount": flt(amount, 2),
		"details": _("启用日后有 {0} 张含外部现金流的凭证未确认指定，金额 {1}").format(len(rows), flt(amount, 2)) if rows else "",
	}


def get_closing_assignment_check(company, from_date, to_date, closing_type="Monthly"):
	"""Keep actual coverage visible; only defer missing confirmations at monthly close."""
	coverage = get_assignment_coverage(company, from_date, to_date)
	settings = get_company_settings(company)
	deferred = bool(
		closing_type == "Monthly"
		and settings
		and settings.get("defer_cash_flow_confirmation_on_monthly_close")
		and not coverage["passed"]
		and coverage.get("count")
	)
	details = coverage["details"]
	if deferred:
		details += "；" + _("本次月结暂不强制确认，现金流量表仍待核对；年结和正式报表仍须完成确认。")
	return {
		"check_code": "CASH_FLOW_ASSIGNMENT",
		"description": _("启用日后现金流量项目均已确认指定"),
		"passed": int(bool(coverage["passed"])),
		"severity": "Warning" if deferred else "Blocking",
		"details": details,
	}


@frappe.whitelist()
def preview_cash_flow_assignment_coverage(company, from_date, to_date):
	if not frappe.has_permission("Company", "read", company):
		frappe.throw(_("无权查看该公司"), frappe.PermissionError)
	return get_assignment_coverage(company, getdate(from_date), getdate(to_date))


@frappe.whitelist()
def get_cash_flow_assignment_list(
	company=None, from_date=None, to_date=None, status="Draft", search=None, direction=None,
	start=0, page_length=20,
):
	from china_finance.services.cash_flow_assignment_list import get_assignment_list

	return get_assignment_list(company, from_date, to_date, status, search, direction, start, page_length)


@frappe.whitelist(methods=["POST"])
def process_cash_flow_assignments(assignments, action="save"):
	from china_finance.services.cash_flow_assignment_list import process_assignments

	return process_assignments(assignments, action)
