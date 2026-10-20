/* global china_finance */
frappe.provide("china_finance.voucher_entry_split");

(() => {
	const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
	const service = "china_finance.services.source_voucher_edit";
	const format_amount = (item, value) =>
		frappe.format(
			value,
			{ fieldtype: "Currency", options: "currency", precision: item.amount_precision },
			{ only_value: true },
			{ currency: item.currency || item.account_currency }
		);

	china_finance.voucher_entry_split.open = async (report, row) => {
		try {
			await report.datatable?.cellmanager?.submitEditing?.();
			report.datatable?.cellmanager?.deactivateEditing(false);
		} catch (error) {
			frappe.msgprint(error.message);
			return;
		}
		if (!row.editable_split) return;
		if (report._china_inline_edit_state?.changes?.size) {
			frappe.msgprint(__("请先保存或取消当前凭证的修改，再新增一行"));
			return;
		}
		if (report._china_split_dialog) return;
		const candidates = [
			...new Map(
				(report.data || [row])
					.filter(
						(item) =>
							item.edit_voucher_key === row.edit_voucher_key && item.editable_split
					)
					.map((item) => [item.edit_key, item])
			).values(),
		];
		if (!candidates.length) {
			frappe.msgprint(__("当前凭证没有可新增行的分录，请刷新报表或打开原单处理"));
			return;
		}
		row = candidates.find((item) => item.edit_key === row.edit_key) || candidates[0];
		let original = Number(row.debit || row.credit || 0);
		let pending = false;
		let args = null;
		let closed = false;
		let dialog;
		const editable = (value) => {
			for (const field of ["source_row", "account", "amount", "summary"])
				dialog.set_df_property(field, "read_only", !value);
		};
		const reset = () => {
			if (pending || !dialog) return;
			args = null;
			editable(true);
			dialog.fields_dict.preview.$wrapper.empty();
			dialog.set_primary_action(__("预览新增"), run);
		};
		const select_source_row = async () => {
			if (!dialog || pending) return;
			const selected = candidates.find(
				(item) => item.edit_key === dialog.get_value("source_row")
			);
			if (!selected || selected === row) return;
			row = selected;
			original = Number(row.debit || row.credit || 0);
			reset();
			dialog.doc.currency = row.currency || row.account_currency;
			for (const field of ["original_amount", "amount"]) {
				dialog.set_df_property(field, "options", "currency");
				dialog.set_df_property(field, "precision", row.amount_precision);
			}
			await dialog.set_values({
				original_account: row.account,
				original_amount: original,
				account: "",
				amount: null,
				summary: "",
			});
		};
		const run = async () => {
			if (pending) return;
			const values = dialog.get_values();
			if (!values) return;
			const applying = !!args;
			if (
				!applying &&
				(!Number.isFinite(Number(values.amount)) ||
					Number(values.amount) <= 0 ||
					Number(values.amount) >= Math.abs(original))
			) {
				frappe.msgprint(__("新增行金额必须大于零，且小于原分录金额的绝对值"));
				return;
			}
			pending = true;
			editable(false);
			dialog.get_primary_btn().prop("disabled", true);
			try {
				const request = args || {
					source_doctype: row.edit_source_doctype,
					source_name: row.edit_source_name,
					modified: row.edit_source_modified,
					changes: [
						{
							field: "split",
							edit_key: row.edit_key,
							value: {
								account: values.account,
								amount: Math.sign(original) * Number(values.amount),
								summary: values.summary || "",
							},
						},
					],
				};
				const result = (
					await frappe.call({
						method: `${service}.${
							applying
								? "apply_inline_voucher_changes"
								: "preview_inline_voucher_changes"
						}`,
						type: applying ? "POST" : "GET",
						args: request,
						freeze: true,
					})
				).message;
				if (applying) {
					dialog.hide();
					frappe.show_alert({ message: result.message, indicator: "green" });
					await report.refresh();
					return;
				}
				if (closed) return;
				args = request;
				const bank_note =
					result.bank_action === "unreconcile_and_restore"
						? __("；系统将安全撤销并恢复银行核销")
						: "";
				const notice = result.requires_confirmation
					? __("该凭证已记账。确认后将取消原凭证、生成修订并重新记账{0}。", [bank_note])
					: __("草稿新增一行后仍需重新核对再记账");
				const amount = Math.sign(original) * Number(values.amount);
				const format = (value) => esc(format_amount(row, value));
				dialog.fields_dict.preview.$wrapper.html(`<p>${esc(
					notice
				)}</p><table class="table table-bordered">
					<thead><tr><th>${__("科目")}</th><th>${__("新增后金额")} (${
					row.debit ? __("借方") : __("贷方")
				})</th></tr></thead><tbody>
					<tr><td>${esc(row.account)}</td><td>${format(original - amount)}</td></tr>
					<tr><td>${esc(values.account)}</td><td>${format(amount)}</td></tr></tbody></table>`);
				dialog.set_primary_action(
					result.requires_confirmation ? __("确认更正") : __("保存新增"),
					run
				);
			} catch (error) {
				args = null;
				if (!closed) {
					dialog.fields_dict.preview.$wrapper.empty();
					dialog.set_primary_action(__("预览新增"), run);
					frappe.msgprint(error.message || __("操作失败，请检查凭证状态后重试"));
				}
			} finally {
				pending = false;
				if (!closed) {
					editable(!args);
					dialog.get_primary_btn().prop("disabled", false);
				}
			}
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
							"新增分录沿用原分录的往来单位、成本中心、项目及辅助维度；需要调整这些信息时请打开原单。"
						)
					)}</p>`,
				},
				{
					fieldname: "source_row",
					fieldtype: "Select",
					label: __("原分录行"),
					reqd: 1,
					default: row.edit_key,
					options: candidates.map((item, index) => ({
						value: item.edit_key,
						label: esc(
							__("第 {0} 行 · {1} · {2} {3}", [
								item.entry_idx || index + 1,
								item.account_label || item.account,
								item.debit ? __("借方") : __("贷方"),
								format_amount(item, Number(item.debit || item.credit || 0)),
							])
						),
					})),
					onchange: select_source_row,
				},
				{
					fieldname: "original_account",
					fieldtype: "Data",
					label: __("原科目"),
					default: row.account,
					read_only: 1,
				},
				{
					fieldname: "original_amount",
					fieldtype: "Currency",
					label: __("原分录金额"),
					default: original,
					options: "currency",
					precision: row.amount_precision,
					read_only: 1,
				},
				{
					fieldname: "account",
					fieldtype: "Link",
					options: "Account",
					label: __("新增分录科目"),
					reqd: 1,
					only_select: 1,
					get_query: () => ({
						query: `${service}.get_compatible_accounts`,
						filters: {
							source_doctype: row.edit_source_doctype,
							source_name: row.edit_source_name,
							edit_key: row.edit_key,
						},
					}),
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
				{ fieldname: "preview", fieldtype: "HTML" },
			],
			primary_action_label: __("预览新增"),
			primary_action: run,
			secondary_action_label: __("重新预览"),
			secondary_action: reset,
			onhide: () => {
				closed = true;
				report._china_split_dialog = null;
			},
		});
		report._china_split_dialog = dialog;
		dialog.show();
	};
})();
