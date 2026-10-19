frappe.ui.form.on("China Finance Settings", {
	refresh(frm) {
		show_cash_flow_check_notice(frm);
		if (frm.is_new() || !frm.doc.company) return;
		frm.add_custom_button(__("同步科目配置"), () => sync_coa_profile(frm, false), __("中国科目模板"));
		frm.add_custom_button(__("恢复模板默认科目"), () => {
			frappe.confirm(__("这将把公司默认科目恢复为中国模板定义，是否继续？"), () => sync_coa_profile(frm, true));
		}, __("中国科目模板"));
		frm.add_custom_button(__("检查主数据"), () => sync_master_data(frm, false), __("中国科目模板"));
		frm.add_custom_button(__("补齐空白主数据配置"), () => {
			frappe.confirm(__("只补齐空白的默认科目、税务建议、现金范围和收付款方式科目，不会覆盖已有配置或创建业务主数据。是否继续？"), () => sync_master_data(frm, true));
		}, __("中国科目模板"));
		load_configuration_readiness(frm);
	},
	defer_cash_flow_confirmation_on_monthly_close: show_cash_flow_check_notice,
});

function show_cash_flow_check_notice(frm) {
	frm.set_intro(frm.doc.defer_cash_flow_confirmation_on_monthly_close
		? __("月结暂不强制确认现金流项目；未确认事项仍需后续核对，年结和正式报表的确认要求不变。")
		: "", "orange");
}

function sync_coa_profile(frm, repairDefaults) {
	frappe.call({
		method: "china_finance.api.sync_china_coa_profile",
		args: { company: frm.doc.company, repair_defaults: repairDefaults ? 1 : 0 },
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}

function sync_master_data(frm, repair) {
	frappe.call({
		method: "china_finance.api.sync_china_coa_master_data",
		args: { company: frm.doc.company, repair: repair ? 1 : 0 },
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}

function load_configuration_readiness(frm) {
	frm.dashboard.parent.find(".china-finance-readiness").closest(".form-dashboard-section").remove();
	frappe.call({
		method: "china_finance.services.configuration_readiness.get_configuration_readiness",
		args: { company: frm.doc.company },
		callback: (result) => {
			if (!result.message) return;
			const section = frm.dashboard.add_section(render_readiness(result.message), __("配置就绪度"));
			section.find(".china-finance-readiness [data-route]").on("click", function () {
				const route = JSON.parse($(this).attr("data-route"));
				if (route.type === "form") {
					frappe.set_route("Form", route.name, route.filters.name);
				} else {
					frappe.set_route("List", route.name, route.filters);
				}
			});
			frm.dashboard.show();
		},
		error: (error) => {
			frm.dashboard.add_section(
				`<div class="china-finance-readiness text-muted small">${frappe.utils.escape_html(error.message || __("无法加载配置就绪度"))}</div>`,
				__("配置就绪度"),
			);
			frm.dashboard.show();
		},
	});
}

function render_readiness(readiness) {
	const escape = frappe.utils.escape_html;
	const sections = readiness.sections.map((section) => {
		const items = section.items.map((item) => {
			const status = item.passed ? __("已就绪") : __("待处理");
			const color = item.passed ? "text-success" : "text-danger";
			return `<button class="btn btn-sm btn-default text-left w-100 mb-2" data-route='${escape(JSON.stringify(item.route))}'>
				<span class="${color}">${escape(status)}</span>
				<span class="ml-2 font-weight-bold">${escape(item.label)}</span>
				<span class="text-muted ml-2">${escape(item.details || "")}</span>
			</button>`;
		}).join("");
		return `<div class="mb-3"><div class="text-muted small mb-2">${escape(section.label)}</div>${items}</div>`;
	}).join("");
	return `<div class="china-finance-readiness">${sections}</div>`;
}
