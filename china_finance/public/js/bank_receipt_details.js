frappe.provide("china_finance.bank_receipts");

china_finance.bank_receipts.details_html = raw => {
	const d = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {});
	const e = value => frappe.utils.escape_html(String(value ?? ""));
	const fields = [
		["业务类型", d.business_type], ["回单编号", d.receipt_number], ["业务编号", d.business_number],
		["付款人", d.payer], ["付款账号", d.payer_account], ["付款开户行", d.payer_bank],
		["收款人", d.payee], ["收款账号", d.payee_account], ["收款开户行", d.payee_bank],
		["附言", d.postscript], ["收费期间", d.fee_period], ["税票号码", d.tax_number],
		["纳税人", d.taxpayer_name], ["纳税人识别号", d.taxpayer_id],
	];
	const fees = (d.fee_details || []).map(x => [x.item, __("{0} 笔", [x.count]), `${x.amount} CNY`]);
	const taxes = (d.tax_details || []).map(x => [x.item, `${x.period_from} — ${x.period_to}`, `${x.amount} CNY`]);
	const table = (title, rows) => rows.length ? `<p><strong>${e(__(title))}</strong></p><table class="table table-bordered"><tbody>${rows.map(row => `<tr>${row.map(x => `<td>${e(x)}</td>`).join("")}</tr>`).join("")}</tbody></table>` : "";
	return `<div class="receipt-details">${fields.filter(([, v]) => v).map(([k, v]) => `<div><strong>${e(__(k))}：</strong>${e(v)}</div>`).join("")}${table("收费明细", fees)}${table("税费明细", taxes)}${china_finance.bank_receipts.allocation_html(d.accounting_decision || {})}</div>`;
};

china_finance.bank_receipts.allocation_html = (suggestion, bank = {}) => {
	if (!suggestion.breakdown?.length) return "";
	const e = value => frappe.utils.escape_html(String(value ?? ""));
	const lines = [...(suggestion.journal_lines || [])];
	if (lines.length) lines.push({ account: bank.account || suggestion.bank_gl_account, debit: "0.00", credit: bank.amount ?? suggestion.bank_amount, summary: suggestion.bank_summary });
	const money = value => Number(value || 0).toLocaleString(frappe.boot?.lang || undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
	const period = suggestion.coverage_period || suggestion.social_period;
	const journal = lines.length ? `<p>${__("所属时期：{0} · 计提及支付分录（CNY）", [e(period)])}</p><table class="table table-bordered receipt-journal"><thead><tr><th>${__("摘要")}</th><th>${__("科目")}</th><th>${__("借方")}</th><th>${__("贷方")}</th></tr></thead><tbody>${lines.map(r => `<tr><td>${e(r.summary)}</td><td>${e(r.account)}</td><td>${money(r.debit)}</td><td>${money(r.credit)}</td></tr>`).join("")}</tbody></table>` : "";
	return `<table class="table table-bordered receipt-allocation"><thead><tr><th>${__("项目")}</th><th>${__("总额（CNY）")}</th><th>${__("公司比例")}</th><th>${__("公司承担")}</th><th>${__("个人承担")}</th></tr></thead><tbody>${suggestion.breakdown.map(r => `<tr><td>${e(r.item)}</td><td>${e(r.amount)}</td><td>${e(r.company_percent)}%</td><td>${e(r.company_amount)}</td><td>${e(r.personal_amount)}</td></tr>`).join("")}</tbody></table>${journal}`;
};
