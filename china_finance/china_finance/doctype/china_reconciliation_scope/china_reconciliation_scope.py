import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


REFERENCE_DOCTYPES = {"Customer": "Customer", "Supplier": "Supplier", "Bank": "Bank Account"}


class ChinaReconciliationScope(Document):
	def _set_scope_identity(self):
		self.reference_doctype = REFERENCE_DOCTYPES.get(self.scope_type)
		if self.company and self.scope_type and self.reference_name:
			self.scope_key = f"{self.company}|{self.scope_type}|{self.reference_name}"

	def _validate_links(self):
		# Frappe validates Dynamic Links and names new documents before before_validate.
		# Derive the read-only link type and naming key before those native checks.
		self._set_scope_identity()
		super()._validate_links()

	def before_validate(self):
		self._set_scope_identity()

	def validate(self):
		if not self.reference_doctype:
			frappe.throw(_("不支持的对账范围类型"))
		if self.effective_to and self.effective_from > self.effective_to:
			frappe.throw(_("失效日期不能早于生效日期"))
		if flt(self.amount_tolerance) < 0:
			frappe.throw(_("金额容差不能小于零"))
		if self.scope_type == "Bank":
			self.confirmation_method = "Bank Statement"
			bank = frappe.db.get_value("Bank Account", self.reference_name, ["company", "is_company_account"], as_dict=True)
			if not bank or bank.company != self.company or not bank.is_company_account:
				frappe.throw(_("银行对账范围必须选择该公司的银行账户"))
		elif self.confirmation_method not in ("External Confirmation", "Internal Review"):
			frappe.throw(_("客户和供应商对账必须选择外部确认或内部复核"))

