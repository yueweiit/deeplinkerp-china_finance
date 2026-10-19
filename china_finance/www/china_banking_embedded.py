from pathlib import Path

import frappe
from bs4 import BeautifulSoup

no_cache = 1


def prepare_banking_html(html):
	page = BeautifulSoup(html, "html.parser")
	if page.body is None:
		raise ValueError("ERPNext Banking template must contain a body")
	# Module scripts are deferred; the session dictionary is ready before React mounts.
	script = page.new_tag("script")
	script.string = "frappe._messages = frappe.boot.__messages || {};"
	page.body.append(script)
	progress_adapter = page.new_tag("script", src="/assets/china_finance/js/banking_i18n.js?v=1")
	page.body.append(progress_adapter)
	return str(page)


def get_context(context):
	if frappe.session.user == "Guest":
		raise frappe.PermissionError
	from erpnext.www.banking import get_context as get_banking_context

	values = get_banking_context(context)
	path = Path(frappe.get_app_path("erpnext", "www", "banking.html"))
	html = frappe.render_template(path.read_text(encoding="utf-8"), values, is_path=False)
	context.banking_html = prepare_banking_html(html)
	context.base_template = None
	return context
