/* global china_finance */

frappe.provide("china_finance.preparation");

(() => {
	const api = async (method, args) => (await frappe.call({method: `china_finance.services.voucher_preparation.${method}`, args, type: "POST", freeze: true})).message;
	const esc = value => frappe.utils.escape_html(String(value ?? ""));
	const money = (value, currency) => frappe.format(value, {fieldtype: "Currency", options: "currency"}, {only_value: true}, {currency});

	china_finance.preparation.trial = async (company, from_date, to_date) => {
		const result = await api("trial_balance", {company, from_date, to_date});
		const labels = {opening: "期初余额", posted_debit: "已记账借方", posted_credit: "已记账贷方", draft_debit: "草稿借方", draft_credit: "草稿贷方", expected_balance: "预计余额"};
		const dialog = new frappe.ui.Dialog({title: __("含未记账凭证的科目余额试算"), size: "extra-large", fields: [{fieldtype: "HTML", options: `
			<p>${esc(company)} · ${esc(from_date)} — ${esc(to_date)} · ${esc(result.currency)}</p><p>${esc(result.message)}</p>
			<p>${__("本期草稿 {0} 张；更早期间草稿 {1} 张。", [result.draft_count, result.earlier_drafts])}</p>
			${result.excluded.length ? `<div class="alert alert-warning">${__("以下草稿未纳入，试算不完整：{0}", [result.excluded.map(r => `${esc(r.name)}：${esc(r.error)}`).join("<br>")])}</div>` : ""}
			<div style="overflow:auto"><table class="table table-bordered"><thead><tr><th>${__("科目")}</th>${Object.values(labels).map(label => `<th>${esc(__(label))}</th>`).join("")}</tr></thead><tbody>${result.rows.map(row => `<tr><td>${esc(row.account)}</td>${Object.keys(labels).map(key => `<td class="text-right">${money(row[key], result.currency)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`}]});
		dialog.show();
	};

	china_finance.preparation.review = async names => {
		const preview = await api("review_preview", {names});
		const options = row => Object.fromEntries(row.cash.options.map(o => [o.value, o.label]));
		const body = preview.map(row => `<details open><summary>${esc(row.name)} · ${esc(row.posting_date)}</summary><table class="table table-bordered"><tr><th>${__("摘要")}</th><th>${__("科目")}</th><th>${__("借方")}</th><th>${__("贷方")}</th></tr>${row.accounts.map(r => `<tr><td>${esc(r.summary)}</td><td>${esc(r.account)}</td><td>${esc(r.debit)}</td><td>${esc(r.credit)}</td></tr>`).join("")}</table><p>${row.cash.rows.map(r => `${esc(r.cash_account)} ${esc(r.direction)} ${esc(r.amount)} → ${esc(options(row)[r.row_code] || "待选择现金流项目")}`).join("<br>")}${esc(row.cash.error)}</p></details>`).join("");
		return new Promise(resolve => {
			const dialog = new frappe.ui.Dialog({title: __("核对凭证与现金流项目"), size: "extra-large", fields: [{fieldtype: "HTML", options: body}], primary_action_label: __("核对完成，待记账"), primary_action: async () => {
				dialog.get_primary_btn().prop("disabled", true);
				try {
					const rows = await api("review_drafts", {names, versions: Object.fromEntries(preview.map(r => [r.name, r.modified])), cash_plans: Object.fromEntries(preview.map(r => [r.name, r.cash.rows]))});
					frappe.msgprint(rows.map(r => `${esc(r.name)}：${r.success ? __("核对完成，待记账") : esc(r.error)}`).join("<br>"));
					dialog.hide(); resolve(rows);
				} finally { dialog.get_primary_btn().prop("disabled", false); }
			}});
			dialog.onhide = () => resolve([]);
			dialog.show();
		});
	};

	china_finance.preparation.bind_report = report => {
		report.page.add_menu_item(__("凭证准备模式设置"), () => frappe.set_route("Form", "China Finance Settings", report.get_filter_value("company")));
		report.page.add_inner_button(__("核对所选草稿"), async () => {
			const indices = report.datatable?.rowmanager.getCheckedRows() || [];
			const names = [...new Set(indices.map(i => report.data[i]?.draft_name).filter(Boolean))];
			if (!names.length) return frappe.msgprint(__("请勾选未记账凭证的分录行；同张凭证只处理一次"));
			await china_finance.preparation.review(names);
			report.refresh();
		});
		report.page.add_inner_button(__("未记账试算"), () => china_finance.preparation.trial(report.get_filter_value("company"), report.get_filter_value("from_date"), report.get_filter_value("to_date")));
		report.page.add_inner_button(__("月末结账"), () => frappe.new_doc("China Closing Run", {company: report.get_filter_value("company"), from_date: report.get_filter_value("from_date"), to_date: report.get_filter_value("to_date")}));
		report.page.add_inner_button(__("新增草稿"), () => frappe.new_doc("Journal Entry", {company: report.get_filter_value("company")}));
	};
})();
