app_name = "china_finance"
app_title = "China Finance"
app_publisher = "yuewei"
app_description = "China Financial Compliance for ERPNext"
app_email = "china@example.com"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]

after_install = "china_finance.setup.install.after_install"
after_migrate = "china_finance.setup.install.after_migrate"

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "china_finance",
# 		"logo": "/assets/china_finance/logo.png",
# 		"title": "China Finance",
# 		"route": "/china_finance",
# 		"has_permission": "china_finance.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
app_include_css = [
	"/assets/china_finance/css/china_finance.css?v=receipt-tabs-2",
	"/assets/china_finance/css/china_cash_flow_assignment_list.css?v=3",
]
# app_include_js = "/assets/china_finance/js/china_finance.js"

# include js, css files in header of web template
# web_include_css = "/assets/china_finance/css/china_finance.css"
# web_include_js = "/assets/china_finance/js/china_finance.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "china_finance/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

doctype_js = {
	"China Bank Receipt Import": "public/js/bank_receipt_details.js",
	"China Bank Receipt": "public/js/bank_receipt_details.js",
	"Bank Account": "public/js/bank_account.js",
	"Bank Statement Import": "public/js/bank_statement_import.js",
	"Sales Invoice": ["public/js/sales_invoice_invoice_control.js", "public/js/gl_source_snapshot.js"],
	"Sales Order": "public/js/sales_order_settlement.js",
	"Delivery Note": ["public/js/delivery_note_settlement.js", "public/js/gl_source_snapshot.js"],
	"Journal Entry": [
		"public/js/gl_source_snapshot.js",
		"public/js/journal_entry_direct_post.js",
		"public/js/source_voucher_amendment.js",
		"public/js/voucher_bank_receipts.js",
	],
	"Payment Entry": [
		"public/js/payment_entry_invoice_selector.js",
		"public/js/voucher_bank_receipts.js",
		"public/js/gl_source_snapshot.js",
		"public/js/source_voucher_amendment.js",
	],
	"Purchase Invoice": "public/js/gl_source_snapshot.js",
	"Purchase Receipt": "public/js/gl_source_snapshot.js",
	"Stock Entry": "public/js/gl_source_snapshot.js",
	"Asset": "public/js/gl_source_snapshot.js",
	"Asset Capitalization": "public/js/gl_source_snapshot.js",
	"Asset Depreciation Entry": "public/js/gl_source_snapshot.js",
	"Payroll Entry": "public/js/gl_source_snapshot.js",
	"Period Closing Voucher": [
		"public/js/gl_source_snapshot.js",
		"public/js/period_closing_voucher_direct_post.js",
	],
	"China Finance Settings": "public/js/china_finance_settings.js",
	"China Accounting Voucher": "public/js/china_accounting_voucher.js",
	"China Cash Flow Assignment": "public/js/china_cash_flow_assignment.js",
	"China Financial Statement Mapping": "public/js/china_financial_statement_mapping.js",
}
doctype_list_js = {
	"Bank Transaction": "public/js/bank_transaction_list.js",
	"Journal Entry": "public/js/source_voucher_number_list.js",
	"Payment Entry": "public/js/source_voucher_number_list.js",
	"Period Closing Voucher": "public/js/period_closing_voucher_list.js",
	"China Accounting Voucher": "public/js/china_accounting_voucher_list.js",
	"China Cash Flow Assignment": "public/js/china_cash_flow_assignment_list.js",
	"China Financial Statement Template": "public/js/china_financial_statement_template_list.js",
	"China Financial Statement Mapping": "public/js/china_financial_statement_mapping_list.js",
	"China Sales Settlement": "public/js/china_sales_settlement_list.js",
}
page_js = {
	"bank-reconciliation-tool": "public/js/bank_reconciliation_tool.js",
}

app_include_js = [
	"/assets/china_finance/js/bank_reconciliation_tool.js",
	"/assets/china_finance/js/account_display.js",
	"/assets/china_finance/js/company_context.js",
	"/assets/china_finance/js/datatable_layout.js",
]

doctype_tree_js = {
	"Account": "public/js/account_tree.js",
}

override_whitelisted_methods = {
	"frappe.translate.get_boot_translations":
		"china_finance.services.banking_translations.get_boot_translations",
	"frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs":
		"china_finance.services.voucher_batch.submit_cancel_or_update_docs",
	"frappe.model.workflow.bulk_workflow_approval":
		"china_finance.services.voucher_batch.bulk_workflow_approval",
	"erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool.get_bank_transactions":
		"china_finance.services.bank_reconciliation.get_bank_transactions_with_summary",
	"erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts.get_charts_for_country":
		"china_finance.services.account_template.get_charts_for_country",
}

# Keep ERPNext's bank statement import workflow intact while allowing the
# China Finance bank adapters to return the complete converted-file preview.
override_doctype_class = {
	"Bank Statement Import": "china_finance.overrides.bank_statement_import.ChinaFinanceBankStatementImport",
	"Bank Statement Import Log": "china_finance.overrides.bank_statement_import.ChinaFinanceBankStatementImportLog",
	"Company": "china_finance.overrides.company.ChinaFinanceCompany",
	"Journal Entry": "china_finance.overrides.journal_entry.ChinaFinanceJournalEntry",
	"Period Closing Voucher": "china_finance.overrides.period_closing_voucher.ChinaFinancePeriodClosingVoucher",
}

extend_doctype_class = {
	"Journal Entry": ["china_finance.overrides.source_voucher_lock.CompanyFirstSourceVoucherLock"],
	"Payment Entry": ["china_finance.overrides.source_voucher_lock.CompanyFirstSourceVoucherLock"],
	"Period Closing Voucher": ["china_finance.overrides.source_voucher_lock.CompanyFirstSourceVoucherLock"],
}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "china_finance/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "china_finance.utils.jinja_methods",
# 	"filters": "china_finance.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "china_finance.install.before_install"
# after_install = "china_finance.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "china_finance.uninstall.before_uninstall"
# after_uninstall = "china_finance.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "china_finance.utils.before_app_install"
# after_app_install = "china_finance.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "china_finance.utils.before_app_uninstall"
# after_app_uninstall = "china_finance.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

after_build = "china_finance.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "china_finance.notifications.get_notification_config"

# These records are audit trails derived from ERPNext business documents. They
# must remain queryable after a source is cancelled or deleted, but must never
# become reverse dependencies that block the native business workflow.
ignore_links_on_delete = [
	"China Accounting Voucher",
	"China Cash Flow Assignment",
	"China Voucher Sync Issue",
	"China Electronic Document",
	"China Reconciliation Difference",
]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

_gl_source_approval_events = {
	"before_submit": "china_finance.services.voucher.validate_source_approval",
	"before_cancel": "china_finance.services.voucher.prepare_source_cancellation",
}

_journal_entry_events = {
	"before_naming": "china_finance.services.naming.sync_journal_entry_series",
	**_gl_source_approval_events,
	"on_trash": "china_finance.services.bank_receipt_import.prepare_voucher_delete",
	"on_submit": [
		"china_finance.services.voucher.on_gl_source_submit",
		"china_finance.services.bank_reconciliation.on_journal_entry_submit",
		"china_finance.services.bank_receipt_import.update_receipt_status",
	],
	"on_cancel": "china_finance.services.voucher.on_gl_source_cancel",
}

_payment_entry_events = {
	**_gl_source_approval_events,
	"on_submit": "china_finance.services.voucher.on_gl_source_submit",
	"on_cancel": "china_finance.services.voucher.on_gl_source_cancel",
}

for _events in (_journal_entry_events, _payment_entry_events):
	_events["validate"] = "china_finance.services.bank_receipt_import.validate_linked_voucher"
	_events["before_cancel"] = [_events["before_cancel"], "china_finance.services.bank_receipt_import.allow_voucher_cancellation"]
	for _event in ("on_cancel", "on_update"):
		_existing = _events.get(_event)
		_events[_event] = ([_existing] if _existing else []) + ["china_finance.services.bank_receipt_import.update_receipt_status"]
_payment_entry_events["on_submit"] = [_payment_entry_events["on_submit"], "china_finance.services.bank_receipt_import.update_receipt_status"]

doc_events = {
	doctype: (
		_journal_entry_events
		if doctype == "Journal Entry"
		else _payment_entry_events
		if doctype == "Payment Entry"
		else _gl_source_approval_events
	)
	for doctype in (
		"Journal Entry",
		"Payment Entry",
		"Sales Invoice",
		"Purchase Invoice",
		"Stock Entry",
		"Delivery Note",
		"Purchase Receipt",
		"Asset",
		"Asset Capitalization",
		"Asset Depreciation Entry",
		"Payroll Entry",
		"Period Closing Voucher",
	)
}
doc_events["Company"] = {"on_update": "china_finance.api.initialize_profile_company_on_update"}
doc_events["China Finance Settings"] = {"on_update": "china_finance.api.sync_settings_mappings_on_update"}
doc_events["Sales Order"] = {"before_insert": "china_finance.services.sales_settlement.apply_sales_order_settlement_mode"}
doc_events["Delivery Note"] = {
	"before_submit": [
		"china_finance.services.voucher.validate_source_approval",
		"china_finance.services.sales_settlement.validate_delivery_note_settlement_mode",
	],
	"before_cancel": "china_finance.services.voucher.prepare_source_cancellation",
	"on_submit": [
		"china_finance.services.voucher.on_gl_source_submit",
		"china_finance.services.auto_invoice.on_delivery_note_submit",
	],
}
doc_events["Purchase Receipt"] = {
	"before_submit": "china_finance.services.voucher.validate_source_approval",
	"before_cancel": "china_finance.services.voucher.prepare_source_cancellation",
	"on_submit": [
		"china_finance.services.voucher.on_gl_source_submit",
		"china_finance.services.auto_invoice.on_purchase_receipt_submit",
	],
}
doc_events["Sales Invoice"] = {
	"before_submit": [
		"china_finance.services.voucher.validate_source_approval",
		"china_finance.services.sales_settlement.validate_sales_invoice_settlement",
	],
	"before_cancel": "china_finance.services.voucher.prepare_source_cancellation",
	"on_cancel": "china_finance.services.sales_settlement.handle_sales_invoice_cancellation",
}
doc_events["*"] = {
	"on_submit": "china_finance.services.voucher.on_gl_source_submit",
	"on_cancel": "china_finance.services.voucher.on_gl_source_cancel",
}
doc_events["Bank Transaction"] = {
	"before_insert": "china_finance.services.bank_reconciliation.prepare_bank_transaction",
	"on_submit": "china_finance.services.bank_reconciliation.auto_create_voucher_on_submit",
	"on_update_after_submit": "china_finance.services.bank_receipt_import.update_receipt_status",
	"on_cancel": "china_finance.services.bank_receipt_import.update_receipt_status",
}

doc_events["File"] = {
	"before_validate": "china_finance.services.bank_receipt_import.protect_receipt_file",
	"on_trash": "china_finance.services.bank_receipt_import.protect_receipt_file",
}

# Share a company/period gate across normal forms, imports, APIs and month-end steps.
doc_events["*"]["before_validate"] = "china_finance.services.month_end.guard_period_write"
doc_events["*"]["before_cancel"] = "china_finance.services.month_end.guard_period_write"
doc_events["*"]["on_trash"] = "china_finance.services.month_end.guard_period_write"
_journal_entry_events["validate"] = [
	"china_finance.services.bank_receipt_import.validate_linked_voucher",
	"china_finance.services.voucher_preparation.validate_preparation",
]
_journal_entry_events["before_cancel"] = [
	"china_finance.services.opening_balance.guard_linked_opening_source",
	*_journal_entry_events.get("before_cancel", []),
]
_journal_entry_events["before_update_after_submit"] = "china_finance.services.opening_balance.guard_linked_opening_source"

# Scheduled Tasks
# ---------------

scheduler_events = {
	"daily": ["china_finance.tasks.backfill_voucher_snapshots"],
}

# Testing
# -------

# before_tests = "china_finance.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "china_finance.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "china_finance.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "china_finance.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["china_finance.utils.before_request"]
# after_request = ["china_finance.utils.after_request"]

# Job Events
# ----------
# before_job = ["china_finance.utils.before_job"]
# after_job = ["china_finance.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"china_finance.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

# China Finance deliberately uses standard ERPNext posting behavior. Regional
# overrides are not required because statutory vouchers are immutable snapshots
# of the resulting GL Entries.

# Currency context is part of each financial document, including historical reads.
for _currency_doctype in (
	"China Finance Settings", "China Cash Flow Assignment", "China Prior Period Error Adjustment",
	"China Purchase Reconciliation Rule", "China Reconciliation Difference",
	"China Reconciliation Scope", "China Reconciliation Statement",
	"China Tax Invoice Request", "China Input Tax Deduction Batch",
	"China Accounting Voucher", "China Tax Invoice",
):
	_currency_events = doc_events.setdefault(_currency_doctype, {})
	for _currency_event in ("onload", "before_validate"):
		_currency_events[_currency_event] = "china_finance.services.currency_context.set_document_currency"
