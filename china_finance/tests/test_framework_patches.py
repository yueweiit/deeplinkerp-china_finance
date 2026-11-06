"""Patch delivery checks without Docker, Frappe dependencies or a database."""

import ast
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from china_finance import build, hooks

TARGET = Path("frappe/core/doctype/prepared_report/prepared_report.py")
SOURCE = '''def generate_report(prepared_report):
    frappe.publish_realtime(
        "report_generated",
        {"report_name": instance.report_name, "name": instance.name},
        user=frappe.session.user,
    )


@dangerously_reconnect_on_connection_abort
def _save_error():
    pass
'''.replace("    ", "\t")

USER_TARGET = Path("frappe/core/doctype/user/user.js")
# Native v16.23.0 excerpts for offline patch-delivery checks. The JavaScript
# regression suite exercises the full native User and ModuleEditor scripts.
USER_SOURCE = '''frappe.ui.form.on("User", {
    onload: function (frm) {
            if (!frm.roles_editor) {
                frm.roles_editor = new frappe.RoleEditor(
                    role_area,
                    frm,
                    frm.doc.role_profiles && frm.doc.role_profiles.length ? 1 : 0
                );

                if (frm.doc.user_type == "System User") {
                    var module_area = $("<div>").appendTo(frm.fields_dict.modules_html.wrapper);
                    frm.module_editor = new frappe.ModuleEditor(
                        frm,
                        module_area,
                        frm.doc.module_profile ? 1 : 0
                    );
                }
            } else {
                frm.roles_editor.show();
            }
    },
    refresh: function (frm) {
        if (!frm.is_new()) {
            if (frm.roles_editor && frm.can_edit_roles) {
                frm.roles_editor.show();
            }

            if (frm.module_editor) {
                frm.module_editor.disable = frm.doc.module_profile ? 1 : 0;
                frm.module_editor.show();
            }
        }
    },
});
'''.replace("    ", "\t")


class FrameworkPatchTests(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.bench = Path(self.directory.name)
		self.repo = self.bench / "apps/frappe"
		self.target = self.repo / TARGET
		self.target.parent.mkdir(parents=True)
		self.target.write_text(SOURCE, encoding="utf-8")
		self.user_target = self.repo / USER_TARGET
		self.user_target.parent.mkdir(parents=True)
		self.user_target.write_text(USER_SOURCE, encoding="utf-8")
		self.output = redirect_stdout(io.StringIO())
		self.output.__enter__()
		self.addCleanup(self.output.__exit__, None, None, None)

	def test_applies_without_git_metadata_and_records_checksum(self):
		build.apply_patches(self.repo)
		calls = [n for n in ast.walk(ast.parse(self.target.read_text())) if isinstance(n, ast.Call)]
		self.assertEqual(len(calls), 1)
		keywords = {
			kw.arg: ast.literal_eval(kw.value)
			for kw in calls[0].keywords
			if kw.arg == "after_commit"
		}
		self.assertEqual(keywords, {"after_commit": True})
		records = json.loads((self.bench / build.MANIFEST_NAME).read_text())
		patch_files = sorted(build.PATCH_DIR.glob("*.patch"))
		self.assertEqual(
			{path.name for path in patch_files},
			{"prepared-report-after-commit.patch", "user-module-editor-refresh.patch"},
		)
		self.assertEqual(
			records,
			[{
				"app": "frappe",
				"delivered_by": "china_finance",
				"patch": patch_file.name,
				"status": "applied",
				"sha256": hashlib.sha256(patch_file.read_bytes()).hexdigest(),
			} for patch_file in patch_files],
		)

	def test_applies_inside_git_checkout(self):
		subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
		self.assertEqual(build.apply_patches(self.repo)[0]["status"], "applied")

	def test_already_fixed_source_and_repeat_build_do_not_modify_source(self):
		fixed = SOURCE.replace(
			"user=frappe.session.user,",
			"user=frappe.session.user,\n\t\tafter_commit=True,",
		)
		self.target.write_text(fixed, encoding="utf-8")
		self.assertEqual(build.apply_patches(self.repo)[0]["status"], "already-applied")
		self.assertEqual(build.apply_patches(self.repo)[0]["status"], "already-applied")
		self.assertEqual(self.target.read_text(), fixed)

	def test_incompatible_source_stops_build_without_modification(self):
		incompatible = SOURCE.replace('"report_generated"', '"changed_event"')
		self.target.write_text(incompatible, encoding="utf-8")
		with self.assertRaises(subprocess.CalledProcessError):
			build.apply_patches(self.repo)
		self.assertEqual(self.target.read_text(), incompatible)
		self.assertFalse((self.bench / build.MANIFEST_NAME).exists())

	def test_missing_patch_input_is_rejected(self):
		with self.assertRaises(ValueError):
			build.apply_patches(self.repo, self.bench / "missing")

	def test_user_patch_is_idempotent_and_preserves_other_framework_fix(self):
		self.assertEqual([row["status"] for row in build.apply_patches(self.repo)], ["applied", "applied"])
		fixed_user = self.user_target.read_text()
		fixed_report = self.target.read_text()
		self.assertEqual(
			[row["status"] for row in build.apply_patches(self.repo)],
			["already-applied", "already-applied"],
		)
		self.assertEqual(self.user_target.read_text(), fixed_user)
		self.assertEqual(self.target.read_text(), fixed_report)

	def test_incompatible_user_source_rejects_build(self):
		incompatible = USER_SOURCE.replace("frm.module_editor.show();", "frm.changed_editor.show();")
		self.user_target.write_text(incompatible, encoding="utf-8")
		with self.assertRaises(subprocess.CalledProcessError):
			build.apply_patches(self.repo)
		self.assertEqual(self.user_target.read_text(), incompatible)
		self.assertFalse((self.bench / build.MANIFEST_NAME).exists())

	def test_registered_hook_resolves_frappe_location(self):
		self.assertEqual(hooks.after_build, "china_finance.build.after_build")
		fake_frappe = SimpleNamespace(__file__=str(self.repo / "frappe/__init__.py"))
		with patch.dict(sys.modules, {"frappe": fake_frappe}):
			records = build.after_build()
		self.assertEqual(records[0]["status"], "applied")
		self.assertTrue((self.bench / build.MANIFEST_NAME).exists())


if __name__ == "__main__":
	unittest.main()
