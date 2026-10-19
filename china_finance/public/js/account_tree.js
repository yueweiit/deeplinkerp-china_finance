/* Display Account.account_name while keeping Account.name as the node value. */
(function configure_account_tree_display() {
	const settings = frappe.treeview_settings?.Account;
	if (!settings) return;

	settings.get_tree_nodes = "china_finance.services.account_display.get_children";
	settings.get_label = (node) => {
		const title = node?.data?.title || node?.label || "";
		return frappe.utils.escape_html(title);
	};
	settings.add_tree_node = "china_finance.services.account_creation.add_account";
	settings.toolbar = (settings.toolbar || []).map((item) => {
		if (item.label !== "Add Child" && item.label !== __("Add Child")) return item;

		return {
			...item,
			// Keep the internal label equal to the native button so
			// extend_toolbar removes the native duplicate; get_label controls
			// what is shown to the user.
			label: __("Add Child"),
			get_label: () => __("新增下级科目"),
			click(node) {
				const treeview = frappe.views.trees["Account"];
				node = node || treeview?.tree?.get_selected_node();
				if (treeview && node) open_account_dialog(treeview, node);
			},
		};
	});

	const native_fields = settings.fields || [];
	const field = (fieldname) => native_fields.find((item) => item.fieldname === fieldname) || {};
	const escape_regexp = (value) => String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const refresh_preview = (control) => {
		const layout = control?.layout;
		if (!layout) return;

		const parent_account = layout.get_value("parent_account");
		const company = layout.get_value("company");
		const manual = Boolean(layout.__china_finance_manual_code);
		const manual_number = manual ? layout.get_value("account_number") || "" : "";
		if (!parent_account || !company) {
			if (!manual) layout.set_value("account_number", "");
			layout.set_value("account_name_preview", layout.get_value("account_name") || "");
			return;
		}

		frappe.call({
			method: "china_finance.services.account_creation.get_next_account_number",
			args: { parent_account, company },
			callback: (response) => {
				const result = response.message || {};
				if (layout.get_value("parent_account") !== parent_account) return;
				const current_manual = Boolean(layout.__china_finance_manual_code);
				const current_manual_number = current_manual
					? layout.get_value("account_number") || ""
					: "";
				if (!current_manual) {
					layout.__china_finance_setting_auto_code =
						(layout.__china_finance_setting_auto_code || 0) + 1;
					Promise.resolve(layout.set_value("account_number", result.account_number || ""))
						.finally(() => {
							layout.__china_finance_setting_auto_code = Math.max(
								(layout.__china_finance_setting_auto_code || 1) - 1,
								0
							);
						});
				}
				const number = current_manual ? current_manual_number : result.account_number || "";
				const name = layout.get_value("account_name") || "";
				const parent_name = result.parent_name || "";
				const child_name = name.replace(
					new RegExp(`^${escape_regexp(parent_name)}\\s*[-－—–]\\s*`),
					""
				);
				const full_name = result.include_parent_prefix
					? parent_name && child_name
						? `${parent_name}-${child_name}`
						: name
					: child_name || name;
				layout.set_value(
					"account_name_preview",
					number && full_name
						? `${number} - ${full_name}`
						: result.manual_required && !current_manual
							? __("{0}（请直接填写科目编码）", [full_name])
							: full_name
				);
			},
		});
	};

	const open_account_dialog = (treeview, node) => {
		if (node && !node.expandable) {
			frappe.msgprint(__("请选择分组科目后再新增下级科目"));
			return;
		}

		const dialog_fields = settings.fields.map((field) => ({ ...field }));
		const parent_field = dialog_fields.find((field) => field.fieldname === "parent_account");
		if (parent_field) {
			parent_field.read_only = Boolean(node && !node.is_root);
			parent_field.get_query = () => ({
				filters: {
					company: treeview.args.company,
					is_group: 1,
				},
			});
		}

		const dialog = new frappe.ui.Dialog({
			title: __("新建科目"),
			fields: dialog_fields,
		});
		dialog.set_value("is_group", 0);

		const initial_values = {
			company: treeview.args.company,
		};
		if (node && !node.is_root) initial_values.parent_account = node.label;

		// Wait for the read-only parent/company values to be set before showing
		// the dialog, then render the initial code preview exactly once.
		Promise.resolve(dialog.set_values(initial_values)).then(() => dialog.show());

		dialog.set_primary_action(__("创建科目"), () => {
			const values = dialog.get_values();
			if (!values) return;

			const button = dialog.get_primary_btn();
			button.prop("disabled", true).text(__("保存中..."));
			const parent = node && !node.is_root ? node.label : values.parent_account;
			const args = {
				...treeview.args,
				...values,
				doctype: "Account",
				parent: parent || null,
				is_root: !parent,
			};

			frappe.call({
				method: settings.add_tree_node || "frappe.desk.treeview.add_node",
				args,
				callback: (response) => {
					if (response.exc) return;
					dialog.hide();
					if (node) {
						treeview.tree.load_children(node);
					} else {
						treeview.make_tree();
					}
				},
				always: () => {
					button.prop("disabled", false).text(__("创建科目"));
				},
			});
		});
	};

	// The standard Account tree uses the primary "New" button to call the
	// native dialog directly. Route it through the same dialog so a new root
	// account can choose an existing parent account.
	const native_post_render = settings.post_render;
	settings.post_render = function (treeview) {
		native_post_render && native_post_render(treeview);
		if (!treeview.can_create) return;

		treeview.page.set_primary_action(
			__("新建"),
			() => {
				const root_company = treeview.page.fields_dict.root_company?.get_value();
				if (root_company && !frappe.flags.ignore_root_company_validation) {
					frappe.throw(__("请将科目添加到根公司 {0}"), [root_company]);
					return;
				}
				open_account_dialog(treeview, null);
			},
			"add"
		);
	};

	const search_account = async (treeview, search_control) => {
		const query = String(search_control?.get_value() || "").trim().toLocaleLowerCase();
		if (!query) {
			frappe.msgprint(__("请输入科目编码或名称"));
			search_control?.$input?.focus();
			return;
		}

		const company = treeview.page.fields_dict.company?.get_value();
		if (!company) {
			frappe.msgprint(__("请先选择公司"));
			return;
		}

		const accounts = await frappe.db.get_list("Account", {
			filters: { company, disabled: 0 },
			fields: ["name", "account_name", "account_number", "parent_account", "lft"],
			order_by: "lft asc",
			limit: 0,
		});
		const matching_accounts = (accounts || [])
			.filter((account) =>
				[account.account_number, account.account_name, account.name]
					.filter(Boolean)
					.some((value) => String(value).toLocaleLowerCase().includes(query))
			)
			.sort((left, right) => {
				const rank = (account) => {
					if (String(account.account_number || "").toLocaleLowerCase() === query) return 0;
					if (String(account.account_name || "").toLocaleLowerCase() === query) return 1;
					if (String(account.name || "").toLocaleLowerCase() === query) return 2;
					return 3;
				};
				return rank(left) - rank(right) || (left.lft || 0) - (right.lft || 0);
			});

		if (!matching_accounts.length) {
			frappe.msgprint({
				title: __("未找到科目"),
				message: __("当前公司没有匹配“{0}”的科目。", [query]),
				indicator: "orange",
			});
			return;
		}

		const account = matching_accounts[0];
		const accounts_by_name = Object.fromEntries(accounts.map((row) => [row.name, row]));
		const tree = treeview.tree;
		if (!tree) return;

		const ancestors = [];
		const visited = new Set([account.name]);
		let parent_name = account.parent_account;
		while (parent_name && !visited.has(parent_name)) {
			visited.add(parent_name);
			const parent = accounts_by_name[parent_name];
			if (!parent) break;
			ancestors.unshift(parent);
			parent_name = parent.parent_account;
		}

		const search_path = new Set([
			tree.root_node.label,
			...ancestors.map((ancestor) => ancestor.name),
			account.name,
		]);
		for (const node of Object.values(tree.nodes)) {
			if (!node.is_root && node.loaded && node.expanded && !search_path.has(node.label)) {
				tree.expand_node(node);
			}
		}

		if (!tree.root_node.loaded) await tree.load_children(tree.root_node);
		for (const ancestor of ancestors) {
			const node = tree.nodes[ancestor.name];
			if (!node) break;
			if (!node.loaded) await tree.load_children(node);
			else if (!node.expanded) tree.expand_node(node);
		}

		const target = tree.nodes[account.name];
		if (!target) {
			frappe.msgprint(__("科目已找到，但当前科目树无法展开到该位置，请刷新后重试。"));
			return;
		}
		if (target.expandable) {
			if (!target.loaded) await tree.load_children(target);
			else if (!target.expanded) tree.expand_node(target);
		}

		tree.set_selected_node(target);
		tree.select_link(target);
		target.$tree_link[0]?.scrollIntoView({ behavior: "smooth", block: "center" });
		treeview.__china_account_search_hit?.removeClass("china-account-search-hit");
		treeview.__china_account_search_hit = target.$tree_link.addClass("china-account-search-hit");
		const display_name = account.account_number
			? `${account.account_number} - ${account.account_name || account.name}`
			: account.account_name || account.name;
		frappe.show_alert({
			message: __("已定位科目：{0}", [display_name]),
			indicator: "green",
		});
	};

	const native_onload = settings.onload;
	settings.onload = function (treeview) {
		native_onload && native_onload(treeview);
		const search_control = treeview.page.add_field(
			{
				fieldtype: "Data",
				fieldname: "china_account_search",
				label: __("搜索科目"),
				placeholder: __("输入科目编码或名称"),
			},
			treeview.page.filters
		);
		const run_search = () => search_account(treeview, search_control);
		treeview.page.add_inner_button(__("搜索科目"), run_search, null, "primary");
		search_control?.$input?.on("keydown", (event) => {
			if (event.key !== "Enter") return;
			event.preventDefault();
			run_search();
		});
	};

	const account_name_field = {
		...field("account_name"),
		label: __("下级科目名称/大类名称"),
		description: __("普通下级科目只填写名称；在无编码大类下新增时，填写新的大类名称"),
		onchange() {
			refresh_preview(this);
		},
	};
	const account_number_field = {
		...field("account_number"),
		label: __("科目编码"),
		description: __("默认自动递增生成；直接修改后将按手动编码保存"),
		onchange() {
			const layout = this.layout;
			if (!layout || layout.__china_finance_setting_auto_code) return;

			layout.__china_finance_manual_code = true;
			refresh_preview(this);
		},
	};
	const optional_fields = native_fields.filter((item) =>
		["is_group", "root_type", "account_type", "account_category", "tax_rate", "account_currency"].includes(item.fieldname)
	);
	const is_group_field = optional_fields.find((item) => item.fieldname === "is_group") || {
		fieldtype: "Check",
		fieldname: "is_group",
		label: __("是否分组"),
	};

	settings.fields = [
		{
			fieldtype: "Link",
			fieldname: "parent_account",
			label: __("上级科目"),
			options: "Account",
			onchange() {
				refresh_preview(this);
			},
		},
		{
			fieldtype: "Link",
			fieldname: "company",
			label: __("公司"),
			options: "Company",
			read_only: 1,
		},
		account_name_field,
		account_number_field,
		{
			fieldtype: "Data",
			fieldname: "account_name_preview",
			label: __("保存前预览"),
			read_only: 1,
		},
		is_group_field,
		...optional_fields.filter((item) => item.fieldname !== "is_group"),
	];
	settings.ignore_fields = ["parent_account", "company", "root_type", "report_type"];
})();
