const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const source = fs.readFileSync(
	path.join(__dirname, "../public/js/voucher_entry_delete.js"),
	"utf8"
);
function harness() {
	const dialogs = [],
		messages = [],
		staged = [];
	class Dialog {
		constructor(options) {
			this.options = options;
			dialogs.push(this);
		}
		show() {}
		hide() {
			this.options.onhide();
		}
	}
	const context = vm.createContext({
		china_finance: { voucher_entry_delete: {} },
		__: (text) => text,
		frappe: {
			provide() {},
			ui: { Dialog },
			format: (value) => String(value),
			utils: { escape_html: (value) => String(value) },
			msgprint: (message) => messages.push(message),
		},
	});
	vm.runInContext(source, context);
	const row = {
		edit_voucher_key: "JE:1",
		editable_delete: true,
		edit_key: "je:one",
		account: "Account",
		debit: 125,
	};
	const report = {
		data: [
			row,
			{ ...row, edit_key: "je:two" },
			{ ...row, edit_key: "je:blocked", editable_delete: false },
		],
	};
	return {
		report,
		row,
		dialogs,
		messages,
		staged,
		open: () =>
			context.china_finance.voucher_entry_delete.open(report, row, (item) => {
				staged.push(item);
				return true;
			}),
	};
}
test("delete dialog has one instance and only stages the selected eligible row", async () => {
	const h = harness();
	await h.open();
	await h.open();
	assert.equal(h.dialogs.length, 1);
	const dialog = h.dialogs[0];
	assert.deepEqual(
		Array.from(dialog.options.fields[1].options, (item) => item.value),
		["je:one", "je:two"]
	);
	assert.equal(h.staged.length, 0);
	dialog.options.primary_action({ source_row: "je:two" });
	assert.equal(h.staged.length, 1);
	assert.equal(h.staged[0].edit_key, "je:two");
	assert.equal(h.report._china_delete_dialog, null);
});
test("delete waits for active cell and blocks a pending or different voucher", async () => {
	const h = harness();
	let submitted = false;
	h.report.datatable = {
		cellmanager: {
			submitEditing: async () => {
				submitted = true;
				h.report._china_inline_edit_state = { voucher_key: "other" };
			},
			deactivateEditing() {},
		},
	};
	await h.open();
	assert.equal(submitted, true);
	assert.equal(h.dialogs.length, 0);
	assert.match(h.messages[0], /另一张凭证/);
});
