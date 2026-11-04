"""Source matching must distinguish intact rows from merged GL contributions."""

from china_finance.services.voucher_summary import find_journal_summary_row


def source(name, debit=0, credit=0, **values):
	return dict(name=name, account="Expense", debit=debit, credit=credit, **values)


def test_merged_amount_equal_to_another_source_row_is_not_unique_provenance():
	doc = {"accounts": [source("A", 10), source("B", 30), source("offset", -10)]}
	merged = source("GL", 30)
	assert find_journal_summary_row(doc, merged, ledger_rows=[merged]) is None


def test_separate_reference_group_cannot_make_a_merged_row_editable():
	doc = {"accounts": [source("A", 10), source("B", 20), source("C", 30)]}
	ledger = [source("merged-AB", 30), source("reference-C", 30)]
	assert find_journal_summary_row(doc, ledger[0], ledger_rows=ledger) is None
	assert find_journal_summary_row(doc, ledger[1], ledger_rows=ledger) is None


def test_accrual_and_payment_are_matched_only_when_the_whole_group_is_intact():
	doc = {"accounts": [source("accrual", credit=624), source("payment", debit=624)]}
	ledger = [source("GL1", credit=624), source("GL2", debit=624)]
	assert find_journal_summary_row(doc, ledger[0], ledger_rows=ledger)["name"] == "accrual"
	assert find_journal_summary_row(doc, ledger[1], ledger_rows=ledger)["name"] == "payment"
	assert find_journal_summary_row(doc, ledger[0], ledger_rows=ledger[:1]) is None
	assert find_journal_summary_row(doc, ledger[0]) is None


def test_equal_amounts_keep_source_rows_ambiguous_unless_draft_identity_is_present():
	doc = {"accounts": [source("A", 30), source("B", 30)]}
	ledger = [source("GL1", 30), source("GL2", 30)]
	assert find_journal_summary_row(doc, ledger[0], ledger_rows=ledger) is None
	draft = dict(ledger[0], source_row_name="B")
	assert find_journal_summary_row(doc, draft)["name"] == "B"


def test_dimensions_remain_part_of_source_identity():
	doc = {"accounts": [source("A", 30, party="Supplier A"), source("B", 30, party="Supplier B")]}
	ledger = source("GL", 30, party="Supplier B")
	assert find_journal_summary_row(doc, ledger)["name"] == "B"
