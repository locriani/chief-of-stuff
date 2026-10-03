"""The configured login shell chooses runtime executables and receives exact argv."""

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import shell_setup as shell  # noqa: E402


class ShellSetupTest(unittest.TestCase):
    def test_login_shell_resolves_a_binary_from_zshrc(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            binary = root / "bin" / "fakecli"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            (home / ".zshrc").write_text(f'export PATH="{binary.parent}:$PATH"\n')
            source = {"SHELL": "/bin/zsh", "HOME": str(home), "PATH": "/usr/bin:/bin"}
            self.assertEqual(shell.resolve("fakecli", source=source), str(binary))

    def test_shell_input_cannot_be_command_text(self):
        with self.assertRaises(shell.ShellError):
            shell.resolve("codex; echo leaked")

    def test_login_command_keeps_worker_arguments_separate(self):
        source = {"SHELL": "/bin/zsh"}
        self.assertEqual(shell.login_argv(["/bin/echo", "two words"])[-2:], ["/bin/echo", "two words"])
        self.assertEqual(shell.clean_env(dict(source, CODEX_SESSION_ID="secret")), source)
        with mock.patch.object(shell, "login_shell", return_value="/bin/fish"):
            self.assertEqual(shell.login_argv(["/bin/echo", "two words"]),
                             ["/bin/fish", "-lic", "exec $argv", "/bin/echo", "two words"])

    def test_bad_configured_shell_is_reported(self):
        with self.assertRaises(shell.ShellError):
            shell.login_shell({"SHELL": "/missing/shell"})

    def test_chief_of_stuff_shell_replaces_the_login_shell(self):
        """#48: an eval case has to make a runtime absent, and the harness cannot take a real binary off PATH.
        With `CHIEF_OF_STUFF_SHELL` set, `resolve` asks that program and not `$SHELL`. The stand-in is the only
        thing that knows where `fakecli48` is: no PATH and no rc file names its directory."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "home").mkdir()
            binary = root / "opt" / "fakecli48"
            binary.parent.mkdir()
            binary.write_text(f"#!{sys.executable}\n")
            binary.chmod(0o755)
            standin = root / "standin.py"
            # Called as a login shell is: `<shell> -lic "command -v <program>"`.
            standin.write_text(f"#!{sys.executable}\nimport sys\n"
                               f"if sys.argv[1:] != ['-lic', 'command -v fakecli48']:\n    sys.exit(1)\n"
                               f"print({str(binary)!r})\n")
            standin.chmod(0o755)
            source = {"SHELL": "/bin/zsh", "HOME": str(root / "home"), "PATH": "/usr/bin:/bin"}
            self.assertIsNone(shell.resolve("fakecli48", source=source), "$SHELL alone does not find it")
            source["CHIEF_OF_STUFF_SHELL"] = str(standin)
            self.assertEqual(shell.login_shell(source), str(standin))
            self.assertEqual(shell.resolve("fakecli48", source=source), str(binary))
            self.assertIsNone(shell.resolve("othercli48", source=source), "the stand-in knows no other program")
