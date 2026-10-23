const china_finance_source_snapshot = {
	refresh(frm) {
		// Form refresh can run more than once while the desk restores a route.
		// Remove only our own indicator so repeated refreshes cannot duplicate it
		// or alter native ERPNext dashboard statistics.
		frm.dashboard.stats_area_row.find("[data-china-voucher-indicator]").remove();
		// These are audit records generated from the source document. They must
		// not be offered as business documents to cancel with the source.
		frm.ignore_doctypes_on_cancel_all = Array.from(new Set([
			...(frm.ignore_doctypes_on_cancel_all || []),
			"China Accounting Voucher",
			"China Cash Flow Assignment",
		]));
		if (frm.is_new() || frm.doc.docstatus !== 1) return;
		const name = frm.doc.name;
		const formal = ["Journal Entry", "Payment Entry"].includes(frm.doctype);
		if (formal) frm.add_custom_button(__("在查凭证中查看"), () => china_finance_source_snapshot.open_ledger(frm.doc), __("中国财务"));
		frappe.call({
			method: formal ? "china_finance.services.voucher_posting_status.get_posting_state" : "china_finance.services.voucher.get_source_snapshot_status",
			args: { source_doctype: frm.doctype, source_name: frm.doc.name },
			callback: (response) => {
				if (frm.doc.name !== name || frm.doc.docstatus !== 1) return;
				const state = response.message;
				if (state?.not_applicable && !formal) return;
				if (!state?.snapshot_ready) {
					frm.dashboard.add_indicator(formal ? (state?.has_gl ? __("已记账，正式凭证待补齐") : __("已提交，请核对总账处理状态")) : __(state?.reason || "审计快照尚未生成"), "orange").attr("data-china-voucher-indicator", "1");
					if (formal && state?.reason) frm.dashboard.add_indicator(state.reason, "orange").attr("data-china-voucher-indicator", "1");
					if (state?.can_repair) frm.add_custom_button(__("补齐正式凭证"), () => china_finance_source_snapshot.repair(frm), __("中国财务"));
					if (state?.can_create_assignment) {
						frm.add_custom_button(__("创建现金流量指定"), () => create_assignment(frm), __("中国财务"));
					}
					return;
				}
				if (state.statutory_number) {
					frm.dashboard
						.add_indicator(__("凭证字号：{0}", [state.statutory_number]), "blue")
						.attr("data-china-voucher-indicator", "1");
				}
				if (state.can_view_snapshot) {
					frm.add_custom_button(__("查看审计快照"), () => {
						frappe.set_route("Form", "China Accounting Voucher", state.snapshot_name);
					}, __("中国财务"));
				}
				if (state.assignment?.name && state.assignment.status !== "Cancelled") {
					frm.add_custom_button(__("现金流量指定"), () => {
						frappe.set_route("Form", "China Cash Flow Assignment", state.assignment.name);
					}, __("中国财务"));
					return;
				}
				if (state.assignment?.status === "Cancelled") {
					frm.add_custom_button(__("重新创建现金流量指定"), () => recreate_assignment(frm), __("中国财务"));
					return;
				}
				if (state.assignment_required) {
					frm.add_custom_button(__("创建现金流量指定"), () => create_assignment(frm), __("中国财务"));
				} else if (state.assignment_reason) {
					frm.dashboard.add_indicator(__(state.assignment_reason), "gray");
				}
			},
		});
	},
};

china_finance_source_snapshot.open_ledger = doc => {
	frappe.route_options = {company: doc.company, from_date: doc.posting_date, to_date: doc.posting_date,
		period_preset: "", voucher_status: "全部有效凭证", source_doctype: doc.doctype, source_name: doc.name,
		voucher_number: doc.name, receipt_import: "", accounting_period: "", voucher_word: "", account: "",
		party_type: "", party: "", search_text: "", source_event: ""};
	return frappe.set_route("query-report", "China Voucher Ledger");
};

china_finance_source_snapshot.posting_result = async doc => {
	try {
		const {message: state} = await frappe.call({method: "china_finance.services.voucher_posting_status.get_posting_state",
			args: {source_doctype: doc.doctype, source_name: doc.name}});
		const message = !state?.posted ? __("凭证正在后台记账，请稍后刷新核对")
			: state.snapshot_ready ? __("已记账，凭证字号：{0}", [state.statutory_number || doc.name])
				: state.has_gl ? __("已记账，正式凭证待补齐") : __("已提交，请核对总账处理状态");
		frappe.msgprint({title: __("记账结果"), message: frappe.utils.escape_html(message)
			+ (state?.posted && state.has_gl ? `<p>${__("可在已记账凭证中查看；未记账筛选不再显示此凭证。")}</p>` : "")
			+ (state?.reason && !state.snapshot_ready ? `<p>${frappe.utils.escape_html(state.reason)}</p>` : ""),
			indicator: state?.snapshot_ready ? "green" : "orange", primary_action: {label: __("在查凭证中查看"),
				action: () => china_finance_source_snapshot.open_ledger(doc)}});
	} catch {
		frappe.msgprint(__("未能读取记账结果，请刷新原单核对，勿重复记账"));
	}
};

china_finance_source_snapshot.repair = frm => {
	if (frm.__china_snapshot_repairing) return;
	const doc = {...frm.doc};
	frappe.confirm(__("补齐正式凭证只生成展示记录，不会再次记账。是否继续？"), async () => {
		if (frm.__china_snapshot_repairing || frm.doc.name !== doc.name || frm.doc.docstatus !== 1) return;
		frm.__china_snapshot_repairing = true;
		try {
			await frappe.call({method: "china_finance.services.voucher_posting_status.repair_posting_snapshot", type: "POST",
				args: {source_doctype: doc.doctype, source_name: doc.name}, freeze: true});
			if (frm.doc.name === doc.name) await frm.reload_doc();
			await china_finance_source_snapshot.posting_result(doc);
		} catch (error) {
			frappe.msgprint(error?.message || __("正式凭证补齐失败，请刷新原单核对"));
		} finally {frm.__china_snapshot_repairing = false;}
	});
};

function create_assignment(frm) {
	frappe.call({
		method: "china_finance.services.voucher.create_cash_flow_assignment_from_source",
		args: { source_doctype: frm.doctype, source_name: frm.doc.name },
		freeze: true,
		callback: (response) => response.message?.name && frappe.set_route("Form", "China Cash Flow Assignment", response.message.name),
	});
}

function recreate_assignment(frm) {
	frappe.call({
		method: "china_finance.services.voucher.recreate_cash_flow_assignment_from_source",
		args: { source_doctype: frm.doctype, source_name: frm.doc.name },
		freeze: true,
		callback: (response) => response.message?.name && frappe.set_route("Form", "China Cash Flow Assignment", response.message.name),
	});
}

[
	"Journal Entry",
	"Payment Entry",
	"Sales Invoice",
	"Purchase Invoice",
	"Stock Entry",
	"Delivery Note",
	"Purchase Receipt",
	"Asset",
	"Asset Capitalization",
	"Asset Depreciation Entry",
	"Payroll Entry",
	"Period Closing Voucher",
].forEach((doctype) => frappe.ui.form.on(doctype, china_finance_source_snapshot));
