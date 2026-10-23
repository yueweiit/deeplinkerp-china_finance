"""Shared accounting-party rules for generated voucher lines."""


COMPANY_SCOPED_PARTY_TYPES = {"Employee", "Shareholder"}
PARTY_OPTIONAL_ACCOUNT_NUMBERS = {"122101"}


def party_type_matches_account(party_type, account_type=None, root_type=None):
	"""Return whether a party type is valid for the selected account."""
	if party_type == "Customer":
		return account_type == "Receivable"
	if party_type == "Supplier":
		return account_type == "Payable"
	if party_type == "Employee":
		return account_type in {"Receivable", "Payable"}
	if party_type == "Shareholder":
		return account_type == "Payable" or root_type == "Equity"
	return False


def account_allows_missing_party(account_number):
	"""Return whether a generated voucher may intentionally omit its party."""
	return str(account_number or "").strip() in PARTY_OPTIONAL_ACCOUNT_NUMBERS
