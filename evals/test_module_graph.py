"""module_graph.run: the one subprocess helper the board's git reads and the clone fetch go through (#451).

A repository-selecting variable in the launcher's environment (git_trees.REPO_VARS) wins over `-C <path>`, so the read or
fetch would land in another repository. `run` builds its environment per call from `git_trees.user_env()`: the
launcher's own environment, less those variables, plus GIT_TERMINAL_PROMPT=0. The user's HOME, PATH and credentials stay,
because the fetch uses them. `run` also launches `branch-graph`; only the env of git commands is pinned here.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import board_sources as bs  # noqa: E402
import git_trees  # noqa: E402
import module_graph  # noqa: E402

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd: Path, *args: str) -> str:
    env = {**git_trees.user_env(), **IDENTITY}
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def repo(path: Path, branch: str) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", branch)
    commit(path, "a.txt")
    return path


class ModuleGraphEnvTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def elsewhere(self) -> dict[str, str]:
        other = repo(self.tmp / "other", "elsewhere")
        return {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}

    def test_a_read_lands_in_the_named_tree_not_the_launchers_repository(self):
        tree = repo(self.tmp / "tree", "main")
        cmd = ["git", "-C", str(tree), "rev-parse", "--abbrev-ref", "HEAD"]
        with mock.patch.dict(os.environ, self.elsewhere()):
            # Control: under the plain inherited environment the other repository shows through.
            plain = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ}).stdout.strip()
            self.assertEqual(plain, "elsewhere")
            self.assertEqual(module_graph.run(cmd).strip(), "main")

    def test_a_fetch_updates_the_clone_not_the_launchers_repository(self):
        remote = self.tmp / "remote.git"
        seed = repo(self.tmp / "seed", "main")
        base = git(seed, "rev-parse", "HEAD")
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True, capture_output=True)
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True, capture_output=True)
        # The change's head and a newer base live only on the remote; the clone has neither.
        git(seed, "checkout", "-q", "-b", "change")
        head = commit(seed, "change.txt")
        git(seed, "checkout", "-q", "main")
        newer = commit(seed, "main2.txt")
        git(seed, "push", "-q", str(remote), "main", f"{head}:refs/pull/5/head")
        change = bs.Change("#5", "https://forge.example/o/r/pull/5", "t", "open", False, None, False, 0, None, "main",
                           (), (), head=head)
        other = self.elsewhere()
        with mock.patch.dict(os.environ, other):
            self.assertEqual(module_graph.fetch(change, clone), base)
        self.assertEqual(git(clone, "rev-parse", "refs/remotes/origin/main"), newer)
        self.assertEqual(git(clone, "cat-file", "-t", head), "commit")
        self.assertEqual(git(Path(other["GIT_WORK_TREE"]), "for-each-ref", "refs/remotes"), "")

    def spied_envs(self, **env) -> list[dict[str, str]]:
        seen = []

        def spy(cmd, **kw):
            seen.append(kw["env"])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.dict(os.environ, env), mock.patch.object(module_graph.subprocess, "run", spy):
            module_graph.run(["git", "status"])
        return seen

    def test_the_env_drops_each_repository_selecting_variable(self):
        for name in git_trees.REPO_VARS:
            with self.subTest(name=name):
                (env,) = self.spied_envs(**{name: "/nowhere"})
                self.assertNotIn(name, list(env))  # the keys only: a failure message must not print the launcher's secrets

    def test_the_env_never_prompts_and_keeps_the_launchers_home_and_path(self):
        (env,) = self.spied_envs(**{name: "/nowhere" for name in git_trees.REPO_VARS})
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual((env.get("HOME"), env.get("PATH")), (os.environ["HOME"], os.environ["PATH"]))

    def test_the_env_is_built_per_call_not_at_import(self):
        (env,) = self.spied_envs(CHIEF_TEST_MARKER="after-import")
        self.assertEqual(env.get("CHIEF_TEST_MARKER"), "after-import")


if __name__ == "__main__":
    unittest.main()
