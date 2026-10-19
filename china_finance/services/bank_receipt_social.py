"""Company-configured payroll contribution allocations from receipt details."""

from frappe import _


import hashlib
import json
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import frappe

from china_finance.services.bank_receipt_parser import money

SOCIAL_ITEMS = {
	"工伤保险费": "injury_company_percent",
	"企业职工基本养老保险费": "pension_company_percent",
	"基本医疗保险费": "medical_company_percent",
	"失业保险费": "unemployment_company_percent",
}
HOUSING_FUND_ITEMS = {"住房公积金", "公积金"}
ALLOCATION_RULE_TYPES = {"社保分摊", "公积金分摊"}


def coverage_period(details, posting_date):
	"""Use receipt coverage dates; never infer a month from the payment date."""
	months = set()
	for detail in details:
		if any(not re.fullmatch(r"\d{8}", str(detail.get(f) or "")) for f in ("period_from", "period_to")):
			return None
		try:
			start, end = (
				datetime.strptime(str(detail.get(f) or ""), "%Y%m%d").date()
				for f in ("period_from", "period_to")
			)
		except ValueError:
			return None
		if start > end or (start.year, start.month) != (end.year, end.month):
			return None
		months.add((start.year, start.month))
	if len(months) != 1:
		return None
	year, month = months.pop()
	# Include the year when paying a prior-year contribution.
	label = f"{month}月" if year == datetime.fromisoformat(str(posting_date)).year else f"{year}年{month}月"
	return {"month": f"{year:04d}-{month:02d}", "label": label}


def social_period(details, posting_date):
	"""Backward-compatible name for callers that already use the social helper."""
	return coverage_period(details, posting_date)


def is_housing_fund_item(item):
	return str(item or "").strip() in HOUSING_FUND_ITEMS


def is_housing_fund_receipt(data):
	"""Identify a housing-fund payment from details or explicit receipt text."""
	details = data.get("tax_details") or []
	if details:
		return all(is_housing_fund_item(detail.get("item")) for detail in details)
	text = " ".join(
		str(data.get(field) or "")
		for field in ("summary", "business_type", "counterparty", "payee")
	)
	return data.get("direction") == "支出" and "公积金" in text


def housing_fund_period(data):
	"""Use explicit coverage dates, or the transaction month when none were supplied."""
	details = data.get("tax_details") or []
	if details:
		return coverage_period(details, data.get("posting_date"))
	if not is_housing_fund_receipt(data):
		return None
	try:
		posting_date = datetime.fromisoformat(str(data.get("posting_date"))).date()
	except (TypeError, ValueError):
		return None
	return {
		"month": f"{posting_date.year:04d}-{posting_date.month:02d}",
		"label": f"{posting_date.month}月",
		"inferred_from_transaction_date": True,
	}


def social_suggestion(rule, data):
	def blocked(reason):
		return {"account": None, "rule_type": "社保分摊", "blocked": True, "reason": reason}

	details = data.get("tax_details") or []
	if data["direction"] != "支出" or not details or any(d["item"] not in SOCIAL_ITEMS for d in details):
		return blocked("社保分摊只支持已配置的四类险种，请核对其他税费项目")
	if sum(money(d["amount"]) for d in details) != money(data["amount"]):
		frappe.throw(_("社保明细合计与银行付款不一致"))
	period = social_period(details, data["posting_date"])
	if not period:
		return blocked("社保所属时期缺失、无效或涉及多个月份，请按原件分期核对后手工制证并关联")
	if not rule.get("accrual_account"):
		return blocked("请先在该公司社保规则中设置计提及支付使用的应付科目")
	breakdown = []
	company_total = personal_total = Decimal("0.00")
	for detail in details:
		amount = money(detail["amount"])
		percent = Decimal(str(rule.get(SOCIAL_ITEMS[detail["item"]]) or 0))
		if not percent.is_finite() or not 0 <= percent <= 100:
			frappe.throw(_("社保公司承担比例必须在 0 到 100 之间"))
		company_amount = (amount * percent / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
		personal_amount = amount - company_amount
		company_total += company_amount
		personal_total += personal_amount
		breakdown.append(
			{
				**detail,
				"company_percent": str(percent),
				"company_amount": str(company_amount),
				"personal_amount": str(personal_amount),
			}
		)
	result = {
		"account": rule.account,
		"rule": rule.name,
		"rule_type": "社保分摊",
		"reason": f"所属时期 {period['month']}，生成计提三行、支付两行；须确认该所属期尚未计提，已计提时应手工冲应付科目后关联。",
		"social_period": period["month"],
		"voucher_summary": f"计提并支付{period['label']}社保",
		"bank_summary": f"支付{period['label']}社保",
		"breakdown": breakdown,
		"allocations": [
			{"account": rule.account, "amount": str(company_total)},
			{"account": rule.personal_account, "amount": str(personal_total)},
		],
		"journal_lines": [
			{
				"account": rule.account,
				"debit": str(company_total),
				"credit": "0.00",
				"summary": f"计提{period['label']}公司部分社保",
			},
			{
				"account": rule.personal_account,
				"debit": str(personal_total),
				"credit": "0.00",
				"summary": f"计提{period['label']}个人部分社保",
			},
			{
				"account": rule.accrual_account,
				"debit": "0.00",
				"credit": str(money(data["amount"])),
				"summary": f"计提{period['label']}社保",
			},
			{
				"account": rule.accrual_account,
				"debit": str(money(data["amount"])),
				"credit": "0.00",
				"summary": f"支付{period['label']}社保",
			},
		],
	}
	result["decision_hash"] = hashlib.sha256(
		json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
	).hexdigest()
	return result


def housing_fund_suggestion(rule, data):
	"""Split a housing-fund receipt into accrual and payment entries."""

	def blocked(reason):
		return {"account": None, "rule_type": "公积金分摊", "blocked": True, "reason": reason}

	details = data.get("tax_details") or []
	if data["direction"] != "支出" or not is_housing_fund_receipt(data):
		return blocked("公积金分摊只支持明细全部为住房公积金的支出回单，请核对税费项目")
	if details and sum(money(detail["amount"]) for detail in details) != money(data["amount"]):
		frappe.throw(_("公积金明细合计与银行付款不一致"))
	period = housing_fund_period(data)
	if not period:
		return blocked("公积金所属时期缺失、无效或涉及多个月份，请按原件分期核对后手工制证并关联")
	if not rule.get("account") or not rule.get("personal_account") or not rule.get("accrual_account"):
		return blocked("请先在该公司公积金规则中设置公司承担、个人承担及计提支付科目")
	percent_value = rule.get("housing_fund_company_percent")
	if percent_value is None:
		return blocked("请先在该公司公积金规则中设置公司承担比例")
	try:
		percent = Decimal(str(percent_value))
	except InvalidOperation:
		frappe.throw(_("公积金公司承担比例必须是有效数字"))
	if not percent.is_finite() or not 0 <= percent <= 100:
		frappe.throw(_("公积金公司承担比例必须在 0 到 100 之间"))

	breakdown = []
	company_total = personal_total = Decimal("0.00")
	allocation_details = details or [
		{
			"item": "住房公积金",
			"amount": str(money(data["amount"])),
			"period_from": None,
			"period_to": None,
			"period_source": "交易日期",
		}
	]
	for detail in allocation_details:
		amount = money(detail["amount"])
		company_amount = (amount * percent / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
		personal_amount = amount - company_amount
		company_total += company_amount
		personal_total += personal_amount
		breakdown.append(
			{
				**detail,
				"company_percent": str(percent),
				"company_amount": str(company_amount),
				"personal_amount": str(personal_amount),
			}
		)

	total = money(data["amount"])
	accrual_summary = f"计提{period['label']}公积金"
	payment_summary = f"支付{period['label']}公积金"
	result = {
		"account": rule.account,
		"rule": rule.name,
		"rule_type": "公积金分摊",
		"reason": (
			(
				f"回单未提供所属时期，按交易日期确定为 {period['month']}；"
				if period.get("inferred_from_transaction_date")
				else f"所属时期 {period['month']}，"
			)
			+ "生成计提三行、支付两行；须确认该所属期尚未计提，已计提时应手工冲应付科目后关联。"
		),
		"coverage_period": period["month"],
		"voucher_summary": f"计提并支付{period['label']}公积金",
		"bank_summary": payment_summary,
		"breakdown": breakdown,
		"allocations": [
			{"account": rule.account, "amount": str(company_total)},
			{"account": rule.personal_account, "amount": str(personal_total)},
		],
		"journal_lines": [
			{
				"account": rule.account,
				"debit": str(company_total),
				"credit": "0.00",
				"summary": accrual_summary,
			},
			{
				"account": rule.personal_account,
				"debit": str(personal_total),
				"credit": "0.00",
				"summary": accrual_summary,
			},
			{
				"account": rule.accrual_account,
				"debit": "0.00",
				"credit": str(total),
				"summary": accrual_summary,
			},
			{
				"account": rule.accrual_account,
				"debit": str(total),
				"credit": "0.00",
				"summary": payment_summary,
			},
		],
	}
	result["decision_hash"] = hashlib.sha256(
		json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
	).hexdigest()
	return result
