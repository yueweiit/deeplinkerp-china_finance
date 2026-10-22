frappe.listview_settings["China Closing Run"] = {
	add_fields: ["status", "preparation_state", "reclosing_of"],
	has_indicator_for_draft: true,
	get_indicator(doc) {
		if (doc.docstatus === 2) return [__("已作废"), "grey", "docstatus,=,2"];
		const states = {
			Closed: [__("已结账"), "green"],
			Reopened: [__("已反结账（历史）"), "grey"],
			Reopening: [__("反结账处理中"), "orange"],
			"Reopen Failed": [__("反结账失败"), "red"],
		};
		if (doc.docstatus === 1 && states[doc.status]) {
			return [...states[doc.status], `status,=,${doc.status}`];
		}
		const phases = {
			Posting: [__("凭证记账中"), "blue"], Checking: [__("结账检查中"), "blue"],
			Closing: [__("损益结转中"), "blue"], Ready: [__("待确认结账"), "orange"],
			Paused: [__("已暂停"), "orange"], Failed: [__("处理失败"), "red"],
		};
		if (phases[doc.preparation_state]) return [...phases[doc.preparation_state], `preparation_state,=,${doc.preparation_state}`];
		return [__("待结账"), "grey", "docstatus,=,0"];
	},
};
