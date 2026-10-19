import frappe
from frappe import _
from frappe.utils import add_days, flt, get_first_day, get_last_day, getdate, now_datetime

from erpnext.accounts.utils import get_fiscal_year

from china_finance.services.financial_statement import (
	PROFIT_AND_LOSS_CLOSING_REMARK,
	get_comparison_period,
	get_template,
	snapshot_statement,
	validate_statement_links,
)
from china_finance.services.ledger_reconciliation import get_ar_ap_ledger_check
from china_finance.services.reconciliation_control import get_reconciliation_closing_checks
from china_finance.services.archive import create_archive_package
from china_finance.services.voucher import calculate_entries_hash, get_pending_cancellation_sync_issues
from china_finance.services.purchase_reconciliation import get_blocked_purchase_invoices
from china_finance.services.tax_reconciliation import get_input_tax_closing_checks, get_output_tax_closing_checks
from china_finance.services.disclosure import get_disclosure_closing_checks, get_notes_payload
from china_finance.services.sales_settlement import MODE_SETTLEMENT, get_sales_settlement_closing_check
from china_finance.services.cash_flow_assignment import get_closing_assignment_check
from china_finance.services.statutory_reporting import get_statutory_report_readiness_data
from china_finance.services.prior_period_error import get_prior_period_error_readiness
from china_finance.setup.china_coa_profile import (
	TEMPORARY_ACCOUNT_NUMBERS, get_china_coa_master_data_readiness,
	get_company_accounts_by_number, get_profile_status,
)
from china_finance.setup.templates import (
	STATEMENT_TYPE_LABELS,
	classify_company_account, is_strictly_excluded_from_statement,
	refine_classification_for_template, requires_manual_cash_flow_assignment,
)

PERIOD_CLOSING_WORKFLOW_STATES = ("Draft", "Pending Review", "Approved", "Posted")


def _get_period_closing_next_states(current_state):
	if current_state not in PERIOD_CLOSING_WORKFLOW_STATES[:-1]:
		return ()
	return PERIOD_CLOSING_WORKFLOW_STATES[PERIOD_CLOSING_WORKFLOW_STATES.index(current_state) + 1 :]


def _has_active_rule(doctype, company, from_date, to_date):
	rows = frappe.get_all(
		doctype,
		filters={"company": company, "enabled": 1, "effective_from": ["<=", to_date]},
		fields=["effective_to"],
	)
	return any(not row.effective_to or getdate(row.effective_to) >= getdate(from_date) for row in rows)


def _has_period_tax_invoices(company, from_date, to_date, direction=None):
	filters = {
		"company": company, "docstatus": 1,
		"invoice_date": ["between", [from_date, to_date]],
	}
	if direction:
		filters["direction"] = direction
	return bool(frappe.db.exists("China Tax Invoice", filters))


def _has_period_sales_settlement(company, from_date, to_date):
	return bool(frappe.db.exists(
		"Delivery Note",
		{
			"company": company, "posting_date": ["between", [from_date, to_date]],
			"docstatus": 1, "is_return": 0, "custom_china_settlement_mode": MODE_SETTLEMENT,
		},
	))


def run_closing_checks(company, from_date, to_date, period_closing_voucher=None, closing_type="Monthly"):
	checks = []
	settings = frappe.get_cached_doc("China Finance Settings", company)
	voucher_from_date = max(getdate(from_date), getdate(settings.activation_date))

	def add(code, description, passed, details="", severity="Blocking"):
		checks.append({"check_code": code, "description": description, "passed": int(bool(passed)), "details": details, "severity": severity})

	from china_finance.services.bank_receipt_import import pending_receipts
	unfinished_receipts = pending_receipts(company, from_date, to_date)
	add("BANK_RECEIPTS_PROCESSED", "本期银行回单已处理并记账", not unfinished_receipts,
		"、".join(r.transaction_id for r in unfinished_receipts[:20]))

	configuration_errors = []
	role_separation_warning = False
	if not settings.enforce_role_separation:
		role_separation_warning = True
	if not settings.profit_loss_account:
		configuration_errors.append(_("未配置本年利润科目"))
	if not settings.retained_earnings_account:
		configuration_errors.append(_("未配置利润分配科目"))
	if flt(settings.reconciliation_tolerance) <= 0:
		configuration_errors.append(_("对账金额容差必须大于零"))
	add(
		"CONFIGURATION_READINESS", _("中国财务关键配置完整"), not configuration_errors and not role_separation_warning,
		"；".join(
			[*configuration_errors, *([_("未启用制单、审核、记账职责分离")] if role_separation_warning else [])]
		),
		"Blocking" if configuration_errors else ("Warning" if role_separation_warning else "Blocking"),
	)
	prior_errors = get_prior_period_error_readiness(company, from_date, to_date)
	add(
		"PRIOR_PERIOD_ERROR_ADJUSTMENT", _("前期差错更正已审批并附依据"),
		prior_errors["passed"], prior_errors["details"],
	)
	coa_status = get_profile_status(company)
	if coa_status.get("supported"):
		coa_errors = coa_status.get("errors", [])
		coa_warnings = coa_status.get("warnings", [])
		add(
			"CHINA_COA_INTEGRITY", _("中国科目模板及公司默认科目完整"), not coa_errors,
			"；".join([*coa_errors, *coa_warnings]),
			"Blocking" if coa_errors else ("Warning" if coa_warnings else "Blocking"),
		)
		accounts, _duplicates = get_company_accounts_by_number(company)
		temporary_accounts = [accounts[number].name for number in TEMPORARY_ACCOUNT_NUMBERS if number in accounts]
		temporary_balances = []
		if temporary_accounts:
			temporary_balances = frappe.db.sql(
				"""SELECT account, COALESCE(SUM(debit-credit), 0) AS balance FROM `tabGL Entry`
				WHERE company=%(company)s AND posting_date<=%(to_date)s AND is_cancelled=0 AND account IN %(accounts)s
				GROUP BY account""",
				{"company": company, "to_date": to_date, "accounts": temporary_accounts},
				as_dict=True,
			)
		violations = [row for row in temporary_balances if abs(flt(row.balance)) > flt(settings.reconciliation_tolerance)]
		add(
			"CHINA_COA_TEMPORARY_BALANCE", _("临时及待处理科目期末余额为零"),
			not violations,
			"；".join(f"{row.account}: {row.balance}" for row in violations),
		)
		master_data = get_china_coa_master_data_readiness(company)
		blocking_items = [item for item in master_data["items"] if not item["passed"] and item["severity"] == "Blocking"]
		add(
			"CHINA_COA_MASTER_DATA", _("主数据与业务默认科目已就绪"), not blocking_items,
			"；".join(item["label"] + "：" + item["details"] for item in blocking_items),
		)

	required_scope_types = []
	for fieldname, scope_type in (
		("require_customer_reconciliation", "Customer"),
		("require_supplier_reconciliation", "Supplier"),
		("require_bank_reconciliation", "Bank"),
	):
		if settings.get(fieldname):
			required_scope_types.append(scope_type)
	active_scope_types = {
		row.scope_type for row in frappe.get_all(
			"China Reconciliation Scope",
			filters={"company": company, "enabled": 1, "effective_from": ["<=", to_date]},
			fields=["scope_type", "effective_to"],
		)
		if not row.effective_to or getdate(row.effective_to) >= getdate(from_date)
	}
	missing_scope_types = [scope_type for scope_type in required_scope_types if scope_type not in active_scope_types]
	add(
		"RECONCILIATION_SCOPE_CONFIGURATION", _("客户、供应商和银行对账范围已按设置配置"),
		not missing_scope_types,
		_("缺少对账范围：{0}").format(", ".join(missing_scope_types)) if missing_scope_types else "",
		"Blocking" if closing_type == "Year End" else "Warning",
	)

	trial = frappe.db.sql(
		"SELECT COALESCE(SUM(debit-credit), 0) FROM `tabGL Entry` WHERE company=%s AND posting_date<=%s AND is_cancelled=0",
		(company, to_date),
	)[0][0]
	add("TRIAL_BALANCE", _("总账借贷平衡"), abs(flt(trial)) <= 0.005, str(trial))

	missing = count_missing_vouchers(company, voucher_from_date, to_date)
	pending_cancellations = get_pending_cancellation_sync_issues(company, voucher_from_date, to_date)
	voucher_coverage_details = [_("缺少 {0} 张").format(missing)] if missing else []
	if pending_cancellations:
		voucher_coverage_details.append(_("待补齐冲销审计快照 {0} 张").format(len(pending_cancellations)))
	add(
		"VOUCHER_COVERAGE", _("总账来源与冲销均已生成审计快照"),
		missing == 0 and not pending_cancellations, "；".join(voucher_coverage_details),
	)

	hash_errors = count_voucher_hash_errors(company, voucher_from_date, to_date)
	add("VOUCHER_HASH", _("凭证快照与分录哈希一致"), hash_errors == 0, _("异常 {0} 张").format(hash_errors))

	# Monthly closing is an operational lock.  The statutory report package has
	# its own readiness gate, so report mapping and disclosure checks belong to
	# year-end/report preparation instead of every monthly close.
	if closing_type == "Monthly":
		checks.extend(get_monthly_report_template_checks(company, from_date, to_date))
	if closing_type == "Year End":
		current_templates = [
			get_template(company, statement_type, to_date).name
			for statement_type in ("Balance Sheet", "Profit and Loss", "Cash Flow", "Changes in Equity")
		]
		unreviewed = frappe.db.count(
			"China Financial Statement Mapping",
			{"company": company, "template": ["in", current_templates], "reviewed": 0},
		)
		add("MAPPING_REVIEW", _("财务报表科目映射已复核"), unreviewed == 0, _("未复核 {0} 条").format(unreviewed))

		missing_templates = []
		for statement_type in ("Balance Sheet", "Profit and Loss", "Cash Flow", "Changes in Equity"):
			template = get_template(company, statement_type, to_date).name
			if not template or not frappe.db.exists(
				"China Financial Statement Mapping", {"company": company, "template": template, "reviewed": 1}
			):
				missing_templates.append(statement_type)
		add(
			"MAPPING_COVERAGE",
			_("四类财务报表均已配置并复核科目映射"),
			not missing_templates,
			_("缺少：{0}").format(", ".join(missing_templates)) if missing_templates else "",
		)
		mapping_coverage = get_account_mapping_coverage(company, current_templates)
		add(
			"ACCOUNT_MAPPING_COMPLETENESS", _("适用会计科目均已纳入四类报表映射"),
			mapping_coverage["passed"], mapping_coverage["details"],
		)

	reconciliation_checks = get_reconciliation_closing_checks(company, from_date, to_date)
	add(
		"RECONCILIATION_COVERAGE", _("强制对账范围均已完成当期确认"),
		reconciliation_checks["coverage"]["passed"], reconciliation_checks["coverage"]["details"],
	)
	add(
		"RECONCILIATION_DIFFERENCE", _("强制对账差异均已闭环"),
		reconciliation_checks["differences"]["passed"], reconciliation_checks["differences"]["details"],
	)
	# Draft and approved-timing rows are informational only.  They remain
	# available on the reconciliation screens but are not closing checks.

	ledger_check = get_ar_ap_ledger_check(company, to_date)
	add(
		"AR_AP_LEDGER_MATCH", _("应收应付总账与付款台账一致"),
		ledger_check["passed"], ledger_check["details"],
	)
	checks.append(get_closing_assignment_check(company, from_date, to_date, closing_type))

	if closing_type == "Year End":
		statement_checks = validate_statement_links(company, from_date, to_date, settings.reconciliation_tolerance)
		add(
			"BALANCE_SHEET_EQUATION", _("资产总计等于负债和所有者权益总计"),
			statement_checks["balance_sheet"]["passed"], statement_checks["balance_sheet"]["details"],
		)
		add(
			"PROFIT_EQUITY_LINK", _("利润表净利润与所有者权益变动表衔接"),
			statement_checks["profit_equity"]["passed"], statement_checks["profit_equity"]["details"],
		)
		add(
			"CASH_FLOW_RECONCILIATION", _("现金流量表与现金及现金等价物变动衔接"),
			statement_checks["cash_flow"]["passed"], statement_checks["cash_flow"]["details"],
		)
		disclosure_checks = get_disclosure_closing_checks(company, from_date, to_date, closing_type)
		add(
			"ACCOUNTING_POLICY_COVERAGE", _("核心会计政策已完整配置并生效"),
			disclosure_checks["policy"]["passed"], disclosure_checks["policy"]["details"], "Blocking",
		)
		add(
			"FINANCIAL_STATEMENT_NOTES", _("财务报表附注已提交"),
			disclosure_checks["notes"]["passed"], disclosure_checks["notes"]["details"], "Blocking",
		)
		statutory_readiness = get_statutory_report_readiness_data(company, from_date, to_date)
		add(
			"STATUTORY_REPORT_READINESS", _("中国会计准则四表一注正式输出就绪"),
			statutory_readiness["passed"],
			_("未完成项 {0}").format(statutory_readiness["blocking_count"]), "Blocking",
		)

	if _has_active_rule("China Purchase Reconciliation Rule", company, from_date, to_date):
		blocked_purchases = get_blocked_purchase_invoices(company, from_date, to_date)
		add(
			"PURCHASE_RECONCILIATION",
			_("受控采购已完成齐套校验"),
			not blocked_purchases,
			_("未齐套 {0} 张").format(len(blocked_purchases)),
		)

	if _has_period_sales_settlement(company, from_date, to_date):
		sales_settlement = get_sales_settlement_closing_check(company, from_date, to_date)
		add(
			"SALES_SETTLEMENT_COVERAGE",
			_("对账结算销售出库已完成正式应收确认"),
			sales_settlement["passed"],
			sales_settlement["details"],
		)

	has_output_tax = (
		_has_period_tax_invoices(company, from_date, to_date, "销项")
		or _has_active_rule("China Invoice Control Rule", company, from_date, to_date)
	)
	has_input_tax = _has_period_tax_invoices(company, from_date, to_date, "进项")
	if has_output_tax or has_input_tax:
		unallocated_tax = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabChina Tax Invoice` ti
			WHERE ti.company=%s AND ti.invoice_date BETWEEN %s AND %s AND ti.docstatus=1
			AND NOT EXISTS (SELECT 1 FROM `tabChina Tax Invoice Allocation` a WHERE a.parent=ti.name)
			""",
			(company, from_date, to_date),
		)[0][0]
		add("TAX_ALLOCATION", _("税务发票已完成业务分摊"), unallocated_tax == 0, _("未分摊 {0} 张").format(unallocated_tax), "Warning")

	if has_output_tax:
		output_tax_checks = get_output_tax_closing_checks(company, from_date, to_date)
		add(
			"OUTPUT_INVOICE_COVERAGE",
			_("必须开票的销售发票已完成票账闭环"),
			output_tax_checks["coverage"]["passed"],
			output_tax_checks["coverage"]["details"],
		)
		add(
			"OUTPUT_TAX_GL",
			_("销项税税票与总账发生额一致"),
			output_tax_checks["gl"]["passed"],
			output_tax_checks["gl"]["details"],
		)

	if has_input_tax:
		input_tax_checks = get_input_tax_closing_checks(company, from_date, to_date)
		add(
			"INPUT_TAX_ACCOUNTING",
			_("进项税票分摊与总账发生额一致"),
			input_tax_checks["accounting"]["passed"],
			input_tax_checks["accounting"]["details"],
		)

	pcv_ok = bool(period_closing_voucher and frappe.db.get_value("Period Closing Voucher", period_closing_voucher, "docstatus") == 1)
	add("PERIOD_CLOSING", _("损益结转已完成"), pcv_ok)
	return checks


@frappe.whitelist()
def get_period_closing_entries(name):
	"""Return the generated GL entries for a Period Closing Voucher.

	The standard Period Closing Voucher creates its accounting entries in GL
	Entry, sometimes asynchronously. Reading the active GL rows keeps this
	view aligned with the accounting ledger and avoids maintaining a second,
	editable copy of the closing entries on the voucher itself.
	"""
	if not name:
		return {"rows": [], "total_debit": 0, "total_credit": 0, "processing_status": ""}

	voucher = frappe.get_doc("Period Closing Voucher", name)
	voucher.check_permission("read")
	currency = frappe.db.get_value("Company", voucher.company, "default_currency")
	rows = frappe.get_all(
		"GL Entry",
		filters={
			"company": voucher.company,
			"voucher_type": "Period Closing Voucher",
			"voucher_no": voucher.name,
			"is_cancelled": 0,
		},
		fields=["name", "account", "debit", "credit", "remarks", "account_currency"],
		order_by="creation asc, name asc",
		ignore_permissions=True,
	)

	total_debit = 0
	total_credit = 0
	for index, row in enumerate(rows, 1):
		row["idx"] = index
		row["summary"] = row.get("remarks") or voucher.remarks or ""
		total_debit += flt(row.get("debit"))
		total_credit += flt(row.get("credit"))

	return {
		"rows": rows,
		"total_debit": total_debit,
		"total_credit": total_credit,
		"currency": currency,
		"processing_status": voucher.gle_processing_status or "",
		"docstatus": voucher.docstatus,
	}


def get_account_mapping_coverage(company, templates):
	template_types = dict(
		frappe.get_all(
			"China Financial Statement Template", filters={"name": ["in", templates]},
			fields=["name", "statement_type"], as_list=True,
		)
	)
	mapped = {
		(row.template, row.account)
		for row in frappe.get_all(
			"China Financial Statement Mapping", filters={"company": company, "template": ["in", templates]},
			fields=["template", "account"],
		)
	}
	accounts = frappe.get_all(
		"Account", filters={"company": company, "is_group": 0, "disabled": 0},
		fields=["name", "root_type", "account_type"],
	)
	missing = []
	for template, statement_type in template_types.items():
		valid_rows = {
			row.row_code: row.row_type
			for row in frappe.get_cached_doc("China Financial Statement Template", template).rows
		}
		accounts_by_name = {
			row.name: row for row in frappe.get_all(
				"Account", filters={"company": company, "disabled": 0},
				fields=["name", "account_name", "account_number", "parent_account", "root_type", "account_type", "is_group"],
			)
		}
		for account in accounts:
			if not is_account_applicable_to_statement(statement_type, account.root_type, account.account_type):
				continue
			if is_strictly_excluded_from_statement(account.account_number, statement_type):
				continue
			if statement_type == "Cash Flow" and requires_manual_cash_flow_assignment(account.account_number):
				continue
			classification, _basis = classify_company_account(company, account, statement_type, accounts_by_name)
			classification = refine_classification_for_template(account, statement_type, valid_rows, classification)
			row_code = classification[0] if isinstance(classification, tuple) else classification
			# An account with no applicable row in the selected statutory form is
			# intentionally outside that form, not a missing mapping.
			required = bool(row_code and valid_rows.get(row_code) == "Mapped Accounts")
			if required and (template, account.name) not in mapped:
				missing.append(f"{statement_type}:{account.name}")
	return {
		"passed": not missing, "missing": missing,
		"details": _("未映射科目 {0} 个：{1}").format(len(missing), "；".join(missing[:10])) if missing else _("适用科目均已映射"),
	}


def is_account_applicable_to_statement(statement_type, root_type, account_type=None):
	if statement_type == "Balance Sheet":
		return root_type in {"Asset", "Liability", "Equity"}
	if statement_type == "Profit and Loss":
		return root_type in {"Income", "Expense"}
	if statement_type == "Changes in Equity":
		return root_type == "Equity"
	if statement_type == "Cash Flow":
		return account_type not in {"Cash", "Bank"}
	return False


def count_missing_vouchers(company, from_date, to_date):
	return frappe.db.sql(
		"""
		SELECT COUNT(*) FROM (
			SELECT DISTINCT gle.voucher_type, gle.voucher_no
			FROM `tabGL Entry` gle
			LEFT JOIN `tabChina Accounting Voucher` cav
				ON cav.source_key=CONCAT('Posting|', gle.voucher_type, '|', gle.voucher_no) AND cav.docstatus=1
			WHERE gle.company=%s AND gle.posting_date BETWEEN %s AND %s AND gle.is_cancelled=0
			AND cav.name IS NULL
		) missing
		""",
		(company, from_date, to_date),
	)[0][0]


def get_pending_bank_vouchers(company, from_date, to_date):
	"""Return bank-generated Journal Entries that are still drafts.

	A Period Closing Voucher reads GL Entry. Bank transactions whose linked
	Journal Entries are still drafts are therefore not ready to be included in
	the monthly closing calculation.
	"""
	if not frappe.db.has_column("Bank Transaction", "custom_china_journal_entry"):
		return []
	return frappe.db.sql(
		"""
		SELECT bt.name, bt.date, bt.reference_number, bt.custom_china_journal_entry
		FROM `tabBank Transaction` bt
		LEFT JOIN `tabJournal Entry` je ON je.name=bt.custom_china_journal_entry
		WHERE bt.company=%s
			AND bt.date BETWEEN %s AND %s
			AND bt.docstatus=1
			AND bt.custom_china_journal_entry IS NOT NULL
			AND (je.name IS NULL OR je.docstatus!=1)
		ORDER BY bt.date, bt.reference_number
		""",
		(company, from_date, to_date),
		as_dict=True,
	)


def get_ordinary_profit_loss_closings(company, from_date, to_date):
	"""Find legacy ordinary Journal Entries used for period closing."""
	return frappe.get_all(
		"Journal Entry",
		filters={
			"company": company,
			"posting_date": ["between", [from_date, to_date]],
			"docstatus": ["!=", 2],
			"voucher_type": "Journal Entry",
		},
		or_filters=[
			["Journal Entry", "user_remark", "=", PROFIT_AND_LOSS_CLOSING_REMARK],
			["Journal Entry", "remark", "=", PROFIT_AND_LOSS_CLOSING_REMARK],
		],
		fields=["name", "posting_date", "docstatus"],
		order_by="posting_date, name",
	)


def count_voucher_hash_errors(company, from_date, to_date):
	errors = 0
	for name in frappe.get_all(
		"China Accounting Voucher",
		filters={"company": company, "posting_date": ["between", [from_date, to_date]], "docstatus": 1},
		pluck="name",
	):
		doc = frappe.get_doc("China Accounting Voucher", name)
		if calculate_entries_hash(doc.entries) != doc.source_hash:
			errors += 1
	return errors


def get_monthly_report_template_checks(company, from_date, to_date):
	missing_current, missing_comparison = [], []
	for statement_type, label in STATEMENT_TYPE_LABELS.items():
		if not get_template(company, statement_type, to_date, required=False):
			missing_current.append(f"{label}（{to_date}）")
		_comparison_from, comparison_to = get_comparison_period(company, statement_type, from_date, to_date)
		if not get_template(company, statement_type, comparison_to, required=False):
			missing_comparison.append(f"{label}（{comparison_to}）")
	return [
		{
			"check_code": "REPORT_TEMPLATE_COVERAGE", "description": _("本期报表模板可用"),
			"passed": int(not missing_current), "severity": "Blocking",
			"details": _("缺少本期报表模板：{0}").format("；".join(missing_current)) if missing_current else "",
		},
		{
			"check_code": "COMPARISON_TEMPLATE_COVERAGE", "description": _("比较期报表模板可用"),
			"passed": int(not missing_comparison), "severity": "Warning",
			"details": _("缺少比较期报表模板：{0}。月结可继续，相关报表保留为草表，比较金额留空；正式报表需补齐比较数据。").format(
				"；".join(missing_comparison)
			) if missing_comparison else "",
		},
	]


def create_report_snapshots(closing_run):
	validation_results = validate_statement_links(
		closing_run.company, closing_run.from_date, closing_run.to_date,
		frappe.db.get_value("China Finance Settings", closing_run.company, "reconciliation_tolerance"),
	)
	notes_payload = get_notes_payload(closing_run.company, closing_run.from_date, closing_run.to_date)
	snapshots = [
		snapshot_statement(closing_run, statement_type, validation_results, notes_payload)
		for statement_type in ("Balance Sheet", "Profit and Loss", "Cash Flow", "Changes in Equity")
	]
	return snapshots


def create_closing_archive(closing_run_name):
	"""Create the ZIP package after the closing transaction has committed.

	Writing an attached file during the submit request can restart a development
	server's file watcher before the HTTP response is returned.  Running this as
	after-commit work also ensures an archive is never created for a rolled-back
	closing run.
	"""
	closing_run = frappe.get_doc("China Closing Run", closing_run_name)
	if closing_run.docstatus != 1 or closing_run.status != "Closed":
		frappe.throw(_("月末结账单 {0} 尚未完成，不能生成结账档案").format(closing_run.name))
	if closing_run.archive_package:
		return closing_run.archive_package

	package = create_archive_package(
		closing_run.company,
		"China Closing Run",
		closing_run.name,
		closing_run=closing_run.name,
	)
	closing_run.db_set("archive_package", package["file_url"], update_modified=False)
	return package["file_url"]


@frappe.whitelist()
def create_period_closing_voucher(company, from_date, to_date, closing_type="Monthly", allow_first_period=False):
	"""Create the draft PCV for the next continuous closing period.

	ERPNext calculates the actual entries when the Period Closing Voucher is
	submitted. This endpoint only creates the reviewed draft and supplies the
	configured profit-and-loss closing account as the target account. An explicit
	``allow_first_period`` opt-in permits a complete monthly period to be used as
	the first period when no earlier closing voucher exists.
	"""
	frappe.only_for(("System Manager", "China Finance Manager"))

	if not company:
		frappe.throw(_("必须选择公司"))

	from_date = getdate(from_date)
	to_date = getdate(to_date)
	if not from_date or not to_date or from_date > to_date:
		frappe.throw(_("损益结转期间不正确"))
	if closing_type not in ("Monthly", "Year End"):
		frappe.throw(_("不支持的结账类型：{0}").format(closing_type))

	settings = frappe.get_cached_doc("China Finance Settings", company)
	if not settings.profit_loss_account:
		frappe.throw(_("请先在中国财务设置中配置本年利润科目"))

	fiscal_year_info = get_fiscal_year(to_date, company=company)
	fiscal_year, fiscal_year_start, fiscal_year_end = (
		fiscal_year_info[0], getdate(fiscal_year_info[1]), getdate(fiscal_year_info[2])
	)
	if to_date > fiscal_year_end:
		frappe.throw(_("结转截止日期不能晚于会计年度结束日 {0}").format(fiscal_year_end))

	previous_end_date = frappe.db.get_value(
		"Period Closing Voucher",
		{"company": company, "fiscal_year": fiscal_year, "docstatus": 1},
		"period_end_date",
		order_by="period_end_date desc",
	)
	required_start_date = add_days(getdate(previous_end_date), 1) if previous_end_date else fiscal_year_start
	first_period_override = bool(
		allow_first_period
		and not previous_end_date
		and closing_type == "Monthly"
		and from_date >= fiscal_year_start
	)
	if from_date != required_start_date and not first_period_override:
		frappe.throw(
			_("本期必须从 {0} 开始，不能跳过或重复结转期间").format(required_start_date),
			title=_("结转期间不连续"),
		)

	if closing_type == "Monthly":
		if from_date != get_first_day(from_date) or to_date != get_last_day(from_date):
			frappe.throw(_("月度结转期间必须是完整自然月"), title=_("月度结转期间不正确"))
	elif to_date != fiscal_year_end:
		frappe.throw(_("年度结转截止日期必须是会计年度结束日 {0}").format(fiscal_year_end))

	from china_finance.services.bank_receipt_import import pending_receipts
	unfinished_receipts = pending_receipts(company, from_date, to_date)
	if unfinished_receipts:
		frappe.throw("本期还有未处理或未记账的银行回单：" + "、".join(r.transaction_id for r in unfinished_receipts[:20]))
	pending_bank_vouchers = get_pending_bank_vouchers(company, from_date, to_date)
	if pending_bank_vouchers:
		references = [row.reference_number or row.name for row in pending_bank_vouchers[:10]]
		frappe.throw(
			_("本期还有 {0} 张银行流水凭证未审核提交，请先处理：{1}").format(
				len(pending_bank_vouchers), "、".join(references)
			),
			title=_("银行凭证尚未完成"),
		)

	legacy_closings = get_ordinary_profit_loss_closings(company, from_date, to_date)
	if legacy_closings:
		frappe.throw(
			_("本期已有普通记账凭证作为损益结转：{0}。请先取消或确认旧结转，再生成月末结转凭证").format(
				"、".join(row.name for row in legacy_closings)
			),
			title=_("存在旧损益结转"),
		)

	existing = frappe.db.get_value(
		"Period Closing Voucher",
		{
			"company": company,
			"period_start_date": from_date,
			"period_end_date": to_date,
			"docstatus": ["!=", 2],
		},
		["name", "docstatus", "closing_account_head"],
		as_dict=True,
	)
	if existing:
		if existing.closing_account_head != settings.profit_loss_account:
			frappe.throw(
				_("该期间已有损益结转凭证 {0}，但结转科目不是当前设置的 {1}").format(
					existing.name, settings.profit_loss_account
				),
				title=_("结转科目不一致"),
			)
		return {
			"name": existing.name,
			"created": False,
			"docstatus": existing.docstatus,
			"message": _("该期间已经存在损益结转凭证 {0}").format(existing.name),
		}

	voucher = frappe.get_doc(
		{
			"doctype": "Period Closing Voucher",
			"transaction_date": to_date,
			"company": company,
			"fiscal_year": fiscal_year,
			"period_start_date": from_date,
			"period_end_date": to_date,
			"closing_account_head": settings.profit_loss_account,
			"remarks": _("{0}至{1}损益结转").format(from_date, to_date),
		}
	)
	voucher.insert()
	return {
		"name": voucher.name,
		"created": True,
		"docstatus": voucher.docstatus,
		"message": _("已生成损益结转凭证草稿 {0}，提交后系统将按实际损益科目生成分录").format(voucher.name),
	}


@frappe.whitelist()
def preview_closing_checks(company, from_date, to_date, period_closing_voucher=None, closing_type="Monthly"):
	frappe.only_for(("System Manager", "China Finance Manager"))
	return run_closing_checks(company, getdate(from_date), getdate(to_date), period_closing_voucher, closing_type)


@frappe.whitelist(methods=["POST"])
def submit_closing_run(name):
	"""Submit the latest server copy of a closing run through its normal hooks.

	The Desk submit action posts the complete client document, including its
	``modified`` timestamp.  A closing-check save can otherwise race that request
	and raise a timestamp mismatch.  Loading the document again under a row lock
	keeps submission serial while preserving all standard validations, report
	snapshots, after-commit archive generation, and account-freeze behavior.
	"""
	if not name:
		frappe.throw(_("请选择需要确认的月末结账单"))
	if not frappe.db.exists("China Closing Run", name):
		frappe.throw(_("月末结账单 {0} 不存在").format(name))

	frappe.db.sql("SELECT name FROM `tabChina Closing Run` WHERE name=%s FOR UPDATE", (name,))
	doc = frappe.get_doc("China Closing Run", name)
	doc.check_permission("submit")

	if doc.docstatus == 1:
		if doc.status != "Closed":
			frappe.throw(_("月末结账单 {0} 已提交，但当前状态为 {1}").format(doc.name, doc.status))
		return {
			"name": doc.name,
			"docstatus": int(doc.docstatus),
			"status": doc.status,
			"already_submitted": True,
			"archive_package": doc.archive_package,
		}
	if doc.docstatus != 0:
		frappe.throw(_("只有草稿状态的月末结账单可以确认结账"))

	try:
		doc.submit()
	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=_("月末结账失败：{0}").format(name),
		)
		raise
	return {
		"name": doc.name,
		"docstatus": int(doc.docstatus),
		"status": doc.status,
		"already_submitted": False,
		"archive_package": doc.archive_package,
	}


@frappe.whitelist(methods=["POST"])
def save_and_complete_period_closing_voucher(name):
	"""Complete the configured approval workflow for a Period Closing Voucher.

	The workflow transitions are deliberately applied one by one instead of
	assigning ``workflow_state`` or ``docstatus`` directly. This preserves the
	workflow comments/actions and lets the normal Period Closing Voucher submit
	hooks create the China Accounting Voucher and GL processing job.
	"""
	from frappe.model.workflow import apply_workflow, get_transitions, get_workflow

	if not name:
		frappe.throw(_("请先保存期末结账凭证"))

	doc = frappe.get_doc("Period Closing Voucher", name)
	doc.check_permission("write")
	if doc.docstatus != 0:
		frappe.throw(_("期末结账凭证 {0} 不是草稿，不能重复完成结账").format(doc.name))

	workflow = get_workflow(doc.doctype)
	workflow_state_field = workflow.workflow_state_field
	current_state = doc.get(workflow_state_field)
	if current_state == "Rejected":
		frappe.throw(_("凭证已被退回，请先修改后重新提交审核"))

	if current_state not in PERIOD_CLOSING_WORKFLOW_STATES[:-1]:
		frappe.throw(_("期末结账凭证当前状态为 {0}，不能使用快捷完成结账").format(current_state or _("未设置")))
	target_states = _get_period_closing_next_states(current_state)

	save_point = f"china_period_closing_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(save_point)
	try:
		for target_state in target_states:
			if doc.docstatus == 1:
				break

			transitions = [
				transition
				for transition in get_transitions(doc, workflow, raise_exception=True)
				if transition.next_state == target_state
			]
			if len(transitions) != 1:
				frappe.throw(
					_("当前用户无法将期末结账凭证从 {0} 流转到 {1}，请检查工作流权限").format(
						current_state or _("未设置"), target_state
					),
					title=_("无法完成结账"),
				)

			doc = apply_workflow(doc, transitions[0].action)
			current_state = doc.get(workflow_state_field)

		if doc.docstatus != 1 or current_state != "Posted":
			frappe.throw(
				_("期末结账凭证未完成记账，当前状态为 {0}").format(current_state or _("未设置")),
				title=_("无法完成结账"),
			)
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise

	return {
		"name": doc.name,
		"docstatus": int(doc.docstatus),
		"workflow_state": current_state,
		"gle_processing_status": doc.get("gle_processing_status"),
	}


def _closing_runs_to_reverse(doc, *, for_update=False):
	query = """
		SELECT name, from_date, to_date, period_closing_voucher
		FROM `tabChina Closing Run`
		WHERE company=%s AND to_date >= %s AND status='Closed' AND docstatus=1
		ORDER BY to_date DESC, creation DESC
	"""
	if for_update:
		query += " FOR UPDATE"
	runs = frappe.db.sql(query, (doc.company, doc.to_date), as_dict=True)
	if not any(run.name == doc.name for run in runs):
		frappe.throw(_("未找到需要反结账的当前月末结账单"))
	return runs


def _get_closing_run_for_reverse(name, *, for_update=False):
	if not name or not frappe.db.exists("China Closing Run", name):
		frappe.throw(_("请选择需要反结账的月末结账单"))
	if for_update:
		frappe.db.sql("SELECT name FROM `tabChina Closing Run` WHERE name=%s FOR UPDATE", name)
	doc = frappe.get_doc("China Closing Run", name)
	doc.check_permission("write")
	if doc.docstatus != 1 or doc.status != "Closed":
		frappe.throw(_("只有已结账的月末结账单可以反结账"))
	return doc


def _reverse_closing_runs(doc, runs, reason):
	reopened_on = now_datetime()
	for run in runs:
		frappe.db.set_value(
			"China Closing Run",
			run.name,
			{
				"status": "Reopened",
				"reopen_reason": reason,
				"reopened_by": frappe.session.user,
				"reopened_on": reopened_on,
			},
			update_modified=True,
		)
	frappe.db.set_value("Company", doc.company, "accounts_frozen_till_date", doc.previous_frozen_date)


def _period_closing_vouchers_to_cancel(doc, runs):
	vouchers = []
	seen = set()
	for run in runs:
		name = run.period_closing_voucher
		if not name or name in seen:
			continue
		seen.add(name)
		row = frappe.db.get_value(
			"Period Closing Voucher",
			name,
			["name", "company", "period_start_date", "period_end_date", "docstatus", "gle_processing_status"],
			as_dict=True,
		)
		if not row:
			frappe.throw(_("月末结账单 {0} 关联的损益结转凭证 {1} 不存在").format(run.name, name))
		if row.company != doc.company:
			frappe.throw(_("损益结转凭证 {0} 不属于公司 {1}").format(name, doc.company))
		if getdate(row.period_start_date) != getdate(run.from_date) or getdate(row.period_end_date) != getdate(run.to_date):
			frappe.throw(_("损益结转凭证 {0} 与月末结账单 {1} 的期间不一致").format(name, run.name))
		if row.docstatus == 0:
			frappe.throw(_("损益结转凭证 {0} 仍是草稿，不能执行反结账").format(name))
		if row.docstatus == 1 and row.gle_processing_status == "In Progress":
			frappe.throw(_("损益结转凭证 {0} 的总账仍在处理中，请稍后再反结账").format(name))
		vouchers.append(row)
	return vouchers


@frappe.whitelist()
def preview_reverse_closing(name):
	"""Show the exact periods and closing vouchers affected by one-click reversal."""
	frappe.only_for(("System Manager", "China Finance Manager"))
	doc = _get_closing_run_for_reverse(name)
	runs = _closing_runs_to_reverse(doc)
	vouchers = _period_closing_vouchers_to_cancel(doc, runs)
	return {
		"name": doc.name,
		"company": doc.company,
		"runs": runs,
		"period_closing_vouchers": vouchers,
		"previous_frozen_date": doc.previous_frozen_date,
	}


@frappe.whitelist(methods=["POST"])
def reverse_closing(name, reason):
	"""Reopen affected periods and cancel their P&L closing vouchers in one action."""
	frappe.only_for(("System Manager", "China Finance Manager"))
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("反结账必须填写原因"))
	if len(reason) > 500:
		frappe.throw(_("反结账原因不能超过 500 个字符"))

	doc = _get_closing_run_for_reverse(name)
	frappe.db.sql("SELECT name FROM `tabCompany` WHERE name=%s FOR UPDATE", doc.company)
	doc = _get_closing_run_for_reverse(name, for_update=True)
	runs = _closing_runs_to_reverse(doc, for_update=True)
	vouchers = _period_closing_vouchers_to_cancel(doc, runs)

	# The native cancellation is allowed only after restoring the prior freeze
	# boundary. It still creates the normal reversal GL and audit snapshot.
	_reverse_closing_runs(doc, runs, reason)
	cancelled = []
	already_cancelled = []
	pending = []
	for row in vouchers:
		if row.docstatus == 2:
			already_cancelled.append(row.name)
			continue
		voucher = frappe.get_doc("Period Closing Voucher", row.name, for_update=True)
		voucher.flags.ignore_permissions = True
		voucher.cancel()
		cancelled.append(voucher.name)
		if frappe.db.get_value("Period Closing Voucher", voucher.name, "gle_processing_status") != "Completed":
			pending.append(voucher.name)

	return {
		"name": doc.name,
		"status": "Reopened",
		"reopened_runs": runs,
		"cancelled_period_closing_vouchers": cancelled,
		"already_cancelled_period_closing_vouchers": already_cancelled,
		"pending_period_closing_vouchers": pending,
		"previous_frozen_date": doc.previous_frozen_date,
	}


@frappe.whitelist(methods=["POST"])
def reopen_closing(name, reason):
	"""Compatibility endpoint for integrations that explicitly cancel PCVs themselves."""
	frappe.only_for(("System Manager", "China Finance Manager"))
	reason = str(reason or "").strip()
	if not reason:
		frappe.throw(_("反结账必须填写原因"))
	doc = _get_closing_run_for_reverse(name)
	frappe.db.sql("SELECT name FROM `tabCompany` WHERE name=%s FOR UPDATE", doc.company)
	doc = _get_closing_run_for_reverse(name, for_update=True)
	runs = _closing_runs_to_reverse(doc, for_update=True)
	_period_closing_vouchers_to_cancel(doc, runs)
	_reverse_closing_runs(doc, runs, reason)
	return {
		"name": doc.name,
		"status": "Reopened",
		"reopened_runs": runs,
		"period_closing_vouchers": [run.period_closing_voucher for run in runs if run.period_closing_voucher],
		"previous_frozen_date": doc.previous_frozen_date,
	}
