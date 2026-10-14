import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("ci_install", SCRIPTS / "install_ci.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Budget:
    def __init__(self, status="confirmed"):
        self.status = status

    def actions_budget(self, org):
        return {"status": self.status, "amount": 0, "stop_usage": self.status == "confirmed"}


class InstallerChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin",
                        "https://github.com/meenseek/synthetic-product.git"], check=True)
        self.readme = self.root / "README.md"
        self.before = "# Synthetic product\n## 연결과 검증\n[Owner](README.md)\n\n## Other\nKeep this.\n"
        self.readme.write_text(self.before)
        self.workflow = module.render(["python -B -m unittest discover -s tests -v"], ["src/**", "tests/**"], python="3.12")

    def install(self, budget=None):
        return module.install(self.root, "README.md", self.workflow, budget or Budget())

    def test_installs_discoverable_workflow_and_preserves_existing_owner_text_and_mode(self):
        self.readme.chmod(0o640)
        result = self.install()
        self.assertEqual((self.root / module.WORKFLOW).read_text(), self.workflow)
        text = self.readme.read_text()
        self.assertIn(module.WORKFLOW, module.declared_links(module.section(text)))
        self.assertEqual(text.replace("\n\nCI: [자동 검증](.github/workflows/ci.yml)\n", ""), self.before)
        self.assertEqual(self.readme.stat().st_mode & 0o777, 0o640)
        self.assertEqual(result["actions_budget"]["status"], "confirmed")
        self.assertEqual(list(self.root.rglob(".temporary-ci-*")), [])

    def test_repeated_install_preserves_bytes_and_refuses_an_existing_differently_named_workflow(self):
        self.install()
        files = {p: p.read_bytes() for p in (self.readme, self.root / module.WORKFLOW)}
        with self.assertRaisesRegex(ValueError, "existing CI"):
            self.install()
        self.assertTrue(all(p.read_bytes() == data for p, data in files.items()))
        (self.root / module.WORKFLOW).rename(self.root / ".github/workflows/test.yaml")
        with self.assertRaisesRegex(ValueError, "existing workflows"):
            self.install()

    def test_prospective_generic_link_is_preserved_and_receives_selected_ci_declaration(self):
        prospective = self.before.replace("[Owner](README.md)", "[CI](.github/workflows/ci.yml)")
        self.readme.write_text(prospective)
        self.install()
        text = self.readme.read_text()
        self.assertIn("[CI](.github/workflows/ci.yml)", text)
        self.assertEqual(module.declared_ci_links(module.section(text)), [module.WORKFLOW])
        self.assertEqual(text.count("CI: "), 1)

    def test_budget_denial_or_unknown_installs_nothing(self):
        for status in ("failed", "unknown"):
            with self.assertRaisesRegex(ValueError, "zero-dollar stop"):
                self.install(Budget(status))
            self.assertEqual(self.readme.read_text(), self.before)
            self.assertFalse((self.root / ".github").exists())

    def test_other_owner_duplicate_sections_symlinks_and_missing_contract_fail_without_mutation(self):
        subprocess.run(["git", "-C", str(self.root), "remote", "set-url", "origin",
                        "https://github.com/other/synthetic-product.git"], check=True)
        with self.assertRaisesRegex(ValueError, "meenseek"):
            self.install()
        subprocess.run(["git", "-C", str(self.root), "remote", "set-url", "origin",
                        "https://github.com/meenseek/synthetic-product.git"], check=True)
        for text in ("# No connection section", self.before + "\n## 연결과 검증\nDuplicate"):
            self.readme.write_text(text)
            with self.assertRaises((ValueError, module.ReadError)):
                self.install()
            self.assertEqual(self.readme.read_text(), text)
        self.readme.write_text(self.before)
        (self.root / "AGENTS.md").write_text("## 연결과 검증\n[Owner](AGENTS.md)")
        with self.assertRaisesRegex(ValueError, "one existing owner"):
            self.install()
        (self.root / "AGENTS.md").unlink()
        (self.root / ".github").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "directory"):
            self.install()
        self.assertFalse((self.root / "workflows").exists())

    def test_document_replace_failure_rolls_back_ci_and_removes_temporary_files(self):
        with patch.object(module.os, "replace", side_effect=OSError("synthetic failure")), self.assertRaises(OSError):
            self.install()
        self.assertEqual(self.readme.read_text(), self.before)
        self.assertFalse((self.root / module.WORKFLOW).exists())
        self.assertEqual(list(self.root.rglob(".temporary-ci-*")), [])

    def test_commands_are_encoded_as_data_and_never_executed_by_installer(self):
        marker = self.root / "must-not-exist"
        command = f'touch "{marker}"\necho "quoted: value"'
        workflow = module.render([command], ["src/**"])
        module.install(self.root, "README.md", workflow, Budget())
        self.assertFalse(marker.exists())
        encoded = next(line.split("run: ", 1)[1] for line in workflow.splitlines() if "run: " in line)
        self.assertEqual(json.loads(encoded), command)

    def test_lightweight_defaults_are_bounded_and_explicit_inputs_cannot_remove_workflow_triggers(self):
        self.assertIn("timeout-minutes: 5", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn('".github/workflows/ci.yml"', self.workflow)
        self.assertNotIn("schedule:", self.workflow)
        self.assertNotIn("matrix:", self.workflow)
        self.assertNotIn("cache:", self.workflow)
        self.assertNotIn("upload-artifact", self.workflow)
        for commands, paths, kwargs in [([], ["src/**"], {}), (["check"], [], {}),
                                       (["check"], ["../outside"], {}), (["check"], ["!**"], {}),
                                       (["check"], ["src/**"], {"timeout": 11}),
                                       (["check"], ["src/**"], {"python": "latest"})]:
            with self.assertRaises(ValueError):
                module.render(commands, paths, **kwargs)

    def test_dry_run_cli_works_without_credentials_or_target_mutation(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "install_ci.py"),
                                 "--repository", str(self.root), "--entrypoint", "README.md",
                                 "--command", "npm ci", "--command", "npm run check", "--path", "src/**",
                                 "--node-version", "24", "--dry-run"], capture_output=True, text=True,
                                env={**os.environ, "GH_TOKEN": "synthetic-invalid-token"}, check=True)
        self.assertIn(module.NODE, result.stdout)
        self.assertIn("package-manager-cache: false", result.stdout)
        self.assertEqual(self.readme.read_text(), self.before)
        self.assertFalse((self.root / ".github").exists())


if __name__ == "__main__":
    unittest.main()
