const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const native = path.resolve(__dirname, "../../../frappe/frappe");
const userSource = fs.readFileSync(path.join(native, "core/doctype/user/user.js"), "utf8");
const moduleSource = fs.readFileSync(path.join(native, "public/js/frappe/module_editor.js"), "utf8");

function harness({ userType = "Website User", isNew = false, canEdit = true, moduleProfile = "" } = {}) {
	const events = {};
	const stats = { roles: 0, modules: 0, dirty: 0, reload: 0, writes: 0 };
	function element() {
		return {
			children: [], attrs: {},
			appendTo(parent) { parent.children.push(this); return this; },
			find() { return this; },
			attr(key, value) { this.attrs[key] = value; return this; },
		};
	}
	const context = {
		__: (value) => value,
		cint: (value) => Number(value || 0),
		has_common: (left, right) => left.some((value) => right.includes(value)),
		$: (value) => typeof value === "string" ? element() : value,
		frappe: {
			ui: { form: {
				on(doctype, handlers) { events[doctype] = handlers; },
				make_control({ df, parent }) {
					stats.modules++;
					return {
						df, parent, selected_options: [],
						refresh_input() { this.options = df.get_data(); },
						get_unchecked_options() {
							return df.get_data().map((option) => option.value)
								.filter((value) => !this.selected_options.includes(value));
						},
					};
				},
			} },
			RoleEditor: class {
				constructor() { stats.roles++; }
				show() {}
				reset() {}
			},
			user_roles: canEdit ? ["System Manager"] : ["Desk User"],
			get_meta: () => ({ permissions: [{ permlevel: 1, write: 1, role: "System Manager" }] }),
			user: { has_role: (role) => canEdit && role === "System Manager" },
			session: { user: "Administrator" },
			boot: { sysdefaults: {} },
			sys_defaults: { time_zone: "Asia/Shanghai" },
			route_flags: {},
			xcall(method) {
				assert.equal(method, "frappe.apps.get_apps");
				return Promise.resolve([]);
			},
			db: { get_single_value: () => Promise.resolve(0) },
			model: {
				can_create: () => false,
				clear_doc(_doctype, name) {
					stats.writes++;
					frm.doc.block_modules = frm.doc.block_modules.filter((row) => row.name !== name);
				},
				add_child(doc, doctype, fieldname) {
					stats.writes++;
					const row = { doctype, name: `block-${stats.writes}` };
					doc[fieldname].push(row);
					return row;
				},
			},
		},
	};
	vm.createContext(context);
	vm.runInContext(moduleSource, context);
	vm.runInContext(userSource, context);
	const frm = {
		doc: {
			name: "operator@example.test", email: "operator@example.test", enabled: 1,
			user_type: userType, role_profiles: [], roles: [{ role: "Manufacturing User" }],
			module_profile: moduleProfile,
			block_modules: [{ doctype: "Block Module", name: "existing-block", module: "Accounts" }],
			__onload: { all_modules: ["Accounts", "Manufacturing", "Stock"] },
		},
		fields_dict: { roles_html: { wrapper: element() }, modules_html: { wrapper: element() } },
		is_new: () => isNew,
		trigger(event) { return events.User[event]?.(this); },
		toggle_display() {}, toggle_enable() {}, set_df_property() {}, add_custom_button() {},
		set_value(fieldname, value) { this.doc[fieldname] = value; },
		reload_doc() { stats.reload++; },
		dirty() { stats.dirty++; },
	};
	return {
		frm, stats,
		load() { events.User.onload(frm); },
		async refresh() { events.User.refresh(frm); await Promise.resolve(); },
	};
}

test("saved website user gains module choices after conversion without a new form load", async () => {
	const h = harness();
	h.load();
	await h.refresh();
	const roleEditor = h.frm.roles_editor;
	const before = JSON.stringify({ roles: h.frm.doc.roles, blocked: h.frm.doc.block_modules });
	assert.equal(h.stats.modules, 0);
	h.frm.doc.user_type = "System User";
	await h.refresh();
	assert.ok(h.frm.module_editor);
	assert.equal(h.frm.roles_editor, roleEditor);
	assert.deepEqual(Array.from(h.frm.module_editor.multicheck.selected_options), ["Manufacturing", "Stock"]);
	await h.refresh();
	assert.equal(h.stats.roles, 1);
	assert.equal(h.stats.modules, 1);
	assert.equal(h.frm.fields_dict.modules_html.wrapper.children.length, 1);
	assert.equal(h.stats.reload, 0);
	assert.equal(h.stats.dirty, 0);
	assert.equal(h.stats.writes, 0);
	assert.equal(JSON.stringify({ roles: h.frm.doc.roles, blocked: h.frm.doc.block_modules }), before);
});

test("normal system user renders module choices on the first refresh", async () => {
	const h = harness({ userType: "System User" });
	h.load();
	await h.refresh();
	assert.equal(h.stats.modules, 1);
	assert.equal(h.frm.module_editor.wrapper.attrs.disabled, false);
});

for (const options of [{ userType: "Website User" }, { userType: "System User", isNew: true },
	{ userType: "System User", canEdit: false }]) {
	test(`module editor retains native eligibility: ${JSON.stringify(options)}`, async () => {
		const h = harness(options);
		h.load();
		await h.refresh();
		assert.equal(h.stats.modules, 0);
		assert.equal(h.stats.writes, 0);
	});
}

test("module profile locks module choices and clearing it restores manual selection", async () => {
	const h = harness({ userType: "System User", moduleProfile: "Factory Profile" });
	h.load();
	await h.refresh();
	assert.equal(h.frm.module_editor.disable, 1);
	assert.equal(h.frm.module_editor.wrapper.attrs.disabled, true);
	h.frm.doc.module_profile = "";
	await h.refresh();
	assert.equal(h.frm.module_editor.wrapper.attrs.disabled, false);
	assert.equal(h.stats.modules, 1);
	assert.equal(h.stats.writes, 0);
});

test("role profile does not prevent a converted system user's module editor", async () => {
	const h = harness();
	h.frm.doc.role_profiles = [{ role_profile: "Factory Roles" }];
	h.load();
	await h.refresh();
	h.frm.doc.user_type = "System User";
	await h.refresh();
	assert.equal(h.frm.roles_editor.disable, 1);
	assert.equal(h.frm.module_editor.disable, 0);
	assert.equal(h.stats.modules, 1);
});

test("reused form displays the next user's module restrictions without recreating controls", async () => {
	const h = harness({ userType: "System User" });
	h.load();
	await h.refresh();
	h.frm.doc = { ...h.frm.doc, name: "another@example.test", block_modules: [{ module: "Stock" }] };
	h.load();
	await h.refresh();
	assert.deepEqual(Array.from(h.frm.module_editor.multicheck.selected_options), ["Accounts", "Manufacturing"]);
	assert.equal(h.stats.modules, 1);
	assert.equal(h.stats.writes, 0);
});

test("module choices still update only the block list when the operator changes a checkbox", async () => {
	const h = harness();
	h.load();
	h.frm.doc.user_type = "System User";
	await h.refresh();
	const roles = JSON.stringify(h.frm.doc.roles);
	const control = h.frm.module_editor.multicheck;
	control.selected_options = ["Stock"];
	control.df.on_change();
	assert.deepEqual(h.frm.doc.block_modules.map((row) => row.module), ["Accounts", "Manufacturing"]);
	assert.equal(JSON.stringify(h.frm.doc.roles), roles);
	assert.equal(h.stats.dirty, 1);
});
