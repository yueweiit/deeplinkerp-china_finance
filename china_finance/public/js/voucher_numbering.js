/* global china_finance */
frappe.provide("china_finance.voucher_numbering");

(() => {
	const esc = value => frappe.utils.escape_html(String(value ?? ""));
	const roles = ["System Manager", "China Finance Manager", "Accounts Manager"];
	const method = "china_finance.services.voucher_number_organization.organize_monthly_voucher_numbers";

	china_finance.voucher_numbering.bind_report = report => {
		if (!roles.some(role => frappe.user.has_role(role))) return;
		report.page.add_menu_item(__("凭证整理编号"), () => open_dialog(report));
	};

	function open_dialog(report) {
		if (report._china_inline_edit_state?.changes?.size) {
			frappe.msgprint(__("请先保存或取消当前凭证的修改，再整理编号"));
			return;
		}
		let preview = null;
		let pending = false;
		let dialog;
		const invalidate = () => {
			preview = null;
			if (!dialog) return;
			dialog.set_value("acknowledge", 0);
			dialog.fields_dict.preview.$wrapper.empty();
			dialog.set_primary_action(__("预览编号"), run);
		};
		const run = async () => {
			if (pending) return;
			const values = dialog.get_values();
			if (!values) return;
			if (preview && !values.acknowledge) {
				frappe.msgprint(__("请确认受影响的打印件和导出文件需要重新生成"));
				return;
			}
			const applying = !!preview;
			pending = true;
			dialog.get_primary_btn().prop("disabled", true);
			for (const field of ["company", "accounting_period", "voucher_word", "reason", "acknowledge"]) {
				dialog.set_df_property(field, "read_only", 1);
			}
			try {
				const args = {
					company: values.company, accounting_period: values.accounting_period,
					voucher_word: values.voucher_word, reason: values.reason,
					apply: applying ? 1 : 0, expected_plan_hash: preview?.plan_hash,
				};
				const result = (await frappe.call({method, args, type: "POST", freeze: true})).message;
				if (applying) {
					if (result.applied) {
						frappe.msgprint({message: __("凭证编号已整理，批次 {0}。请重新运行报表，并重新生成受影响的打印件和导出文件。", [esc(result.batch_id)]), indicator: "green"});
					}
					dialog.hide();
					await report.refresh();
					return;
				}
				dialog.fields_dict.preview.$wrapper.html(render_preview(result));
				preview = result.can_apply ? result : null;
				if (preview) dialog.set_primary_action(__("确认整理"), run);
			} catch (error) {
				// A failed apply must be previewed again; keep the error visible to the user.
				if (applying) invalidate();
				console.error(error);
			} finally {
				pending = false;
				for (const field of ["company", "accounting_period", "voucher_word", "reason", "acknowledge"]) {
					dialog.set_df_property(field, "read_only", 0);
				}
				dialog.get_primary_btn().prop("disabled", false);
			}
		};
		dialog = new frappe.ui.Dialog({
			title: __("凭证整理编号"), size: "extra-large",
			fields: [
				{fieldname: "company", fieldtype: "Link", options: "Company", label: __("公司"), reqd: 1,
					default: report.get_filter_value("company"), onchange: invalidate},
				{fieldname: "accounting_period", fieldtype: "Data", label: __("结账月份"), reqd: 1,
					default: (report.get_filter_value("from_date") || frappe.datetime.get_today()).slice(0, 7), onchange: invalidate},
				{fieldname: "voucher_word", fieldtype: "Data", label: __("凭证字"), reqd: 1,
					default: "记", onchange: invalidate},
				{fieldname: "reason", fieldtype: "Small Text", label: __("整理原因"), reqd: 1},
				{fieldname: "acknowledge", fieldtype: "Check", label: __("已确认受影响的打印件和导出文件需要重新生成")},
				{fieldname: "preview", fieldtype: "HTML"},
			],
			primary_action_label: __("预览编号"), primary_action: run,
			secondary_action_label: __("重新预览"), secondary_action: () => {if (!pending) {invalidate(); run();}},
		});
		dialog.show();
		dialog.fields_dict.accounting_period.$input.attr("type", "month");
	}

	function render_preview(result) {
		const summary = __("有效凭证 {0} 张，保留取消历史 {1} 张，需改号 {2} 张；整理后下一字号为 {3}。",
			[result.voucher_count, result.history_count, result.changed_count, esc(result.next_number)]);
		const completeness = __("本月全部凭证字范围：待补齐正式凭证 {0} 张，未提交草稿 {1} 张。草稿不参与本次编号，后续记账或补录可能需要再次整理。",
			[result.missing_snapshot_count || 0, result.draft_count || 0]);
		const blockers = result.blockers.map(message => `<li>${esc(message)}</li>`).join("");
		const status = blockers
			? `<div class="alert alert-danger"><ul class="mb-0">${blockers}</ul></div>`
			: !result.needs_update ? `<div class="alert alert-success">${__("本月有效凭证编号已连续，无需整理")}</div>` : "";
		const rows = result.changes.map(row => `<tr><td>${esc(row.posting_date)}</td><td>${esc(row.source_name)}</td>
			<td>${esc(row.old_number)}</td><td>${esc(row.new_number)}</td></tr>`).join("");
		return `<p>${summary}</p><p>${esc(completeness)}</p>${status}<div style="max-height:360px;overflow:auto"><table class="table table-bordered">
			<thead><tr><th>${__("凭证日期")}</th><th>${__("来源单据")}</th><th>${__("原字号")}</th><th>${__("新字号")}</th></tr></thead>
			<tbody>${rows}</tbody></table></div>`;
	}
})();
