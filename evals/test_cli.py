"""The one installed command routes fixed operations and preserves release pinning."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import chief_of_stuff as cli  # noqa: E402


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.share = self.root / "share"

    def install(self, **kwargs):
        return cli.install(source=ROOT, install_dir=self.share, bin_dir=self.bin, **kwargs)

    def test_dry_run_does_not_write(self):
        result = self.install(dry_run=True)
        self.assertFalse(result["installed"])
        self.assertTrue(result["wrapper_write"])
        self.assertFalse(self.share.exists())
        self.assertFalse((self.bin / "chief-of-stuff").exists())

    def test_one_wrapper_routes_init_and_models_from_versioned_release(self):
        first = self.install()
        command = self.bin / "chief-of-stuff"
        self.assertTrue(command.is_file())
        self.assertTrue(os.access(command, os.X_OK))
        self.assertTrue((self.share / "current").is_symlink())
        self.assertTrue((Path(first["release"]) / "chief_of_stuff.py").is_file())
        help_text = subprocess.run([str(command), "--help"], capture_output=True, text=True, check=True).stdout
        self.assertIn("worker", help_text)
        self.assertIn("start", help_text)
        workspace = self.root / "workspace"
        workspace.mkdir()
        done = subprocess.run([str(command), "init", "--root", str(workspace), "--user", "Robin",
                               "--timezone", "America/Chicago", "--worker-mode", "one-shot"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('mode = "one-shot"', (workspace / "chief-of-stuff.toml").read_text())
        guidance = workspace / "chief-of-stuff-models.md"
        self.assertTrue(guidance.is_file())
        guidance.unlink()
        done = subprocess.run([str(command), "models", "--root", str(workspace)],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(guidance.is_file())
        second = self.install()
        self.assertFalse(second["wrapper_write"])
        self.assertEqual(first["release"], second["release"])

    def test_wrapper_uses_pinned_release_and_refuses_other_paths(self):
        self.install()
        command = self.bin / "chief-of-stuff"
        pinned = self.share / "versions" / "fixture-pin"
        pinned.mkdir()
        (pinned / "chief_of_stuff.py").write_text("print('pinned release')\n")
        env = dict(os.environ, CHIEF_OF_STUFF_RELEASE=str(pinned))
        out = subprocess.run([str(command), "--help"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "pinned release")
        env["CHIEF_OF_STUFF_RELEASE"] = str(self.root)
        denied = subprocess.run([str(command), "--help"], env=env, capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("invalid pinned", denied.stderr)

    def test_existing_different_wrapper_requires_explicit_replace(self):
        command = self.bin / "chief-of-stuff"
        command.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.install()
        self.assertFalse(self.share.exists())
        result = self.install(replace_wrapper=True)
        self.assertTrue(result["wrapper_write"])
        self.assertIn("os.execv", command.read_text())


if __name__ == "__main__":
    unittest.main()
