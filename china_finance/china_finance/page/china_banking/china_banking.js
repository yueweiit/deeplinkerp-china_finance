frappe.pages["china-banking"].on_page_load = function (wrapper) {
	frappe.utils.set_title(__("银行对账"));
	wrapper.china_banking = new ChinaBankingPage(wrapper);
};

frappe.pages["china-banking"].on_page_show = function (wrapper) {
	frappe.utils.set_title(__("银行对账"));
	wrapper.china_banking.sync_company();
};

class ChinaBankingPage {
	constructor(wrapper) {
		this.wrapper = wrapper;
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("银行对账"),
			single_column: true,
		});
		// The embedded Banking app renders its own breadcrumb. Hide the Desk
		// page head so the two navigation rows do not appear one below another.
		this.page.page_head.hide();
		frappe.breadcrumbs.add("China Finance");
		this.mount_banking_app();
		window.china_finance?.company_context?.register_page("china-banking", {
			before_switch: () => new Promise(resolve => frappe.confirm(__("切换公司会重新加载银行对账，请先保存当前操作。是否继续？"), () => resolve(true), () => resolve(false))),
			capture: previous => previous,
			restore: company => this.sync_company(company, {wait: true}),
			switch_company: company => this.sync_company(company, {wait: true}),
		});
	}

	mount_banking_app() {
		this.sync_company();
		const $main = $(this.wrapper).find(".layout-main-section").first();
		$main.css({
			padding: "0",
			overflow: "hidden",
			background: "var(--card-bg)",
		});

		this.$iframe = $('<iframe>', {
			src: "/china_banking_embedded?embedded=1",
			title: __("银行对账"),
			class: "china-banking-embedded",
			frameborder: "0",
		}).css({
			display: "block",
			width: "100%",
			height: "calc(100vh - 128px)",
			minHeight: "640px",
			border: "0",
			background: "#fff",
		}).appendTo($main);
	}

	sync_company(company = frappe.route_options?.company || window.china_finance?.company_context?.get_company(), {wait = false} = {}) {
		if (!company) return;
		const key = `china_finance:company:${frappe.session.user}:bank`;
		const stored_company = sessionStorage.getItem(key);
		if (company === this.company && stored_company === JSON.stringify(company)) return true;
		this.company = company;
		// The embedded adapter maps only the native Banking company key to this tab.
		sessionStorage.setItem(key, JSON.stringify(company));
		const ready = wait ? this.wait_for_frame() : true;
		if (this.$iframe) this.$iframe.attr("src", this.$iframe.attr("src"));
		return ready;
	}

	wait_for_frame() {
		const frame = this.$iframe?.get?.(0);
		if (!frame) return Promise.resolve(true);
		return new Promise((resolve, reject) => {
			let poll;
			const cleanup = () => { clearTimeout(timeout); clearTimeout(poll); frame.removeEventListener("load", loaded); frame.removeEventListener("error", failed); };
			const failed = () => { cleanup(); reject(new Error(__("公司数据加载失败"))); };
			const loaded = () => {
				try {
					const root = frame.contentDocument?.querySelector("#root");
					if (!root) return failed();
					if (root.childElementCount) { cleanup(); resolve(true); }
					else poll = setTimeout(loaded, 50);
				} catch { failed(); }
			};
			const timeout = setTimeout(failed, 30000);
			frame.addEventListener("load", loaded); frame.addEventListener("error", failed);
		});
	}
}
