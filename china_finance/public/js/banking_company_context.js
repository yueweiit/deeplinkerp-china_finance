// Scope only the embedded Banking company's native storage key to this browser tab.
// ERPNext's atomWithStorage reads this adapter before its deferred module mounts.
(() => {
	const native = window.localStorage;
	const native_key = "bank-rec-selected-company";
	const parent_context = window.parent?.china_finance?.company_context;
	const user = window.parent?.frappe?.session?.user || window.frappe?.boot?.user?.name;
	if (!user || !parent_context) return;
	const key = `china_finance:company:${user}:bank`;
	const subscribers = new Set();
	let revision = 0;
	const adapter = new Proxy(native, {
		get(target, property) {
			// Jotai uses a provided subscription instead of cross-tab storage events.
			if (property === "subscribe") return (name, callback) => {
				if (name === native_key) { subscribers.add(callback); return () => subscribers.delete(callback); }
				const listener = event => { if (event.key === name && event.storageArea === native) callback(event.newValue); };
				window.addEventListener("storage", listener);
				return () => window.removeEventListener("storage", listener);
			};
			if (property === "getItem") return name => name === native_key ? sessionStorage.getItem(key) : native.getItem(name);
			if (property === "removeItem") return name => name === native_key ? sessionStorage.removeItem(key) : native.removeItem(name);
			if (property === "setItem") return (name, value) => {
				if (name !== native_key) return native.setItem(name, value);
				const token = ++revision;
				sessionStorage.setItem(key, value);
				let company;
				try { company = JSON.parse(value); } catch { company = null; }
				Promise.resolve(typeof company === "string" && company
					? parent_context.change_company(company, {apply: false, guard: false}) : null).catch(() => null).then(result => {
					if (token !== revision || result) return;
					// A preceding selection may still be unverified. Roll back to
					// the parent's committed company, not that optimistic value.
					const confirmed = parent_context.get_company();
					const previous = confirmed ? JSON.stringify(confirmed) : null;
					if (previous === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, previous);
					for (const callback of subscribers) callback(previous);
				});
			};
			const value = Reflect.get(target, property, target);
			return typeof value === "function" ? value.bind(target) : value;
		},
	});
	Object.defineProperty(window, "localStorage", {configurable: true, get: () => adapter});
})();
