frappe.provide("china_finance.month_end");

(() => {
	const call = async (method, name) => (await frappe.call({method: `china_finance.services.month_end.${method}`, args: {name}, type: "POST"})).message;
	const esc = value => frappe.utils.escape_html(String(value ?? ""));
	const labels = {Posting: "月末记账", Checking: "月末检查", Closing: "结转损益", Ready: "待确认结账", Paused: "已暂停", Failed: "待处理问题"};
	const clear_status = frm => frm.layout?.message?.find(".china-month-end-status").closest(".form-message").remove();
	const set_status = (frm, html, color) => {
		// Frappe's set_intro appends a new form message, so remove only our previous status.
		clear_status(frm);
		frm.set_intro(`<span class="china-month-end-status">${html}</span>`, color);
	};
	const message_html = value => String(value ?? "").split(/(?:<br\s*\/?>|\r?\n)+/i).filter(Boolean).map(esc).join("<br>");
	const show = (frm, result) => {
		set_status(frm, `${__("{0} · {1}/{2} 张", [esc(__(labels[result.state] || result.state)), result.completed || 0, result.total || 0])}<br>${message_html(result.message)}`, ["Failed", "Paused"].includes(result.state) ? "orange" : "blue");
	};
	const run = async frm => {
		if (frm.__month_running) return;
		frm.__month_running = true;
		frm.__month_stop = false;
		try {
			if (frm.is_new() || frm.is_dirty()) await frm.save();
			let result = await call("start", frm.doc.name);
			show(frm, result);
			frm.add_custom_button(__("暂停"), async () => {
				frm.__month_stop = true;
				await call("pause", frm.doc.name);
				await frm.reload_doc();
			});
			while (!frm.__month_stop && ["Posting", "Checking", "Closing"].includes(result.state)) {
				result = await call("advance", frm.doc.name);
				show(frm, result);
				if (result.state === "Closing") await new Promise(resolve => setTimeout(resolve, 2000));
			}
		} finally {
			frm.__month_running = false;
			await frm.reload_doc();
		}
	};
	china_finance.month_end.render = frm => {
		for (const field of ["naming_series", "status", "previous_frozen_date", "closed_by", "closed_on", "reopened_by", "reopened_on", "reopen_reason", "checks_section", "checks", "notes", "preparation_state", "preparation_message"]) {
			frm.toggle_display(field, false);
		}
		frm.toggle_display("archive_package", !!frm.doc.archive_package);
		frm.toggle_display("period_closing_voucher", !!frm.doc.period_closing_voucher);
		frm.set_df_property("period_closing_voucher", "read_only", 1);
		const data = JSON.parse(frm.doc.preparation_data || "{}");
		if (frm.doc.docstatus === 1) {
			set_status(frm, __(frm.doc.archive_package ? "已结账，结账档案已生成" : "已结账，结账档案正在生成，可稍后刷新"), "green");
		} else if (frm.doc.preparation_state) {
			show(frm, {state: frm.doc.preparation_state, message: frm.doc.preparation_message, completed: data.done?.length, total: data.items?.length});
		} else {
			clear_status(frm);
		}
		frm.fields_dict.preparation_preview.$wrapper.html(`<p>${__("系统会完成本期凭证记账、月末检查和损益结转；核对财务报表后确认结账。关闭页面不会丢失进度。")}</p>${(data.checks || []).length ? `<table class="table table-bordered"><thead><tr><th>${__("结账检查")}</th><th>${__("结果")}</th><th>${__("说明")}</th></tr></thead><tbody>${data.checks.map(r => `<tr><td>${esc(__(r.description))}</td><td>${esc(__(r.passed ? "通过" : r.severity === "Blocking" ? "待处理" : "提示"))}</td><td>${esc(r.details)}</td></tr>`).join("")}</tbody></table>` : ""}`);
		frm.add_custom_button(__("本期凭证"), () => {
			frappe.route_options = {company: frm.doc.company, from_date: frm.doc.from_date, to_date: frm.doc.to_date, period_preset: "", voucher_status: "全部有效凭证"};
			frappe.set_route("query-report", "China Voucher Ledger");
		});
		frm.add_custom_button(__("试算平衡"), () => frappe.require("/assets/china_finance/js/voucher_preparation.js", () => china_finance.preparation.trial(frm.doc.company, frm.doc.from_date, frm.doc.to_date)));
		frm.add_custom_button(__("财务报表"), () => {
			frappe.route_options = {company: frm.doc.company, from_date: frm.doc.from_date, to_date: frm.doc.to_date};
			frappe.set_route("query-report", "China Financial Statements");
		});
		frm.add_custom_button(__("现金流量"), () => frappe.set_route("List", "China Cash Flow Assignment", {company: frm.doc.company}));
		if (frappe.model.can_write("China Finance Settings")) {
			frm.add_custom_button(__("现金流检查设置"), () => frappe.set_route("Form", "China Finance Settings", frm.doc.company));
		}
		if (frm.doc.docstatus === 1) {
			return;
		}
		if (frm.doc.preparation_state === "Ready") {
			frm.page.set_primary_action(__("确认结账"), () => frappe.confirm(__("已核对财务报表，确认结账并生成结账档案？"), async () => {
				await call("finish", frm.doc.name); await frm.reload_doc();
			}));
		} else {
			frm.page.set_primary_action(__(frm.doc.preparation_state ? "继续月末结账" : "开始月末结账"), () => frappe.confirm(__("系统将按日期记账本期已核对凭证，并完成月末检查和损益结转。确认开始？"), () => run(frm)));
		}
		if (["Posting", "Checking", "Closing", "Ready"].includes(frm.doc.preparation_state) || frm.__month_running) {
			frm.add_custom_button(__("暂停"), async () => {
				frm.__month_stop = true;
				await call("pause", frm.doc.name);
				await frm.reload_doc();
			});
		}
	};
})();
