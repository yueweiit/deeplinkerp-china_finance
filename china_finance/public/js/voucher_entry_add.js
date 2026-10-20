/* global china_finance */
frappe.provide("china_finance.voucher_entry_add");

// Stage independent voucher entries; the reference only supplies accounting dimensions.
china_finance.voucher_entry_add.open = async (report, data, stage) => {
	await report.datatable?.cellmanager?.submitEditing?.();
	report.datatable?.cellmanager?.deactivateEditing(false);
	if (report._china_add_dialog) return;
	const state = report._china_inline_edit_state;
	if (state?.pending) return;
	if (state?.voucher_key && state.voucher_key !== data?.edit_voucher_key) {
		frappe.msgprint(__("请先保存或取消当前凭证的修改，再编辑另一张凭证"));
		return;
	}
	const rows = [
		...new Map(
			(report.data || [data])
				.filter(
					(row) =>
						row &&
						row.edit_voucher_key === data?.edit_voucher_key &&
						row.editable_add &&
						!row._inline_deleted &&
						!row._inline_added
				)
				.map((row) => [row.edit_key, row])
		).values(),
	];
	if (!rows.length) {
		frappe.msgprint(__("当前凭证没有可新增行的分录，请刷新报表或打开原单处理"));
		return;
	}
	let row = rows.find((item) => item.edit_key === data?.edit_key) || rows[0];
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	let dialog;
	const select = async () => {
		if (!dialog) return;
		row = rows.find((item) => item.edit_key === dialog.get_value("source_row")) || row;
		dialog.doc.currency = row.currency || row.account_currency;
		dialog.set_df_property("amount", "precision", row.amount_precision);
		await dialog.set_values({
			account: "",
			amount: null,
			summary: "",
			side: row.debit ? "debit" : "credit",
		});
	};
	dialog = new frappe.ui.Dialog({
		title: __("新增一行"),
		size: "large",
		doc: { currency: row.currency || row.account_currency },
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${esc(
					__(
						"参考分录仅用于沿用往来单位、成本中心、项目及辅助维度，原行金额不变。新增后请调整相关金额，借贷平衡后统一保存。"
					)
				)}</p>`,
			},
			{
				fieldname: "source_row",
				fieldtype: "Select",
				label: __("参考分录行"),
				reqd: 1,
				default: row.edit_key,
				options: rows.map((item, index) => ({
					value: item.edit_key,
					label: esc(
						__("第 {0} 行 · {1} · {2} {3}", [
							item.entry_idx || index + 1,
							item.account_label || item.account,
							item.debit ? __("借方") : __("贷方"),
							frappe.format(
								Number(item.debit || item.credit || 0),
								{
									fieldtype: "Currency",
									options: "currency",
									precision: item.amount_precision,
								},
								{ only_value: true },
								{ currency: item.currency || item.account_currency }
							),
						])
					),
				})),
				onchange: select,
			},
			{
				fieldname: "account",
				fieldtype: "Link",
				options: "Account",
				label: __("新增分录科目"),
				reqd: 1,
				only_select: 1,
				get_query: () => ({
					query: "china_finance.services.source_voucher_edit.get_compatible_accounts",
					filters: {
						source_doctype: row.edit_source_doctype,
						source_name: row.edit_source_name,
						edit_key: row.edit_key,
					},
				}),
			},
			{
				fieldname: "side",
				fieldtype: "Select",
				label: __("借贷方向"),
				reqd: 1,
				default: row.debit ? "debit" : "credit",
				options: [
					{ value: "debit", label: __("借方") },
					{ value: "credit", label: __("贷方") },
				],
			},
			{
				fieldname: "amount",
				fieldtype: "Currency",
				label: __("新增行金额"),
				reqd: 1,
				options: "currency",
				precision: row.amount_precision,
			},
			{
				fieldname: "summary",
				fieldtype: "Data",
				label: __("新增分录摘要（留空沿用原摘要）"),
			},
		],
		primary_action_label: __("添加到凭证"),
		primary_action(values) {
			if (!Number.isFinite(Number(values.amount)) || !Number(values.amount)) {
				frappe.msgprint(__("新增行金额必须为有效的非零金额"));
				return;
			}
			if (
				stage(row, {
					source_row: row.edit_key,
					account: values.account,
					side: values.side,
					amount: Number(values.amount),
					summary: values.summary || "",
				})
			)
				dialog.hide();
		},
		onhide() {
			report._china_add_dialog = null;
		},
	});
	report._china_add_dialog = dialog;
	dialog.show();
};
