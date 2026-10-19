"""Deliver reviewed Frappe compatibility fixes through the App build hook."""

import hashlib
import json
import subprocess
from pathlib import Path

PATCH_DIR = Path(__file__).resolve().parent / "framework_patches/frappe"
MANIFEST_NAME = "china-finance-framework-patches.json"


def after_build():
	import frappe

	return apply_patches(Path(frappe.__file__).resolve().parent.parent)


def apply_patches(frappe_repo, patch_dir=PATCH_DIR):
	frappe_repo = Path(frappe_repo).resolve()
	patches = sorted(Path(patch_dir).resolve().glob("*.patch"))
	if not patches:
		raise ValueError("China Finance Frappe compatibility patches are missing")

	records = []
	for patch in patches:
		command = ["git", "-C", str(frappe_repo), "apply"]
		reverse = subprocess.run(
			[*command, "--reverse", "--check", str(patch)],
			capture_output=True,
			text=True,
		)
		if reverse.returncode == 0:
			status = "already-applied"
		else:
			# Conflicting upstream code must stop the build for compatibility review.
			subprocess.run([*command, "--check", str(patch)], check=True)
			subprocess.run([*command, str(patch)], check=True)
			status = "applied"
		records.append(
			{
				"app": "frappe",
				"delivered_by": "china_finance",
				"patch": patch.name,
				"sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
				"status": status,
			}
		)

	manifest = json.dumps(records, indent=2, sort_keys=True) + "\n"
	bench = frappe_repo.parent.parent
	(bench / MANIFEST_NAME).write_text(manifest, encoding="utf-8")
	print(manifest, end="")
	return records
