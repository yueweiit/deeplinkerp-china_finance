from unittest.mock import patch

from china_finance.china_finance.report.china_voucher_ledger import china_voucher_ledger as ledger


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
