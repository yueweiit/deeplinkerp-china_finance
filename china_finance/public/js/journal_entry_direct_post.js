function china_finance_can_direct_post(frm) {
	return Boolean(
		frm.__china_preparation_mode === false
		&& frm.is_new()
		&& frm.doc.docstatus === 0
		&& !frm.doc.custom_china_bank_transaction
		&& !String(frm.doc.title || "").trim().startsWith("Excel导入：")
		&& frm.perm?.[0]?.create
		&& frm.perm?.[0]?.submit
		&& !frappe.model.has_workflow?.(frm.doctype)
	);
}

function china_finance_set_direct_post_action(frm) {
	if (!china_finance_can_direct_post(frm)) return;

	frm.page.set_primary_action(
		__("保存并记账"),
		() => china_finance_save_and_post(frm),
		null,
		__("正在保存并记账...")
	);
	if (!frm.__china_finance_draft_save_button_added) {
		frm.add_custom_button(__("仅保存草稿"), () => frm.save("Save"), __("更多"));
		frm.__china_finance_draft_save_button_added = true;
	}
}

function china_finance_save_and_post(frm) {
	if (frm.__china_finance_direct_posting) return;
	frm.__china_finance_direct_posting = true;
	frm.page.btn_primary.prop("disabled", true);
	frappe.validated = true;

	let request = frm.script_manager.trigger("before_submit");
	request = request.then(() => {
		if (!frappe.validated) return null;
		frappe.validated = true;
		return frm.script_manager.trigger("validate");
	});
	request = request.then(() => {
		if (!frappe.validated) return null;
		return frm.script_manager.trigger("before_save");
	});
	request = request.then(() => {
		if (!frappe.validated) return null;
		return frappe.call({
			method: "china_finance.services.voucher.save_and_post_journal_entry",
			args: { doc: frm.doc },
			freeze: true,
			freeze_message: __("正在保存并记账..."),
		});
	});

	request
		.then((response) => {
			if (!response?.message) return;
			frappe.model.sync(response.message);
			frappe.set_route("Form", "Journal Entry", response.message.name);
			return china_finance_source_snapshot.posting_result(response.message);
		})
		.catch((error) => console.error(error))
		.finally(() => {
			frm.__china_finance_direct_posting = false;
			frm.page.btn_primary.prop("disabled", false);
		});
}

function china_finance_saved_post_action(frm) {
	if (frm.is_new() || frm.doc.docstatus !== 0 || frm.doc.amended_from || frappe.model.has_workflow?.(frm.doctype)) return;
	const state = frm.__china_posting_state;
	if (!state || !frm.perm?.[0]?.submit || frm.save_disabled || frm.toolbar?.read_only) return;
	if (frm.perm?.[0]?.write) frm.add_custom_button(__("仅保存草稿"), () => frm.save("Save"), __("更多"));
	if (state.preparation_enabled && (!state.ready || frm.is_dirty())) {
		frm.page.set_primary_action(__("保存草稿"), () => frm.save());
		return;
	}
	frm.page.set_primary_action(frm.is_dirty() ? __("保存并记账") : __("记账"), () => china_finance_post_saved(frm));
}

async function china_finance_post_saved(frm) {
	if (frm.__china_finance_direct_posting || frm.doc.docstatus !== 0) return;
	frm.__china_finance_direct_posting = true;
	const name = frm.doc.name, company = frm.doc.company;
	const release = () => {frm.__china_finance_direct_posting = false;};
	try {
		if (frm.is_dirty()) {
			await new Promise((resolve, reject) => {
				frm.save("Save", response => response?.exc ? reject(new Error(__("凭证保存失败，请检查后重试"))) : resolve(),
					null, () => reject(new Error(__("凭证保存失败，请检查后重试"))));
			});
			if (frm.is_dirty()) throw new Error(__("凭证修改尚未保存，请检查保存提示后重试"));
		}
		if (frm.doc.name !== name || frm.doc.company !== company) {release(); return;}
		// Keep native confirmation, before_submit, version checks and all hooks.
		// Its cancellation callback also releases our double-click guard.
		frm.savesubmit(null, release, release).catch(error => {release(); frappe.msgprint(error?.message || __("记账未完成，请刷新凭证核对结果"));});
	} catch (error) {release(); frappe.msgprint(error?.message || __("记账未完成，请刷新凭证核对结果"));}
}

frappe.ui.form.on("Journal Entry", {
	on_submit(frm) { return china_finance_source_snapshot.posting_result(frm.doc); },
	after_workflow_action(frm) { if (frm.doc.docstatus === 1) return china_finance_source_snapshot.posting_result(frm.doc); },
	async refresh(frm) {
		$(frm.wrapper).off("dirty.china_finance_direct_post");
		const request = frm.__china_posting_request = (frm.__china_posting_request || 0) + 1;
		const identity = JSON.stringify([frm.doc.name, frm.doc.company, frm.doc.posting_date, frm.doc.modified]);
		if (!frm.is_new() && frm.doc.docstatus === 0 && !frm.doc.amended_from) {
			const response = await frappe.call({method: "china_finance.services.voucher_posting_status.get_posting_state",
				args: {source_doctype: frm.doctype, source_name: frm.doc.name}});
			if (request !== frm.__china_posting_request || identity !== JSON.stringify([frm.doc.name, frm.doc.company, frm.doc.posting_date, frm.doc.modified])) return;
			frm.__china_posting_state = response.message;
			frm.__china_preparation_mode = !!response.message?.preparation_enabled;
		} else if (frm.doc.company) {
			const response = await frappe.call({method: "china_finance.services.voucher_preparation.get_mode", args: {company: frm.doc.company, posting_date: frm.doc.posting_date}});
			frm.__china_preparation_mode = !!response.message?.enabled;
		}
		if (request !== frm.__china_posting_request || identity !== JSON.stringify([frm.doc.name, frm.doc.company, frm.doc.posting_date, frm.doc.modified])) return;
		if (frappe.model.has_workflow?.(frm.doctype) || frm.doc.amended_from) return;
		$(frm.wrapper).on("dirty.china_finance_direct_post", () => setTimeout(() => {
			if (identity === JSON.stringify([frm.doc.name, frm.doc.company, frm.doc.posting_date, frm.doc.modified])) china_finance_saved_post_action(frm);
		}, 0));
		if (frm.__china_preparation_mode && frm.doc.docstatus === 0) {
			frm.page.set_primary_action(__("保存草稿"), () => frm.save());
			if (!frm.is_new()) frm.add_custom_button(__("核对完成，待记账"), async () => {
				if (frm.is_dirty()) await frm.save();
				if (frm.is_dirty()) return;
				await frappe.require("/assets/china_finance/js/voucher_preparation.js");
				await china_finance.preparation.review([frm.doc.name]);
				frm.reload_doc();
			});
			china_finance_saved_post_action(frm); return;
		}
		if (!china_finance_can_direct_post(frm)) {china_finance_saved_post_action(frm); return;}
		frm.__china_finance_draft_save_button_added = false;

		// The native dirty handler restores the primary button to “Save”. Reapply
		// our action after it runs so the behavior remains consistent while editing.
		$(frm.wrapper)
			.off("dirty.china_finance_direct_post")
			.on("dirty.china_finance_direct_post", () => {
				setTimeout(() => china_finance_set_direct_post_action(frm), 0);
			});
		china_finance_set_direct_post_action(frm);
	},
});
