"""China voucher ledger report."""

# Chinese user-facing messages intentionally use Chinese punctuation.
# ruff: noqa: RUF001

import re

import frappe
from frappe import _

from china_finance.services.account_display import (
	get_account_display_title,
	strip_account_company_suffix,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	from china_finance.services.voucher_preparation import check_company, get_draft_ledger_rows

	check_company(filters.company)
	draft_rows = get_draft_ledger_rows(filters)
	allowed = (
		frappe.get_list(
			"China Accounting Voucher",
			filters={"company": filters.company},
			pluck="name",
			limit_page_length=0,
		)
		if frappe.has_permission("China Accounting Voucher", "read")
		else []
	)
	filters.allowed_snapshots = allowed or [""]
	display_number_filter = filters.get("voucher_word")
	effective_posting_date = """CASE
		WHEN v.source_doctype='Period Closing Voucher' THEN pcv.period_end_date
		ELSE COALESCE(je.posting_date, pe.posting_date, v.posting_date)
	END"""
	effective_accounting_period = """CASE
		WHEN v.source_doctype='Period Closing Voucher'
			THEN CONCAT(YEAR(pcv.period_end_date), '-', LPAD(MONTH(pcv.period_end_date), 2, '0'))
		ELSE v.accounting_period
	END"""
	conditions = [
		"v.company=%(company)s",
		f"{effective_posting_date} BETWEEN %(from_date)s AND %(to_date)s",
		"v.docstatus=1",
		"v.name IN %(allowed_snapshots)s",
		"v.status IN ('Posted', 'Reversed')",
		"v.source_doctype IN ('Journal Entry', 'Payment Entry', 'Period Closing Voucher')",
		"v.source_event='Posting'",
		"""(
			(v.source_doctype='Journal Entry' AND je.name IS NOT NULL AND je.docstatus IN (1, 2))
			OR (v.source_doctype='Payment Entry' AND pe.name IS NOT NULL AND pe.docstatus IN (1, 2))
			OR (v.source_doctype='Period Closing Voucher' AND pcv.name IS NOT NULL AND pcv.docstatus IN (1, 2))
		)""",
	]
	for fieldname, column in (
		("accounting_period", effective_accounting_period),
		("account", "e.account"),
		("party_type", "e.party_type"),
		("party", "e.party"),
		("source_doctype", "v.source_doctype"),
		("source_name", "v.source_name"),
		("source_event", "v.source_event"),
	):
		if filters.get(fieldname):
			conditions.append(f"{column}=%({fieldname})s")
	status_filter = filters.get("voucher_status")
	if status_filter in ("未记账", "待记账"):
		conditions.append("1=0")
	elif not status_filter or status_filter == "全部有效凭证":
		conditions.append("v.status='Posted'")
	if status_filter:
		status_value = {
			"已记账": "Posted",
			"已提交": "Posted",
			"Posted": "Posted",
			"已冲销": "Reversed",
			"Reversed": "Reversed",
		}.get(status_filter)
		if status_value:
			filters.voucher_status = status_value
			conditions.append("v.status=%(voucher_status)s")
	if display_number_filter and not re.search(r"\d+$", str(display_number_filter)):
		conditions.append("v.voucher_word=%(voucher_word)s")
	if filters.get("voucher_number"):
		conditions.append("v.source_name=%(voucher_number)s")
	if filters.get("search_text"):
		filters.search_pattern = f"%{filters.search_text}%"
		conditions.append(
			"(v.statutory_number LIKE %(search_pattern)s OR v.source_name LIKE %(search_pattern)s OR v.remarks LIKE %(search_pattern)s OR pcv.remarks LIKE %(search_pattern)s OR e.account LIKE %(search_pattern)s OR e.remarks LIKE %(search_pattern)s)"
		)
	entries = frappe.db.sql(
		f"""
		SELECT v.name AS voucher_snapshot,
			{effective_posting_date} AS posting_date,
			{effective_accounting_period} AS accounting_period,
			CASE
				WHEN v.source_doctype='Period Closing Voucher'
					THEN COALESCE(NULLIF(v.statutory_number, ''), '期末结账')
				ELSE v.statutory_number
			END AS statutory_number,
			v.voucher_word, v.source_doctype, v.source_name, v.source_event,
			COALESCE(NULLIF(v.currency, ''), company.default_currency) AS currency,
			CASE WHEN v.status='Reversed' THEN 2 ELSE 1 END AS voucher_status,
			COALESCE(e.debit, 0) + COALESCE(e.credit, 0) AS base_total_amount,
			e.idx AS entry_idx,
			e.gl_entry,
			gle.voucher_detail_no,
			e.account AS account,
			e.account_currency,
			e.party_type, e.party, e.cost_center, e.project,
			CASE
				WHEN v.source_doctype='Journal Entry' THEN jea.user_remark
				WHEN v.source_doctype='Period Closing Voucher' THEN COALESCE(pcv.remarks, v.remarks, e.remarks)
				ELSE e.remarks
			END AS remarks,
			e.debit, e.credit
		FROM `tabChina Accounting Voucher` v
		INNER JOIN `tabCompany` company ON company.name=v.company
		INNER JOIN `tabChina Accounting Voucher Entry` e ON e.parent=v.name
		LEFT JOIN `tabGL Entry` gle ON gle.name=e.gl_entry
		LEFT JOIN `tabJournal Entry` je ON v.source_doctype='Journal Entry' AND je.name=v.source_name
		LEFT JOIN `tabJournal Entry Account` jea
			ON v.source_doctype='Journal Entry'
			AND jea.parent=v.source_name
			AND jea.parenttype='Journal Entry'
			AND jea.idx=e.idx
		LEFT JOIN `tabPayment Entry` pe ON v.source_doctype='Payment Entry' AND pe.name=v.source_name
		LEFT JOIN `tabPeriod Closing Voucher` pcv
			ON v.source_doctype='Period Closing Voucher' AND pcv.name=v.source_name
		WHERE {" AND ".join(conditions)}
		ORDER BY {effective_accounting_period},
			CASE WHEN v.source_doctype='Period Closing Voucher' THEN 1 ELSE 0 END,
			v.voucher_word, {effective_posting_date}, v.sequence_number, v.name, e.idx
		""",
		filters,
		as_dict=True,
	)
	# Source permissions also apply when viewing an accounting snapshot.
	entries = [
		entry for entry in entries if frappe.has_permission(entry.source_doctype, "read", entry.source_name)
	]
	from china_finance.services.voucher_posting_status import get_pending_ledger_rows

	entries.extend(get_pending_ledger_rows(filters))
	from china_finance.services.opening_identity import get_opening_sources

	opening_names = get_opening_sources(filters.company)
	entries = [
		entry for entry in entries
		if entry.source_doctype != "Journal Entry" or entry.source_name not in opening_names
	]
	entries.extend(draft_rows)
	if filters.get("receipt_import"):
		batch = frappe.get_doc("China Bank Receipt Import", filters.receipt_import)
		batch.check_permission("read")
		if batch.company != filters.company:
			frappe.throw(_("回单批次与公司不一致"))
		linked = set(
			frappe.get_all(
				"China Bank Receipt",
				filters={"name": ["in", [r.receipt for r in batch.rows if r.receipt] or [""]]},
				pluck="voucher_name",
			)
		)
		entries = [entry for entry in entries if entry.source_name in linked]
	entries.sort(
		key=lambda e: (
			str(e.posting_date),
			e.source_doctype == "Period Closing Voucher",
			e.source_name,
			e.entry_idx,
		)
	)
	from china_finance.services.source_voucher_edit import annotate_inline_edit_rows

	annotate_inline_edit_rows([entry for entry in entries if not entry.get("snapshot_pending")])
	_format_account_labels(entries, filters.company)
	if display_number_filter and re.search(r"\d+$", str(display_number_filter)):
		entries = [entry for entry in entries if entry.get("statutory_number") == display_number_filter]
	return get_columns(), build_tree_data(entries)


def _format_account_labels(entries, company):
	"""Render the canonical account name stored by the company chart."""
	account_names = {entry.get("account") for entry in entries if entry.get("account")}
	if not account_names:
		return

	accounts = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=["name", "account_number", "account_name", "parent_account"],
	)
	account_map = {account.name: account for account in accounts}
	label_cache = {}

	def get_label(account_name):
		if account_name in label_cache:
			return label_cache[account_name]

		account = account_map.get(account_name)
		if not account:
			label_cache[account_name] = strip_account_company_suffix(account_name, company)
			return label_cache[account_name]

		label = get_account_display_title(account.account_number, account.account_name)
		if not label:
			label = strip_account_company_suffix(account_name, company)
		label_cache[account_name] = label
		return label

	for entry in entries:
		if entry.get("account"):
			entry["account_label"] = get_label(entry.account)


def build_tree_data(entries):
	voucher_rows = {}
	voucher_order = []
	for entry in entries:
		voucher_snapshot = entry.voucher_snapshot
		if voucher_snapshot not in voucher_rows:
			voucher_order.append(voucher_snapshot)
			voucher_rows[voucher_snapshot] = []
		first_line = not voucher_rows[voucher_snapshot]
		voucher_rows[voucher_snapshot].append(
			{
				**entry,
				"is_voucher_first_row": first_line,
				"posting_date": entry.posting_date if first_line else None,
				"statutory_number": entry.statutory_number if first_line else None,
				"voucher_number": None,
				"print_voucher": None,
				"accounting_period": entry.accounting_period if first_line else None,
				"remarks": clean_voucher_summary(entry.remarks),
				"source_doctype": entry.source_doctype if first_line else None,
				"source_name": entry.source_name if first_line else None,
				"source_event": entry.source_event if first_line else None,
				"voucher_status": entry.voucher_status if first_line else None,
				"prepared_by": None,
				"modified_by": None,
				"row_id": f"{voucher_snapshot}:{entry.entry_idx}",
				"parent_row_id": None,
				"indent": 0,
			}
		)
	return [row for voucher_snapshot in voucher_order for row in voucher_rows[voucher_snapshot]]


def clean_voucher_summary(value):
	"""Remove bank reference metadata copied into historical summaries."""
	value = (value or "").strip()
	if "｜" in value:
		value = value.split("｜", 1)[0].strip()
	return re.split(r"\s*参考\s*#?.*$", value, maxsplit=1, flags=re.IGNORECASE)[0].strip()


def get_source_metadata(entry):
	values = frappe.db.get_value(
		entry.source_doctype,
		entry.source_name,
		["docstatus", "owner", "modified_by"],
		as_dict=True,
	)
	return values or frappe._dict(docstatus=1, owner=None, modified_by=None)


def get_auxiliary_accounting(entry):
	parts = []
	if entry.party:
		parts.append(f"{entry.party_type or '往来'}：{entry.party}")
	if entry.cost_center:
		parts.append(f"成本中心：{entry.cost_center}")
	if entry.project:
		parts.append(f"项目：{entry.project}")
	if entry.finance_book:
		parts.append(f"财务账簿：{entry.finance_book}")
	if entry.dimensions_json:
		parts.append(entry.dimensions_json)
	return "；".join(parts)


def get_columns():
	# Prepared reports persist columns. Keep source labels so the framework can
	# translate them for the viewing/exporting user instead of the worker locale.
	return [
		{"label": "凭证字号", "fieldname": "statutory_number", "fieldtype": "Data", "width": 100},
		{"label": "状态", "fieldname": "voucher_status", "fieldtype": "Data", "width": 85},
		{"label": "凭证日期", "fieldname": "posting_date", "fieldtype": "Date", "width": 130},
		# Keep these fields available to the report filters and export, but hide
		# them from the daily review grid so the voucher fits on one screen.
		{
			"label": "会计期间",
			"fieldname": "accounting_period",
			"fieldtype": "Data",
			"width": 110,
			"hidden": 1,
		},
		{
			"label": "摘要",
			"fieldname": "remarks",
			"fieldtype": "Data",
			"editable": 1,
			"width": 270,
		},
		{
			"label": "科目",
			"fieldname": "account",
			"fieldtype": "Link",
			"options": "Account",
			"editable": 1,
			"width": 330,
		},
		{
			"label": "往来单位",
			"fieldname": "party",
			"fieldtype": "Dynamic Link",
			"options": "party_type",
			"width": 150,
			"hidden": 1,
		},
		{
			"label": "借方",
			"fieldname": "debit",
			"fieldtype": "Currency",
			"editable": 1,
			"options": "currency",
			"width": 150,
		},
		{
			"label": "贷方",
			"fieldname": "credit",
			"fieldtype": "Currency",
			"editable": 1,
			"options": "currency",
			"width": 150,
		},
		{
			"label": "本位币金额",
			"fieldname": "base_total_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 160,
		},
		{
			"label": "本位币",
			"fieldname": "currency",
			"fieldtype": "Link",
			"options": "Currency",
			"hidden": 1,
		},
		{"label": "操作", "fieldname": "source_action", "fieldtype": "Data", "width": 260},
	]
