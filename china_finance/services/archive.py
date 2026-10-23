import hashlib
import io
import json
import mimetypes
import zipfile
from pathlib import Path

import frappe
from frappe import _
from frappe.exceptions import DuplicateEntryError
from frappe.utils import add_years, getdate, now_datetime
from frappe.utils.file_manager import save_file


def get_file_doc(file_url):
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not name:
		frappe.throw(f"File not found: {file_url}")
	return frappe.get_doc("File", name)


def get_file_bytes(file_url):
	content = get_file_doc(file_url).get_content()
	return content.encode("utf-8") if isinstance(content, str) else content


def calculate_file_hash(file_url):
	return hashlib.sha256(get_file_bytes(file_url)).hexdigest()


def populate_archive_metadata(doc):
	file_doc = get_file_doc(doc.file)
	content = get_file_bytes(doc.file)
	doc.file_name = file_doc.file_name or Path(doc.file).name
	doc.mime_type = mimetypes.guess_type(doc.file_name)[0] or "application/octet-stream"
	doc.file_size = len(content)
	doc.sha256 = hashlib.sha256(content).hexdigest()
	doc.archived_on = doc.archived_on or now_datetime()
	doc.archived_by = doc.archived_by or frappe.session.user
	settings = frappe.db.get_value(
		"China Finance Settings", {"company": doc.company, "enabled": 1}, ["archive_retention_years"], as_dict=True
	)
	years = settings.archive_retention_years if settings else 30
	doc.retention_until = doc.retention_until or add_years(getdate(doc.archived_on), years)
	doc.verification_result = "SHA-256 verified"


def create_archive_record(company, reference_doctype, reference_name, category, file_url):
	file_hash = calculate_file_hash(file_url)
	lock_key = hashlib.sha256(
		f"{company}|{reference_doctype}|{reference_name}|{file_hash}".encode()
	).hexdigest()
	locked = frappe.db.sql("SELECT GET_LOCK(%s, 10)", (f"china_archive:{lock_key}",))[0][0]
	if locked != 1:
		frappe.throw(_("归档记录正在被其他请求处理，请稍后重试"))
	try:
		existing = frappe.db.get_value(
			"China Electronic Document",
			{
				"company": company, "reference_doctype": reference_doctype,
				"reference_name": reference_name, "sha256": file_hash,
			},
			"name",
		)
		if existing:
			return existing
		doc = frappe.get_doc(
			{
				"doctype": "China Electronic Document",
				"company": company,
				"document_category": category,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"file": file_url,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
	except DuplicateEntryError:
		return frappe.db.get_value(
			"China Electronic Document",
			{
				"company": company, "reference_doctype": reference_doctype,
				"reference_name": reference_name, "sha256": file_hash,
			},
			"name",
		)
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (f"china_archive:{lock_key}",))


def create_archive_package(company, reference_doctype, reference_name, closing_run=None):
	_validate_archive_reference(company, reference_doctype, reference_name)
	if closing_run:
		closing_doc = frappe.get_doc("China Closing Run", closing_run)
		if closing_doc.company != company:
			frappe.throw(_("期末智能结转不属于请求公司"))
		closing_doc.check_permission("read")
	include_company_documents = reference_doctype == "Company" or bool(closing_run)
	document_filters = {"company": company, "status": "Archived"}
	if not include_company_documents:
		document_filters.update({"reference_doctype": reference_doctype, "reference_name": reference_name})
	documents = frappe.get_all(
		"China Electronic Document",
		filters=document_filters,
		fields=[
			"name", "document_category", "reference_doctype", "reference_name", "file",
			"file_name", "mime_type", "file_size", "sha256", "archived_on", "retention_until",
		],
		order_by="archived_on, name",
	)
	snapshots = []
	if closing_run:
		snapshots = frappe.get_all(
			"China Report Snapshot",
			filters={"closing_run": closing_run},
			fields=["name", "statement_type", "template", "from_date", "to_date", "data_json", "sha256"],
			order_by="statement_type",
		)

	index = {
		"schema_version": "1.0",
		"company": company,
		"generated_on": str(now_datetime()),
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"documents": [dict(row) for row in documents],
		"report_snapshots": [
			{k: v for k, v in dict(row).items() if k != "data_json"}
			for row in snapshots
		],
	}
	buffer = io.BytesIO()
	with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
		archive.writestr("index.json", json.dumps(index, ensure_ascii=False, indent=2, default=str))
		for row in documents:
			try:
				archive.writestr(f"documents/{row.name}-{Path(row.file_name).name}", get_file_bytes(row.file))
			except Exception as exc:
				frappe.log_error(str(exc), f"China Finance archive file missing: {row.name}")
				raise
		for row in snapshots:
			archive.writestr(f"reports/{row.statement_type}.json", row.data_json)

	filename = f"china-finance-archive-{reference_name}-{now_datetime().strftime('%Y%m%d%H%M%S')}.zip"
	file_doc = save_file(filename, buffer.getvalue(), reference_doctype, reference_name, is_private=1)
	archive_name = create_archive_record(
		company, reference_doctype, reference_name, "Closing Package", file_doc.file_url
	)
	return {
		"file_url": file_doc.file_url,
		"archive_record": archive_name,
		"sha256": calculate_file_hash(file_doc.file_url),
		"document_count": len(documents),
		"snapshot_count": len(snapshots),
	}


@frappe.whitelist()
def verify_archive(name):
	doc = frappe.get_doc("China Electronic Document", name)
	doc.check_permission("read")
	actual = calculate_file_hash(doc.file)
	return {"valid": actual == doc.sha256, "expected": doc.sha256, "actual": actual}


@frappe.whitelist()
def export_archive_package(company, reference_doctype="Company", reference_name=None, closing_run=None):
	frappe.only_for(("System Manager", "China Archive User", "China Finance Auditor"))
	return create_archive_package(
		company,
		reference_doctype,
		reference_name or company,
		closing_run=closing_run,
	)


ARCHIVE_REFERENCE_DOCTYPES = {
	"Company", "China Closing Run", "China Tax Invoice", "China Reconciliation Statement",
}


def _validate_archive_reference(company, reference_doctype, reference_name):
	if reference_doctype not in ARCHIVE_REFERENCE_DOCTYPES:
		frappe.throw(_("不允许导出该类型的电子档案：{0}").format(reference_doctype))
	if not reference_name:
		frappe.throw(_("电子档案引用对象不能为空"))
	if reference_doctype == "Company":
		if reference_name != company:
			frappe.throw(_("电子档案引用公司与请求公司不一致"))
		doc = frappe.get_doc("Company", company)
	else:
		doc = frappe.get_doc(reference_doctype, reference_name)
		if doc.get("company") != company:
			frappe.throw(_("电子档案引用对象不属于请求公司"))
	doc.check_permission("read")
