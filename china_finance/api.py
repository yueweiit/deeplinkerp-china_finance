import frappe
from frappe import _
from frappe.utils import cint, getdate, nowdate

from china_finance.services.closing import run_closing_checks
from china_finance.services.voucher import GL_SOURCE_DOCTYPES
from china_finance.setup.china_coa_profile import (
	apply_company_defaults,
	ensure_cash_scope,
	ensure_tax_mappings,
	get_profile_status,
	is_profile_company,
	normalize_generic_vat_templates,
	update_settings_profile,
)
from china_finance.setup.china_coa_profile import (
	get_china_coa_master_data_readiness as get_master_data_readiness,
)
from china_finance.setup.china_coa_profile import (
	sync_china_coa_master_data as sync_master_data,
)
from china_finance.setup.install import ROLES, validate_deployment_schema
from china_finance.setup.templates import (
	create_automatic_mappings,
	seed_statement_templates,
	sync_unreviewed_automatic_mappings,
)


@frappe.whitelist()
def get_prior_period_error_adjustment_readiness(company, from_date, to_date):
	from china_finance.services.prior_period_error import get_prior_period_error_readiness

	frappe.has_permission("Company", "read", company, throw=True)
	return get_prior_period_error_readiness(company, from_date, to_date)


@frappe.whitelist()
def submit_prior_period_error_adjustment(name):
	frappe.only_for(("System Manager", "China Finance Manager"))
	doc = frappe.get_doc("China Prior Period Error Adjustment", name)
	doc.submit()
	return {"name": doc.name, "status": doc.status, "docstatus": doc.docstatus}


def _initialize_company(
	company,
	accounting_standard="小企业会计准则",
	taxpayer_type="一般纳税人",
	activation_date=None,
	voucher_mode="收付转记",
	enforce_role_separation=0,
):
	if not frappe.db.exists("Company", company):
		frappe.throw(_("公司不存在：{0}").format(company))
	seed_statement_templates()
	settings = frappe.db.get_value("China Finance Settings", {"company": company}, "name")
	first_profile_initialization = is_profile_company(company) and not settings
	values = {
		"company": company,
		"enabled": 1,
		"activation_date": getdate(activation_date or nowdate()),
		"cash_flow_assignment_activation_date": getdate(activation_date or nowdate()),
		"accounting_standard": accounting_standard,
		"taxpayer_type": taxpayer_type,
		"voucher_mode": voucher_mode,
		"default_voucher_word": "记",
		"sequence_reset": "会计期间",
		"enforce_role_separation": cint(enforce_role_separation),
		"enable_import_batch_posting": 0,
		"archive_retention_years": 30,
		"require_file_hash": 1,
		"freeze_on_close": 1,
		"reconciliation_tolerance": 0.01,
	}
	if settings:
		doc = frappe.get_doc("China Finance Settings", settings)
		# Existing settings are an accounting configuration, not installation defaults.
		# A second initialization may only complete dependent metadata and suggestions.
		accounting_standard = doc.accounting_standard
	else:
		doc = frappe.get_doc({"doctype": "China Finance Settings", **values}).insert()
	defaults = apply_company_defaults(company, repair=first_profile_initialization)
	mappings = create_automatic_mappings(company, accounting_standard, doc.activation_date)
	mappings_updated = sync_unreviewed_automatic_mappings(company, accounting_standard)
	tax_mappings = ensure_tax_mappings(company, doc.activation_date)
	vat_templates = normalize_generic_vat_templates(company)
	cash_scope = ensure_cash_scope(company, doc.activation_date)
	update_settings_profile(doc)
	return {
		"settings": doc.name, "automatic_mappings_created": mappings,
		"automatic_mappings_updated": mappings_updated,
		"tax_mappings_created": tax_mappings, "cash_scope_created": cash_scope,
		"vat_templates_updated": vat_templates["templates_updated"],
		"item_tax_templates_updated": vat_templates["item_templates_updated"],
		"item_tax_templates_created": vat_templates["item_templates_created"],
		"vat_account_disabled": vat_templates["vat_account_disabled"],
		"vat_account_deleted": vat_templates["vat_account_deleted"],
		"company_defaults_updated": defaults["updated"], "mapping_review_required": bool(mappings),
	}


@frappe.whitelist()
def initialize_company(
	company,
	accounting_standard="小企业会计准则",
	taxpayer_type="一般纳税人",
	activation_date=None,
	voucher_mode="收付转记",
	enforce_role_separation=0,
):
	frappe.only_for(("System Manager", "China Finance Manager"))
	frappe.has_permission("Company", "read", company, throw=True)
	return _initialize_company(
		company, accounting_standard, taxpayer_type, activation_date,
		voucher_mode, enforce_role_separation,
	)


def initialize_profile_company_on_update(doc, method=None):
	"""A new company that explicitly chose the China CoA is a China Finance company.

	This hook creates only app configuration and review-required suggestions. It never
	replaces a chart or changes an existing company's accounting choices.
	"""
	if not is_profile_company(doc.name) or frappe.db.exists("China Finance Settings", {"company": doc.name}):
		return
	_initialize_company(doc.name)


def sync_settings_mappings_on_update(doc, method=None):
	"""Create review-required mappings when a company changes its accounting standard."""
	if not doc.enabled or not doc.company or not doc.accounting_standard:
		return
	create_automatic_mappings(doc.company, doc.accounting_standard, doc.activation_date)
	sync_unreviewed_automatic_mappings(doc.company, doc.accounting_standard)


def initialize_existing_profile_companies():
	"""Backfill companies created before China Finance was installed.

	Only companies that explicitly use the supported China CoA and do not already
	have China Finance Settings are initialized. Existing and Legacy companies are
	never overwritten.
	"""
	initialized = 0
	for company in frappe.get_all("Company", filters={"chart_of_accounts": "中国企业会计准则－一般纳税人制造业（1.0）"}, pluck="name"):
		if frappe.db.exists("China Finance Settings", {"company": company}):
			continue
		_initialize_company(company)
		initialized += 1
	return initialized


@frappe.whitelist()
def get_china_coa_profile_status(company):
	frappe.has_permission("Company", "read", company, throw=True)
	return get_profile_status(company)


@frappe.whitelist()
def sync_china_coa_profile(company, repair_defaults=0):
	frappe.only_for(("System Manager", "China Finance Manager"))
	frappe.has_permission("Company", "read", company, throw=True)
	if not is_profile_company(company):
		frappe.throw(_("公司未使用受支持的中国科目模板"))
	settings = frappe.get_doc("China Finance Settings", company)
	defaults = apply_company_defaults(company, repair=cint(repair_defaults))
	mappings = create_automatic_mappings(company, settings.accounting_standard, settings.activation_date)
	mappings_updated = sync_unreviewed_automatic_mappings(company, settings.accounting_standard)
	tax_mappings = ensure_tax_mappings(company, settings.activation_date)
	vat_templates = normalize_generic_vat_templates(company)
	cash_scope = ensure_cash_scope(company, settings.activation_date)
	status = get_profile_status(company)
	update_settings_profile(settings, status)
	return {
		"status": status, "company_defaults_updated": defaults["updated"],
		"automatic_mappings_created": mappings, "automatic_mappings_updated": mappings_updated,
		"tax_mappings_created": tax_mappings,
		"vat_templates_updated": vat_templates["templates_updated"],
		"item_tax_templates_updated": vat_templates["item_templates_updated"],
		"item_tax_templates_created": vat_templates["item_templates_created"],
		"vat_account_disabled": vat_templates["vat_account_disabled"],
		"vat_account_deleted": vat_templates["vat_account_deleted"],
		"cash_scope_created": cash_scope,
	}


@frappe.whitelist()
def get_china_coa_master_data_readiness(company):
	frappe.has_permission("Company", "read", company, throw=True)
	return get_master_data_readiness(company)


@frappe.whitelist()
def sync_china_coa_master_data(company, repair=0):
	frappe.only_for(("System Manager", "China Finance Manager"))
	frappe.has_permission("Company", "read", company, throw=True)
	return sync_master_data(company, repair=cint(repair))


@frappe.whitelist()
def deployment_health(company=None):
	frappe.only_for(("System Manager", "China Finance Manager", "China Finance Auditor"))
	validate_deployment_schema()
	result = {
		"status": "ok",
		"version": frappe.get_attr("china_finance.__version__"),
		"roles": {role: bool(frappe.db.exists("Role", role)) for role in ROLES},
		"source_doctypes": list(GL_SOURCE_DOCTYPES),
		"templates": frappe.db.count("China Financial Statement Template"),
		"metadata": {
			"doctypes": frappe.db.count("DocType", {"module": "China Finance"}),
			"reports": frappe.db.count("Report", {"module": "China Finance"}),
			"workspace": bool(frappe.db.exists("Workspace", "China Finance")),
			"workspace_sidebar": bool(frappe.db.exists("Workspace Sidebar", "China Finance")),
			"desktop_icon": bool(frappe.db.exists("Desktop Icon", "China Finance")),
			"print_format": bool(frappe.db.exists("Print Format", "China Accounting Voucher")),
			"voucher_sync_issue": bool(frappe.db.exists("DocType", "China Voucher Sync Issue")),
			"cash_flow_assignment": bool(frappe.db.exists("DocType", "China Cash Flow Assignment")),
			"opening_balance": bool(frappe.db.exists("DocType", "China Opening Balance")),
			"opening_balance_page": bool(frappe.db.exists("Page", "china-opening-balances")),
			"prior_period_error_adjustment": bool(
				frappe.db.exists("DocType", "China Prior Period Error Adjustment")
			),
			"retired_purchase_chain": bool(
				frappe.db.get_value("Report", "China Purchase Document Chain", "disabled")
			),
		},
	}
	doc_events = frappe.get_hooks("doc_events")
	wildcard_events = doc_events.get("*", {})
	result["hooks"] = {
		"all_gl_submit": "china_finance.services.voucher.on_gl_source_submit" in wildcard_events.get("on_submit", []),
		"all_gl_cancel": "china_finance.services.voucher.on_gl_source_cancel" in wildcard_events.get("on_cancel", []),
	}
	if not all(result["hooks"].values()):
		result["status"] = "error"
	if company:
		settings = frappe.db.get_value("China Finance Settings", {"company": company}, "name")
		result["company"] = {
			"enabled": bool(settings), "settings": settings, "coa_profile": get_profile_status(company),
			"master_data_readiness": get_master_data_readiness(company),
		}
		if result["company"]["coa_profile"].get("supported") and result["company"]["coa_profile"]["status"] != "Ready":
			result["status"] = "error"
		if result["company"]["master_data_readiness"].get("blocking_count"):
			result["status"] = "error"
	return result
