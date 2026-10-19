"""Strict text-PDF receipt parsers for supported Chinese banks."""

try:
	from frappe import _
except ImportError:
	def _(message):
		return message


import io
import logging
import re
import unicodedata
from datetime import date

from china_finance.services.bank_receipt_parser import ReceiptParseError, money

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_PAGES = 100
MAX_RECEIPTS = 300

BOC_TITLE = re.compile(r"国内支付业务[收付]款回单|客户付费回单")
ABC_TITLE = re.compile(r"客户收付款入账通知")
ICBC_HEADING = re.compile(r"电.回单号码")
CGB_TITLE = re.compile(r"广发银行客户回单")

CHAR_TRANSLATION = str.maketrans({"戶": "户", "⺠": "民", "⻆": "角", "―": "-"})

# Some ICBC/CGB embedded fonts omit FontBBox metadata. Text extraction remains
# valid, so suppress pdfminer's per-character warning flood.
logging.getLogger("pdfminer.pdffont").setLevel(logging.ERROR)


def normalize_text(text):
	return unicodedata.normalize("NFKC", text or "").translate(CHAR_TRANSLATION)


def compact_account(value):
	return re.sub(r"\s+", "", value or "")


def required_match(pattern, text, message, flags=0, group=1):
	match = re.search(pattern, text, flags)
	if not match:
		raise ReceiptParseError(_(message))
	return match.group(group).strip()


def optional_match(pattern, text, flags=0, group=1):
	match = re.search(pattern, text, flags)
	return match.group(group).strip() if match else ""


def iso_date(year, month, day):
	try:
		return date(int(year), int(month), int(day)).isoformat()
	except ValueError as exc:
		raise ReceiptParseError(_("交易日期无效")) from exc


def decimal_amount(value):
	amount = money(value)
	if amount <= 0:
		raise ReceiptParseError(_("交易金额必须大于零"))
	return str(amount)


def colon_field(text, label):
	return optional_match(
		r"(?:^|[ \t])"
		+ re.escape(label)
		+ r":[ \t]*([^\n]*?)(?=[ \t]+[^\s:]+:|\n|$)",
		text,
		re.M,
	)


def line_field(text, label):
	return optional_match(r"^" + re.escape(label) + r"[ \t]*(.*)$", text, re.M)


def validate_flow(value):
	value = compact_account(value)
	if not re.fullmatch(r"[A-Za-z0-9-]{5,140}", value):
		raise ReceiptParseError(_("交易流水号无效"))
	return value


def receipt_row(**values):
	defaults = {
		"business_number": "",
		"business_reference": "",
		"postscript": "",
		"taxpayer_id": "",
		"taxpayer_name": "",
		"own_account": "",
		"own_name": "",
		"counterparty": "",
		"counterparty_account": "",
		"payer": "",
		"payer_account": "",
		"payer_bank": "",
		"payee": "",
		"payee_account": "",
		"payee_bank": "",
		"fee_period": "",
		"tax_number": "",
		"fee_details": [],
		"tax_details": [],
	}
	return {**defaults, **values}


def parse_boc_receipt_text(text):
	text = normalize_text(text)
	title = required_match(r"^(国内支付业务收款回单|国内支付业务付款回单|客户付费回单)", text, "未识别到中国银行回单标题")
	direction = "收入" if title == "国内支付业务收款回单" else "支出"
	date_match = re.search(r"日期:\s*(\d{4})年(\d{1,2})月(\d{1,2})日", text)
	if not date_match:
		raise ReceiptParseError(_("缺少中国银行交易日期"))
	posting_date = iso_date(*date_match.groups())
	amount = decimal_amount(required_match(r"金额:\s*CNY\s*([\d,]+\.\d{2})", text, "缺少中国银行人民币金额"))
	flow = validate_flow(required_match(r"交易流水号:\s*([A-Za-z0-9-]+)", text, "缺少中国银行交易流水号"))
	receipt_number = required_match(r"回单编号:\s*([A-Za-z0-9-]+)", text, "缺少中国银行回单编号")
	payer = colon_field(text, "付款人名称")
	payee = colon_field(text, "收款人名称")
	payer_account = compact_account(colon_field(text, "付款人账号"))
	payee_account = compact_account(colon_field(text, "收款人账号"))
	payer_bank = colon_field(text, "付款人开户行")
	payee_bank = colon_field(text, "收款人开户行")
	business_type = colon_field(text, "业务类型") or colon_field(text, "业务种类") or title
	fee_name = colon_field(text, "费用名称")
	taxes = []
	for item, fy, fm, fd, ty, tm, td, detail_amount in re.findall(
		r"^(.+?)\s+(\d{4})/(\d{2})/(\d{2})-(\d{4})/(\d{2})/(\d{2})\s+CNY([\d,]+\.\d{2})$",
		text,
		re.M,
	):
		taxes.append(
			{
				"item": item.strip(),
				"period_from": f"{fy}{fm}{fd}",
				"period_to": f"{ty}{tm}{td}",
				"amount": decimal_amount(detail_amount),
			}
		)
	if "税(费)种名称" in text and not taxes:
		raise ReceiptParseError(_("中国银行税费明细无法完整识别"))
	if taxes and sum(money(item["amount"]) for item in taxes) != money(amount):
		raise ReceiptParseError(_("中国银行税费明细合计与交易金额不一致"))
	fees = [{"item": fee_name, "count": 1, "amount": amount}] if fee_name else []
	purpose = colon_field(text, "用途")
	postscript = colon_field(text, "附言")
	remark = colon_field(text, "备注")
	summary = fee_name or purpose or postscript or remark or business_type
	if "住房公积" in payee:
		summary = "住房公积金"
	if not summary:
		raise ReceiptParseError(_("缺少中国银行交易摘要"))
	own_account = payer_account if direction == "支出" else payee_account
	own_name = payer if direction == "支出" else payee
	counterparty = payee if direction == "支出" else payer
	counterparty_account = payee_account if direction == "支出" else payer_account
	return receipt_row(
		posting_date=posting_date,
		direction=direction,
		currency="CNY",
		amount=amount,
		transaction_id=flow,
		receipt_number=receipt_number,
		business_type=business_type,
		business_number=colon_field(text, "业务编号"),
		business_reference=colon_field(text, "业务标识号") or colon_field(text, "缴款书交易流水号"),
		summary=summary,
		postscript=postscript,
		taxpayer_id=colon_field(text, "纳税人识别号"),
		taxpayer_name=colon_field(text, "纳税人全称"),
		own_account=own_account,
		own_name=own_name,
		counterparty=counterparty,
		counterparty_account=counterparty_account,
		payer=payer,
		payer_account=payer_account,
		payer_bank=payer_bank,
		payee=payee,
		payee_account=payee_account,
		payee_bank=payee_bank,
		tax_number=colon_field(text, "税票号码"),
		fee_details=fees,
		tax_details=taxes,
		raw_text=text,
	)


def parse_abc_receipt_text(text):
	text = normalize_text(text)
	if "客户收付款入账通知" not in text:
		raise ReceiptParseError(_("未识别到中国农业银行回单标题"))
	receipt_number = required_match(r"回单编号:\s*([A-Za-z0-9-]+)", text, "缺少农业银行回单编号")
	accounts = re.search(r"^账号\s+([0-9 ]+)\s+账号\s+([0-9 ]+)$", text, re.M)
	if not accounts:
		raise ReceiptParseError(_("缺少农业银行收付款账号"))
	names = re.search(r"付款方\s+户名\s*(.*?)\s+收款方\s+户名\s*([^\n]*)", text)
	banks = re.search(r"开户行\s+(.+?)\s+开户行\s*([^\n]*)", text)
	payer = names.group(1).strip() if names else ""
	payee = names.group(2).strip() if names else ""
	payer_bank = banks.group(1).strip() if banks else ""
	payee_bank = banks.group(2).strip() if banks else ""
	datetime_match = re.search(r"交易时间\s+(\d{4})-(\d{2})-(\d{2})\s+\d{2}:\d{2}:\d{2}", text)
	if not datetime_match:
		raise ReceiptParseError(_("缺少农业银行交易时间"))
	posting_date = iso_date(*datetime_match.groups())
	amount = decimal_amount(required_match(r"金额\(小写\)\s+([\d,]+\.\d{2})", text, "缺少农业银行金额"))
	if not re.search(r"币种\s+人民币", text):
		raise ReceiptParseError(_("农业银行回单币种不是人民币"))
	summary = optional_match(r"摘要\s+(.+?)\s+凭证号", text)
	lines = [line.strip() for line in text.splitlines()]
	for candidate in ("短信费", "手续费", "服务费"):
		if candidate in lines:
			summary = candidate
			break
	if not summary:
		raise ReceiptParseError(_("缺少农业银行交易摘要"))
	fee_details = (
		[{"item": summary, "count": 1, "amount": amount}]
		if any(word in summary for word in ("费", "手续费"))
		else []
	)
	return receipt_row(
		posting_date=posting_date,
		direction="",
		currency="CNY",
		amount=amount,
		transaction_id=validate_flow(receipt_number),
		receipt_number=receipt_number,
		business_type="客户收付款入账通知",
		business_number=compact_account(optional_match(r"凭证号\s+([A-Za-z0-9-]+)", text)),
		summary=summary,
		payer=payer,
		payer_account=compact_account(accounts.group(1)),
		payer_bank=payer_bank,
		payee=payee,
		payee_account=compact_account(accounts.group(2)),
		payee_bank=payee_bank,
		fee_details=fee_details,
		raw_text=text,
	)


def parse_icbc_receipt_text(text):
	text = normalize_text(text)
	receipt_number = required_match(r"电子回单号码:\s*([A-Za-z0-9-]+)", text, "缺少工商银行电子回单号码")
	flow = validate_flow(required_match(r"交易流水号\s+([0-9 ]+?)\s+时间戳", text, "缺少工商银行交易流水号"))
	timestamp = re.search(r"时间戳\s+(\d{4})-(\d{2})-(\d{2})-", text)
	if not timestamp:
		raise ReceiptParseError(_("缺少工商银行交易时间"))
	posting_date = iso_date(*timestamp.groups())
	amount = decimal_amount(required_match(r"金\s*额\s+[¥￥]\s*([\d,]+\.\d{2})元", text, "缺少工商银行人民币金额"))
	accounts = re.search(r"付款\s+([0-9 ]+)\s+收款\s+([0-9* ]+)", text)
	if not accounts:
		raise ReceiptParseError(_("缺少工商银行收付款账号"))
	names_block = required_match(r"电子回单号码:[^\n]+\n(.+?)\n户\s*名", text, "缺少工商银行收付款户名")
	names = names_block.split()
	payer = names[0] if names else ""
	payee = "".join(names[1:]) if len(names) > 1 else ""
	summary_match = re.search(r"摘\s*要\s+(.+?)\s+业务\(产品\)种类\s+([^\n]+)", text)
	if not summary_match:
		raise ReceiptParseError(_("缺少工商银行摘要或业务种类"))
	summary, business_type = (value.strip() for value in summary_match.groups())
	purpose = optional_match(r"^用[ \t]*途[ \t]*(.*)$", text, re.M)
	remark = colon_field(text, "备注")
	postscript = colon_field(text, "附言") or colon_field(text, "客户附言")
	if purpose:
		summary = purpose
	if "住房公积" in payee:
		summary = "住房公积金"
	elif not summary or summary.isdigit():
		summary = "住房公积金" if "住房公积金" in text else (remark or business_type)
	if "住房公积" in summary:
		summary = "住房公积金"
	fees = (
		[{"item": summary, "count": 1, "amount": amount}]
		if business_type == "对公收费" or any(word in summary for word in ("手续费", "汇款费"))
		else []
	)
	return receipt_row(
		posting_date=posting_date,
		direction="",
		currency="CNY",
		amount=amount,
		transaction_id=flow,
		receipt_number=receipt_number,
		business_type=business_type,
		business_number=optional_match(r"指令编号:\s*([A-Za-z0-9-]+)", text),
		summary=summary,
		postscript=postscript,
		payer=payer,
		payer_account=compact_account(accounts.group(1)),
		payee=payee,
		payee_account=compact_account(accounts.group(2)),
		fee_details=fees,
		raw_text=text,
	)


def parse_cgb_receipt_text(text):
	text = normalize_text(text)
	if not text.startswith("广发银行客户回单"):
		raise ReceiptParseError(_("未识别到广发银行回单标题"))
	header = re.search(
		r"交易日期:\s*(\d{4})(\d{2})(\d{2})\s+交易时间:[^\n]+?回单流水号:\s*([0-9]+)", text
	)
	if not header:
		raise ReceiptParseError(_("缺少广发银行交易日期或回单流水号"))
	year, month, day, receipt_number = header.groups()
	transaction_reference = required_match(r"交易流水号\s+([A-Za-z0-9]+)", text, "缺少广发银行交易流水号")
	amount = decimal_amount(required_match(r"小写金额\s+[¥￥]\s*([\d,]+\.\d{2})", text, "缺少广发银行人民币金额"))
	names = re.search(r"名\s*称\s+(.+?)\s+名\s*称(?:\s+([^\n]*))?", text)
	accounts = re.search(r"款\s+账\s*号\s+([0-9 ]+)\s+款\s+账\s*号(?:\s+([0-9 ]+))?", text)
	banks = re.search(r"开户银行\s+(.+?)\s+开户银行(?:\s+([^\n]*))?", text)
	if not names or not accounts:
		raise ReceiptParseError(_("缺少广发银行收付款信息"))
	payer, payee = names.group(1).strip(), (names.group(2) or "").strip()
	payer_account = compact_account(accounts.group(1))
	payee_account = compact_account(accounts.group(2) or "")
	payer_bank = banks.group(1).strip() if banks else ""
	payee_bank = (banks.group(2) or "").strip() if banks else ""
	business_type = required_match(r"业务类型\s+([^\s]+)", text, "缺少广发银行业务类型")
	bank_summary = line_field(text, "摘要")
	postscript = line_field(text, "附言")
	remark = line_field(text, "备注")
	summary = postscript or remark or bank_summary
	if "住房公积" in summary or "住房公积" in payee:
		summary = "住房公积金"
	if not summary:
		raise ReceiptParseError(_("缺少广发银行交易摘要"))
	fees = (
		[{"item": summary, "count": 1, "amount": amount}]
		if any(word in summary for word in ("手续费", "汇款费"))
		else []
	)
	return receipt_row(
		posting_date=iso_date(year, month, day),
		direction="",
		currency="CNY",
		amount=amount,
		transaction_id=validate_flow(receipt_number),
		receipt_number=receipt_number,
		business_type=business_type,
		business_reference=transaction_reference,
		summary=summary,
		postscript=postscript,
		payer=payer,
		payer_account=payer_account,
		payer_bank=payer_bank,
		payee=payee,
		payee_account=payee_account,
		payee_bank=payee_bank,
		fee_details=fees,
		raw_text=text,
	)


def parse_text_pdf(content, heading_pattern, parse_text, parser_version, bank_label, ignore_blank_pages=False):
	import pdfplumber

	if not content.startswith(b"%PDF-") or len(content) > MAX_FILE_SIZE:
		raise ReceiptParseError(_("请上传不超过 20 MB 的 PDF 回单"))
	rows, errors = [], []
	try:
		with pdfplumber.open(io.BytesIO(content)) as pdf:
			if not 1 <= len(pdf.pages) <= MAX_PAGES:
				raise ReceiptParseError(_("单次支持 1—100 页回单"))
			for page_number, page in enumerate(pdf.pages, 1):
				headings = sorted(page.search(heading_pattern), key=lambda item: item["top"])
				if not headings:
					page_text = normalize_text(page.extract_text() or "")
					if ignore_blank_pages and not page_text.strip():
						continue
					errors.append({"page": page_number, "message": _("未识别到{0}文字版回单").format(_(bank_label))})
					continue
				for index, heading in enumerate(headings):
					if len(rows) >= MAX_RECEIPTS:
						raise ReceiptParseError(_("单次最多支持 300 张回单"))
					bottom = headings[index + 1]["top"] if index + 1 < len(headings) else page.height
					bbox = (0, max(0, heading["top"] - 2), page.width, bottom - 2 if index + 1 < len(headings) else bottom)
					try:
						row = parse_text(page.crop(bbox).extract_text(x_tolerance=2, y_tolerance=2) or "")
						row.update(page=page_number, position=index + 1, bbox=list(bbox))
						rows.append(row)
					except ReceiptParseError as exc:
						errors.append({"page": page_number, "position": index + 1, "message": str(exc)})
	except ReceiptParseError:
		raise
	except Exception as exc:
		raise ReceiptParseError(_("无法读取 PDF，请确认文件未加密、未损坏且包含文字层")) from exc
	return {"rows": rows, "errors": errors, "parser_version": parser_version}


def parse_boc_receipts(content):
	return parse_text_pdf(content, BOC_TITLE, parse_boc_receipt_text, "boc-text-v1", "中国银行")


def parse_abc_receipts(content):
	return parse_text_pdf(content, ABC_TITLE, parse_abc_receipt_text, "abc-text-v1", "中国农业银行")


def parse_icbc_receipts(content):
	return parse_text_pdf(
		content, ICBC_HEADING, parse_icbc_receipt_text, "icbc-text-v1", "中国工商银行", ignore_blank_pages=True
	)


def parse_cgb_receipts(content):
	return parse_text_pdf(content, CGB_TITLE, parse_cgb_receipt_text, "cgb-text-v1", "广发银行")
