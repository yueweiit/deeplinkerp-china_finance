"""Exact opening amounts and evidence comparisons, without database side effects."""

import hashlib
import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

AMOUNTS = ("debit", "credit", "base_debit", "base_credit")
IDENTITY = ("account", "account_currency", "party_type", "party")
MAX_AMOUNT = Decimal("1000000000000")


def decimal(value):
	try:
		result = Decimal(str(value if value not in (None, "") else 0))
	except (InvalidOperation, ValueError, TypeError):
		raise ValueError("金额必须是有效数字")
	if not result.is_finite() or abs(result) >= MAX_AMOUNT:
		raise ValueError("金额超出范围或不是有限数字")
	return result


def amount(value, precision=2):
	result = decimal(value)
	quantum = Decimal(1).scaleb(-precision)
	if result < 0 or result != result.quantize(quantum):
		raise ValueError("金额不得为负数，且最多允许 {0} 位小数".format(precision))
	# Native Frappe Currency/Float fields persist through flt(). Validate
	# its shortest decimal round trip before storage, never silently round.
	if Decimal(str(float(result))) != result:
		raise ValueError("金额有效位过多，超出原生字段可无损保存的精度范围")
	return result


def formatted(value, precision=2):
	return format(value, ".{0}f".format(precision))


def dimensions(row):
	value = row.get("dimensions_json") or {}
	try:
		value = json.loads(value) if isinstance(value, str) else value
	except (ValueError, TypeError):
		raise ValueError("会计维度必须是 JSON 对象")
	if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, (str, type(None))) for k, v in value.items()):
		raise ValueError("会计维度必须是字段名与记录名称的对象")
	return {key: value for key, value in value.items() if value}


def row_key(row):
	return (*[row.get(key) or "" for key in IDENTITY], json.dumps(dimensions(row), sort_keys=True, ensure_ascii=False))


def validate_amounts(row, currency, precision=2, account_precision=2):
	errors = []
	try:
		debit, credit = (amount(row.get(key), account_precision) for key in AMOUNTS[:2])
		base_debit, base_credit = (amount(row.get(key), precision) for key in AMOUNTS[2:])
		rate = amount(row.get("exchange_rate"), 9)
		if rate <= 0:
			raise ValueError("汇率必须大于零")
		if (debit and credit) or (base_debit and base_credit):
			raise ValueError("同一行借贷金额不能同时非零")
		if row.get("account_currency") == currency and rate != 1:
			raise ValueError("本位币科目汇率必须为 1")
		quantum = Decimal(1).scaleb(-precision)
		for source, base in ((debit, base_debit), (credit, base_credit)):
			if (source * rate).quantize(quantum, rounding=ROUND_HALF_UP) != base:
				raise ValueError("本位币金额与原币金额乘汇率不一致")
	except ValueError as exc:
		errors.append(str(exc))
	return errors


def totals(rows, precision=2):
	debit = sum((decimal(row.get("base_debit")) for row in rows), Decimal(0))
	credit = sum((decimal(row.get("base_credit")) for row in rows), Decimal(0))
	return {"debit": formatted(debit, precision), "credit": formatted(credit, precision), "difference": formatted(debit - credit, precision)}


def accounting_totals(rows):
	result = defaultdict(lambda: [Decimal(0)] * 4)
	for row in rows:
		if not row.get("account") or not row.get("account_currency"):
			raise ValueError("分录缺少科目或币种")
		key = row_key(row)
		for index, field in enumerate(AMOUNTS):
			result[key][index] += decimal(row.get(field))
	return {key: tuple(values) for key, values in result.items() if any(values)}


def historical_rows_match(actual, expected):
	def signature(rows):
		return sorted((row_key(row), tuple(decimal(row.get(field)) for field in AMOUNTS), decimal(row.get("exchange_rate") or 1), row.get("remarks") or "") for row in rows)

	return signature(actual) == signature(expected)


def delta_rows(target, previous, precision=2):
	"""Full targets yield one signed net change per accounting identity.

	Changing a currency rate with zero currency movement cannot be represented by
	an honest opening JE. Such exchange-only changes require a separate process.
	"""
	buckets = defaultdict(lambda: [Decimal(0), Decimal(0)])
	examples = {}
	for sign, rows in ((1, target), (-1, previous)):
		for row in rows:
			key = row_key(row)
			examples.setdefault(key, row)
			buckets[key][0] += sign * (decimal(row.get("debit")) - decimal(row.get("credit")))
			buckets[key][1] += sign * (decimal(row.get("base_debit")) - decimal(row.get("base_credit")))
	result = []
	for key, (native, base) in sorted(buckets.items()):
		if not native and not base:
			continue
		if not native or not base or native * base < 0:
			raise ValueError("调整差额的原币与本位币方向不一致，或仅有汇兑差额；请另行处理后重试")
		rate = (abs(base) / abs(native)).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
		row = {field: examples[key].get(field) for field in (*IDENTITY, "dimensions_json", "remarks")}
		row.update({
			"row_id": "delta-" + str(len(result) + 1),
			"debit": str(abs(native)) if native > 0 else "0",
			"credit": str(abs(native)) if native < 0 else "0",
			"base_debit": formatted(abs(base), precision) if base > 0 else formatted(Decimal(0), precision),
			"base_credit": formatted(abs(base), precision) if base < 0 else formatted(Decimal(0), precision),
			"exchange_rate": str(rate),
		})
		result.append(row)
	return result


def fingerprint(payload):
	return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False).encode()).hexdigest()


def validate_source_identity(source, gl_rows, company, opening_date):
	errors = []
	if source.get("docstatus") != 1 or source.get("is_opening") != "Yes":
		errors.append("来源必须是原生已提交且标记为期初的 Journal Entry")
	if source.get("company") != company or str(source.get("posting_date")) != str(opening_date):
		errors.append("来源公司或期初日期不一致")
	if not gl_rows:
		errors.append("来源没有可信总账分录")
	for row in gl_rows:
		if (
			row.get("voucher_type") != "Journal Entry" or row.get("voucher_no") != source.get("name")
			or row.get("company") != company or str(row.get("posting_date")) != str(opening_date)
			or row.get("is_opening") != "Yes" or row.get("is_cancelled")
		):
			errors.append("来源总账含非期初、已取消或公司日期不一致的分录")
			break
	return errors
