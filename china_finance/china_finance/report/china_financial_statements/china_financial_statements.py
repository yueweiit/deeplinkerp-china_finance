import frappe
from frappe import _

from china_finance.services.currency_context import bind_report_currency, company_currency

from china_finance.services.account_display import get_account_display_title
from frappe.utils import flt, getdate, nowdate
from frappe.utils.file_manager import save_file
from frappe.utils.pdf import get_pdf

from china_finance.services.expense_turnover import (
	adjust_account_expense_turnover,
	adjust_party_expense_turnover,
	get_activity_gl_query,
	get_expense_turnover_offsets,
)
from china_finance.services.financial_statement import (
	build_statement,
	get_comparison_period,
	get_fiscal_year_start,
	get_template,
	get_unclosed_profit,
)


def _apply_default_period(filters):
	"""Resolve the report period before the browser-side filter script is ready."""
	to_date = getdate(filters.to_date or nowdate())
	filters.to_date = to_date
	if not filters.get("fiscal_year"):
		from erpnext.accounts.utils import get_fiscal_year

		filters.fiscal_year = get_fiscal_year(to_date, company=filters.company)[0]
	filters.from_date = getdate(filters.from_date or get_fiscal_year_start(filters.company, to_date))


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_apply_default_period(filters)
	columns, rows, *rest = _execute(filters)
	# Localize the report projection; keep template labels and row codes intact.
	for row in rows:
		for field in ("label", "asset_label", "liability_equity_label"):
			if row.get(field):
				row[field] = _(row[field])
	if frappe.has_permission("Journal Entry", "read"):
		pending = frappe.get_list("Journal Entry", filters={"company": filters.company, "docstatus": 0,
			"posting_date": ["between", [filters.from_date, filters.to_date]]}, fields=[{"COUNT": "name", "as": "total"}])[0].total
		if pending:
			message = _("本期另有 {0} 张未记账凭证，尚未计入本报表。记账前可在查凭证中查看未记账试算。").format(pending)
			if rest:
				rest[0] = (rest[0] or "") + "<br>" + message
			else:
				rest.append(message)
	bind_report_currency(columns, rows, filters.company, force_company=True)
	if len(rest) > 1 and isinstance(rest[1], dict):
		rest[1]["currency"] = company_currency(filters.company)
	return columns, rows, *rest


def _execute(filters=None):
	filters = frappe._dict(filters or {})
	_apply_default_period(filters)
	if filters.statement_type in ("Trial Balance", "Account Activity and Balance"):
		if filters.get("expand_party"):
			return execute_account_activity_balance(filters)
		return execute_native_trial_balance(
			filters,
			activity_balance=filters.statement_type == "Account Activity and Balance",
		)
	result = build_statement(
		filters.company,
		filters.statement_type,
		filters.from_date,
		filters.to_date,
		filters.finance_book,
		filters.cost_center,
		filters.project,
	)
	comparison_from = filters.comparison_from_date
	comparison_to = filters.comparison_to_date
	if comparison_to and get_template(
		filters.company, filters.statement_type, comparison_to, required=False
	):
		comparison = build_statement(
			filters.company,
			filters.statement_type,
			comparison_from,
			comparison_to,
			filters.finance_book,
			filters.cost_center,
			filters.project,
			restate_prior_period=True,
		)
		comparison_values = {row["row_code"]: row["amount"] for row in comparison["rows"]}
		for row in result["rows"]:
			row["comparison_amount"] = comparison_values.get(row["row_code"], 0)
			row["variance_amount"] = flt(row["amount"] - row["comparison_amount"], 2)
			row["variance_rate"] = (
				flt(row["variance_amount"] / abs(row["comparison_amount"]) * 100, 2)
				if abs(row["comparison_amount"]) > 0.01 else None
			)
		result["warnings"].extend(comparison["warnings"])
	elif comparison_to:
		result["warnings"].append(
			_("比较期 {0} 未配置适用报表模板，当前仅显示本期草表").format(comparison_to)
		)
		comparison_to = None
	warnings = list(dict.fromkeys(result["warnings"]))
	message_parts = [_('编制状态：草表。正式法定财务报表须从已通过检查的期末智能结转生成。')]
	if result.get("checks"):
		failed_checks = [check for check in result["checks"] if not check["passed"]]
		blocking_checks = [check for check in failed_checks if check.get("blocking", True)]
		if blocking_checks:
			message_parts.append(_("报表检查：有 {0} 项阻断问题，暂不可作为正式报表。").format(len(blocking_checks)))
		elif failed_checks:
			message_parts.append(_("报表检查：通过；另有 {0} 项需复核。").format(len(failed_checks)))
		else:
			message_parts.append(_("报表检查：全部通过。"))
	message_parts.extend(warnings)
	message = "<br>".join(message_parts)
	if filters.statement_type == "Balance Sheet":
		return (
			get_balance_sheet_columns(bool(comparison_to), filters.company),
			build_balance_sheet_rows(result["rows"]),
			message,
			get_balance_sheet_chart(result["rows"], filters.company, filters),
			get_balance_sheet_summary(result["rows"], filters.company),
		)
	if filters.statement_type == "Profit and Loss":
		return (
			get_columns(
				include_comparison=bool(comparison_to),
				include_variance=bool(comparison_to),
				company=filters.company,
			),
			result["rows"],
			message,
			get_profit_and_loss_chart(result["rows"], filters.company, filters),
			get_profit_and_loss_summary(result["rows"], filters.company),
		)
	if filters.statement_type == "Cash Flow":
		return (
			get_columns(include_comparison=bool(comparison_to), company=filters.company),
			result["rows"],
			message,
			get_cash_flow_chart(result["rows"], filters.company, filters),
			get_cash_flow_summary(result["rows"], filters.company, filters),
		)
	if filters.statement_type == "Changes in Equity" and result.get("equity_matrix"):
		return get_equity_columns(result["equity_matrix"], filters.company), result["equity_matrix"]["rows"], message
	return get_columns(include_comparison=bool(comparison_to), company=filters.company), result["rows"], message


def _native_project_filter(project):
	# This report has a single Project Link; native Trial Balance expects a list.
	return [project] if isinstance(project, str) and project else project


def execute_native_trial_balance(filters, activity_balance=False):
	"""Reuse native balances, with signed expense turnover for Chinese reports."""
	from erpnext.accounts.report.trial_balance.trial_balance import execute as execute_trial_balance

	native_filters = frappe._dict({
		"company": filters.company,
		"fiscal_year": filters.fiscal_year,
		"from_date": filters.from_date,
		"to_date": filters.to_date,
		"finance_book": filters.finance_book,
		"cost_center": filters.cost_center,
		"project": _native_project_filter(filters.project),
		"include_default_book_entries": 1,
		"show_net_values": 1,
		"show_group_accounts": 1,
		"show_zero_values": 1,
		"with_period_closing_entry_for_opening": 1,
		"with_period_closing_entry_for_current_period": 1,
	})
	columns, data = execute_trial_balance(native_filters)
	_adjust_opening_entries_by_posting_date(data, native_filters)
	_apply_expense_turnover_presentation(data, native_filters)
	_filter_native_zero_rows(data, filters.get("show_zero_values"))
	_keep_numbered_group_accounts(data)
	_format_native_account_labels(columns, data, filters.company)
	if activity_balance:
		_rename_activity_balance_columns(columns)
		return columns, data, _activity_balance_message(filters)
	return columns, data


def _apply_expense_turnover_presentation(rows, native_filters):
	account_names = [
		row["account"] for row in rows or []
		if row.get("account") and row.get("is_group_account") == 0
		and not row.get("warn_if_negative")
	]
	offsets = get_expense_turnover_offsets(native_filters, account_names)
	adjust_account_expense_turnover(rows or [], offsets)
	return offsets


def _format_native_account_labels(columns, rows, company):
	"""Show the numbered account hierarchy without changing the Account master."""
	accounts = frappe.get_all(
		"Account",
		filters={"company": company},
		fields=["name", "account_number", "account_name", "parent_account"],
	)
	account_map = {account.name: account for account in accounts}
	label_cache = {}

	def account_label(account_name):
		if account_name in label_cache:
			return label_cache[account_name]
		current = account_map.get(account_name)
		label_cache[account_name] = (
			get_account_display_title(current.account_number, current.account_name)
			if current and current.account_number
			else account_name
		)
		return label_cache[account_name]

	for column in columns:
		if column.get("fieldname") == "account":
			column["fieldtype"] = "Data"
			column.pop("options", None)
			column["width"] = max(column.get("width") or 0, 280)
	for row in rows:
		if row.get("account") in account_map:
			row["account"] = account_label(row["account"])


def _adjust_opening_entries_by_posting_date(rows, filters):
	"""Remove future initialization using native filters and signed leaf balances."""
	if not rows or not filters.get("from_date"):
		return
	# These native modes do not include future opening GL in their opening base.
	if frappe.get_single_value("Accounts Settings", "ignore_is_opening_check_for_reporting"):
		return
	if not frappe.get_single_value("Accounts Settings", "ignore_account_closing_balance") and frappe.db.exists(
		"Period Closing Voucher", {"company": filters.company, "docstatus": 1, "period_end_date": ("<", filters.from_date)}
	):
		return
	from erpnext.accounts.report.financial_statements import apply_additional_conditions
	from erpnext.accounts.report.trial_balance.trial_balance import calculate_total_row
	from frappe.query_builder.functions import Sum

	gle = frappe.qb.DocType("GL Entry")
	query = (
		frappe.qb.from_(gle)
		.select(gle.account, Sum(gle.debit).as_("debit"), Sum(gle.credit).as_("credit"))
		.where((gle.company == filters.company) & (gle.is_cancelled == 0))
		.where((gle.is_opening == "Yes") & (gle.posting_date > filters.to_date))
		.groupby(gle.account)
	)
	# Exactly the same cost-center subtree, project, book and dimension scope as
	# the native report, rather than another set of equality-only filters.
	future = apply_additional_conditions("GL Entry", query, None, False, filters.copy()).run(as_dict=True)
	if not future:
		return
	future_net = {item.account: flt(item.debit) - flt(item.credit) for item in future}
	by_account = {row["account"]: row for row in rows if row.get("account") and not row.get("warn_if_negative")}
	balance_fields = ("opening_debit", "opening_credit", "closing_debit", "closing_credit")
	for row in by_account.values():
		if row.get("is_group_account"):
			for field in balance_fields:
				row[field] = 0
		else:
			for prefix in ("opening", "closing"):
				# Native rows are already netted. Removing gross columns separately
				# loses balances whenever the result changes from debit to credit.
				net = flt(row.get(prefix + "_debit")) - flt(row.get(prefix + "_credit")) - future_net.get(row["account"], 0)
				if abs(net) < 0.000000001:
					net = 0
				row[prefix + "_debit"], row[prefix + "_credit"] = max(net, 0), max(-net, 0)
	# Rebuild groups from corrected leaves, preserving opposite-side children.
	for row in by_account.values():
		if row.get("is_group_account"):
			continue
		parent = row.get("parent_account")
		visited = set()
		while parent in by_account and parent not in visited:
			visited.add(parent)
			for field in balance_fields:
				by_account[parent][field] += flt(row.get(field))
			parent = by_account[parent].get("parent_account")
	for row in rows:
		if row.get("warn_if_negative"):
			corrected = calculate_total_row(list(by_account.values()), row.get("currency"), show_group_accounts=True)
			for field in balance_fields:
				row[field] = corrected[field]


def _filter_native_zero_rows(rows, show_zero_values):
	"""Apply native visibility after corrections, before removing chart roots."""
	if show_zero_values or not rows:
		return
	from erpnext.accounts.report.financial_statements import filter_out_zero_value_rows
	from erpnext.accounts.report.trial_balance.trial_balance import value_fields
	from erpnext.accounts.utils import get_zero_cutoff

	children = {}
	for row in rows:
		if row.get("account") and not row.get("warn_if_negative"):
			children.setdefault(row.get("parent_account"), []).append({"name": row["account"]})
			row["has_value"] = any(abs(flt(row.get(field))) >= get_zero_cutoff(row.get("currency")) for field in value_fields)
	rows[:] = filter_out_zero_value_rows(rows, children, show_zero_values=False)


def _rename_activity_balance_columns(columns):
	"""Use Chinese period labels and wider native columns for this report."""
	for column in columns:
		fieldname = column.get("fieldname")
		labels = {
			"account": _("科目"),
			"acc_name": _("科目名称"),
			"acc_number": _("科目编码"),
			"currency": _("币种"),
			"opening_debit": _("期初（借方）"),
			"opening_credit": _("期初（贷方）"),
			"debit": _("本期借方"),
			"credit": _("本期贷方"),
			"closing_debit": _("期末（借方）"),
			"closing_credit": _("期末（贷方）"),
			"party_type": _("往来类型"),
			"party": _("往来单位"),
		}
		if fieldname in labels:
			column["label"] = labels[fieldname]
		native_widths = {
			"account": 280,
			"party_type": 120,
			"party": 220,
			"opening_debit": 150,
			"opening_credit": 150,
			"debit": 150,
			"credit": 150,
			"closing_debit": 150,
			"closing_credit": 150,
		}
		if fieldname in native_widths:
			column["width"] = native_widths[fieldname]
		if column.get("fieldname") == "debit":
			column["label"] = _("本期借方")
		elif column.get("fieldname") == "credit":
			column["label"] = _("本期贷方")


def get_account_activity_columns(expand_party=False):
	columns = [
		{"label": _("科目类别"), "fieldname": "account_category", "fieldtype": "Data", "width": 120},
		{"label": _("科目编码"), "fieldname": "account_number", "fieldtype": "Data", "width": 100},
		{"label": _("科目名称"), "fieldname": "account_name", "fieldtype": "Data", "width": 260},
		{"label": _("币种"), "fieldname": "currency", "fieldtype": "Data", "width": 70},
		{"label": _("期初借方"), "fieldname": "opening_debit", "fieldtype": "Currency", "options": "currency", "width": 150},
		{"label": _("期初贷方"), "fieldname": "opening_credit", "fieldtype": "Currency", "options": "currency", "width": 150},
		{"label": _("本期借方"), "fieldname": "period_debit", "fieldtype": "Currency", "options": "currency", "width": 150},
		{"label": _("本期贷方"), "fieldname": "period_credit", "fieldtype": "Currency", "options": "currency", "width": 150},
		{"label": _("期末借方"), "fieldname": "closing_debit", "fieldtype": "Currency", "options": "currency", "width": 150},
		{"label": _("期末贷方"), "fieldname": "closing_credit", "fieldtype": "Currency", "options": "currency", "width": 150},
	]
	if expand_party:
		columns.insert(3, {"label": _("往来单位"), "fieldname": "party", "fieldtype": "Data", "width": 220})
	return columns


def _party_label(party_type, party):
	"""Return a readable party label while keeping the party code visible."""
	if not party:
		return _("未指定往来")

	party_name = None
	party_name_field = {
		"Customer": "customer_name",
		"Supplier": "supplier_name",
		"Employee": "employee_name",
		"Shareholder": "title",
	}.get(party_type)
	if party_name_field and frappe.get_meta(party_type).has_field(party_name_field):
		party_name = frappe.db.get_value(party_type, party, party_name_field)
	return f"{party} - {party_name}" if party_name and party_name != party else party


def _legacy_execute_account_activity_balance(filters):
	"""Return a GL-based activity/balance table without using statement mappings."""
	if not filters.company:
		frappe.throw(_("请选择公司"))
	if getdate(filters.from_date) > getdate(filters.to_date):
		frappe.throw(_("起始日期不能晚于截止日期"))
	frappe.has_permission("Company", doc=filters.company, throw=True)

	account_filters = {"company": filters.company}
	if filters.account:
		account = frappe.db.get_value("Account", filters.account, ["company", "lft", "rgt"], as_dict=True)
		if not account or account.company != filters.company:
			frappe.throw(_("科目不属于当前公司"))
		account_filters.update({"lft": [">=", account.lft], "rgt": ["<=", account.rgt]})
	accounts = frappe.get_all(
		"Account", filters=account_filters,
		fields=["name", "account_name", "account_number", "root_type", "is_group", "parent_account", "lft", "rgt", "account_currency"],
		order_by="lft asc",
	)
	if not accounts:
		return get_account_activity_columns(bool(filters.expand_party)), [], _("期间内没有可显示的科目")

	params = {"company": filters.company, "from_date": filters.from_date, "to_date": filters.to_date}
	conditions = ["gle.company=%(company)s", "gle.is_cancelled=0", "gle.posting_date<=%(to_date)s"]
	if filters.finance_book:
		conditions.append("gle.finance_book=%(finance_book)s")
		params["finance_book"] = filters.finance_book
	if filters.cost_center:
		conditions.append("gle.cost_center=%(cost_center)s")
		params["cost_center"] = filters.cost_center
	if filters.project:
		conditions.append("gle.project=%(project)s")
		params["project"] = filters.project
	account_names = tuple(row.name for row in accounts)
	conditions.append("gle.account IN %(accounts)s")
	params["accounts"] = account_names
	group_fields = "gle.account, gle.party_type, gle.party" if filters.expand_party else "gle.account"
	rows = frappe.db.sql(
		f"""
		SELECT gle.account, gle.party_type, gle.party,
			SUM(CASE WHEN gle.posting_date < %(from_date)s OR gle.is_opening='Yes' THEN gle.debit ELSE 0 END) opening_debit,
			SUM(CASE WHEN gle.posting_date < %(from_date)s OR gle.is_opening='Yes' THEN gle.credit ELSE 0 END) opening_credit,
			SUM(CASE WHEN gle.posting_date BETWEEN %(from_date)s AND %(to_date)s AND COALESCE(gle.is_opening,'No')!='Yes' THEN gle.debit ELSE 0 END) period_debit,
			SUM(CASE WHEN gle.posting_date BETWEEN %(from_date)s AND %(to_date)s AND COALESCE(gle.is_opening,'No')!='Yes' THEN gle.credit ELSE 0 END) period_credit
		FROM `tabGL Entry` gle
		WHERE {' AND '.join(conditions)}
		GROUP BY {group_fields}
		""",
		params, as_dict=True,
	)
	account_by_name = {row.name: row for row in accounts}
	parent_map = {row.name: row.parent_account for row in accounts}
	levels = {}
	for account_name in account_by_name:
		level = 0
		parent = parent_map.get(account_name)
		while parent and parent in parent_map:
			level += 1
			parent = parent_map.get(parent)
		levels[account_name] = level
	category = {"Asset": _("资产"), "Liability": _("负债"), "Equity": _("权益"), "Income": _("收入"), "Expense": _("费用"), "" : _("未分类")}

	def blank():
		return {"opening_debit": 0, "opening_credit": 0, "period_debit": 0, "period_credit": 0}

	def add(target, source):
		for key in ("opening_debit", "opening_credit", "period_debit", "period_credit"):
			target[key] += source.get(key) or 0

	def balance_values(values):
		opening_net = (values["opening_debit"] or 0) - (values["opening_credit"] or 0)
		closing_net = opening_net + (values["period_debit"] or 0) - (values["period_credit"] or 0)
		# Normalize database floating-point residue so the UI never prints -0.00.
		opening_net = round(opening_net, 2) if abs(opening_net) < 0.005 else opening_net
		closing_net = round(closing_net, 2) if abs(closing_net) < 0.005 else closing_net
		values["opening_debit"] = max(opening_net, 0)
		values["opening_credit"] = max(-opening_net, 0)
		values["closing_debit"] = max(closing_net, 0)
		values["closing_credit"] = max(-closing_net, 0)
		return values

	# Keep party rows separate, while account totals are rolled up through parents.
	leaf_totals = {}
	for row in rows:
		key = (row.account, row.party_type or "", row.party or "") if filters.expand_party else (row.account, "", "")
		leaf_totals[key] = {field: row.get(field) or 0 for field in ("opening_debit", "opening_credit", "period_debit", "period_credit")}
	account_totals = {name: blank() for name in account_by_name}
	for (account_name, _party_type, _party), values in leaf_totals.items():
		current = account_name
		while current and current in account_totals:
			add(account_totals[current], values)
			current = parent_map.get(current)
	rows_out = []
	for account in accounts:
		values = balance_values(account_totals[account.name].copy())
		if not filters.show_zero_values and not any(values.get(key) for key in ("opening_debit", "opening_credit", "period_debit", "period_credit", "closing_debit", "closing_credit")):
			continue
		rows_out.append({"account": account.name, "account_category": category.get(account.root_type or "", account.root_type or ""), "account_number": account.account_number or "", "account_name": account.account_name or account.name, "currency": company_currency(filters.company), "parent_account": account.parent_account or "", "indent": levels[account.name], "is_group": account.is_group, **values})
		if filters.expand_party and not account.is_group:
			for (account_name, party_type, party), party_values in leaf_totals.items():
				if account_name != account.name:
					continue
				party_row = balance_values(party_values.copy())
				if not filters.show_zero_values and not any(party_row.get(key) for key in ("opening_debit", "opening_credit", "period_debit", "period_credit", "closing_debit", "closing_credit")):
					continue
				rows_out.append({"account": "", "account_category": category.get(account.root_type or "", account.root_type or ""), "account_number": "", "account_name": "", "currency": company_currency(filters.company), "party_type": party_type or _("未指定往来"), "party": _party_label(party_type, party), "parent_account": account.name, "indent": levels[account.name] + 1, "is_group": 0, **party_row})
	return get_account_activity_columns(bool(filters.expand_party)), rows_out, _("金额按 GL Entry 聚合；期初 + 本期发生 = 期末余额")


def execute_account_activity_balance(filters):
	"""Use ERPNext Trial Balance as the account-level result and add party rows."""
	from erpnext.accounts.report.trial_balance.trial_balance import execute as execute_trial_balance

	native_filters = frappe._dict({
		"company": filters.company,
		"fiscal_year": filters.fiscal_year,
		"from_date": filters.from_date,
		"to_date": filters.to_date,
		"finance_book": filters.finance_book,
		"cost_center": filters.cost_center,
		"project": _native_project_filter(filters.project),
		"include_default_book_entries": 1,
		"show_net_values": 1,
		"show_group_accounts": 1,
		"show_zero_values": 1,
		"with_period_closing_entry_for_opening": 1,
		"with_period_closing_entry_for_current_period": 1,
	})
	columns, native_rows = execute_trial_balance(native_filters)
	_adjust_opening_entries_by_posting_date(native_rows, native_filters)
	expense_offsets = _apply_expense_turnover_presentation(native_rows, native_filters)
	_filter_native_zero_rows(native_rows, filters.get("show_zero_values"))
	_keep_numbered_group_accounts(native_rows)
	if not filters.get("expand_party"):
		_format_native_account_labels(columns, native_rows, filters.company)
		return columns, native_rows, _activity_balance_message(filters)

	account_names = [row.get("account") for row in native_rows if row.get("account") and row.get("is_group_account") == 0]
	if not account_names:
		return columns, native_rows, _activity_balance_message(filters)
	party_rows = _get_activity_party_rows(native_filters, account_names)
	adjust_party_expense_turnover(party_rows, expense_offsets)
	by_account = {}
	for row in party_rows:
		opening = (row.get("opening_debit") or 0) - (row.get("opening_credit") or 0)
		closing = opening + (row.get("debit") or 0) - (row.get("credit") or 0)
		row["opening_debit"], row["opening_credit"] = max(opening, 0), max(-opening, 0)
		row["closing_debit"], row["closing_credit"] = max(closing, 0), max(-closing, 0)
		by_account.setdefault(row.get("account"), []).append(row)

	party_columns = list(columns)
	_rename_activity_balance_columns(party_columns)
	account_index = next((index for index, column in enumerate(party_columns) if column.get("fieldname") == "account"), 0)
	party_columns[account_index + 1:account_index + 1] = [
		{"label": _("往来单位"), "fieldname": "party", "fieldtype": "Data", "width": 220},
	]
	output = []
	for row in native_rows:
		output.append(row)
		if row.get("is_group_account") == 0 and row.get("account") in by_account:
			for party in by_account[row.get("account")]:
				# Keep the account total, but avoid a noisy detail row when the
				# underlying GL entry has no party dimension at all.
				if not party.get("party") and not party.get("party_type"):
					continue
				if not filters.get("show_zero_values") and not any(party.get(field) for field in ("opening_debit", "opening_credit", "debit", "credit", "closing_debit", "closing_credit")):
					continue
				party_row = {
					# A party detail is not an account. Keep the 科目 column limited
					# to actual Account values; show the party only in its own column.
					"account": "",
					"account_name": "",
					"party_type": party.get("party_type") or _("未指定往来"),
					"party": _party_label(party.get("party_type"), party.get("party")),
					"indent": (row.get("indent") or 0) + 1,
					"currency": row.get("currency"),
					"opening_debit": party.get("opening_debit"),
					"opening_credit": party.get("opening_credit"),
					"debit": party.get("debit"),
					"credit": party.get("credit"),
					"closing_debit": party.get("closing_debit"),
					"closing_credit": party.get("closing_credit"),
				}
				output.append(party_row)
	_format_native_account_labels(party_columns, output, filters.company)
	return party_columns, output, _activity_balance_message(filters)


def _get_activity_party_rows(native_filters, account_names):
	from frappe.query_builder import Case
	from frappe.query_builder.functions import Coalesce, Sum

	gle = frappe.qb.DocType("GL Entry")
	opening = gle.posting_date < native_filters.from_date
	current = gle.posting_date >= native_filters.from_date
	if not frappe.get_single_value("Accounts Settings", "ignore_is_opening_check_for_reporting"):
		opening |= gle.is_opening == "Yes"
		current &= gle.is_opening == "No"
	party_type = Coalesce(gle.party_type, "")
	party = Coalesce(gle.party, "")
	return (
		get_activity_gl_query(native_filters, account_names, current_period_only=False)
		.select(
			gle.account, party_type.as_("party_type"), party.as_("party"),
			Sum(Case().when(opening, gle.debit).else_(0)).as_("opening_debit"),
			Sum(Case().when(opening, gle.credit).else_(0)).as_("opening_credit"),
			Sum(Case().when(current, gle.debit).else_(0)).as_("debit"),
			Sum(Case().when(current, gle.credit).else_(0)).as_("credit"),
		)
		.groupby(gle.account, party_type, party)
		.run(as_dict=True)
	)


def _keep_numbered_group_accounts(rows):
	"""Keep coded parent accounts as summary rows, but hide chart root nodes.

	The statutory account balance layout uses four-digit accounts as category
	 totals and six-digit accounts as detail accounts. ERPNext also returns
	 uncoded root nodes such as ``资产`` and ``负债`` when group accounts are
	 enabled; those nodes are structural containers rather than report rows.
	"""
	if not rows:
		return

	rows[:] = [
		row
		for row in rows
		if not row.get("is_group_account") or row.get("acc_number") or row.get("warn_if_negative")
	]

	# ERPNext's grouped total is based on the root rows after netting them by
	# account type. For this report the footer must remain the debit/credit
	# total of actual accounts, as it was before parent rows were displayed.
	# Native Trial Balance marks its translated footer with warn_if_negative;
	# the localized account label is not a stable row identifier.
	total_row = next(
		(row for row in rows if row.get("warn_if_negative")),
		None,
	)
	if total_row:
		detail_rows = [
			row
			for row in rows
			if row.get("account")
			and not row.get("warn_if_negative")
			and not row.get("is_group_account")
		]
		for fieldname in ("opening_debit", "opening_credit", "debit", "credit", "closing_debit", "closing_credit"):
			total_row[fieldname] = sum(flt(row.get(fieldname)) for row in detail_rows)


def _activity_balance_message(filters):
	message = _("数据来源：ERPNext 原生试算平衡表")
	message += _("；费用冲减按负借方列示，反向损益结转按负贷方列示")
	message += _("；本期发生额包含损益结转凭证，期末余额按结转后列示")
	open_profit = get_unclosed_profit(
		filters.company,
		filters.to_date,
		filters.finance_book,
		filters.cost_center,
		filters.project,
	)
	if abs(flt(open_profit)) > 0.005:
		closing_account = frappe.db.get_value("China Finance Settings", filters.company, "profit_loss_account")
		closing_label = closing_account or _("本年利润")
		message += _("；提示：截至 {0} 损益类科目尚未全部结转至 {1}，当前净余额 {2}，需完成月度结转后再作为正式报表使用").format(
			filters.to_date, closing_label, flt(open_profit, 2)
		)
	return message


def _set_report_currency(columns, company):
	"""Bind report currency fields to the selected company's currency."""
	for column in columns:
		if column.get("fieldtype") == "Currency":
			column["options"] = "currency"
	return columns


def get_columns(include_comparison=False, include_variance=False, company=None):
	columns = [
		{"label": _("项目"), "fieldname": "label", "fieldtype": "Data", "width": 420},
		{"label": _("期初金额"), "fieldname": "opening_amount", "fieldtype": "Currency", "options": "currency", "width": 160},
		{"label": _("本期金额/期末余额"), "fieldname": "amount", "fieldtype": "Currency", "options": "currency", "width": 180},
		{"label": _("本年累计"), "fieldname": "year_to_date_amount", "fieldtype": "Currency", "options": "currency", "width": 160},
	]
	if include_comparison:
		columns.append({"label": _("比较期金额"), "fieldname": "comparison_amount", "fieldtype": "Currency", "options": "currency", "width": 180})
	if include_variance:
		columns.extend([
			{"label": _("增减额"), "fieldname": "variance_amount", "fieldtype": "Currency", "options": "currency", "width": 160},
			{"label": _("增减率"), "fieldname": "variance_rate", "fieldtype": "Percent", "width": 130},
		])
	return _set_report_currency(columns, company)


def get_equity_columns(matrix, company=None):
	columns = [{"label": _("项目"), "fieldname": "label", "fieldtype": "Data", "width": 320}]
	columns.extend(
		{"label": _(component["label"]), "fieldname": component["fieldname"], "fieldtype": "Currency", "options": "currency", "width": 150}
		for component in matrix["components"]
	)
	columns.append({"label": _("所有者权益合计"), "fieldname": "total", "fieldtype": "Currency", "options": "currency", "width": 170})
	return _set_report_currency(columns, company)


def get_balance_sheet_columns(include_comparison=False, company=None):
	columns = [
		{"label": _("资产"), "fieldname": "asset_label", "fieldtype": "Data", "width": 280},
		{"label": _("期初余额"), "fieldname": "asset_opening_amount", "fieldtype": "Currency", "options": "currency", "width": 140},
		{"label": _("期末余额"), "fieldname": "asset_amount", "fieldtype": "Currency", "options": "currency", "width": 150},
	]
	if include_comparison:
		columns.append({"label": _("比较期余额"), "fieldname": "asset_comparison_amount", "fieldtype": "Currency", "options": "currency", "width": 150})
	columns.extend([
		{"label": _("负债和所有者权益"), "fieldname": "liability_equity_label", "fieldtype": "Data", "width": 280},
		{"label": _("期初余额"), "fieldname": "liability_equity_opening_amount", "fieldtype": "Currency", "options": "currency", "width": 140},
		{"label": _("期末余额"), "fieldname": "liability_equity_amount", "fieldtype": "Currency", "options": "currency", "width": 150},
	])
	if include_comparison:
		columns.append({"label": _("比较期余额"), "fieldname": "liability_equity_comparison_amount", "fieldtype": "Currency", "options": "currency", "width": 150})
	return _set_report_currency(columns, company)


def build_balance_sheet_rows(rows):
	"""Pair statutory assets with liabilities and equity for a two-sided balance sheet."""
	asset_end = next((index for index, row in enumerate(rows) if row["row_code"] == "TOTAL_ASSETS"), -1)
	if asset_end < 0:
		return rows
	asset_rows = rows[: asset_end + 1]
	liability_equity_rows = rows[asset_end + 1 :]

	# The small-enterprise statutory form presents liabilities from the top and
	# fills the owners' equity section upward from the bottom.  Keeping its footer
	# aligned with "资产合计" also makes the accounting equation auditable on paper.
	equity_start = next(
		(index for index, row in enumerate(liability_equity_rows) if row.get("row_code") == "OWNERS_EQUITY_HEADING"),
		None,
	)
	if equity_start is not None:
		liability_rows = liability_equity_rows[:equity_start]
		equity_rows = liability_equity_rows[equity_start:]
		row_count = max(len(asset_rows), len(liability_equity_rows))
		liability_equity_rows = liability_rows + ([{}] * max(0, row_count - len(liability_rows) - len(equity_rows))) + equity_rows
	paired_rows = []
	for index in range(max(len(asset_rows), len(liability_equity_rows))):
		asset = asset_rows[index] if index < len(asset_rows) else {}
		liability_equity = liability_equity_rows[index] if index < len(liability_equity_rows) else {}
		paired_rows.append({
			"indent": max(asset.get("indent", 0), liability_equity.get("indent", 0)),
			"asset_label": asset.get("label"),
			"asset_statutory_line_number": asset.get("statutory_line_number"),
			"asset_row_type": asset.get("row_type"),
			"asset_opening_amount": asset.get("opening_amount"),
			"asset_amount": asset.get("amount"),
			"asset_comparison_amount": asset.get("comparison_amount"),
			"asset_source_accounts": asset.get("source_accounts", []),
			"asset_indent": asset.get("indent", 0),
			"asset_bold": asset.get("bold", 0),
			"liability_equity_label": liability_equity.get("label"),
			"liability_equity_statutory_line_number": liability_equity.get("statutory_line_number"),
			"liability_equity_row_type": liability_equity.get("row_type"),
			"liability_equity_opening_amount": liability_equity.get("opening_amount"),
			"liability_equity_amount": liability_equity.get("amount"),
			"liability_equity_comparison_amount": liability_equity.get("comparison_amount"),
			"liability_equity_source_accounts": liability_equity.get("source_accounts", []),
			"liability_equity_indent": liability_equity.get("indent", 0),
			"liability_equity_bold": liability_equity.get("bold", 0),
		})
		# Display-only headings have no balance; preserve actual zero-valued
		# mapped/formula rows, including fully depreciated fixed assets.
		for side in ("asset", "liability_equity"):
			if paired_rows[-1].get(f"{side}_row_type") == "Heading":
				for field in ("opening_amount", "amount", "comparison_amount"):
					paired_rows[-1][f"{side}_{field}"] = None
	return paired_rows


@frappe.whitelist()
def export_current_report_pdf(filters=None):
	"""Generate a report PDF on the server without Query Report's print dialog.

	Frappe 16's browser-side query-report print template path is not compatible
	with this report's custom balance-sheet rendering.  This endpoint renders the
	actual report result directly, so the exported values always match the page.
	"""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	filters = frappe._dict(filters or {})
	if not filters.company:
		frappe.throw(_("请选择公司"))
	frappe.has_permission("Company", doc=filters.company, throw=True)

	columns, rows, message, *_ = execute(filters)
	filters = frappe._dict(filters)
	_apply_default_period(filters)
	html = _render_report_pdf(filters, columns, rows, message)
	statement_title = _statement_title(filters.statement_type)
	filename = f"{statement_title}-{filters.company}-{filters.to_date}.pdf"
	file_doc = save_file(filename, get_pdf(html), "Company", filters.company, is_private=1)
	return {"file_url": file_doc.file_url, "file_name": file_doc.file_name}


@frappe.whitelist()
def export_current_report_xlsx(filters=None):
	"""Export the selected Chinese financial report with an explicit Chinese title."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters)
	filters = frappe._dict(filters or {})
	if not filters.company:
		frappe.throw(_("请选择公司"))
	frappe.has_permission("Company", doc=filters.company, throw=True)

	columns, rows, *_ = execute(filters)
	visible_columns = [column for column in columns if column.get("fieldname") and not column.get("hidden")]
	if not visible_columns:
		frappe.throw(_("当前报表没有可导出的列"))

	from io import BytesIO

	import xlsxwriter

	buffer = BytesIO()
	workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
	statement_title = _statement_title(filters.statement_type)
	worksheet = workbook.add_worksheet(statement_title[:31])
	title = workbook.add_format({"bold": True, "font_size": 15, "align": "center", "valign": "vcenter"})
	subtitle = workbook.add_format({"font_size": 10, "align": "center", "valign": "vcenter"})
	header = workbook.add_format({"bold": True, "bg_color": "#E2E8F0", "border": 1, "align": "center"})
	text_cell = workbook.add_format({"border": 1})
	number_cell = workbook.add_format({"border": 1, "num_format": "#,##0.00"})
	last_column = len(visible_columns) - 1
	worksheet.merge_range(0, 0, 0, last_column, statement_title, title)
	worksheet.merge_range(
		1, 0, 1, last_column,
		f"{filters.company}　期间：{filters.from_date} 至 {filters.to_date}",
		subtitle,
	)
	for index, column in enumerate(visible_columns):
		worksheet.write(3, index, column.get("label") or column.get("fieldname"), header)

	for row_index, row in enumerate(rows, start=4):
		for column_index, column in enumerate(visible_columns):
			fieldname = column["fieldname"]
			value = row.get(fieldname, "") if hasattr(row, "get") else ""
			if value is None:
				value = ""
			if column.get("fieldtype") in {"Currency", "Float", "Percent", "Int"} and value != "":
				worksheet.write_number(row_index, column_index, flt(value), number_cell)
			else:
				worksheet.write(row_index, column_index, str(value), text_cell)

	for column_index, column in enumerate(visible_columns):
		width = min(max(flt(column.get("width") or 120) / 8, 12), 36)
		worksheet.set_column(column_index, column_index, width)
	worksheet.freeze_panes(4, 0)
	worksheet.autofilter(3, 0, max(3, len(rows) + 3), last_column)
	workbook.close()

	filename = f"{statement_title}_{filters.company}_{filters.from_date}_{filters.to_date}.xlsx"
	file_doc = save_file(filename, buffer.getvalue(), "Company", filters.company, is_private=1)
	return {"file_url": file_doc.file_url, "file_name": file_doc.file_name}


def _statement_title(statement_type):
	return {
		"Balance Sheet": _("资产负债表"),
		"Profit and Loss": _("利润表"),
		"Cash Flow": _("现金流量表"),
		"Changes in Equity": _("所有者权益变动表"),
		"Trial Balance": _("试算平衡表"),
		"Account Activity and Balance": _("发生额及余额表"),
	}.get(statement_type, _("中国财务报表"))


FORM_CODES = {
	"企业会计准则": {
		"Balance Sheet": "会企01表",
		"Profit and Loss": "会企02表",
		"Cash Flow": "会企03表",
		"Changes in Equity": "会企04表",
	},
	"小企业会计准则": {
		"Balance Sheet": "会小企01表",
		"Profit and Loss": "会小企02表",
		"Cash Flow": "会小企03表",
		"Changes in Equity": "会小企04表",
	},
}


def _render_report_pdf(filters, columns, rows, message):
	"""Render the current report data into a self-contained printable document."""
	statement_type = filters.statement_type
	template = get_template(filters.company, statement_type, filters.to_date, required=False)
	standard = template.accounting_standard if template else ""
	title = _statement_title(statement_type)
	form_code = FORM_CODES.get(standard, {}).get(statement_type, "")

	escape = frappe.utils.escape_html
	tax_id = frappe.get_cached_value("Company", filters.company, "tax_id") or ""
	meta = (
		"<table class='meta'>"
		f"<tr><td>{escape(form_code)}</td><td class='right'>税款所属期起止：{filters.from_date} 至 {filters.to_date}</td></tr>"
		f"<tr><td>纳税人识别号：{escape(tax_id)}</td><td class='right'>报送日期：{nowdate()}</td></tr>"
		f"<tr><td>编制单位：{escape(filters.company)}</td><td class='right'>单位：元</td></tr>"
		"</table>"
	)
	if statement_type == "Balance Sheet":
		body = _render_balance_sheet_pdf_rows(rows)
	elif statement_type == "Account Activity and Balance":
		body = _render_standard_pdf_rows(columns, rows)
	else:
		body = _render_standard_pdf_rows(columns, rows)
	warning = f"<p class='notice'>{message}</p>" if message else ""
	return f"""
	<!doctype html><html><head><meta charset='utf-8'>
	<style>
	@page {{ size: A4 landscape; margin: 12mm; }}
	body {{ font-family: 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif; font-size: 9pt; color: #111; }}
	h1 {{ text-align:center; font-size:16pt; margin:0 0 8px; }}
	.meta {{ width:100%; border-collapse:collapse; margin-bottom:8px; }}
	.meta td {{ font-size:9pt; padding:1px 0; }}
	.meta td.right {{ text-align:right; }}
	.notice {{ color:#666; font-size:8pt; }} table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
	th, td {{ border:1px solid #777; padding:5px 6px; vertical-align:middle; }} th {{ background:#f2f2f2; text-align:center; }}
	td.num {{ text-align:right; white-space:nowrap; }} td.line {{ text-align:center; width:36px; }}
	tr.bold td {{ font-weight:700; }}
	</style></head><body><h1>{title}</h1>{meta}{warning}{body}</body></html>
	"""


def _amount(value):
	return f"{flt(value):,.2f}"


def _render_balance_sheet_pdf_rows(rows):
	parts = [
		"<table><thead><tr><th>项目</th><th>行次</th><th>期末余额</th><th>年初余额</th>"
		"<th>负债和所有者权益</th><th>行次</th><th>期末余额</th><th>年初余额</th></tr></thead><tbody>"
	]
	for row in rows:
		asset_label = frappe.utils.escape_html(str(row.get("asset_label") or ""))
		liability_label = frappe.utils.escape_html(str(row.get("liability_equity_label") or ""))
		if row.get("asset_row_type") == "Heading" and asset_label:
			asset_label += "："
		if row.get("liability_equity_row_type") == "Heading" and liability_label:
			liability_label += "："
		bold = " class='bold'" if row.get("asset_bold") or row.get("liability_equity_bold") else ""
		asset_indent = int(row.get("asset_indent") or 0) * 12
		liability_indent = int(row.get("liability_equity_indent") or 0) * 12
		asset_opening = "" if row.get("asset_row_type") == "Heading" else _amount(row.get("asset_opening_amount"))
		asset_amount = "" if row.get("asset_row_type") == "Heading" else _amount(row.get("asset_amount"))
		liability_opening = "" if row.get("liability_equity_row_type") == "Heading" else _amount(row.get("liability_equity_opening_amount"))
		liability_amount = "" if row.get("liability_equity_row_type") == "Heading" else _amount(row.get("liability_equity_amount"))
		parts.append(
			f"<tr{bold}><td style='padding-left:{asset_indent + 6}px'>{asset_label}</td><td class='line'>{row.get('asset_statutory_line_number') or ''}</td>"
			f"<td class='num'>{asset_amount}</td><td class='num'>{asset_opening}</td>"
			f"<td style='padding-left:{liability_indent + 6}px'>{liability_label}</td><td class='line'>{row.get('liability_equity_statutory_line_number') or ''}</td>"
			f"<td class='num'>{liability_amount}</td><td class='num'>{liability_opening}</td></tr>"
		)
	parts.append("</tbody></table>")
	return "".join(parts)


def _render_standard_pdf_rows(columns, rows):
	visible_columns = [column for column in columns if column.get("fieldname")]
	head = "".join(f"<th>{frappe.utils.escape_html(str(column.get('label') or ''))}</th>" for column in visible_columns)
	parts = [f"<table><thead><tr>{head}</tr></thead><tbody>"]
	for row in rows:
		is_bold = " class='bold'" if row.get("bold") else ""
		cells = []
		for column in visible_columns:
			fieldname = column["fieldname"]
			value = row.get(fieldname, "")
			if column.get("fieldtype") in {"Currency", "Float", "Int"}:
				cells.append(f"<td class='num'>{_amount(value)}</td>")
			else:
				indent = "&nbsp;" * (int(row.get("indent") or 0) * 4)
				cells.append(f"<td>{indent}{frappe.utils.escape_html(str(value or ''))}</td>")
		parts.append(f"<tr{is_bold}>{''.join(cells)}</tr>")
	parts.append("</tbody></table>")
	return "".join(parts)


def get_balance_sheet_chart(rows, company, filters):
	amounts = {row["row_code"]: flt(row.get("amount")) for row in rows}
	period_label = filters.get("accounting_period") or filters.get("to_date")
	return {
		"data": {
			"labels": [period_label],
			"datasets": [
				{"name": _("资产"), "values": [amounts.get("TOTAL_ASSETS", 0)]},
				# The chart uses the zero axis to distinguish the two sides of the accounting equation.
				{"name": _("负债"), "values": [-amounts.get("TOTAL_LIABILITIES", 0)]},
				{"name": _("所有者权益"), "values": [-amounts.get("OWNERS_EQUITY", 0)]},
			],
		},
		"type": "bar",
		"fieldtype": "Currency",
		"options": "currency",
		"colors": ["#2563eb", "#f59e0b", "#16a34a"],
	}


def get_balance_sheet_summary(rows, company):
	amounts = {row["row_code"]: flt(row.get("amount")) for row in rows}
	total_assets = amounts.get("TOTAL_ASSETS", 0)
	total_liabilities = amounts.get("TOTAL_LIABILITIES", 0)
	total_equity = amounts.get("OWNERS_EQUITY", 0)
	balance_difference = flt(total_assets - total_liabilities - total_equity, 2)
	currency = frappe.get_cached_value("Company", company, "default_currency")
	return [
		{"value": total_assets, "label": _("资产总计"), "datatype": "Currency", "currency": currency},
		{"value": total_liabilities, "label": _("负债合计"), "datatype": "Currency", "currency": currency},
		{"value": total_equity, "label": _("所有者权益合计"), "datatype": "Currency", "currency": currency},
		{
			"value": balance_difference,
			"label": _("平衡差额（资产 - 负债 - 所有者权益）"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Green" if not balance_difference else "Red",
		},
	]


def get_profit_and_loss_metrics(rows):
	"""Calculate year-to-date highlights using stable statutory report row codes."""
	amounts = {row["row_code"]: flt(row.get("year_to_date_amount")) for row in rows}
	total_expenses = sum(
		amounts.get(code, 0)
		for code in (
			"OPERATING_COST",
			"TAX_SURCHARGES",
			"SELLING_EXPENSES",
			"ADMIN_EXPENSES",
			"RESEARCH_EXPENSES",
			"FINANCE_EXPENSES",
			"NONOPERATING_EXPENSE",
			"INCOME_TAX",
		)
	)
	return {
		"income": amounts.get("OPERATING_REVENUE", 0),
		"gross_profit": amounts.get("OPERATING_REVENUE", 0) - amounts.get("OPERATING_COST", 0),
		"expenses": total_expenses,
		"profit": amounts.get("NET_PROFIT", 0),
	}


def get_profit_and_loss_chart(rows, company, filters):
	metrics = get_profit_and_loss_metrics(rows)
	period_label = filters.get("accounting_period") or filters.get("to_date")
	return {
		"data": {
			"labels": [period_label],
			"datasets": [
				# Present the operating bridge consistently: income increases profit,
				# expenses reduce it, and profit keeps its actual accounting sign.
				{"name": _("收入"), "values": [metrics["income"]]},
				{"name": _("费用"), "values": [-metrics["expenses"]]},
				{"name": _("净利润"), "values": [metrics["profit"]]},
			],
		},
		"type": "bar",
		"fieldtype": "Currency",
		"options": "currency",
		"colors": ["#ec6d9d", "#3187d4", "#45b978"],
	}


def get_profit_and_loss_summary(rows, company):
	metrics = get_profit_and_loss_metrics(rows)
	income = metrics["income"]
	metrics["gross_margin"] = (
		flt(metrics["gross_profit"] / abs(income) * 100, 2) if abs(income) > 0.01 else None
	)
	metrics["net_margin"] = (
		flt(metrics["profit"] / abs(income) * 100, 2) if abs(income) > 0.01 else None
	)
	currency = frappe.get_cached_value("Company", company, "default_currency")
	return [
		{"value": metrics["income"], "label": _("本年收入"), "datatype": "Currency", "currency": currency},
		{"value": metrics["gross_profit"], "label": _("本年毛利"), "datatype": "Currency", "currency": currency},
		{"value": metrics["gross_margin"], "label": _("毛利率"), "datatype": "Percent", "indicator": "Green" if metrics["gross_margin"] is not None and metrics["gross_margin"] >= 0 else "Red"},
		{"value": metrics["expenses"], "label": _("本年费用"), "datatype": "Currency", "currency": currency},
		{
			"value": metrics["profit"],
			"label": _("本年利润"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Green" if metrics["profit"] >= 0 else "Red",
		},
		{"value": metrics["net_margin"], "label": _("净利率"), "datatype": "Percent", "indicator": "Green" if metrics["net_margin"] is not None and metrics["net_margin"] >= 0 else "Red"},
	]


def get_cash_flow_metrics(rows):
	"""Calculate cash-flow highlights from the statutory cash-flow rows."""
	amounts = {row["row_code"]: flt(row.get("year_to_date_amount")) for row in rows}
	return {
		"operating": amounts.get("OPERATING_CASH_FLOW", 0),
		"investing": amounts.get("INVESTING_CASH_FLOW", 0),
		"financing": amounts.get("FINANCING_CASH_FLOW", 0),
		"net_increase": amounts.get("NET_CASH_INCREASE", 0),
	}


def get_cash_flow_dashboard_metrics(rows, company, filters):
	metrics = get_cash_flow_metrics(rows)
	closing_balance_sheet = build_statement(
		company,
		"Balance Sheet",
		filters.from_date,
		filters.to_date,
		filters.finance_book,
		filters.cost_center,
		filters.project,
	)
	balance_amounts = {row["row_code"]: flt(row.get("amount")) for row in closing_balance_sheet["rows"]}
	return {
		"operating": metrics["operating"],
		"cash_balance": flt(next(
			(row.get("amount") for row in rows if row.get("row_code") == "CLOSING_CASH"),
			0,
		)),
		"accounts_receivable": balance_amounts.get("ACCOUNTS_RECEIVABLE", 0),
		"inventory": balance_amounts.get("INVENTORIES", 0),
	}


def get_cash_flow_chart(rows, company, filters):
	metrics = get_cash_flow_dashboard_metrics(rows, company, filters)
	period_label = filters.get("accounting_period") or filters.get("to_date")
	return {
		"data": {
			"labels": [period_label],
			"datasets": [
				{"name": _("经营现金流"), "values": [metrics["operating"]]},
				{"name": _("期末现金及现金等价物"), "values": [metrics["cash_balance"]]},
				{"name": _("应收账款"), "values": [metrics["accounts_receivable"]]},
				{"name": _("存货"), "values": [metrics["inventory"]]},
			],
		},
		"type": "bar",
		"fieldtype": "Currency",
		"options": "currency",
		"colors": ["#16a34a", "#2563eb", "#f59e0b", "#e76f51"],
	}


def get_cash_flow_summary(rows, company, filters):
	metrics = get_cash_flow_dashboard_metrics(rows, company, filters)
	currency = frappe.get_cached_value("Company", company, "default_currency")
	return [
		{
			"value": metrics["operating"],
			"label": _("经营活动产生的现金流量净额"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Green" if metrics["operating"] >= 0 else "Red",
		},
		{
			"value": metrics["cash_balance"],
			"label": _("期末现金及现金等价物余额"),
			"datatype": "Currency",
			"currency": currency,
		},
		{
			"value": metrics["accounts_receivable"],
			"label": _("应收账款余额"),
			"datatype": "Currency",
			"currency": currency,
		},
		{
			"value": metrics["inventory"],
			"label": _("存货余额"),
			"datatype": "Currency",
			"currency": currency,
		},
	]
