/* Hide the internal company suffix from Account link suggestions. */
(function patch_account_link_display() {
	const patch = () => {
		const ControlLink = frappe.ui?.form?.ControlLink;
		if (!ControlLink || ControlLink.prototype.__china_finance_account_display_patched) return;

		const native_setup = ControlLink.prototype.setup_awesomeplete;
		if (typeof native_setup !== "function") return;

		ControlLink.prototype.setup_awesomeplete = function () {
			native_setup.call(this);
			if (this.df?.fieldtype !== "Link" || this.df.options !== "Account" || !this.awesomplete) return;

			const native_item = this.awesomplete.item;
			if (native_item.__china_finance_account_display_patched) return;

			const render_item = function (item) {
				if (item?.value && item?.label) {
					item = { ...item, description: "" };
				}
				return native_item.call(this, item);
			};
			render_item.__china_finance_account_display_patched = true;
			this.awesomplete.item = render_item;
		};
		ControlLink.prototype.__china_finance_account_display_patched = true;
	};

	patch();
	if (!frappe.ui?.form?.ControlLink?.prototype?.__china_finance_account_display_patched) {
		const timer = setInterval(() => {
			patch();
			if (frappe.ui?.form?.ControlLink?.prototype?.__china_finance_account_display_patched) {
				clearInterval(timer);
			}
		}, 50);
		setTimeout(() => clearInterval(timer), 10000);
	}
})();
