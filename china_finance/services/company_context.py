"""Permission-aware, bounded company choices for the finance workspace."""

import frappe
from frappe.utils import cint


@frappe.whitelist()
def search_companies(doctype, txt, searchfield, start, page_len, filters=None):
	"""Company Link search using names and abbreviations without expanding permissions."""
	rows = frappe.get_list(
		"Company", filters={"is_group": 0}, fields=["name", "abbr"],
		or_filters={field: ["like", f"%{str(txt or '').strip()[:100]}%"] for field in ("name", "abbr")},
		start=max(0, cint(start)), page_length=min(20, max(1, cint(page_len))), order_by="name",
	)
	return [[row.name, row.abbr] for row in rows]


@frappe.whitelist()
def get_company_choices(txt="", start=0, recent=None):
	text = str(txt or "").strip()[:100]
	recent = frappe.parse_json(recent) if isinstance(recent, str) else recent
	recent = (
		list(dict.fromkeys(name for name in recent if isinstance(name, str)))[:5]
		if isinstance(recent, list) else []
	)
	filters = {"is_group": 0}
	kwargs = {}
	if text:
		kwargs["or_filters"] = {"name": ["like", f"%{text}%"], "abbr": ["like", f"%{text}%"]}
	rows = frappe.get_list(
		"Company", filters=filters, fields=["name", "abbr"],
		start=max(0, cint(start)), page_length=21, order_by="name", **kwargs,
	)
	recent_rows = (
		frappe.get_list("Company", filters={**filters, "name": ["in", recent]},
			fields=["name", "abbr"], page_length=5)
		if recent else []
	)
	choices = rows[:20]
	names = list({row.name for row in [*choices, *recent_rows]})
	# Only expose setup status for companies already authorized above.
	settings = (
		frappe.get_all("China Finance Settings", filters={"company": ["in", names]},
			fields=["company", "enabled"], page_length=20 + len(recent_rows))
		if names else []
	)
	enabled = {row.company for row in settings if row.enabled}
	for row in [*choices, *recent_rows]:
		row["finance_enabled"] = row.name in enabled
	by_name = {row.name: row for row in recent_rows}
	return {
		"companies": choices,
		"recent": [by_name[name] for name in recent if name in by_name],
		"has_more": len(rows) > 20,
	}
