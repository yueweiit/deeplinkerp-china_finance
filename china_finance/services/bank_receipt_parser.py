"""Local, strict parser for CMB text receipts. No OCR or network service."""

try:
	from frappe import _
except ImportError:
	def _(message):
		return message


import importlib
import io
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

PARSER_VERSION = "cmb-text-v3"
TITLE = re.compile(r"[出入]\s*账\s*回\s*单")
FEE_TITLE = re.compile(r"对\s*公\s*业\s*务\s*收\s*费\s*回\s*单")
CMB_TITLE = re.compile(rf"(?:{TITLE.pattern}|{FEE_TITLE.pattern})")
MONEY_TEXT = r"(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}"

# Keep bank-specific formats behind a small registry.  A parser is selected
# from the Bank Account's bank name; adding another bank must not change the
# common import and accounting workflow.
PARSER_REGISTRY = {
	"cmb_text_pdf_v1": {
		"bank_keywords": ("招商银行", "招商"),
		"currency": "CNY",
		"parser": "china_finance.services.bank_receipt_parser.parse_cmb_receipts",
		"parser_version": PARSER_VERSION,
		"label": "招商银行文字版回单 PDF",
	},
	"boc_text_pdf_v1": {
		"bank_keywords": ("中国银行",),
		"currency": "CNY",
		"parser": "china_finance.services.bank_receipt_parsers_cn.parse_boc_receipts",
		"parser_version": "boc-text-v1",
		"label": "中国银行文字版回单 PDF",
	},
	"abc_text_pdf_v1": {
		"bank_keywords": ("中国农业银行", "农业银行"),
		"currency": "CNY",
		"parser": "china_finance.services.bank_receipt_parsers_cn.parse_abc_receipts",
		"parser_version": "abc-text-v1",
		"label": "中国农业银行文字版回单 PDF",
	},
	"icbc_text_pdf_v1": {
		"bank_keywords": ("中国工商银行", "工商银行"),
		"currency": "CNY",
		"parser": "china_finance.services.bank_receipt_parsers_cn.parse_icbc_receipts",
		"parser_version": "icbc-text-v1",
		"label": "中国工商银行文字版回单 PDF",
	},
	"cgb_text_pdf_v1": {
		"bank_keywords": ("广发银行", "广东发展银行"),
		"currency": "CNY",
		"parser": "china_finance.services.bank_receipt_parsers_cn.parse_cgb_receipts",
		"parser_version": "cgb-text-v1",
		"label": "广发银行文字版回单 PDF",
	},
}


class ReceiptParseError(ValueError):
	pass


def get_receipt_parser(bank_name):
	"""Return the configured parser for a bank name, or ``None``.

	The callable is resolved at lookup time so tests and integrations can
	replace ``parse_cmb_receipts`` without leaving a stale function reference in
	the registry.
	"""
	bank_name = str(bank_name or "").strip()
	for parser_version, config in PARSER_REGISTRY.items():
		if any(keyword in bank_name for keyword in config["bank_keywords"]):
			module_name, function_name = config["parser"].rsplit(".", 1)
			parser = getattr(importlib.import_module(module_name), function_name, None)
			if not callable(parser):
				raise ReceiptParseError(_("银行回单解析器配置不完整：{0}").format(parser_version))
			return {
				**config,
				"label": _(config["label"]),
				"parser_version": config.get("parser_version", parser_version),
				"parser_key": parser_version,
				"parse": parser,
			}
	return None


def money(value):
	try:
		amount = Decimal(str(value).replace(",", "").strip())
	except InvalidOperation as exc:
		raise ReceiptParseError(_("金额无法识别")) from exc
	if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
		raise ReceiptParseError(_("金额必须是有效的非负数，最多两位小数"))
	return amount


def _parse_fee_line(line):
	"""Read every item/count/amount cell, including side-by-side fee columns.

	Require the complete line to be consumed so an incomplete second column
	cannot silently disappear. Reject amounts embedded in an item name rather
	than swallowing a malformed first column into the next item's name.
	"""
	details, position = [], 0
	for match in re.finditer(r"(.+?)\s+(\d+)\s+((?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2})(?=\s|$)", line):
		item = match[1].strip()
		if line[position:match.start()].strip() or re.search(
			r"(?:^|\s)[+-]?[\d,.]+\s+(?:[A-Z]{3}|[￥¥])?\s*[+-]?[\d,.]+(?=\s|$)", item
		):
			return []
		details.append({"item": item, "count": int(match[2]), "amount": str(money(match[3]))})
		position = match.end()
	return [] if line[position:].strip() else details


def parse_receipt_text(text):
	"""Parse one receipt; reject missing/ambiguous financial identity fields."""
	text = text.replace("：", ":").replace("（", "(").replace("）", ")")

	def field(label, required=False):
		matches = re.findall(
			r"(?:^|\s)" + re.escape(label) + r":([^\n]*?)(?=[^\S\n]+[^\s:]+:|\n|$)", text
		)
		value = matches[0].strip() if len(matches) == 1 else ""
		if len(matches) > 1 or (required and not value):
			raise ReceiptParseError(_("缺少或重复的字段：{0}").format(label))
		return value

	title = TITLE.search(text)
	if not title or len(TITLE.findall(text)) != 1:
		raise ReceiptParseError(_("未找到独立的出账/入账回单标题"))
	direction = "支出" if title.group().startswith("出") else "收入"
	d = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", field("交易日期", True))
	if not d:
		raise ReceiptParseError(_("交易日期格式不支持"))
	try:
		posting_date = date(*map(int, d.groups())).isoformat()
	except ValueError as exc:
		raise ReceiptParseError(_("交易日期无效")) from exc
	business_type = field("业务类型", True)
	# These two CMB outbound layouts omit both '(小写)' and the summary.
	# Stars are leading amount padding, never permission to infer masked digits.
	cross_border = direction == "支出" and business_type in ("汇款接单(企业银行)", "嵌套购付汇")
	plain_amount = field("交易金额")
	amount_text = field("交易金额(小写)", not (cross_border and plain_amount))
	if plain_amount and amount_text:
		raise ReceiptParseError(_("缺少或重复的字段：{0}").format("交易金额"))
	new_layout = cross_border and bool(plain_amount)
	m = re.fullmatch(
		(r"(CNY)\s*\**(" + MONEY_TEXT + ")") if new_layout
		else (r"(CNY|￥|¥)\s*(" + MONEY_TEXT + ")"),
		plain_amount if new_layout else amount_text,
	)
	if not m:
		raise ReceiptParseError(_("第一期仅支持明确标注的人民币金额"))
	amount = money(m[2])
	if amount <= 0:
		raise ReceiptParseError(_("交易金额必须大于零"))
	flow = field("交易流水", True)
	if not re.fullmatch(r"[A-Za-z0-9-]{5,140}", flow):
		raise ReceiptParseError(_("交易流水号无效"))
	receipt_number = field("回单编号", True).split()[0]
	payer_account, payee_account = field("付款账号"), field("收款账号")
	payer, payee = field("付款人"), field("收款人")
	own_account = payer_account if direction == "支出" else payee_account
	if not re.fullmatch(r"\d{8,32}", own_account):
		raise ReceiptParseError(_("本方账号缺失、被掩码或格式不支持"))
	fees, taxes = [], []
	for line in text.splitlines():
		tax = re.fullmatch(r"(.+?)\s+(\d{8})--(\d{8})\s+[￥¥]?([\d,]+\.\d{2})", line.strip())
		if tax:
			taxes.append(
				{"item": tax[1], "period_from": tax[2], "period_to": tax[3], "amount": str(money(tax[4]))}
			)
		if "收费项目" in text:
			fees.extend(_parse_fee_line(line.strip()))
	if "税(费)种名称" in text and not taxes:
		raise ReceiptParseError(_("税费明细无法完整识别"))
	if "收费项目" in text and not fees:
		raise ReceiptParseError(_("收费明细无法完整识别"))
	for details in (fees, taxes):
		if details and sum(money(item["amount"]) for item in details) != amount:
			raise ReceiptParseError(_("回单明细合计与交易金额不一致"))
	metadata = {}
	if new_layout and business_type == "汇款接单(企业银行)":
		if not fees:
			raise ReceiptParseError(_("收费明细无法完整识别"))
		underlying = re.fullmatch(r"([A-Z]{3})\s*(" + MONEY_TEXT + ")", field("业务金额", True))
		if not underlying:
			raise ReceiptParseError(_("缺少或重复的字段：{0}").format("业务金额"))
		metadata["business_amount"] = {"currency": underlying[1], "amount": str(money(underlying[2]))}
	elif new_layout and business_type == "嵌套购付汇":
		sold = re.fullmatch(r"CNY\s*(" + MONEY_TEXT + ")", field("卖出币种/金额", True))
		bought = re.fullmatch(
			r"([A-Z]{3})\s*(" + MONEY_TEXT + r")\s*\(即付款金额\)", field("买入币种/金额", True)
		)
		if not sold or not bought or bought[1] == "CNY" or money(bought[2]) <= 0:
			raise ReceiptParseError(_("缺少或重复的字段：{0}").format("买卖币种/金额"))
		if money(sold[1]) != amount:
			raise ReceiptParseError(_("回单明细合计与交易金额不一致"))
		metadata["foreign_exchange"] = {
			"sold_currency": "CNY", "sold_amount": str(money(sold[1])),
			"bought_currency": bought[1], "bought_amount": str(money(bought[2])),
			# CMB quotes may be per 100 foreign units. Preserve, never recalculate.
			"quoted_rate": field("成交汇率", True),
		}
	return {
		"posting_date": posting_date,
		"direction": direction,
		"currency": "CNY",
		"amount": str(amount),
		"transaction_id": flow,
		"receipt_number": receipt_number,
		"business_type": business_type,
		"business_number": field("业务编号"),
		"summary": field("交易摘要", not new_layout) or business_type,
		"business_reference": field("业务参考号") or field("参考号"),
		"postscript": field("附言"),
		"taxpayer_id": field("纳税人识别号"),
		"taxpayer_name": field("纳税人全称"),
		"own_account": own_account,
		"own_name": payer if direction == "支出" else payee,
		"counterparty": payee if direction == "支出" else payer,
		"counterparty_account": payee_account if direction == "支出" else payer_account,
		"payer": payer,
		"payer_account": payer_account,
		"payer_bank": field("付款开户行"),
		"payee": payee,
		"payee_account": payee_account,
		"payee_bank": field("收款开户行"),
		"fee_period": field("收费时段"),
		"tax_number": field("税票号码"),
		"fee_details": fees,
		"tax_details": taxes,
		"raw_text": text,
		**metadata,
	}


def parse_fee_receipt_text(text):
	"""Parse CMB's standalone corporate-service fee receipt layout."""
	text = unicodedata.normalize("NFKC", text or "")
	text = text.replace("：", ":").replace("（", "(").replace("）", ")")

	def field(label, required=False):
		matches = re.findall(
			r"(?:^|\s)" + re.escape(label) + r":([^\n]*?)(?=[^\S\n]+[^\s:]+:|\n|$)",
			text,
		)
		value = matches[0].strip() if len(matches) == 1 else ""
		if len(matches) > 1 or (required and not value):
			raise ReceiptParseError(_("缺少或重复的字段：{0}").format(label))
		return value

	if len(FEE_TITLE.findall(text)) != 1:
		raise ReceiptParseError(_("未识别到文字版回单；扫描件、非回单页面或其他版式暂不支持"))

	d = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", field("收费日期", True))
	if not d:
		raise ReceiptParseError(_("交易日期格式不支持"))
	try:
		posting_date = date(*map(int, d.groups())).isoformat()
	except ValueError as exc:
		raise ReceiptParseError(_("交易日期无效")) from exc
	charge_method = field("收费方式", True)
	if charge_method != "转账":
		raise ReceiptParseError(_("对公业务收费回单仅支持收费方式为“转账”"))
	business_type = field("业务类型", True)
	if business_type != "对公业务收费":
		raise ReceiptParseError(_("对公业务收费回单的业务类型不支持：{0}").format(business_type))

	amount_match = re.fullmatch(r"CNY\s*(" + MONEY_TEXT + ")", field("交易金额(小写)", True))
	if not amount_match:
		raise ReceiptParseError(_("第一期仅支持明确标注的人民币金额"))
	amount = money(amount_match[1])
	if amount <= 0:
		raise ReceiptParseError(_("交易金额必须大于零"))

	flow = field("交易流水", True)
	if not re.fullmatch(r"[A-Za-z0-9-]{5,140}", flow):
		raise ReceiptParseError(_("交易流水号无效"))
	account = re.sub(r"\s+", "", field("客户账号", True))
	if not re.fullmatch(r"\d{8,32}", account):
		raise ReceiptParseError(_("本方账号缺失、被掩码或格式不支持"))
	customer_name = field("客户名称", True)

	fees = []
	for line in text.splitlines():
		fees.extend(_parse_fee_line(line.strip()))
	if not fees:
		raise ReceiptParseError(_("收费明细无法完整识别"))
	if sum(money(item["amount"]) for item in fees) != amount:
		raise ReceiptParseError(_("回单明细合计与交易金额不一致"))

	source_summary = field("交易摘要", True)
	business_number_match = re.fullmatch(r"业务编号\s+([^\s]+)", source_summary)
	return {
		"posting_date": posting_date,
		"direction": "支出",
		"currency": "CNY",
		"amount": str(amount),
		"transaction_id": flow,
		"receipt_number": field("回单编号", True).split()[0],
		"business_type": business_type,
		"business_number": business_number_match[1] if business_number_match else "",
		"summary": source_summary,
		"business_reference": "",
		"postscript": "",
		"taxpayer_id": "",
		"taxpayer_name": "",
		"own_account": account,
		"own_name": customer_name,
		"counterparty": "",
		"counterparty_account": "",
		"payer": customer_name,
		"payer_account": account,
		"payer_bank": field("开户行"),
		"payee": "",
		"payee_account": "",
		"payee_bank": "",
		"fee_period": "",
		"tax_number": "",
		"fee_details": fees,
		"tax_details": [],
		"raw_text": text,
	}


def parse_cmb_receipts(content):
	import pdfplumber

	if not content.startswith(b"%PDF-") or len(content) > 20 * 1024 * 1024:
		raise ReceiptParseError(_("请上传不超过 20 MB 的 PDF 回单"))
	rows, errors = [], []
	try:
		with pdfplumber.open(io.BytesIO(content)) as pdf:
			if not 1 <= len(pdf.pages) <= 100:
				raise ReceiptParseError(_("单次支持 1—100 页回单"))
			for page_number, page in enumerate(pdf.pages, 1):
				headings = page.search(CMB_TITLE)
				if not headings:
					errors.append(
						{
							"page": page_number,
							"message": _("未识别到文字版回单；扫描件、非回单页面或其他版式暂不支持"),
						}
					)
					continue
				for index, heading in enumerate(headings):
					if len(rows) >= 300:
						raise ReceiptParseError(_("单次最多支持 300 张回单"))
					bottom = headings[index + 1]["top"] if index + 1 < len(headings) else page.height
					bbox = (
						0,
						max(0, heading["top"] - 2),
						page.width,
						bottom - 2 if index + 1 < len(headings) else bottom,
					)
					try:
						text = page.crop(bbox).extract_text(x_tolerance=2, y_tolerance=2) or ""
						parser = parse_fee_receipt_text if FEE_TITLE.search(text) else parse_receipt_text
						row = parser(text)
						row.update(page=page_number, position=index + 1, bbox=list(bbox))
						rows.append(row)
					except ReceiptParseError as exc:
						errors.append({"page": page_number, "position": index + 1, "message": str(exc)})
	except ReceiptParseError:
		raise
	except Exception as exc:
		raise ReceiptParseError(_("无法读取 PDF，请确认文件未加密、未损坏且包含文字层")) from exc
	return {"rows": rows, "errors": errors, "parser_version": PARSER_VERSION}
