import unittest

from china_finance.services.bank_receipt_parsers_cn import (
	ABC_TITLE,
	parse_abc_receipt_text,
	parse_boc_receipt_text,
	parse_cgb_receipt_text,
	parse_icbc_receipt_text,
)


class TestChineseBankReceiptParsers(unittest.TestCase):
	def test_boc_tax_receipt(self):
		row = parse_boc_receipt_text(
			"""国内支付业务付款回单
客户号：100000 日期：2026年08月17日
付款人账号：123456789012  收款人账号：
付款人名称：测试公司  收款人名称：国家金库测试支库
付款人开户行：中国银行测试支行  收款人开户行：
金额：CNY383.42
业务种类：实时缴税 业务编号：12345
纳税人识别号：TEST-TAX-ID  缴款书交易流水号：TAX-001  税票号码：TICKET-001
纳税人全称：测试公司
税（费）种名称 所属日期 实缴金额
个人所得税 2026/07/01-2026/07/31 CNY383.42
交易机构：10000 交易渠道：其他 交易流水号：BOC-FLOW-001 经办：
回单编号：BOC-RECEIPT-001 回单验证码：VERIFY
"""
		)
		self.assertEqual(row["posting_date"], "2026-08-17")
		self.assertEqual(row["direction"], "支出")
		self.assertEqual(row["summary"], "实时缴税")
		self.assertEqual(row["tax_details"][0]["period_from"], "20260701")
		self.assertEqual(row["tax_details"][0]["amount"], "383.42")

	def test_abc_fee_receipt_uses_receipt_number_as_identity(self):
		row = parse_abc_receipt_text(
			"""网上银行电子回单
客户收付款入账通知
回单编号： ABC-RECEIPT-001 第 2 次打印
账号 123456789012 账号 999999999999
付款方 户名 测试公司 收款方 户名
开户行 中国农业银行测试支行 开户行 1258
金额（小写） 15.00 金额（大写） 壹拾伍元整
币种 人民币 交易渠道 批处理
摘要 转取 凭证号 ABC-VOUCHER-001
交易时间 2026-08-09 20:19:08 会计日期 20260809
短信费
附言
"""
		)
		self.assertEqual(row["transaction_id"], "ABC-RECEIPT-001")
		self.assertEqual(row["summary"], "短信费")
		self.assertEqual(row["direction"], "")
		self.assertEqual(row["payee"], "")
		self.assertEqual(row["fee_details"][0]["amount"], "15.00")

	def test_abc_charge_notice_preserves_fee_name_period_and_alphanumeric_account(self):
		self.assertTrue(ABC_TITLE.search("扣费入账通知书"))
		row = parse_abc_receipt_text(
			"""网上银行电子回单
扣费入账通知书
回单编号： 61714238804992854246 第 2 次打印
账号 26125801040014640 账号 26125843CNYFC1641
付款方 户名 广州凌翔电子产品有限公司西安分公司 收款方 户名 中国农业银行股份有限公司西安长延堡支行
开户行 中国农业银行股份有限公司西安长延堡支行 开户行 中国农业银行股份有限公司西安长延堡支行
金额（小写） 200.00 金额（大写） 贰佰元整
币种 人民币 交易渠道 批处理
摘要 批量扣费 凭证号 26125850000000445
交易时间 2026-10-24 01:25:02 会计日期 20261024
批量扣费 收费渠道：TERM 收费名称：企业网银年服务费 收费周期：2026年10月
附言
电子回单可以重复打印，回单编号相同表示同一笔业务，请勿重复记账。
打印日期： 2026-11-02
"""
		)
		self.assertEqual(row["posting_date"], "2026-10-24")
		self.assertEqual(row["transaction_id"], "61714238804992854246")
		self.assertEqual(row["business_type"], "扣费入账通知书")
		self.assertEqual(row["summary"], "企业网银年服务费")
		self.assertEqual(row["payer_account"], "26125801040014640")
		self.assertEqual(row["payee_account"], "26125843CNYFC1641")
		self.assertEqual(row["payee"], "中国农业银行股份有限公司西安长延堡支行")
		self.assertEqual(row["fee_period"], "2026年10月")
		self.assertEqual(
			row["fee_details"],
			[{"item": "企业网银年服务费", "count": 1, "amount": "200.00"}],
		)

	def test_icbc_compatibility_characters_and_fee(self):
		row = parse_icbc_receipt_text(
			"""电⼦回单号码：ICBC-RECEIPT-001
测试公司
⼾ 名 ⼾ 名
付款 123456789012 收款 999999999999
账 号 账 号
⼈ ⼈
测试支行 中国⼯商银⾏测试分行
开⼾银⾏ 开⼾银⾏
⾦ 额 ￥46.50元 ⾦额（⼤写） ⼈⺠币 肆拾陆元伍⻆
摘 要 跨⾏汇款⼿续费 业务（产品）种类 对公收费
⽤ 途
交易流⽔号 12345001 时间戳 2026-08-25-21.45.15.238269
备注：
"""
		)
		self.assertEqual(row["posting_date"], "2026-08-25")
		self.assertEqual(row["summary"], "跨行汇款手续费")
		self.assertEqual(row["business_type"], "对公收费")
		self.assertEqual(row["fee_details"][0]["amount"], "46.50")

	def test_cgb_incoming_receipt_keeps_both_parties(self):
		row = parse_cgb_receipt_text(
			"""广发银行客户回单
交易日期:20260827 交易时间:15:26:34 回单流水号:20260827000000000001
业务类型 转账 交易流水号 CGBFLOW001 交易流水序号 0001
付 收
名 称 测试付款人 名 称 测试公司
款 账 号 999999999999 款 账 号 123456789012
开户银行 招商银行 开户银行 广发银行测试支行
人 人
大写金额 人民币（大写）壹仟柒佰陆拾元整
小写金额 ￥1,760.00
附言 退款
摘要 网银入账
备注
"""
		)
		self.assertEqual(row["posting_date"], "2026-08-27")
		self.assertEqual(row["transaction_id"], "20260827000000000001")
		self.assertEqual(row["summary"], "退款")
		self.assertEqual(row["payer"], "测试付款人")
		self.assertEqual(row["payee"], "测试公司")
