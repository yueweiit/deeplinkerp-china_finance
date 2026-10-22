function remove_empty_check_rows(frm) {
	const rows = frm.doc.checks || [];
	const populated_rows = rows.filter((row) =>
		["check_code", "description", "severity", "details"].some((fieldname) => row[fieldname])
	);

	if (populated_rows.length === rows.length) return;

	frm.clear_table("checks");
	populated_rows.forEach((row) => {
		const child = frm.add_child("checks");
		["check_code", "description", "severity", "passed", "details"].forEach((fieldname) => {
			child[fieldname] = row[fieldname];
		});
	});
	frm.refresh_field("checks");
}

function lock_check_grid(frm) {
	const checks_field = frm.get_field("checks");
	if (!checks_field) return;

	// 检查结果只能由“运行结账检查”生成，用户不能手工添加、删除或编辑。
	frm.set_df_property("checks", "cannot_add_rows", true);
	frm.set_df_property("checks", "cannot_delete_rows", true);
	remove_empty_check_rows(frm);
}

function month_period(month) {
	if (!/^\d{4}-\d{2}$/.test(month || "")) return null;
	const [year, month_number] = month.split("-").map(Number);
	if (month_number < 1 || month_number > 12) return null;

	const last_day = new Date(year, month_number, 0).getDate();
	return {
		from_date: `${year}-${String(month_number).padStart(2, "0")}-01`,
		to_date: `${year}-${String(month_number).padStart(2, "0")}-${String(last_day).padStart(2, "0")}`,
	};
}

function sync_period_from_month(frm) {
	if (frm.doc.closing_type !== "Monthly") return;
	const period = month_period(frm.doc.closing_month);
	if (!period) {
		if (!frm.doc.closing_month) {
			if (frm.doc.from_date) frm.set_value("from_date", null);
			if (frm.doc.to_date) frm.set_value("to_date", null);
		}
		return;
	}

	if (frm.doc.from_date !== period.from_date) frm.set_value("from_date", period.from_date);
	if (frm.doc.to_date !== period.to_date) frm.set_value("to_date", period.to_date);
}

function infer_month_from_period(frm) {
	if (frm.doc.closing_type !== "Monthly" || frm.doc.closing_month || !frm.doc.from_date) return;
	const match = String(frm.doc.from_date).match(/^(\d{4})-(\d{2})-\d{2}$/);
	if (!match) return;
	frm.doc.closing_month = `${match[1]}-${match[2]}`;
	frm.refresh_field("closing_month");
}

function setup_month_picker(frm) {
	const field = frm.get_field("closing_month");
	if (!field || !field.$input) return;

	field.$input.attr("type", "month");
	field.$input.attr("placeholder", __("选择月份"));
	field.$input.off("change.china_closing_month").on("change.china_closing_month", () => {
		sync_period_from_month(frm);
	});
}

function initialize_amended_closing_run(frm) {
	if (!frm.is_new() || !frm.doc.amended_from || frm.__china_closing_amendment_initialized) return;

	frm.__china_closing_amendment_initialized = true;
	Object.assign(frm.doc, {
		status: "Draft",
		archive_package: null,
		previous_frozen_date: null,
		closed_by: null,
		closed_on: null,
		reopened_by: null,
		reopened_on: null,
		reopen_reason: null,
		closing_month: null,
	});
	frm.clear_table("checks");
	frm.refresh_fields();
}

async function save_latest_closing_run(frm) {
	if (!frm.is_dirty()) return;

	await frm.save();
	if (frm.is_dirty()) {
		throw new Error(__("月末结账单保存失败，请刷新页面后重试"));
	}
}

function get_closing_action_error(error) {
	const response = error?.responseJSON || frappe.last_response || error || {};
	if (response._server_messages) {
		try {
			const messages = JSON.parse(response._server_messages)
				.map((item) => {
					if (typeof item !== "string") return item.message || item.title || "";
					try {
						const parsed = JSON.parse(item);
						return parsed.message || parsed.title || item;
					} catch (_parse_error) {
						return item;
					}
				})
				.filter(Boolean);
			if (messages.length) return messages.join("<br>");
		} catch (_parse_error) {
			return response._server_messages;
		}
	}
	return response._error_message
		|| response.message
		|| error?.message
		|| response.exc_type
		|| error?.statusText
		|| __("操作失败，请刷新页面后重试");
}

function show_closing_action_error(title, error) {
	console.error(error);
	frappe.msgprint({
		title: __(title),
		message: get_closing_action_error(error),
		indicator: "red",
	});
}

function open_period_vouchers(frm) {
	frappe.route_options = {
		company: frm.doc.company,
		from_date: frm.doc.from_date,
		to_date: frm.doc.to_date,
		period_preset: "",
		voucher_status: "全部有效凭证",
	};
	frappe.set_route("query-report", "China Voucher Ledger");
}

async function create_reclosing_run(frm) {
	const response = await frappe.call({
		method: "china_finance.services.closing_lifecycle.get_or_create_reclosing",
		args: { name: frm.doc.name }, type: "POST", freeze: true,
	});
	frappe.set_route("Form", "China Closing Run", response.message.name);
}

async function reverse_closing(frm) {
	if (frm.__china_closing_action_running) return;
	frm.__china_closing_action_running = true;
	try {
		const preview_response = await frappe.call({
			method: "china_finance.services.closing.preview_reverse_closing",
			args: { name: frm.doc.name },
		});
		const preview = preview_response.message || {};
		const periods = (preview.runs || [])
			.map((run) => `${frappe.datetime.str_to_user(run.from_date)} - ${frappe.datetime.str_to_user(run.to_date)}`)
			.join("<br>");
		const vouchers = (preview.period_closing_vouchers || [])
			.filter((voucher) => voucher.docstatus === 1)
			.map((voucher) => frappe.utils.escape_html(voucher.name))
			.join("、");
		frappe.prompt(
			[
				{
					fieldtype: "HTML",
					options: `<p>${__("系统将一次完成以下操作：")}</p>
						<ul><li>${__("反结账期间：")}<br>${periods}</li>
						<li>${__("自动撤销损益结转凭证：{0}", [vouchers || __("无")])}</li>
						<li>${__("账务截止日期恢复至：{0}", [preview.previous_frozen_date ? frappe.datetime.str_to_user(preview.previous_frozen_date) : __("未设置")])}</li></ul>`,
				},
				{ fieldname: "reason", fieldtype: "Small Text", label: __("反结账原因"), reqd: 1 },
			],
			async (values) => {
				frappe.dom.freeze(__("正在反结账并撤销损益结转..."));
				try {
					const response = await frappe.call({
						method: "china_finance.services.closing.reverse_closing",
						args: { name: frm.doc.name, reason: values.reason },
						type: "POST",
					});
					const result = response.message || {};
					const runs = (result.reopened_runs || []).map((run) => run.name).join("、");
					const cancelled = (result.cancelled_period_closing_vouchers || []).join("、");
					const pending = (result.pending_period_closing_vouchers || []).join("、");
					const messages = [
						__("已开始反结账：{0}", [runs]),
						__("已撤销损益结转：{0}", [cancelled || __("无")]),
						__("总账撤销完成后才可修改凭证或重新结账，请在本单检查处理进度。"),
					];
					if (pending) messages.splice(2, 0, __("以下凭证的总账撤销仍在后台处理：{0}", [pending]));
					frappe.msgprint({ message: messages.join("<br>"), indicator: pending ? "orange" : "green" });
					await frm.reload_doc();
				} catch (error) {
					show_closing_action_error(__("反结账失败"), error);
				} finally {
					frappe.dom.unfreeze();
				}
			},
			__("反结账"),
			__("确认反结账")
		);
	} catch (error) {
		show_closing_action_error(__("反结账检查失败"), error);
	} finally {
		frm.__china_closing_action_running = false;
	}
}

async function generate_period_closing_voucher(frm) {
	if (frm.__china_closing_action_running) return;

	frm.__china_closing_action_running = true;
	frappe.dom.freeze(__("正在生成并保存损益结转凭证..."));
	try {
		const response = await frappe.call({
			method: "china_finance.services.closing.create_period_closing_voucher",
			args: {
				company: frm.doc.company,
				from_date: frm.doc.from_date,
				to_date: frm.doc.to_date,
				closing_type: frm.doc.closing_type,
			},
		});
		const result = response.message || {};
		if (!result.name) return;

		await frm.set_value("period_closing_voucher", result.name);
		if (!frm.is_new()) {
			await save_latest_closing_run(frm);
			await frm.reload_doc();
		}
		frappe.msgprint({
			message: result.message + (frm.is_new()
				? __("；当前月末结账单尚未保存，请保存后再完成损益结转")
				: ""),
			primary_action: {
				label: __("打开损益结转凭证"),
				action: () => frappe.set_route("Form", "Period Closing Voucher", result.name),
			},
		});
	} catch (error) {
		show_closing_action_error(__("损益结转凭证生成失败"), error);
	} finally {
		frappe.dom.unfreeze();
		frm.__china_closing_action_running = false;
	}
}

async function run_closing_checks(frm) {
	if (frm.__china_closing_action_running) return;
	if (!frm.doc.period_closing_voucher) {
		frappe.msgprint({
			message: __("请先生成或选择损益结转凭证"),
			indicator: "orange",
		});
		return;
	}

	frm.__china_closing_action_running = true;
	frappe.dom.freeze(__("正在运行并保存结账检查..."));
	try {
		const response = await frappe.call({
			method: "china_finance.services.closing.preview_closing_checks",
			args: {
				company: frm.doc.company,
				from_date: frm.doc.from_date,
				to_date: frm.doc.to_date,
				period_closing_voucher: frm.doc.period_closing_voucher,
				closing_type: frm.doc.closing_type,
			},
		});
		frm.clear_table("checks");
		(response.message || []).forEach((row) => frm.add_child("checks", row));
		frm.refresh_field("checks");
		lock_check_grid(frm);
		await save_latest_closing_run(frm);
		await frm.reload_doc();
		frappe.show_alert({
			message: __("结账检查已生成并保存为草稿"),
			indicator: "green",
		});
	} catch (error) {
		show_closing_action_error(__("结账检查保存失败"), error);
	} finally {
		frappe.dom.unfreeze();
		frm.__china_closing_action_running = false;
	}
}

async function submit_and_complete_closing(frm) {
	if (frm.__china_closing_action_running || frm.is_new() || frm.doc.docstatus !== 0) return;

	frm.__china_closing_action_running = true;
	frappe.dom.freeze(__("正在提交并完成期末结账..."));
	try {
		await save_latest_closing_run(frm);
		const response = await frappe.call({
			method: "china_finance.services.closing.submit_closing_run",
			args: { name: frm.doc.name },
			type: "POST",
		});
		await frm.reload_doc();
		frappe.show_alert({
			message: response.message?.already_submitted
				? __("期末结账已经提交")
				: __("期末结账已提交并完成"),
			indicator: "green",
		}, 7);
	} catch (error) {
		show_closing_action_error(__("期末结账提交失败"), error);
	} finally {
		frappe.dom.unfreeze();
		frm.__china_closing_action_running = false;
	}
}

async function check_reversal_progress(frm, resume = false) {
	const response = await frappe.call({
		method: `china_finance.services.closing_reversal.${resume ? "resume_reversal" : "reversal_progress"}`,
		args: { name: frm.doc.name }, type: "POST", freeze: true,
	});
	const result = response.message || {};
	if (result.failed?.length) {
		frappe.msgprint({
			title: __("总账撤销失败"), indicator: "red",
			message: result.failed.map(row => `${frappe.utils.escape_html(row.name)}: ${frappe.utils.escape_html(row.error || "")}`).join("<br>"),
		});
	} else {
		frappe.show_alert({message: result.status === "Reopened" ? __("反结账已完成") : __("总账撤销正在处理中"), indicator: result.status === "Reopened" ? "green" : "orange"});
	}
	await frm.reload_doc();
}

frappe.ui.form.on("China Closing Run", {
	closing_month(frm) {
		sync_period_from_month(frm);
	},

	async refresh(frm) {
		initialize_amended_closing_run(frm);
		infer_month_from_period(frm);
		setup_month_picker(frm);
		sync_period_from_month(frm);
		const businessIndicators = {
			Closed: [__("已结账"), "green"], Reopened: [__("已反结账（历史）"), "grey"],
			Reopening: [__("反结账处理中"), "orange"], "Reopen Failed": [__("反结账失败"), "red"],
		};
		if (frm.doc.docstatus === 1 && businessIndicators[frm.doc.status]) {
			frm.page.set_indicator(...businessIndicators[frm.doc.status]);
		}
		if (frm.doc.company && frm.doc.from_date && frm.doc.to_date && frm.doc.docstatus === 0) {
			const response = await frappe.call({method: "china_finance.services.voucher_preparation.get_mode", args: {company: frm.doc.company, posting_date: frm.doc.from_date}});
			if (response.message?.enabled && frm.doc.closing_type === "Monthly") {
				await frappe.require("/assets/china_finance/js/month_end.js");
				china_finance.month_end.render(frm);
				if (frm.doc.reclosing_of) frm.add_custom_button(__("查看上次结账记录"), () => frappe.set_route("Form", "China Closing Run", frm.doc.reclosing_of));
				return;
			}
		}
		lock_check_grid(frm);
		if (frm.doc.company && frm.doc.from_date && frm.doc.to_date && frm.doc.docstatus === 0) {
			if (!frm.is_new()) {
				frm.page.set_primary_action(__("确认结账"), () => {
					frappe.confirm(
						__("确认提交并完成期末结账 {0}？", [frm.doc.name]),
						() => submit_and_complete_closing(frm)
					);
				}, "check");
			}
			frm.add_custom_button(__(frm.doc.closing_type === "Monthly"
				? "生成损益结转凭证"
				: "生成年度损益结转凭证"), () => generate_period_closing_voucher(frm), __("月末结账"));
			frm.add_custom_button(__("生成本期对账单"), () => frappe.call({
				method: "china_finance.services.reconciliation_control.generate_required_statements",
				args: { company: frm.doc.company, from_date: frm.doc.from_date, to_date: frm.doc.to_date },
				freeze: true,
				callback: (response) => {
					const result = response.message || {};
					frappe.msgprint(__("处理 {0} 项，创建 {1} 张，已存在 {2} 张，失败 {3} 项", [result.processed || 0, result.created || 0, result.existing || 0, result.failed || 0]));
				},
			}), __("月末检查"));
			frm.add_custom_button(__("结账检查"), () => run_closing_checks(frm), __("月末检查"));
		}
		if (frm.doc.reclosing_of) {
			frm.add_custom_button(__("查看上次结账记录"), () => frappe.set_route("Form", "China Closing Run", frm.doc.reclosing_of));
		}
		if (frm.doc.docstatus === 1 && ["Reopening", "Reopen Failed"].includes(frm.doc.status)) {
			frm.set_intro(__("反结账总账撤销尚未完成，本期暂不能修改凭证或重新结账。"), "orange");
			frm.page.set_primary_action(__("检查反结账进度"), () => check_reversal_progress(frm));
			frm.add_custom_button(__("继续撤销总账"), () => check_reversal_progress(frm, true));
		}
		if (frm.doc.docstatus === 1 && frm.doc.status === "Closed") {
			frm.set_intro(frm.doc.archive_package ? __("已结账，结账档案已生成") : __("已结账，结账档案尚未就绪，可刷新或重新生成"), "green");
			if (!frm.doc.archive_package) frm.add_custom_button(__("重新生成结账档案"), async () => {
				const response = await frappe.call({method: "china_finance.services.month_end.retry_archive", args: {name: frm.doc.name}, type: "POST"});
				frappe.msgprint(response.message?.message || __("结账档案已生成"));
				frm.reload_doc();
			});
			frm.page.set_primary_action(__("反结账"), () => reverse_closing(frm));
		}
		if (frm.doc.docstatus === 1 && frm.doc.status === "Reopened") {
			frm.set_intro(__("已反结账，可以修改本期凭证；修改完成后重新结账。"), "orange");
			frm.page.set_primary_action(__("查看本期凭证"), () => open_period_vouchers(frm));
			frm.add_custom_button(__("重新结账"), () => create_reclosing_run(frm));
		}
	},
});
