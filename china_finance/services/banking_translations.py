"""Translation endpoint compatibility for ERPNext Banking on older Frappe builds."""

import frappe
from frappe.translate import get_all_translations, get_language


@frappe.whitelist(methods=["GET"])
def get_boot_translations(lang=None, v=None):
	# The client version is only a cache buster. Keep site Translation overrides.
	language = lang or get_language()
	if language not in {"en", "zh", "es"} and not frappe.db.exists("Language", language):
		frappe.throw(frappe._("Invalid language"))
	return get_all_translations(language)
