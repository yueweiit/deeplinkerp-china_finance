for (const doctype of ["Journal Entry", "Payment Entry"]) {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			if (frm.is_new() || !frappe.model.can_read("China Bank Receipt")) return;
			frm.add_custom_button(__("银行回单"), async () => {
				const { message: receipts } = await frappe.call({ method: "china_finance.services.bank_receipt_import.get_voucher_receipts", args: { doctype: frm.doctype, name: frm.doc.name } });
				if (!receipts.length) return frappe.msgprint(__("暂无关联银行回单，可从中国财务的银行回单导入入口补充"));
				const e = frappe.utils.escape_html;
				frappe.msgprint(receipts.map(r => `${frappe.utils.get_form_link("China Bank Receipt", r.name, true, r.receipt_number)} · ${e(__(r.status))} · <a href="${e(r.source_file)}#page=${r.page_number}" target="_blank" rel="noopener">${__("原件第 {0} 页第 {1} 张", [r.page_number, r.position])}</a>`).join("<br>"));
			});
		},
	});
}
