(() => {
	const root = document.getElementById("root");
	if (!root) return;
	const translateProgressLabel = () => {
		const translated = window.frappe?._messages?.Progress;
		if (!translated || translated === "Progress") return;
		root.querySelectorAll('[data-slot="progress"]').forEach((progress) => {
			const label = progress.previousElementSibling?.querySelector("span");
			if (label?.textContent === "Progress") label.textContent = translated;
		});
	};
	translateProgressLabel();
	new MutationObserver(translateProgressLabel).observe(root, {
		childList: true, subtree: true, characterData: true,
	});
})();
