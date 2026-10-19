
from frappe import _

import frappe
from frappe.model.document import Document
from frappe.utils import getdate

from china_finance.services.bank_receipt_social import ALLOCATION_RULE_TYPES, SOCIAL_ITEMS


class ChinaBankReceiptRule(Document):
	def validate(self):
		if self.rule_type not in ALLOCATION_RULE_TYPES and not self.business_type and not self.keyword:
			frappe.throw(_("业务类型和关键词至少填写一项"))
		if (
			self.effective_from
			and self.effective_to
			and getdate(self.effective_from) > getdate(self.effective_to)
		):
			frappe.throw(_("交易日期起不能晚于交易日期止"))
		account = frappe.get_doc("Account", self.account)
		if (
			account.company != self.company
			or account.is_group
			or account.disabled
			or account.account_currency != "CNY"
		):
			frappe.throw(_("请选择本公司启用的明细科目"))
		if self.rule_type in ALLOCATION_RULE_TYPES:
			label = _("公积金") if self.rule_type == "公积金分摊" else _("社保")
			if self.direction != "支出" or account.root_type != "Expense":
				frappe.throw(_("{0}分摊须为支出，公司承担科目须为费用科目").format(label))
			personal = frappe.get_doc("Account", self.personal_account)
			if (
				personal.company != self.company
				or personal.is_group
				or personal.disabled
				or personal.account_currency != "CNY"
				or personal.root_type != "Asset"
				or personal.account_type in ("Bank", "Cash", "Receivable", "Payable")
			):
				frappe.throw(
					_("{0}个人承担请选择本公司人民币其他应收款明细科目；需要逐人往来核算时请手工制证后关联").format(label)
				)
			if not self.accrual_account:
				frappe.throw(_("请设置{0}计提及支付使用的应付科目").format(label))
			accrual = frappe.get_doc("Account", self.accrual_account)
			if (
				accrual.company != self.company
				or accrual.is_group
				or accrual.disabled
				or accrual.account_currency != "CNY"
				or accrual.root_type != "Liability"
				or accrual.account_type in ("Bank", "Cash", "Receivable", "Payable")
			):
				frappe.throw(
					_("{0}计提请选择本公司启用的人民币负债明细科目；需要往来单位的科目请手工制证后关联").format(label)
				)
			percent_fields = (
				SOCIAL_ITEMS.values()
				if self.rule_type == "社保分摊"
				else ("housing_fund_company_percent",)
			)
			for field in percent_fields:
				if self.get(field) is None or not 0 <= self.get(field) <= 100:
					frappe.throw(_("请填写 0 至 100 之间的{0}公司承担百分比").format(label))
