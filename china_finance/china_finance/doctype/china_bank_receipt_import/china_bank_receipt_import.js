frappe.ui.form.on("China Bank Receipt Import", {
	async source_file(frm) {
		if (frm.doc.source_file && frm.doc.company && frm.doc.bank_account && !frm.doc.source_hash && !frm.__receipt_parsing) await frm.save();
	},
	async after_save(frm) {
		if (!frm.doc.source_file || frm.doc.source_hash || frm.__receipt_parsing || frm.doc.status === "已作废") return;
		frm.__receipt_parsing = true;
		try { await receipt_call("parse_import", {name: frm.doc.name}); await frm.reload_doc(); }
		finally { frm.__receipt_parsing = false; }
	},
	setup(frm) {
		frm.set_query("bank_account", () => ({ filters: { company: frm.doc.company, is_company_account: 1, disabled: 0 } }));
	},
	company(frm) { if (!frm.doc.source_hash) frm.set_value("bank_account", ""); },
	refresh(frm) {
		clear_receipt_preview(frm);
		frm.set_df_property("source_file", "options", { restrictions: { allowed_file_types: [".pdf"], max_file_size: 20 * 1024 * 1024 }, make_attachments_public: false });
		if (frm.doc.company && frappe.model.can_create("China Bank Receipt Rule")) frm.add_custom_button(__("设置社保分摊规则"), async () => {
			const lookup = number => frappe.db.get_value("Account", { company: frm.doc.company, account_number: number, is_group: 0, disabled: 0 }, "name");
			const [expense, personal, accrual] = await Promise.all([lookup("660208"), lookup("122102"), lookup("221101")]);
			frappe.new_doc("China Bank Receipt Rule", { company: frm.doc.company, direction: "支出", rule_type: "社保分摊", account: expense.message?.name,
				personal_account: personal.message?.name, accrual_account: accrual.message?.name, injury_company_percent: 100, pension_company_percent: 66.67,
				medical_company_percent: 86.30, unemployment_company_percent: 80,
				notes: "按回单所属时期生成计提三行、支付两行。公司承担计入管理费用-社会保险费，个人承担计入其他应收款-社保；逐项四舍五入，个人承担取差额。使用前确认该所属期尚未计提。" });
		});
		if (frm.doc.company && frappe.model.can_create("China Bank Receipt Rule")) frm.add_custom_button(__("设置公积金分摊规则"), async () => {
			const lookup = number => frappe.db.get_value("Account", { company: frm.doc.company, account_number: number, is_group: 0, disabled: 0 }, "name");
			const [expense, personal, accrual] = await Promise.all([lookup("660229"), lookup("122103"), lookup("221104")]);
			frappe.new_doc("China Bank Receipt Rule", { company: frm.doc.company, direction: "支出", rule_type: "公积金分摊", account: expense.message?.name,
				personal_account: personal.message?.name, accrual_account: accrual.message?.name, housing_fund_company_percent: 50,
				notes: "按回单所属时期生成计提三行、支付两行。公司承担计入管理费用-公积金，个人承担计入其他应收款-公积金；当前公司和个人各承担 50%，使用前确认该所属期尚未计提。" });
		});
		const parsed = !!frm.doc.source_hash;
		for (const field of ["company", "bank_account", "mode", "source_file"]) frm.set_df_property(field, "read_only", parsed);
		if (frm.is_new()) return;
		if (parsed) frm.disable_save();
		if (!parsed && frm.doc.status !== "已作废") {
			frm.add_custom_button(__("识别回单"), async () => {
				if (frm.is_dirty()) await frm.save();
				await receipt_call("parse_import", { name: frm.doc.name });
				await frm.reload_doc();
			});
		}
		if (parsed && frm.doc.status !== "已作废") {
			frm.add_custom_button(__("刷新处理状态"), () => render_receipt_preview(frm));
			if ((frm.doc.rows || []).some(r => r.receipt)) frm.add_custom_button(__("更新本批草稿摘要"), async () => {
				const result = await receipt_call("refresh_import_draft_summaries", { name: frm.doc.name });
				await frm.reload_doc();
				const skipped = (result.skipped || []).map(r => `${receipt_escape(r.voucher)}：${receipt_escape(r.reason)}`).join("<br>");
				frappe.msgprint(__("已更新 {0} 张未记账草稿。{1}", [result.updated_count, skipped ? `<br>跳过 ${result.skipped_count} 张：<br>${skipped}` : ""]));
			});
			if (!(frm.doc.rows || []).some(r => r.receipt)) frm.add_custom_button(__("作废批次"), () => {
				frappe.prompt({ fieldname: "reason", label: __("作废原因"), fieldtype: "Small Text", reqd: 1 }, async ({ reason }) => {
					await receipt_call("abandon_import", { name: frm.doc.name, reason });
					frm.reload_doc();
				});
			});
		}
		if (parsed) {
			render_receipt_preview(frm);
			frm.add_custom_button(__("查看本批凭证"), () => {
				const dates = (frm.doc.rows || []).map(r => r.posting_date).filter(Boolean).sort();
				frappe.route_options = {company: frm.doc.company, receipt_import: frm.doc.name, from_date: dates[0], to_date: dates[dates.length - 1], period_preset: "", voucher_status: "全部有效凭证"};
				frappe.set_route("query-report", "China Voucher Ledger");
			});
		}
	},
});

async function receipt_call(method, args) {
	const response = await frappe.call({ method: `china_finance.services.bank_receipt_import.${method}`, args, freeze: true });
	return response.message;
}

function receipt_escape(value) { return frappe.utils.escape_html(String(value ?? "")); }

function clear_receipt_preview(frm) {
	frm.__receipt_preview_request = (frm.__receipt_preview_request || 0) + 1;
	frm.receipt_preview = null;
	frm.fields_dict.preview?.$wrapper?.empty();
}

async function render_receipt_preview(frm, preview_data = null) {
	const document_name = frm.doc.name;
	const request_id = (frm.__receipt_preview_request || 0) + 1;
	frm.__receipt_preview_request = request_id;
	const data = preview_data || await receipt_call("preview_import", { name: frm.doc.name });
	if (
		frm.doc.name !== document_name
		|| frm.__receipt_preview_request !== request_id
		|| !frm.doc.source_hash
	) return;
	frm.receipt_preview = data;
	const e = receipt_escape;
	const amount = value => frappe.format(value, { fieldtype: "Currency", options: "currency" }, { only_value: true }, { currency: "CNY" });
	const blocked = data.errors.length || ["已作废", "识别失败"].includes(data.status);
	const is_processable = r => !r.receipt && !r.candidates.some(c => c.blocking !== false) && (r.suggestion.account || r.suggestion.allocations) && !r.suggestion.blocked && r.status !== "冲突";
	const status_exception = r => ["处理失败", "冲突", "凭证已取消或缺失"].includes(r.status);
	const needs_manual = r => status_exception(r) || (!r.receipt && !is_processable(r));
	const processable = data.rows.filter(is_processable);
	const exceptions = data.rows.filter(needs_manual);
	const normal_rows = data.rows.filter(r => !needs_manual(r));
	const active_filter = frm.__receipt_filter || "exception";
	const visible_rows = data.rows
		.map((row, index) => ({ row, index }))
		.filter(({ row }) => active_filter === "exception" ? needs_manual(row) : !needs_manual(row));
	const $wrapper = frm.fields_dict.preview.$wrapper;
	$wrapper.html(`
		<p>${__("{0} · {1} 张回单 / {2} 笔交易 · 收入 {3} · 支出 {4}（按流水号去重合计）", [e(__(data.mode)), data.rows.length, data.transaction_count ?? data.rows.length, amount(data.deposit_total), amount(data.withdrawal_total)])}</p>
		<p class="text-muted">${__("新业务确认一次即按建议科目生成全部可处理草稿，草稿可在查凭证修改，月末统一记账后才进入报表。流水号重复会阻止重复制证；仅日期和金额相同的历史凭证只作提示。")}</p>
		${data.errors.length ? `<div class="alert alert-danger">${data.errors.map(x => __("第 {0} 页：{1}", [e(x.page), e(x.message)])).join("<br>")}<br>${__("请作废本批次并重新上传完整、正确的回单。")}</div>` : ""}
		${exceptions.length && data.mode === "新业务制证" ? `<p class="china-receipt-exception-summary">${__("{0} 笔需要人工处理，暂不生成凭证草稿。", [exceptions.length])}</p>` : ""}
		${!blocked && data.mode === "新业务制证" && processable.length ? `<button class="btn btn-primary btn-sm receipt-batch">${__("确认导入并生成草稿（{0}）", [processable.length])}</button>` : ""}
		<div class="china-receipt-filter-toolbar">
			<span class="china-receipt-filter-label">${__("筛选")}</span>
			<div class="china-receipt-filter-tabs" role="tablist" aria-label="${__('回单处理状态')}">
			<button type="button" class="btn btn-sm china-receipt-filter ${active_filter === "exception" ? "is-active" : ""} is-exception" data-filter="exception" role="tab" aria-selected="${active_filter === "exception"}">
				<span>${__("有异常")}</span><span class="china-receipt-filter-count">${exceptions.length}</span>
			</button>
			<button type="button" class="btn btn-sm china-receipt-filter ${active_filter === "normal" ? "is-active" : ""} is-normal" data-filter="normal" role="tab" aria-selected="${active_filter === "normal"}">
				<span>${__("没有异常")}</span><span class="china-receipt-filter-count">${normal_rows.length}</span>
			</button>
			</div>
		</div>
		<p class="china-receipt-filter-caption">${__("当前显示：{0} · {1} 笔", [__(active_filter === "exception" ? "有异常" : "没有异常"), visible_rows.length])}</p>
		<div style="overflow:auto;margin-top:12px"><table class="table table-bordered"><thead><tr>
		<th>${__("日期 / 原件")}</th><th>${__("收支 / 金额")}</th><th>${__("对方 / 摘要")}</th><th>${__("建议科目 / 依据")}</th><th>${__("状态 / 已有凭证")}</th><th>${__("操作")}</th>
		</tr></thead><tbody>${visible_rows.length ? visible_rows.map(({ row: r, index }) => `<tr class="${needs_manual(r) ? "china-receipt-row-danger" : ""}">
		<td>${e(r.posting_date)}<br><a href="${e(data.source_file)}#page=${r.page_number}" target="_blank" rel="noopener">${__("第 {0} 页第 {1} 张", [r.page_number, r.position])}</a><br>${e(r.transaction_id)}</td>
		<td>${e(__(r.direction))}<br>${amount(r.amount)}</td><td>${e(r.counterparty)}<br>${e(r.summary)}</td>
		<td>${r.suggestion.allocations ? r.suggestion.allocations.map(a => `${e(a.account)}：${amount(a.amount)}`).join("<br>") : e(r.suggestion.account || __("待人工分类"))}<br><small>${e(r.suggestion.reason)}</small></td>
		<td class="${needs_manual(r) ? "china-receipt-status-danger" : ""}">${e(__(r.status))}${r.message ? `<br><small>${e(r.message)}</small>` : ""}${r.voucher_name ? `<br>${frappe.utils.get_form_link(r.voucher_type, r.voucher_name, true)}` : ""}
		${r.candidates.map(c => c.restricted ? `<br>${__("存在需管理员核对的凭证")}` : `<br>${__(c.blocking === false ? "仅提示：" : "疑似凭证：")}${frappe.utils.get_form_link(c.doctype, c.name, true)} ${e(c.posting_date || "")} ${e(__(c.match_type || ""))}`).join("")}</td>
		<td>${r.receipt ? `${frappe.utils.get_form_link("China Bank Receipt", r.receipt, true, __("查看回单记录"))}${r.status === "已关联待核销" ? `<br><button class="btn btn-xs btn-default receipt-reconcile" data-index="${index}">${__("核销")}</button>` : ""}${r.status === "凭证已取消或缺失" ? `<br><button class="btn btn-xs btn-default receipt-process" data-index="${index}">${__("关联修订凭证")}</button>` : ""}` : !blocked && needs_manual(r) && r.status !== "冲突" ? `<button class="btn btn-xs btn-danger receipt-process" data-index="${index}">${__("处理异常")}</button>` : is_processable(r) ? __("确认后生成") : ""}</td>
		</tr>`).join("") : `<tr><td colspan="6" class="china-receipt-filter-empty">${__("当前标签下没有回单")}</td></tr>`}</tbody></table></div>`);
	$wrapper.find(".china-receipt-filter").on("click", event => {
		frm.__receipt_filter = event.currentTarget.dataset.filter;
		render_receipt_preview(frm, data);
	});
	$wrapper.find(".receipt-process").on("click", event => receipt_dialog(frm, data.rows[event.currentTarget.dataset.index]));
	$wrapper.find(".receipt-reconcile").on("click", event => {
		const row = data.rows[event.currentTarget.dataset.index];
		frappe.confirm(__("确认将这笔回单对应银行交易与已关联凭证核销？"), async () => {
			await receipt_call("reconcile_receipt", { name: row.receipt });
			await frm.reload_doc();
		});
	});
	$wrapper.find(".receipt-batch").on("click", async event => {
		const $button = $(event.currentTarget).prop("disabled", true);
		try {
			const result = await receipt_call("process_import_batch", {name: frm.doc.name, confirmed: 1});
			await frm.reload_doc();
			frappe.msgprint(`${__("新增草稿 {0} 张，复用 {1} 笔，需处理 {2} 笔。", [result.created, result.reused, result.failed])}<br>` + result.results.filter(r => r.error).map(r => `${e(r.name)}：${e(r.error)}`).join("<br>"));
		} finally {
			$button.prop("disabled", false);
		}
	});
}

function receipt_dialog(frm, row) {
	const can_create = frm.doc.mode === "新业务制证" && !row.receipt && !row.candidates.some(c => c.blocking !== false) && !row.suggestion.blocked;
	const allocation_name = row.suggestion.rule_type === "公积金分摊" ? "公积金" : "社保";
	const create_label = row.suggestion.allocations ? __("按{0}计提及支付生成草稿", [allocation_name]) : "生成凭证草稿";
	const candidate = row.candidates.find(c => !c.restricted);
	let party_policy_request = 0;
	let dialog;
	const sync_party_fields = async () => {
		if (!dialog) return;
		const request = ++party_policy_request;
		const account = dialog.get_value("account");
		const is_single_account_create = dialog.get_value("action") === create_label && !row.suggestion.allocations;
		let party_types = [];
		if (is_single_account_create && account) {
			const result = await frappe.db.get_value("Account", account, ["account_type", "root_type"]);
			if (request !== party_policy_request) return;
			const values = result.message || {};
			if (values.account_type === "Receivable") party_types = ["Customer", "Employee"];
			else if (values.account_type === "Payable") party_types = ["Supplier", "Employee"];
			else if (values.root_type === "Equity") party_types = ["Shareholder"];
		}
		const visible = party_types.length > 0;
		dialog.set_df_property("party_type", "options", ["", ...party_types].join("\n"));
		dialog.set_df_property("party_type", "hidden", !visible);
		dialog.set_df_property("party", "hidden", !visible);
		if (!visible || !party_types.includes(dialog.get_value("party_type"))) {
			await dialog.set_value("party", "");
			await dialog.set_value("party_type", "");
		}
	};
	dialog = new frappe.ui.Dialog({ title: __("核对回单"), fields: [
		{ fieldtype: "HTML", options: `<p>${receipt_escape(row.posting_date)} · ${receipt_escape(__(row.direction))} ${receipt_escape(row.amount)} CNY</p><p>${receipt_escape(row.summary)}</p><p>${receipt_escape(row.suggestion.reason)}</p>${china_finance.bank_receipts.allocation_html(row.suggestion, {account: frm.receipt_preview.bank_gl_account, amount: row.amount})}<details><summary>${__("收付款方及费用明细")}</summary>${china_finance.bank_receipts.details_html(row.raw_data)}</details>` },
		{ fieldname: "action", label: __("处理方式"), fieldtype: "Select", options: can_create ? __("关联已有凭证\n{0}", [create_label]) : "关联已有凭证", default: can_create ? create_label : "关联已有凭证", onchange: sync_party_fields },
		{ fieldname: "allocation_not_accrued", label: __("已核对回单所属时期，该期{0}尚未计提，同意生成计提及支付分录；已计提时应手工冲应付科目后关联", [allocation_name]), fieldtype: "Check", depends_on: 'eval:doc.action=="按社保计提及支付生成草稿" || doc.action=="按公积金计提及支付生成草稿"' },
		{ fieldname: "account", label: __("对方科目"), fieldtype: "Link", options: "Account", default: row.suggestion.account,
			depends_on: 'eval:doc.action=="生成凭证草稿"', get_query: () => ({ filters: { company: frm.doc.company, is_group: 0, disabled: 0 } }), onchange: sync_party_fields },
		{ fieldname: "party_type", label: __("往来单位类型"), fieldtype: "Select", options: "", hidden: 1 },
		{ fieldname: "party", label: __("往来单位"), fieldtype: "Dynamic Link", options: "party_type", hidden: 1 },
		{ fieldname: "voucher_type", label: __("凭证类型"), fieldtype: "Select", options: "Journal Entry\nPayment Entry", default: candidate?.doctype || "Journal Entry", depends_on: 'eval:doc.action=="关联已有凭证"' },
		{ fieldname: "voucher_name", label: __("已有凭证"), fieldtype: "Dynamic Link", options: "voucher_type", default: candidate?.name, depends_on: 'eval:doc.action=="关联已有凭证"', get_query: () => ({ filters: { company: frm.doc.company, docstatus: ["!=", 2] } }) },
		{ fieldname: "create_bank_transaction", label: __("同时补建银行交易（用于后续核销）"), fieldtype: "Check", default: 0, depends_on: 'eval:doc.action=="关联已有凭证"' },
		{ fieldname: "notes", label: __("核对说明"), fieldtype: "Small Text" },
		{ fieldname: "confirmed", label: __("已核对原回单、业务依据及重复记账情况"), fieldtype: "Check", reqd: 1 },
	], primary_action_label: __("确认处理"), primary_action: async values => {
		if (!values.confirmed) return frappe.msgprint(__("请先核对并勾选确认"));
		dialog.get_primary_btn().prop("disabled", true);
		try {
			const result = await receipt_call("process_receipt", { ...values, decision_hash: row.suggestion.decision_hash, action: values.action === create_label && row.suggestion.allocations ? "create_allocation" : values.action === "生成凭证草稿" ? "create" : "link", name: frm.doc.name, row_name: row.name });
			if (result.error) { frappe.msgprint(receipt_escape(result.error)); return; }
			dialog.hide();
			await frm.reload_doc();
		} finally { dialog.get_primary_btn().prop("disabled", false); }
	} });
	dialog.show();
	sync_party_fields();
}
