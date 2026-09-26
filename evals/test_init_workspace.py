"""A fresh workspace gets valid configuration without touching existing content."""

import contextlib
import io
import sys
import tempfile
import tomllib
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import init_workspace as init  # noqa: E402
import install_model_guidance as guidance  # noqa: E402
from render_board import parse_coordinator  # noqa: E402
from settings import _models, load as load_settings  # noqa: E402


class InitWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = ["--root", str(self.root), "--user", "Robin", "--timezone", "America/Chicago"]

    def run_init(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = init.main([*self.args, *extra])
        return code, out.getvalue(), err.getvalue()

    def test_fresh_workspace_has_parseable_block_and_explicit_off_settings(self):
        code, _, err = self.run_init("--agent", "implementer:task", "--launcher", "tmux", "--board-port", "8765",
                                     "--github-repo", "owner/repo")
        self.assertEqual(code, 0, err)
        config = parse_coordinator((self.root / "CLAUDE.md").read_text(), date.today())
        self.assertEqual((config.user, config.log_dir, config.tracker_path("2026-09-25")),
                         ("Robin", "daily/", "daily/2026-09-25-tracker.md"))
        self.assertEqual(config.backlog.repo, "owner/repo")
        self.assertEqual(config.board_url, "http://127.0.0.1:8765/")
        self.assertIn("- Agent: implementer task", (self.root / "CLAUDE.md").read_text())
        settings = load_settings(self.root, config.settings_path)
        self.assertEqual((settings.notify.adapter, settings.workers.launcher), ("off", "tmux"))
        self.assertEqual(settings.workers.mode, "interactive")
        self.assertIn("Choice order", (self.root / guidance.NAME).read_text())

    def test_initializer_can_select_one_shot_default(self):
        code, _, err = self.run_init("--worker-mode", "one-shot")
        self.assertEqual(code, 0, err)
        self.assertEqual(load_settings(self.root, "chief-of-stuff.toml").workers.mode, "one-shot")
        # #111: the example rotation is commented out, so a fresh workspace keeps the markdown guidance.
        self.assertEqual(load_settings(self.root, "chief-of-stuff.toml").models, {})
        example = "\n".join(line.removeprefix("# ") for line in
                            (self.root / "chief-of-stuff.toml").read_text().splitlines() if line.startswith("# "))
        self.assertIn("implement", _models(tomllib.loads(example)["models"]))

    def test_existing_claude_content_is_preserved_and_a_second_init_refuses(self):
        claude = self.root / "CLAUDE.md"
        claude.write_text("# Existing rules\n\nKeep this.\n")
        self.assertEqual(self.run_init()[0], 0)
        first = claude.read_text()
        self.assertTrue(first.startswith("# Existing rules\n\nKeep this.\n"))
        code, _, err = self.run_init()
        self.assertEqual(code, 1)
        self.assertIn("already has", err)
        self.assertEqual(claude.read_text(), first)

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_init("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would create", out)
        self.assertFalse((self.root / "CLAUDE.md").exists())
        self.assertFalse((self.root / "chief-of-stuff.toml").exists())
        self.assertFalse((self.root / guidance.NAME).exists())

    def test_existing_settings_are_kept(self):
        settings = self.root / "chief-of-stuff.toml"
        settings.write_text('[workers]\nlauncher = "tmux"\n')
        self.assertEqual(self.run_init()[0], 0)
        self.assertEqual(settings.read_text(), '[workers]\nlauncher = "tmux"\n')

    def test_existing_model_guidance_is_preserved(self):
        path = self.root / guidance.NAME
        path.write_text("Use the model selected by Robin.\n")
        self.assertEqual(self.run_init()[0], 0)
        self.assertEqual(path.read_text(), "Use the model selected by Robin.\n")

    def test_guidance_only_installs_into_an_existing_workspace(self):
        (self.root / "CLAUDE.md").write_text("## Coordinator\n")
        self.assertEqual(guidance.main(["--root", str(self.root)]), 0)
        path = self.root / guidance.NAME
        self.assertIn("Suggested routing", path.read_text())
        path.write_text("My model policy.\n")
        self.assertEqual(guidance.main(["--root", str(self.root)]), 0)
        self.assertEqual(path.read_text(), "My model policy.\n")

    def test_gitlab_backlog_is_configured_without_a_token_value(self):
        code, _, err = self.run_init("--gitlab-host", "https://gitlab.example.com",
                                     "--gitlab-project", "group/repo", "--gitlab-token-env", "MY_GITLAB_TOKEN")
        self.assertEqual(code, 0, err)
        text = (self.root / "CLAUDE.md").read_text()
        self.assertIn("project group/repo; token env MY_GITLAB_TOKEN", text)
        self.assertNotIn("token value", text)

    def test_invalid_values_refuse_without_writing(self):
        for extra in (("--daily-dir", "../outside"), ("--agent", "bad name:task"),
                      ("--github-repo", "bad"), ("--board-port", "70000"),
                      ("--gitlab-host", "https://gitlab.example.com")):
            with self.subTest(extra=extra):
                code, _, _ = self.run_init(*extra)
                self.assertEqual(code, 1)
                self.assertFalse((self.root / "CLAUDE.md").exists())
                self.assertFalse((self.root / "chief-of-stuff.toml").exists())
