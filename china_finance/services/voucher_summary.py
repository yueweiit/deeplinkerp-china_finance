"""Read-only source matching and server-side voucher-summary context."""

from collections import Counter
from decimal import Decimal


SUMMARY_IDENTITY_FIELDS = ("account_currency", "party_type", "party", "cost_center", "project")
SUMMARY_CONTEXT_FIELDS = ("source_row_summary", "source_voucher_summary")


def find_journal_summary_row(doc, ledger_row, *, ledger_rows=None):
	"""Resolve one source row only when the ledger has not combined its group."""
	source_name = ledger_row.get("source_row_name")
	fields = tuple(field for field in SUMMARY_IDENTITY_FIELDS if field in ledger_row)

	def same_identity(row):
		return row.get("account") == ledger_row.get("account") and all(
			(row.get(field) or "") == (ledger_row.get(field) or "") for field in fields
		)

	candidates = [
		row for row in doc.get("accounts") or []
		if same_identity(row) and (not source_name or row.get("name") == source_name)
	]
	if not source_name and len(candidates) > 1:
		# A matching amount alone may belong to a different source row after
		# merging (for example, 10 + 30 - 10). Require the whole group's amount
		# multiset, including row counts and debit/credit signs, to be intact.
		if ledger_rows is None or any(field not in ledger_row for field in ("debit", "credit")):
			return None
		group = [row for row in ledger_rows if same_identity(row)]
		if Counter(_amount_key(row) for row in candidates) != Counter(_amount_key(row) for row in group):
			return None

	candidates = [
		row for row in candidates
		if all(
			Decimal(str(row.get(field) or 0)) == Decimal(str(ledger_row.get(field) or 0))
			for field in ("debit", "credit") if field in ledger_row
		)
	]
	return candidates[0] if len(candidates) == 1 else None


def _amount_key(row):
	return tuple(Decimal(str(row.get(field) or 0)) for field in ("debit", "credit"))


def journal_summary_context(doc, source_row):
	"""Return server-only context; never grant editing rights to a pending row."""
	return {
		"source_row_summary": source_row.get("user_remark") if source_row is not None else None,
		"source_voucher_summary": doc.get("remark"),
	}
