"""Native opening purpose shared by numbering, preparation and daily queries.

Classification does not approve an initialization: activation additionally
reconciles every source/ledger amount and dimension in opening_balance.
"""

import frappe


def is_opening_draft(doc):
	return doc.get("doctype") == "Journal Entry" and doc.get("is_opening") == "Yes"


def get_opening_sources(company, names=None):
	if not company or names == []:
		return set()
	params = {"company": company}
	condition = ""
	if names is not None:
		params["names"] = tuple(names)
		if not params["names"]:
			return set()
		condition = "AND je.name IN %(names)s"
	rows = frappe.db.sql(
		f"""SELECT je.name FROM `tabJournal Entry` je
		INNER JOIN `tabGL Entry` gle
			ON gle.voucher_type='Journal Entry' AND gle.voucher_no=je.name
		WHERE je.company=%(company)s AND je.is_opening='Yes'
			AND je.docstatus IN (1,2)
			AND (je.docstatus=2 OR gle.is_cancelled=0) {condition}
		GROUP BY je.name, je.company, je.posting_date
		HAVING SUM(CASE WHEN gle.is_opening='Yes'
			AND gle.company=je.company AND gle.posting_date=je.posting_date
			THEN 0 ELSE 1 END)=0""",
		params, as_dict=True,
	)
	return {row.name for row in rows}


def is_opening_source(source_doctype, source_name, company=None):
	if source_doctype != "Journal Entry" or not source_name:
		return False
	company = company or frappe.db.get_value("Journal Entry", source_name, "company")
	return source_name in get_opening_sources(company, [source_name])
