const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../public/js/voucher_entry_split.js"), "utf8");

function harness({ posted = true, debit = 125, credit = 0, call } = {}) {
	const dialogs = [],
		calls = [],
		notices = [];
	const report = {
		refreshes: 0,
		async refresh() {
			this.refreshes++;
		},
		datatable: { cellmanager: { async submitEditing() {}, deactivateEditing() {} } },
	};
	class Dialog {
		constructor(options) {
			this.options = options;
			this.doc = options.doc;
			this.primary = options.primary_action;
			this.label = options.primary_action_label;
			this.values = { account: "New account", amount: 50, summary: "Split summary" };
			this.readOnly = {};
			this.fields_dict = {
				preview: {
					$wrapper: {
						empty: () => {
							this.html = "";
						},
						html: (value) => {
							this.html = value;
						},
					},
				},
			};
			dialogs.push(this);
		}
		show() {}
		hide() {
			this.options.onhide();
		}
		get_values() {
			return { ...this.values };
		}
		get_value(field) {
			return this.values[field];
		}
		async set_values(values) {
			Object.assign(this.values, values);
		}
		get_primary_btn() {
			return {
				prop: (key, value) => {
					this[key] = value;
				},
			};
		}
		set_df_property(field, key, value) {
			if (key === "read_only") this.readOnly[field] = value;
		}
		set_primary_action(label, callback) {
			this.label = label;
			this.primary = callback;
		}
	}
	const context = vm.createContext({
		china_finance: { voucher_entry_split: {} },
		__: (text, args = []) => text.replace(/\{(\d+)\}/g, (_, index) => args[index] ?? ""),
		frappe: {
			provide() {},
			ui: { Dialog },
			format: (value) => String(value),
			utils: {
				escape_html: (value) =>
					String(value)
						.replaceAll("&", "&amp;")
						.replaceAll("<", "&lt;")
						.replaceAll(">", "&gt;"),
			},
			msgprint: (value) => notices.push(value),
			show_alert() {},
			async call(request) {
				calls.push(request);
				if (call) return call(request, calls.length);
				return {
					message: request.method.includes("preview")
						? { requires_confirmation: posted, bank_action: "unreconcile_and_restore" }
						: { message: "Saved" },
				};
			},
		},
	});
	vm.runInContext(source, context);
	const row = {
		editable_split: true,
		account: "Original account",
		debit,
		credit,
		amount_precision: 2,
		currency: "CNY",
		edit_source_doctype: "Journal Entry",
		edit_source_name: "JV-1",
		edit_source_modified: "version-1",
		edit_key: "je:line-2",
	};
	return {
		report,
		row,
		dialogs,
		calls,
		notices,
		open: () => context.china_finance.voucher_entry_split.open(report, row),
	};
}

test("posted split previews first and applies identical values only after confirmation", async () => {
	const h = harness();
	await h.open();
	const dialog = h.dialogs[0];
	await dialog.primary();
	assert.equal(h.calls.length, 1);
	assert.match(h.calls[0].method, /preview_inline_voucher_changes$/);
	assert.equal(h.calls[0].args.changes[0].field, "split");
	assert.equal(h.calls[0].args.changes[0].value.amount, 50);
	assert.equal(dialog.label, "确认更正");
	assert.equal(dialog.readOnly.amount, true);
	assert.match(dialog.html, /安全撤销并恢复银行核销/);
	assert.match(dialog.html, /75/);
	await dialog.primary();
	assert.equal(h.calls.length, 2);
	assert.match(h.calls[1].method, /apply_inline_voucher_changes$/);
	assert.equal(h.calls[1].type, "POST");
	assert.equal(h.calls[1].args, h.calls[0].args);
	assert.equal(h.report.refreshes, 1);
	assert.equal(h.report._china_split_dialog, null);
});

test("negative debit and credit splits retain their original sign and use draft save", async () => {
	for (const values of [
		{ debit: -125, credit: 0 },
		{ debit: 0, credit: -125 },
	]) {
		const h = harness({ posted: false, ...values });
		await h.open();
		await h.dialogs[0].primary();
		assert.equal(h.calls[0].args.changes[0].value.amount, -50);
		assert.equal(h.dialogs[0].label, "保存新增");
	}
});

test("active cell commits before the dirty-voucher guard and no second dialog opens", async () => {
	const h = harness();
	h.report.datatable.cellmanager.submitEditing = async () => {
		h.report._china_inline_edit_state = { changes: new Map([["debit", 10]]) };
	};
	await h.open();
	assert.equal(h.dialogs.length, 0);
	assert.equal(h.calls.length, 0);
	assert.match(h.notices[0], /请先保存或取消/);
	h.report.datatable.cellmanager.submitEditing = async () => {};
	h.report._china_inline_edit_state = null;
	await h.open();
	await h.open();
	assert.equal(h.dialogs.length, 1);
});

test("zero, excessive and non-finite split amounts never reach the server", async () => {
	for (const amount of [0, -1, 125, 126, NaN, Infinity]) {
		const h = harness();
		await h.open();
		h.dialogs[0].values.amount = amount;
		await h.dialogs[0].primary();
		assert.equal(h.calls.length, 0);
		assert.match(h.notices[0], /新增行金额/);
	}
});

test("failed apply invalidates confirmation and requires another preview", async () => {
	const h = harness({
		call: async (request, count) => {
			if (count === 2) throw new Error("Version changed");
			return { message: { requires_confirmation: true } };
		},
	});
	await h.open();
	const dialog = h.dialogs[0];
	await dialog.primary();
	await dialog.primary();
	assert.equal(dialog.label, "预览新增");
	assert.equal(dialog.readOnly.amount, false);
	assert.equal(h.report.refreshes, 0);
	await dialog.primary();
	assert.match(h.calls[2].method, /preview_inline_voucher_changes$/);
});

test("pending preview cannot submit twice and closing it cannot apply changes", async () => {
	let resolve;
	const h = harness({
		call: () =>
			new Promise((done) => {
				resolve = done;
			}),
	});
	await h.open();
	const dialog = h.dialogs[0];
	const preview = dialog.primary();
	await dialog.primary();
	assert.equal(h.calls.length, 1);
	dialog.hide();
	resolve({ message: { requires_confirmation: true } });
	await preview;
	assert.equal(h.calls.length, 1);
	assert.equal(h.report._china_split_dialog, null);
});

test("preview escapes account names and repreview unlocks all input fields", async () => {
	const h = harness();
	h.row.account = "<img src=x onerror=alert(1)>";
	await h.open();
	const dialog = h.dialogs[0];
	dialog.values.account = "<script>bad</script>";
	await dialog.primary();
	assert.doesNotMatch(dialog.html, /<img|<script/);
	assert.match(dialog.html, /&lt;script&gt;/);
	dialog.options.secondary_action();
	assert.equal(dialog.label, "预览新增");
	assert.equal(dialog.readOnly.account, false);
});

test("voucher dialog lists eligible source rows and changing the source updates the submitted key", async () => {
	const h = harness();
	h.row.edit_voucher_key = "Journal Entry:JV-1";
	h.report.data = [
		h.row,
		{ ...h.row, edit_key: "je:line-3", account: "Second original", debit: 0, credit: 200 },
		{ ...h.row, edit_key: "je:blocked", editable_split: false },
		{ ...h.row, edit_key: "je:other", edit_voucher_key: "Journal Entry:JV-2" },
	];
	await h.open();
	const dialog = h.dialogs[0];
	const field = dialog.options.fields.find((field) => field.fieldname === "source_row");
	assert.deepEqual(
		Array.from(field.options, (option) => option.value),
		["je:line-2", "je:line-3"]
	);
	dialog.values.source_row = "je:line-3";
	await field.onchange();
	assert.equal(dialog.values.original_account, "Second original");
	assert.equal(dialog.values.original_amount, 200);
	assert.equal(dialog.values.account, "");
	dialog.values.account = "New account";
	dialog.values.amount = 50;
	await dialog.primary();
	assert.equal(h.calls[0].args.changes[0].edit_key, "je:line-3");
	assert.equal(h.calls[0].args.changes[0].value.amount, 50);
	assert.match(dialog.html, /150/);
	assert.equal(dialog.readOnly.source_row, true);
	dialog.options.secondary_action();
	assert.equal(dialog.readOnly.source_row, false);
});
