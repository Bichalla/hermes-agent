import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from bridge.compat import CompatError, load_manifest, validate_candidate_source


ROOT = Path(__file__).resolve().parents[1]


def run(argv, cwd=None):
    result = subprocess.run(
        argv, cwd=cwd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"command failed: {argv}: {result.stderr or result.stdout}")
    return result.stdout.strip()


class CompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-compat-test-")
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        run(["git", "init"], cwd=self.repo)
        run(["git", "config", "user.email", "bridge@example.invalid"], cwd=self.repo)
        run(["git", "config", "user.name", "Bridge Test"], cwd=self.repo)
        self._write("gateway/run_startup.py", "await hooks.emit('gateway:startup', {'platforms': []})\n")
        self._write("tools/approval.py", "def check_all_command_guards():\n    return _human_decision()\n")
        self._write("hermes_cli/approval_transport.py", "class ApprovalRequest: pass\n")
        self._write("hermes_cli/kanban_db_dispatch.py", "def dispatch(): pass\n")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "base"], cwd=self.repo)
        self.head = run(["git", "rev-parse", "HEAD"], cwd=self.repo)
        self.manifest = self._manifest()
        self._write("gateway/run_startup.py", "from gateway.kanban_approval import KanbanApprovalService\n")
        self._write("gateway/kanban_approval.py", "class KanbanApprovalService:\n    api_version = 1\n")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "bridge overlay"], cwd=self.repo)
        self.overlay_head = run(["git", "rev-parse", "HEAD"], cwd=self.repo)
        self.manifest["overlay_files"]["gateway/run_startup.py"]["patched_sha256"] = self._hash("gateway/run_startup.py")
        self.manifest["overlay_files"]["gateway/kanban_approval.py"]["patched_sha256"] = self._hash("gateway/kanban_approval.py")
        self.manifest["supported_heads"] = [self.overlay_head]

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_exact_reviewed_overlay(self):
        report = validate_candidate_source(self.repo, self.manifest)
        self.assertEqual(report.head, self.overlay_head)
        self.assertEqual(report.overlay_files, ("gateway/kanban_approval.py", "gateway/run_startup.py"))

    def test_rejects_unknown_overlay_file(self):
        self._write("gateway/extra.py", "x = 1\n")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "unknown overlay"], cwd=self.repo)
        self.manifest["supported_heads"] = [run(["git", "rev-parse", "HEAD"], cwd=self.repo)]
        with self.assertRaisesRegex(CompatError, "file set differs"):
            validate_candidate_source(self.repo, self.manifest)

    def test_rejects_patched_hash_drift(self):
        self._write("gateway/kanban_approval.py", "class KanbanApprovalService:\n    api_version = 2\n")
        with self.assertRaisesRegex(CompatError, "clean"):
            validate_candidate_source(self.repo, self.manifest)

    def test_rejects_base_hash_drift(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["overlay_files"]["gateway/run_startup.py"]["base_sha256"] = "0" * 64
        with self.assertRaisesRegex(CompatError, "base hash drift"):
            validate_candidate_source(self.repo, manifest)

    def test_rejects_unsupported_head(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["supported_heads"] = ["1" * 40]
        with self.assertRaisesRegex(CompatError, "unsupported Hermes source head"):
            validate_candidate_source(self.repo, manifest)

    def test_rejects_scanner_sensitive_drift(self):
        self._write("gateway/kanban_approval.py", "class KanbanApprovalService:\n    api_version = 1\n    json.dump(x, y)\n")
        run(["git", "add", "."], cwd=self.repo)
        run(["git", "commit", "-m", "scanner sensitive"], cwd=self.repo)
        self.manifest["supported_heads"] = [run(["git", "rev-parse", "HEAD"], cwd=self.repo)]
        self.manifest["overlay_files"]["gateway/kanban_approval.py"]["patched_sha256"] = self._hash("gateway/kanban_approval.py")
        with self.assertRaisesRegex(CompatError, "scanner-sensitive"):
            validate_candidate_source(self.repo, self.manifest)

    def test_script_dry_run_outputs_json_for_real_manifest(self):
        source = ROOT / ".work/hermes-source"
        if not source.exists():
            self.skipTest("local Hermes source fixture is absent")
        dirty = subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain=v1", "--untracked-files=all"],
            text=True,
        ).strip()
        if dirty:
            self.skipTest("local Hermes source overlay is not committed yet")
        result = subprocess.run(
            [
                "python3", str(ROOT / "scripts/prepare_candidate.py"), "--source", str(source),
                "--runtime-protection", "/Users/honbul/.hermes/ops/runtime-protection",
                "--active-config", "/Users/honbul/.hermes/ops/runtime-protection/runtime-protection.json",
            ],
            cwd=ROOT, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "compatible")

    def _manifest(self):
        return {
            "schema_version": 1,
            "active_head": self.head,
            "base_ref": self.head,
            "supported_heads": [],
            "required_base_hashes": {
                rel: self._base_hash(rel)
                for rel in [
                    "gateway/run_startup.py",
                    "tools/approval.py",
                    "hermes_cli/approval_transport.py",
                    "hermes_cli/kanban_db_dispatch.py",
                ]
            },
            "overlay_files": {
                "gateway/run_startup.py": {
                    "state": "modified",
                    "base_sha256": self._base_hash("gateway/run_startup.py"),
                    "patched_sha256": "",
                },
                "gateway/kanban_approval.py": {
                    "state": "added",
                    "patched_sha256": "",
                },
            },
            "allowed_scanner_sensitive_files": [],
            "required_snippets": {
                "gateway/run_startup.py": ["KanbanApprovalService"],
                "gateway/kanban_approval.py": ["api_version = 1"],
            },
        }

    def _write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _hash(self, rel):
        import hashlib

        return hashlib.sha256((self.repo / rel).read_bytes()).hexdigest()

    def _base_hash(self, rel):
        import hashlib

        blob = subprocess.check_output(["git", "-C", str(self.repo), "show", f"HEAD:{rel}"])
        return hashlib.sha256(blob).hexdigest()


if __name__ == "__main__":
    unittest.main()
