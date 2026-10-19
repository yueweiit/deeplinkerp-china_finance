"""Controlled repair helpers for reviewed China Finance data.

These functions deliberately use Frappe documents for accounting changes. They
are idempotent for master-data and metadata repairs, while submitted Journal
Entries are corrected through cancellation and amendment so the original
posting remains auditable.
"""

from frappe import _


import frappe
from frappe.utils import flt, getdate, now_datetime

from china_finance.services.bank_reconciliation import clean_bank_summary
from china_finance.services.voucher import process_cancellation_snapshot


COMPANY = "悦为智能技术(东莞)有限公司"
LEGACY_BANK_ACCOUNT = "100201 - 基本存款账户 - T"
CUSTOMER = "东莞市奥领数控机器有限公司"
INVESTMENT_SOURCE_ROOTS = {
	"ACC-JV-2026-00143", "ACC-JV-2026-00153", "ACC-JV-2026-00155",
	"ACC-JV-2026-00157", "ACC-JV-2026-00199", "ACC-JV-2026-00216",
}
BANK_RECLASSIFICATION_TRANSACTIONS = {
	"ACC-BTN-2026-00175",  # 退回招聘备用金
	"ACC-BTN-2026-00176",  # 公众号注册退款
	"ACC-BTN-2026-00190",  # 截图标注为个税
	"ACC-BTN-2026-00193",  # 批量代发付费
	"ACC-BTN-2026-00195",  # 公众号注册退款
	"ACC-BTN-2026-00200",  # 公众号注册退款
	"ACC-BTN-2026-00209",  # 截图标注为 7 月社保
	"ACC-BTN-2026-00217",  # 退回招聘备用金
}
BANK_SUMMARY_REPAIRS = {
	"ACC-BTN-2026-00190": "个税",
	"ACC-BTN-2026-00209": "7月社保",
}

DETAIL_ACCOUNTS = (
	("660201", "管理费用－办公费", "6602", "Expense Account"),
	("660202", "管理费用－房租", "6602", "Expense Account"),
	("660203", "管理费用－物业管理费", "6602", "Expense Account"),
	("660204", "管理费用－水电费", "6602", "Expense Account"),
	("660207", "管理费用－差旅费", "6602", "Expense Account"),
	("660208", "管理费用－社会保险费", "6602", "Expense Account"),
	("660209", "管理费用－工资", "6602", "Expense Account"),
	("660210", "管理费用－保险金", "6602", "Expense Account"),
	("660211", "管理费用－福利费", "6602", "Expense Account"),
	("660212", "管理费用－油费", "6602", "Expense Account"),
	("660213", "管理费用－住房公积金", "6602", "Expense Account"),
	("660229", "管理费用－公积金", "6602", "Expense Account"),
)

OTHER_PAYABLE_ACCOUNTS = (
	("224100", "其他应付款（明细）", None, None),
	# These are aggregated payroll-liability details, not supplier subledgers.
	# Leaving account_type empty prevents Journal Entry from requiring a party
	# for every monthly social-security/provident-fund accrual.
	("224101", "其他应付款-社保", "224100", None),
	("224102", "其他应付款-公积金", "224100", None),
)

NON_PARTY_LIABILITY_ACCOUNT_NUMBERS = ("224101", "224102")


def repair_aaa_finance(company=COMPANY, apply=False, limit=None):
	"""Preview or apply the reviewed aaa finance repair.

	Call with ``apply=True`` only for the explicitly approved company. The
	preview path performs no writes and returns the exact Journal Entries that
	would be amended.
"""
	if company != COMPANY:
		frappe.throw(_("此维护脚本仅允许处理 aaa 的悦为智能技术(东莞)有限公司"))

	plan = build_repair_plan(company)
	if not apply:
		return plan

	account_map = ensure_detail_accounts(company)
	non_party_liability_accounts = normalize_non_party_liability_accounts(company, apply=True)
	fix_bank_account(company, account_map)
	clone_statement_mappings(company, account_map)
	customer_name = ensure_customer(company)
	shareholder_name = ensure_shareholder(company)
	bank_summaries = repair_bank_summaries(company)
	metadata = repair_source_metadata(company)

	amended = []
	names_to_amend = plan["journal_entries_to_amend"][:limit] if limit else plan["journal_entries_to_amend"]
	for source_name in names_to_amend:
		result = amend_journal_entry(company, source_name, account_map, customer_name, shareholder_name)
		if result:
			amended.append(result)
	if not limit:
		for source_name in plan["cancelled_orphans"]:
			result = amend_journal_entry(
				company, source_name, account_map, customer_name, shareholder_name, allow_cancelled=True
			)
			if result:
				amended.append(result)

	resolved = resolve_pending_cancellations(company)
	frappe.db.commit()
	return {
		"company": company,
		"created_accounts": account_map,
		"non_party_liability_accounts": non_party_liability_accounts,
		"customer": customer_name,
		"shareholder": shareholder_name,
		"bank_summaries": bank_summaries,
		"metadata": metadata,
		"amended": amended,
		"resolved_cancellations": resolved,
		"manual_review": plan["manual_review"],
	}


def build_repair_plan(company):
	wrong_bank_names = frappe.get_all(
		"Journal Entry Account",
		filters={"account": LEGACY_BANK_ACCOUNT, "parenttype": "Journal Entry"},
		pluck="parent",
	)
	wrong_bank_names = set(
		frappe.get_all(
			"Journal Entry",
			filters={"name": ["in", list(wrong_bank_names)], "company": company, "docstatus": 1},
			pluck="name",
		)
	)
	special_names = {
		"ACC-JV-2026-00139",
		"ACC-JV-2026-00139-1",
		"ACC-JV-2026-00142",
		"ACC-JV-2026-00145",
		"ACC-JV-2026-00145-1",
		"ACC-JV-2026-00148",
		"ACC-JV-2026-00148-1",
		"ACC-JV-2026-00151",
		"ACC-JV-2026-00154",
		"ACC-JV-2026-00164",
		"ACC-JV-2026-00165",
		"ACC-JV-2026-00168",
		"ACC-JV-2026-00169",
		"ACC-JV-2026-00185-1",
		"ACC-JV-2026-00186-1",
		"ACC-JV-2026-00187-1",
		"ACC-JV-2026-00143",
		"ACC-JV-2026-00153",
		"ACC-JV-2026-00155",
		"ACC-JV-2026-00157",
		"ACC-JV-2026-00199-1",
		"ACC-JV-2026-00216-1",
	}
	bank_linked_names = set(
		frappe.get_all(
			"Bank Transaction",
			filters={"name": ["in", list(BANK_RECLASSIFICATION_TRANSACTIONS)]},
			pluck="custom_china_journal_entry",
		)
	)
	bank_linked_names.discard(None)
	bank_linked_names.discard("")
	bank_linked_names = set(
		frappe.get_all(
			"Journal Entry",
			filters={"name": ["in", list(bank_linked_names)], "company": company, "docstatus": 1},
			pluck="name",
		)
	)
	to_amend = sorted(wrong_bank_names | bank_linked_names | {
		name for name in special_names
		if frappe.db.get_value("Journal Entry", name, ["company", "docstatus"]) == (company, 1)
	})
	cancelled_orphans = [
		name for name in (*special_names, "ACC-JV-2026-00182")
		if (
			frappe.db.get_value("Journal Entry", name, ["company", "docstatus", "amended_from"])
			== (company, 2, None)
			and not frappe.db.exists("Journal Entry", {"amended_from": name})
		)
	]

	return {
		"company": company,
		"wrong_bank_journal_entry_count": len(wrong_bank_names),
		"journal_entries_to_amend": to_amend,
		"cancelled_orphans": cancelled_orphans,
		"manual_review": [
			{
				"source_name": "ACC-JV-2026-00154",
				"reason": "已按明细摘要拆分租金、物业和电费",
			},
			{
				"source_name": "ACC-JV-2026-00165",
				"reason": "社保/公积金承担方及应付款政策仍需财务复核",
			},
			{
				"source_name": "ACC-JV-2026-00196-1",
				"reason": "7月10日收到30000元银行转账但流水无对方信息，暂保留原科目，需确认是投资款、往来款还是内部调拨",
			},
			{
				"source_name": "ACC-JV-2026-00138",
				"reason": "初始实收资本10000元凭证没有投资人摘要，需确认股东名称后补充 Shareholder 辅助核算",
			},
		],
	}


def _account(company, number):
	return frappe.db.get_value(
		"Account", {"company": company, "account_number": str(number)}, "name"
	)


def split_management_fee_accounts(company=COMPANY, apply=False):
	"""Validate the source chart's separate property/utilities accounts."""
	if company != COMPANY:
		frappe.throw(_("此维护脚本仅允许处理 aaa 的悦为智能技术(东莞)有限公司"))

	property_account = _account(company, "660203")
	if not property_account:
		frappe.throw(_("aaa 缺少 660203 管理费用－物业管理费科目"))

	utilities_before = _account(company, "660204")
	result = {
		"company": company,
		"property_account_before": property_account,
		"property_account_after": property_account,
		"utilities_account": utilities_before,
		"renamed": False,
		"created": False,
		"statement_mappings_cloned": False,
	}
	if not apply:
		return result

	from erpnext.accounts.doctype.account.account import update_account_number

	if frappe.db.get_value("Account", property_account, "account_name") != "管理费用－物业管理费":
		new_name = update_account_number(property_account, "管理费用－物业管理费", "660203")
		result["property_account_after"] = new_name or _account(company, "660203")
		result["renamed"] = True

	account_map = ensure_detail_accounts(company)
	result["utilities_account"] = account_map["660204"]
	result["created"] = not utilities_before
	clone_statement_mappings(company, account_map)
	result["statement_mappings_cloned"] = True
	frappe.db.commit()
	return result


def ensure_detail_accounts(company):
	accounts = {}
	parent_6602 = _account(company, "6602")
	if not parent_6602:
		frappe.throw(_("aaa 缺少 6602 管理费用父科目"))
	legacy_other_payable = _account(company, "2241")
	parent_current_liability = frappe.db.get_value("Account", legacy_other_payable, "parent_account")
	if not parent_current_liability:
		frappe.throw(_("aaa 缺少流动负债父科目"))

	for number, label, parent_number, account_type in DETAIL_ACCOUNTS + OTHER_PAYABLE_ACCOUNTS:
		name = _account(company, number)
		if not name:
			parent = parent_6602 if parent_number == "6602" else None
			if parent_number == "224100":
				parent = _account(company, "224100")
			if parent is None:
				parent = parent_current_liability
			doc = frappe.get_doc({
				"doctype": "Account",
				"account_number": number,
				"account_name": label,
				"company": company,
				"parent_account": parent,
				"root_type": "Expense" if number.startswith("660") else "Liability",
				"account_type": account_type,
				"is_group": 1 if number == "224100" else 0,
			})
			doc.insert(ignore_permissions=True)
			name = doc.name
		accounts[number] = name
	return accounts


def normalize_non_party_liability_accounts(company=COMPANY, apply=False):
	"""Keep aggregated payroll-liability details outside the party subledger."""
	if company != COMPANY:
		frappe.throw(_("此维护脚本仅允许处理 aaa 的悦为智能技术(东莞)有限公司"))

	rows = frappe.get_all(
		"Account",
		filters={
			"company": company,
			"account_number": ["in", list(NON_PARTY_LIABILITY_ACCOUNT_NUMBERS)],
		},
		fields=["name", "account_number", "account_name", "account_type", "root_type", "is_group"],
		order_by="account_number",
	)
	result = {
		"company": company,
		"accounts": rows,
		"changed": [],
		"missing": [
			number for number in NON_PARTY_LIABILITY_ACCOUNT_NUMBERS
			if number not in {row.account_number for row in rows}
		],
	}
	if not apply:
		return result

	for row in rows:
		if row.account_type:
			frappe.db.set_value("Account", row.name, "account_type", "", update_modified=False)
			result["changed"].append({"name": row.name, "account_number": row.account_number})
	frappe.db.commit()
	return result


def fix_bank_account(company, account_map):
	main_bank = account_map.get("100201") or _account(company, "100201")
	if not main_bank:
		frappe.throw(_("aaa 缺少悦为公司的 100201 银行科目"))
	rows = frappe.get_all(
		"Bank Account",
		filters={"company": company, "bank": "招商银行"},
		fields=["name", "account"],
	)
	if len(rows) != 1:
		frappe.throw(_("aaa 招商银行公司账户数量异常：{0}，未自动修改").format(len(rows)))
	row = rows[0]
	if row.account != main_bank:
		doc = frappe.get_doc("Bank Account", row.name)
		doc.account = main_bank
		doc.save(ignore_permissions=True)
	return {"bank_account": row.name, "account": main_bank}


def ensure_customer(company):
	name = frappe.db.get_value("Customer", {"customer_name": CUSTOMER}, "name")
	if name:
		return name
	doc = frappe.get_doc({
		"doctype": "Customer",
		"customer_name": CUSTOMER,
		"customer_type": "Company",
		"customer_group": "Commercial",
		"territory": "China",
	})
	doc.insert(ignore_permissions=True)
	return doc.name


def ensure_shareholder(company):
	name = frappe.db.get_value("Shareholder", {"title": "周悦", "company": company}, "name")
	if name:
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Shareholder",
			# This site's Shareholder DocType uses prompt naming even though the
			# naming-series field is available, so provide a deterministic name.
			"name": f"周悦-{company}",
			"naming_series": "ACC-SH-.YYYY.-",
			"title": "周悦",
			"company": company,
			"is_company": 0,
		}
	).insert(ignore_permissions=True)
	return doc.name


def clone_statement_mappings(company, account_map):
	clones = {
		**{number: name for number, name in account_map.items() if number != "100201"},
	}
	for source_number, target_numbers in (("660299", ("660201", "660202", "660203", "660204", "660207", "660208", "660209", "660210", "660211", "660212", "660213", "660229")), ("2241", ("224101", "224102"))):
		source = _account(company, source_number)
		if not source:
			continue
		rows = frappe.get_all(
			"China Financial Statement Mapping",
			filters={"company": company, "account": source},
			fields=[
				"template", "row_code", "supplementary_row_code", "cash_inflow_row_code",
				"cash_outflow_row_code", "sign_multiplier", "effective_from", "effective_to",
				"mapping_rule_version",
			],
		)
		for target_number in target_numbers:
			target = clones.get(target_number) or _account(company, target_number)
			if not target:
				continue
			for row in rows:
				if frappe.db.exists(
					"China Financial Statement Mapping",
					{"company": company, "template": row.template, "account": target, "effective_from": row.effective_from},
				):
					continue
				mapping = frappe.get_doc({
					"doctype": "China Financial Statement Mapping",
					"company": company,
					"template": row.template,
					"row_code": row.row_code,
					"supplementary_row_code": row.supplementary_row_code,
					"account": target,
					"cash_inflow_row_code": row.cash_inflow_row_code,
					"cash_outflow_row_code": row.cash_outflow_row_code,
					"sign_multiplier": row.sign_multiplier,
					"effective_from": row.effective_from,
					"effective_to": row.effective_to,
					"mapping_basis": "Manual",
					"mapping_rule_version": row.mapping_rule_version,
					"mapping_source": "Manual",
					"reviewed": 0,
				})
				mapping.insert(ignore_permissions=True)


def repair_source_metadata(company):
	updated_summaries = 0
	linked_bank_transactions = 0
	for je in frappe.get_all(
		"Journal Entry", filters={"company": company, "docstatus": 1},
		fields=["name", "cheque_no", "remark", "user_remark"],
	):
		summary = _get_je_summary(company, je)
		if summary and not (je.user_remark or "").strip():
			frappe.db.set_value("Journal Entry", je.name, "user_remark", summary, update_modified=False)
			updated_summaries += 1
		for line in frappe.get_all(
			"Journal Entry Account", filters={"parent": je.name},
			fields=["name", "user_remark"], order_by="idx asc",
		):
			if summary and not (line.user_remark or "").strip():
				frappe.db.set_value("Journal Entry Account", line.name, "user_remark", summary, update_modified=False)

		bank_transaction = _get_bank_transaction(company, je.cheque_no)
		if bank_transaction and frappe.db.has_column("Journal Entry", "custom_china_bank_transaction"):
			if not frappe.db.get_value("Journal Entry", je.name, "custom_china_bank_transaction"):
				frappe.db.set_value("Journal Entry", je.name, "custom_china_bank_transaction", bank_transaction.name, update_modified=False)
				linked_bank_transactions += 1
			if frappe.db.has_column("Bank Transaction", "custom_china_journal_entry") and not bank_transaction.custom_china_journal_entry:
				frappe.db.set_value("Bank Transaction", bank_transaction.name, "custom_china_journal_entry", je.name, update_modified=False)
	return {"updated_summaries": updated_summaries, "linked_bank_transactions": linked_bank_transactions}


def repair_bank_summaries(company):
	"""Persist the two business summaries evidenced by the supplied July ledger."""
	if not frappe.db.has_column("Bank Transaction", "custom_summary"):
		return 0
	updated = 0
	for name, summary in BANK_SUMMARY_REPAIRS.items():
		if not frappe.db.exists("Bank Transaction", {"name": name, "company": company}):
			continue
		if not (frappe.db.get_value("Bank Transaction", name, "custom_summary") or "").strip():
			frappe.db.set_value("Bank Transaction", name, "custom_summary", summary, update_modified=False)
			updated += 1
	return updated


def _get_bank_transaction(company, reference_number):
	if not reference_number:
		return None
	name = frappe.db.get_value(
		"Bank Transaction", {"company": company, "reference_number": reference_number}, "name"
	)
	return frappe.get_doc("Bank Transaction", name) if name else None


def _get_je_summary(company, je):
	for value in (je.user_remark,):
		cleaned = clean_bank_summary(value)
		if cleaned:
			return cleaned
	line = frappe.db.sql(
		"""
		SELECT user_remark FROM `tabJournal Entry Account`
		WHERE parent=%s AND parenttype='Journal Entry'
			AND TRIM(COALESCE(user_remark, '')) <> ''
		ORDER BY idx LIMIT 1
		""",
		(je.name,), as_dict=True,
	)
	if line:
		cleaned = clean_bank_summary(line[0].user_remark)
		if cleaned:
			return cleaned
	bank_transaction = _get_bank_transaction(company, je.cheque_no)
	if bank_transaction:
		cleaned = clean_bank_summary(bank_transaction.custom_summary or bank_transaction.description)
		if cleaned:
			return cleaned
	return clean_bank_summary(je.remark)


def _row_account_number(row):
	return frappe.db.get_value("Account", row.account, "account_number")


def _has_mixed_facility_terms(description):
	text = description or ""
	categories = (
		any(term in text for term in ("租金", "房租")),
		"物业" in text,
		any(term in text for term in ("水电", "电费")),
	)
	return sum(categories) >= 2


def _target_account(company, source_name, row, summary, bank_transaction, account_map, source_root=None):
	number = _row_account_number(row)
	source_root = source_root or source_name
	bank_account = _account(company, "100201")
	if row.account == LEGACY_BANK_ACCOUNT or number == "100201":
		return bank_account
	if bank_transaction and _is_bank_transaction_side(row, bank_transaction):
		return bank_account

	line_summary = (row.user_remark or summary or "").strip()
	if bank_transaction:
		bank_summary = clean_bank_summary(bank_transaction.custom_summary or bank_transaction.description)
		bank_name = bank_transaction.name
		# These are supported by the bank summary and the supplied July ledger
		# annotations. They are deliberately explicit so a generic tax reference
		# cannot be mistaken for individual income tax or social insurance.
		explicit_numbers = {
			"ACC-BTN-2026-00175": "660299",
			"ACC-BTN-2026-00176": "122101",
			"ACC-BTN-2026-00190": "222112",
			"ACC-BTN-2026-00193": "660303",
			"ACC-BTN-2026-00195": "122101",
			"ACC-BTN-2026-00200": "122101",
			"ACC-BTN-2026-00209": "221103",
			"ACC-BTN-2026-00217": "660299",
		}
		if bank_name in explicit_numbers:
			return _account(company, explicit_numbers[bank_name])
		if "批量代发" in bank_summary and any(term in bank_summary for term in ("付费", "费用", "手续费", "服务费")):
			return _account(company, "660303")
		if "招聘" in bank_summary and "备用金" in bank_summary:
			return _account(company, "660299")
		if "公众号注册退款" in bank_summary:
			return _account(company, "122101")
	if source_root == "ACC-JV-2026-00164" and number == "660303":
		return _account(company, "660302")
	if source_root == "ACC-JV-2026-00145":
		if source_name == source_root and number == "221101":
			return _account(company, "122101")
		if source_name != source_root and number == "1221":
			return _account(company, "221101")
	if source_root == "ACC-JV-2026-00154" and number == "660299":
		if "房租" in line_summary or "租金" in line_summary:
			return account_map["660202"]
		if "水电" in line_summary or "电费" in line_summary:
			return account_map["660204"]
		if "物业" in line_summary:
			return account_map["660203"]
	if number == "660299" and not _has_mixed_facility_terms(line_summary):
		if "房租" in line_summary or "租金" in line_summary:
			return account_map["660202"]
		if "水电" in line_summary or "电费" in line_summary:
			return account_map["660204"]
		if "物业" in line_summary:
			return account_map["660203"]
	if source_root in {"ACC-JV-2026-00139", "ACC-JV-2026-00165"}:
		if number == "660201" and "社保" in line_summary:
			return account_map["660208"]
	if source_root in {"ACC-JV-2026-00148", "ACC-JV-2026-00165"}:
		if number == "660201" and "公积金" in line_summary:
			return account_map["660229"]
		if number == "2241":
			if "公积金" in line_summary:
				return account_map["224102"]
			if "社保" in line_summary:
				return account_map["224101"]
	if number == "2241":
		if "社保" in line_summary or source_root in {"ACC-JV-2026-00139"}:
			return account_map["224101"]
		if "公积金" in line_summary or source_root in {"ACC-JV-2026-00148"}:
			return account_map["224102"]
		if source_root in {"ACC-JV-2026-00151", "ACC-JV-2026-00169"}:
			amount = flt(row.debit_in_account_currency or row.credit_in_account_currency)
			if amount in {830.86, 1246.29}:
				return account_map["224101"]
			if amount in {208.0, 312.0}:
				return account_map["224102"]

	if bank_transaction:
		description = bank_transaction.description or ""
		if number == "660299" and "报销" in description:
			return _account(company, "660201")
		if number == "660299" and any(word in description for word in ("招聘", "退款", "退回", "验证", "实名")):
			return _account(company, "122101")
		if number == "660299" and "社保" in description:
			return _account(company, "221103")
		if number == "660299" and any(word in description for word in ("公积金", "补缴")):
			return _account(company, "221104")

	return None


def _is_bank_transaction_side(row, bank_transaction):
	"""Identify the bank side even if an earlier repair gave both rows one account."""
	amount = max(flt(bank_transaction.deposit), flt(bank_transaction.withdrawal))
	if amount <= 0:
		return False
	if flt(bank_transaction.deposit) > 0:
		return abs(flt(row.debit) - amount) <= 0.005 and flt(row.credit) <= 0.005
	return abs(flt(row.credit) - amount) <= 0.005 and flt(row.debit) <= 0.005


def amend_journal_entry(
	company, source_name, account_map, customer_name, shareholder_name=None, allow_cancelled=False
):
	doc = frappe.get_doc("Journal Entry", source_name)
	if doc.company != company or doc.docstatus not in (1, 2) or (doc.docstatus == 2 and not allow_cancelled):
		return None
	bank_transaction = None
	if frappe.db.has_column("Journal Entry", "custom_china_bank_transaction"):
		bank_transaction_name = frappe.db.get_value("Journal Entry", source_name, "custom_china_bank_transaction")
		if bank_transaction_name and frappe.db.get_value(
			"Bank Transaction", {"name": bank_transaction_name, "company": company}, "name"
		):
			bank_transaction = frappe.get_doc("Bank Transaction", bank_transaction_name)
	if not bank_transaction:
		bank_transaction = _get_bank_transaction(company, doc.cheque_no)
	summary = _get_je_summary(company, doc)
	changes = []
	party_changes = []
	source_root = doc.amended_from or doc.name
	for row in doc.accounts:
		target = _target_account(
			company, doc.name, row, summary, bank_transaction, account_map, source_root=source_root
		)
		if target and target != row.account:
			changes.append((row.idx, row.account, target))
		if source_root == "ACC-JV-2026-00168" and _row_account_number(row) == "2203":
			if row.party_type != "Customer" or row.party != customer_name:
				party_changes.append((row.idx, "Customer", customer_name))
		if (
			shareholder_name
			and source_root in INVESTMENT_SOURCE_ROOTS
			and _row_account_number(row) == "4001"
			and (row.party_type != "Shareholder" or row.party != shareholder_name)
		):
			party_changes.append((row.idx, "Shareholder", shareholder_name))
	if not changes and not party_changes:
		return None

	# ERPNext cannot cancel a historically malformed Journal Entry whose GL
	# account belongs to another company. Correct only that source row first;
	# leave expense reclassifications for the amended document so cancellation
	# still reverses the original expense account exactly.
	bank_account = _account(company, "100201")
	legacy_bank_rows = [row for row in doc.accounts if row.account == LEGACY_BANK_ACCOUNT]
	if legacy_bank_rows and doc.docstatus == 1:
		for row in legacy_bank_rows:
			row.account = bank_account
		doc.flags.ignore_validate_update_after_submit = True
		doc.flags.ignore_reposting_on_reconciliation = True
		doc.save(ignore_permissions=True)
		doc.add_comment(
		"Comment",
		"历史银行流水科目跨公司，已在冲销前更正来源行至本公司银行科目；原始中国会计凭证快照保留原记录。",
	)
	if (
		bank_transaction
		and frappe.db.has_column("Bank Transaction", "custom_china_journal_entry")
		and bank_transaction.custom_china_journal_entry == source_name
	):
		frappe.db.set_value(
			"Bank Transaction", bank_transaction.name, "custom_china_journal_entry", None,
			update_modified=False,
		)

	if doc.docstatus == 1:
		doc.cancel()
		frappe.db.commit()
		issue_name = frappe.db.get_value(
			"China Voucher Sync Issue",
			{"issue_key": f"Cancellation|Journal Entry|{source_name}"},
			"name",
		)
		if issue_name:
			process_cancellation_snapshot("Journal Entry", source_name, issue_name)
			frappe.db.commit()

	old_doc = frappe.get_doc("Journal Entry", source_name)
	amended = frappe.copy_doc(old_doc)
	amended.amended_from = source_name
	amended.flags.ignore_permissions = True
	if amended.meta.has_field("custom_china_voucher_number"):
		amended.custom_china_voucher_number = None
	if any(target == _account(company, "122101") for _idx, _old, target in changes):
		amended.party_not_required = 1
	if party_changes:
		amended.party_not_required = 0
	if summary:
		amended.remark = summary
		if amended.meta.has_field("user_remark"):
			amended.user_remark = summary
	for row in amended.accounts:
		for idx, _old_account, target in changes:
			if row.idx == idx:
				row.account = target
		for idx, party_type, party in party_changes:
			if row.idx == idx:
				row.party_type = party_type
				row.party = party
			if summary and not (row.user_remark or "").strip():
				row.user_remark = summary
	if bank_transaction and amended.meta.has_field("custom_china_bank_transaction"):
		amended.custom_china_bank_transaction = bank_transaction.name
	# ERPNext's standard Party Type matrix treats Customer as Receivable only,
	# while China's 2203 advance-receipt liability is Payable. Preserve the
	# requested customer auxiliary on this historical voucher with a narrowly
	# scoped validation bypass; all other amended vouchers use normal validation.
	if source_root == "ACC-JV-2026-00168":
		amended.flags.ignore_validate = True
	amended.insert(ignore_permissions=True)
	amended.submit()
	frappe.db.commit()
	return {
		"old": source_name, "new": amended.name, "changes": changes,
		"party_changes": party_changes, "summary": summary,
	}


def resolve_pending_cancellations(company):
	resolved = []
	for issue in frappe.get_all(
		"China Voucher Sync Issue",
		filters={"company": company, "status": "Pending"},
		fields=["name", "source_doctype", "source_name"],
	):
		# A China Accounting Voucher is already the audit snapshot. Its own
		# cancellation must not recursively create another snapshot; this is a
		# legacy pending row left by the former cancellation hook.
		if issue.source_doctype == "China Accounting Voucher":
			docstatus = frappe.db.get_value("China Accounting Voucher", issue.source_name, "docstatus")
			if docstatus == 2:
				frappe.db.set_value(
					"China Voucher Sync Issue",
					issue.name,
					{
						"status": "Resolved",
						"resolved_on": now_datetime(),
						"last_error": "来源已是已取消的中国会计凭证，无需再次生成冲销快照",
					},
					update_modified=False,
				)
				resolved.append({"issue": issue.name, "voucher": issue.source_name})
				continue
		result = process_cancellation_snapshot(issue.source_doctype, issue.source_name, issue.name)
		if result.get("status") == "resolved":
			resolved.append({"issue": issue.name, "voucher": result.get("voucher")})
	return resolved
