from unittest.mock import patch

import frappe

from china_finance.china_finance.report.china_voucher_ledger import china_voucher_ledger as ledger
from china_finance.services.voucher import deduplicate_accounting_remarks, to_voucher_entry


def test_prepared_report_columns_keep_source_labels_and_edit_contract():
	with patch.object(ledger, "_", side_effect=AssertionError("labels must not use the worker locale")):
		columns = ledger.get_columns()
	by_name = {column["fieldname"]: column for column in columns}
	assert [column["label"] for column in columns if not column.get("hidden")] == [
		"凭证字号", "状态", "凭证日期", "摘要", "科目", "借方", "贷方", "本位币金额", "操作"
	]
	assert by_name["account"]["options"] == "Account"
	assert by_name["party"]["options"] == "party_type"
	assert by_name["party"]["hidden"] == 1
	for field in ("account", "remarks", "debit", "credit"):
		assert by_name[field]["editable"] == 1
	for field in ("debit", "credit", "base_total_amount"):
		assert by_name[field]["fieldtype"] == "Currency"
		assert by_name[field]["options"] == "currency"


def test_voucher_summary_collapses_historical_duplicate_lines_only():
	assert ledger.clean_voucher_summary("支付宝结算\n支付宝结算") == "支付宝结算"
	assert ledger.clean_voucher_summary("分录摘要\n凭证摘要") == "分录摘要\n凭证摘要"
	assert ledger.clean_voucher_summary("人工摘要\n人工摘要") == "人工摘要"
	assert ledger.clean_voucher_summary("重复摘要\n\n重复摘要") == "重复摘要\n\n重复摘要"
	assert deduplicate_accounting_remarks("甲\n\n乙") == "甲\n\n乙"
	assert deduplicate_accounting_remarks("  分录摘要  \n凭证摘要  ") == "  分录摘要  \n凭证摘要  "


def test_voucher_summary_prefers_exactly_matched_source_row_summary():
	assert ledger.clean_voucher_summary(
		"报销技术开发费用款-张海立\n报销款-张海立",
		"报销技术开发费用款-张海立",
		"报销款-张海立",
	) == "报销技术开发费用款-张海立"
	assert ledger.clean_voucher_summary(
		"报销技术开发费用款-张海立\n人工修改的凭证摘要",
		"报销技术开发费用款-张海立",
		"报销款-张海立",
	) == "报销技术开发费用款-张海立\n人工修改的凭证摘要"
	assert ledger.clean_voucher_summary(
		"报销技术开发费用款-张海立\n报销款-张海立",
		None,
		"报销款-张海立",
	) == "报销技术开发费用款-张海立\n报销款-张海立"
	assert ledger.clean_voucher_summary(
		"支付手续费\n招商银行2026年账户管理费",
		"支付手续费",
		"招商银行2026年账户管理费",
	) == "支付手续费"
	assert ledger.clean_voucher_summary(
		"支付报销款-张海立\n报销款-张海立",
		"支付报销款-张海立",
		"报销款-张海立",
	) == "支付报销款-张海立"


def test_voucher_summary_matches_native_join_with_source_whitespace():
	row_summary = "报销技术开发费用款-测试员工"
	voucher_summary = "报销款-测试员工"
	for raw_row in (row_summary + "\n", row_summary + "\n\n", row_summary + "\r\n", row_summary + " \n"):
		for raw_voucher in (voucher_summary, "\n" + voucher_summary, voucher_summary + "\n"):
			stored = f"{raw_row}\n{raw_voucher}"
			assert ledger.clean_voucher_summary(stored, raw_row, raw_voucher) == row_summary


def test_voucher_summary_preserves_unverified_extra_blank_lines():
	row_summary = "报销技术开发费用款-测试员工"
	voucher_summary = "报销款-测试员工"
	stored = f"{row_summary}\n\n{voucher_summary}"
	assert ledger.clean_voucher_summary(stored, row_summary, voucher_summary) == stored
	assert ledger.clean_voucher_summary(stored, None, voucher_summary) == stored
	changed = f"{row_summary}\n\n人工补充说明"
	assert ledger.clean_voucher_summary(changed, row_summary + "\n", voucher_summary) == changed


def test_voucher_summary_separates_verified_parent_context_and_keeps_source_internal_lines():
	row_summary = "支付手续费\n"
	voucher_summary = "招商银行2026年账户管理费"
	stored = f"{row_summary}\n{voucher_summary}"
	assert ledger.clean_voucher_summary(stored, row_summary, voucher_summary) == row_summary.strip()
	row_summary = "项目报销说明\n\n报销款-测试员工\n"
	voucher_summary = "报销款-测试员工"
	assert ledger.clean_voucher_summary(
		f"{row_summary}\n{voucher_summary}", row_summary, voucher_summary,
	) == row_summary.strip()


def test_voucher_tree_cleans_duplicate_summary_without_bank_marker():
	entry = frappe._dict(
		voucher_snapshot="CAV-TEST",
		posting_date="2026-07-27",
		statutory_number="记2",
		accounting_period="2026-07",
		remarks="悦为智能投资款\n悦为智能投资款",
		source_doctype="Journal Entry",
		source_name="ACC-JV-TEST",
		source_event="Posting",
		voucher_status=1,
		entry_idx=1,
	)

	row = ledger.build_tree_data([entry])[0]

	assert "bank_transaction" not in entry
	assert row["remarks"] == "悦为智能投资款"


def test_voucher_tree_uses_source_confirmed_line_summary_for_display_and_export():
	entry = frappe._dict(
		voucher_snapshot="CAV-SOURCE-SUMMARY",
		posting_date="2026-07-24",
		statutory_number="记40",
		accounting_period="2026-07",
		remarks="报销技术开发费用款-张海立\n报销款-张海立",
		source_row_summary="报销技术开发费用款-张海立",
		source_voucher_summary="报销款-张海立",
		source_doctype="Journal Entry",
		source_name="ACC-JV-SOURCE-SUMMARY",
		source_event="Posting",
		voucher_status=1,
		entry_idx=1,
	)

	row = ledger.build_tree_data([entry])[0]

	assert row["remarks"] == "报销技术开发费用款-张海立"
	assert "source_voucher_summary" not in row
	assert "source_row_summary" not in row
	assert entry.source_row_summary == "报销技术开发费用款-张海立"


def test_summary_uses_verified_row_even_when_voucher_context_differs():
	for row_summary, voucher_summary in (
		("支付手续费", "招商银行2026年账户管理费"),
		("支付电费1000元", "支付电费100元"),
		("报销技术开发费用款-张海立", "报销款-李四"),
		("报销技术开发费用款-张海立", "报销款-张海立，2026年6月"),
		("支付员工4月工资", "计提员工4月工资"),
		("招聘预算消耗", "支付测试员工招聘预算"),
		("支付测试员工报销it技术开发费用", "支付测试员工报销"),
		("计提6月公积金", "计提6月社保费用"),
	):
		combined = f"{row_summary}\n{voucher_summary}"
		assert ledger.clean_voucher_summary(combined, row_summary, voucher_summary) == row_summary


def test_summary_keeps_unverified_notes_and_missing_or_ambiguous_source_context():
	row_summary = "支付员工4月工资"
	voucher_summary = "计提员工4月工资"
	combined = f"{row_summary}\n{voucher_summary}"
	assert ledger.clean_voucher_summary(combined, None, voucher_summary) == combined
	assert ledger.clean_voucher_summary(combined, row_summary, None) == combined
	assert ledger.clean_voucher_summary(combined, "其他来源摘要", voucher_summary) == combined
	assert ledger.clean_voucher_summary(combined, row_summary, "其他凭证摘要") == combined
	extra = combined + "\n人工补充说明"
	assert ledger.clean_voucher_summary(extra, row_summary, voucher_summary) == extra
	assert ledger.clean_voucher_summary("\n" + voucher_summary, "", voucher_summary) == voucher_summary


def test_voucher_tree_cleans_native_double_newline_without_changing_input():
	entry = frappe._dict(
		voucher_snapshot="CAV-SOURCE-TRAILING-NEWLINE",
		posting_date="2026-07-17",
		statutory_number="记34",
		accounting_period="2026-07",
		remarks="报销技术开发费用款-测试员工\n\n报销款-测试员工",
		source_row_summary="报销技术开发费用款-测试员工\n",
		source_voucher_summary="报销款-测试员工",
		source_doctype="Journal Entry",
		source_name="ACC-JV-SOURCE-TRAILING-NEWLINE",
		source_event="Posting",
		voucher_status=1,
		entry_idx=1,
	)
	before = dict(entry)

	row = ledger.build_tree_data([entry])[0]

	assert row["remarks"] == "报销技术开发费用款-测试员工"
	assert "source_row_summary" not in row
	assert "source_voucher_summary" not in row
	assert entry == before


def test_new_voucher_snapshot_preserves_gl_remarks_verbatim():
	row = frappe._dict(
		name="GL-TEST",
		account="100201 - 银行存款",
		account_currency="CNY",
		debit=50000,
		credit=0,
		debit_in_account_currency=50000,
		credit_in_account_currency=0,
		party_type=None,
		party=None,
		cost_center=None,
		project=None,
		finance_book=None,
		against_voucher_type=None,
		against_voucher=None,
		remarks="支付宝结算\n支付宝结算",
	)

	assert to_voucher_entry(row)["remarks"] == "支付宝结算\n支付宝结算"
