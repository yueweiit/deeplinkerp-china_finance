import frappe
from frappe import _
from frappe.model.document import Document

from china_finance.services.opening_balance import protect_opening_document


class ChinaOpeningBalance(Document):
	def validate(self):
		protect_opening_document()
		old = self.get_doc_before_save()
		if old and old.status == "Active":
			frappe.throw(_("已启用的期初版本不可修改，请新建调整"))
		if self.get("initialization_key") and self.initialization_key != self.company:
			frappe.throw(_("期初初始化键与公司不一致"))
		if self.mode == "Adjustment" and (not self.previous_batch or not self.root_batch):
			frappe.throw(_("期初调整必须关联初始化批次及前一版本"))

	def on_trash(self):
		protect_opening_document()
		frappe.throw(_("期初余额保留完整审计历史，不能删除"))

	def before_rename(self, old_name, new_name, merge=False):
		protect_opening_document()
		frappe.throw(_("期初余额批次不能重命名或合并"))
