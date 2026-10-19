import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class ChinaFinanceSettings(Document):
	def validate(self):
		old = self.get_doc_before_save()
		if old and any(self.get(f) != old.get(f) for f in (
			"cash_flow_assignment_activation_date", "defer_cash_flow_confirmation_on_monthly_close",
		)):
			from china_finance.services.month_end import lock_company

			lock_company(self.company)
			if frappe.db.exists("China Closing Run", {
				"company": self.company, "docstatus": 0,
				"preparation_state": ["in", ["Posting", "Checking", "Closing"]],
			}):
				frappe.throw(_("请先暂停进行中的月末结账，再调整现金流检查设置"))
		if old and any(self.get(f) != old.get(f) for f in ("enabled", "enable_voucher_preparation", "preparation_from_date")):
			if frappe.db.has_column("China Closing Run", "preparation_state") and frappe.db.exists("China Closing Run", {
				"company": self.company, "docstatus": 0, "preparation_state": ["in", ["Posting", "Checking", "Closing"]],
			}):
				frappe.throw(_("请先暂停进行中的月末结账，再调整凭证准备模式"))
		if self.get("enable_voucher_preparation") and not self.get("preparation_from_date"):
			frappe.throw(_("请设置凭证准备模式生效日期"))
		if self.activation_date:
			self.activation_date = getdate(self.activation_date)
		if self.archive_retention_years and self.archive_retention_years < 10:
			frappe.throw(_("电子会计档案保管年限不能少于 10 年"))
		if self.reconciliation_tolerance is not None and self.reconciliation_tolerance <= 0:
			frappe.throw(_("对账金额容差必须大于零"))
		if self.sales_settlement_mode == "对账结算后确认应收" and not self.sales_settlement_activation_date:
			frappe.throw(_("启用对账结算后确认应收时必须设置切换生效日期"))
		for fieldname in ("profit_loss_account", "retained_earnings_account"):
			account = self.get(fieldname)
			if account and frappe.db.get_value("Account", account, "company") != self.company:
				frappe.throw(_("{0} 必须属于公司 {1}").format(self.meta.get_label(fieldname), self.company))
