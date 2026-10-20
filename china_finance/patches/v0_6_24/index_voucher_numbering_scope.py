from china_finance.china_finance.doctype.china_accounting_voucher.china_accounting_voucher import (
	on_doctype_update,
)


def execute():
	# Existing sites may skip unchanged DocType JSON during model sync.
	on_doctype_update()
