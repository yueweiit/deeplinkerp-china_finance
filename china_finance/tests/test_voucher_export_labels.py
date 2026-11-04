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
