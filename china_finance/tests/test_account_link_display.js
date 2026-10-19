const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

let nativeCalls = 0;
class ControlLink {
	constructor(fieldtype, options) {
		this.df = { fieldtype, options };
		this.awesomplete = { item: (item) => item };
	}

	setup_awesomeplete() { nativeCalls++; }

	get_options() { throw new Error("Dynamic target is unavailable during control construction"); }
}
const context = { frappe: { ui: { form: { ControlLink } } } };
vm.createContext(context);
const source = fs.readFileSync(path.join(__dirname, "../public/js/account_display.js"), "utf8");
vm.runInContext(source, context);

const account = new ControlLink("Link", "Account");
account.setup_awesomeplete();
const item = { value: "1001 - Test", label: "1001 - Cash", description: "Company suffix" };
const rendered = account.awesomplete.item(item);
assert.equal(rendered.description, "");
assert.equal(rendered.value, item.value);
assert.equal(item.description, "Company suffix");

for (const [type, options] of [["Link", "Supplier"], ["Dynamic Link", "party_type"]]) {
	const control = new ControlLink(type, options);
	const renderer = control.awesomplete.item;
	assert.doesNotThrow(() => control.setup_awesomeplete());
	assert.equal(control.awesomplete.item, renderer);
	assert.equal(control.awesomplete.item(item), item);
}
const patched = ControlLink.prototype.setup_awesomeplete;
vm.runInContext(source, context);
assert.equal(ControlLink.prototype.setup_awesomeplete, patched);
assert.equal(nativeCalls, 3);
console.log("PASS: account display, native suggestions, dynamic report links and idempotent setup");
