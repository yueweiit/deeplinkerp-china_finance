/* global china_finance */
frappe.provide("china_finance.voucher_entry_delete");

china_finance.voucher_entry_delete.open = async (report, data, stage) => {
	await report.datatable?.cellmanager?.submitEditing?.();
	report.datatable?.cellmanager?.deactivateEditing(false);
	if (report._china_delete_dialog) return;
	const state = report._china_inline_edit_state;
	if (state?.pending) return;
	if (state?.voucher_key && state.voucher_key !== data?.edit_voucher_key) {
		frappe.msgprint(__("请先保存或取消当前凭证的修改，再编辑另一张凭证"));
		return;
	}
	const rows = [
		...new Map(
			(report.data || [])
				.filter(
					(row) =>
						data &&
						row.edit_voucher_key === data.edit_voucher_key &&
						row.editable_delete &&
						!row._inline_deleted
				)
				.map((row) => [row.edit_key, row])
		).values(),
	];
	if (!rows.length) {
		frappe.msgprint(__("当前凭证没有可删除的分录，请刷新报表或打开原单处理"));
		return;
	}
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	const dialog = new frappe.ui.Dialog({
		title: __("删除行"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p>${esc(
					__(
						"选中分录后仅标记待删除，请调整剩余金额并统一保存。已记账凭证保存时会生成修订，原凭证历史仍保留。"
					)
				)}</p>`,
			},
			{
				fieldname: "source_row",
				fieldtype: "Select",
				label: __("原分录行"),
				reqd: 1,
				default: rows[0].edit_key,
				options: rows.map((row, index) => ({
					value: row.edit_key,
					label: esc(
						__("第 {0} 行 · {1} · {2} {3}", [
							row.entry_idx || index + 1,
							row.account_label || row.account,
							row.debit ? __("借方") : __("贷方"),
							frappe.format(
								Number(row.debit || row.credit || 0),
								{
									fieldtype: "Currency",
									options: "currency",
									precision: row.amount_precision,
								},
								{ only_value: true },
								{ currency: row.currency || row.account_currency }
							),
						])
					),
				})),
			},
		],
		primary_action_label: __("标记删除"),
		primary_action(values) {
			const row = rows.find((item) => item.edit_key === values.source_row);
			if (row && stage(row)) dialog.hide();
		},
		onhide() {
			report._china_delete_dialog = null;
		},
	});
	report._china_delete_dialog = dialog;
	dialog.show();
};
