frappe.ui.form.on("China Statement Opening Balance", {
	setup(frm) {
		frm.set_query("template", () => ({filters: {statement_type: "Balance Sheet", is_active: 1}}));
	},
	refresh(frm) {
		frm.set_intro(__("该基准直接保存来源报表金额，仅用于年初列；不生成凭证，不修改总账，已有快照和档案不会自动更新。"), "blue");
		if (frm.doc.docstatus !== 0) return;
		frm.add_custom_button(__("预填报表项目"), async () => {
			if (!frm.doc.company || !frm.doc.opening_date) {
				frappe.msgprint(__("请先选择公司及报表年初日期"));
				return;
			}
			const company = frm.doc.company;
			const date = frm.doc.opening_date;
			const {message: result} = await frappe.call({
				method: "china_finance.services.statement_opening.get_definition",
				args: {company, opening_date: date},
			});
			if (company !== frm.doc.company || date !== frm.doc.opening_date || frm.doc.docstatus !== 0) return;
			if (frm.doc.rows?.length && frm.doc.template && frm.doc.template !== result.template) {
				frappe.msgprint(__("已有项目属于其他模板，请先核对并移除后再预填"));
				return;
			}
			await frm.set_value({template: result.template, opening_date: result.opening_date, source_period_end: result.source_period_end});
			const existing = new Set((frm.doc.rows || []).map((row) => row.row_code));
			for (const row of result.rows) {
				if (!existing.has(row.row_code)) frm.add_child("rows", {...row, provided: 0, currency: frm.doc.currency});
			}
			frm.refresh_field("rows");
		});
	},
});
