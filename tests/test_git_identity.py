#!/usr/bin/env python3
"""Tests for the "Committer identity unknown" failure of the KernelSU-Next variant.

A CI runner has no git identity, so the two `git cherry-pick` calls that apply pershoot's susfs4ksu fixes stopped the
KernelSU-Next builds. These tests run with an environment in which git refuses to guess an identity (the same situation
as on the runner), show that the failure happens without the fix, and that it is gone with it.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SETUP_IDENTITY = {
    "GIT_AUTHOR_NAME": "setup", "GIT_AUTHOR_EMAIL": "setup@example.com",
    "GIT_COMMITTER_NAME": "setup", "GIT_COMMITTER_EMAIL": "setup@example.com",
}


def clean_env(global_config="/dev/null", **extra):
    """An environment where git has no identity and is not allowed to guess one."""
    env = {
        "PATH": os.environ["PATH"], "HOME": tempfile.gettempdir(),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_GLOBAL": global_config,
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.useConfigOnly", "GIT_CONFIG_VALUE_0": "true",
        "GIT_TERMINAL_PROMPT": "0",
    }
    env.update(extra)
    return env


def sh(script, env=None, cwd=None):
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=cwd)
    return result.returncode, result.stdout + result.stderr


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=clean_env(**SETUP_IDENTITY))
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout.strip()


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class Fixture(unittest.TestCase):
    """upstream: base, then two fixes that only touch kernel_patches/fs/susfs.c, like pershoot's."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        up = cls.up = cls.tmp / "pershoot"
        subprocess.run(["git", "init", "-q", "-b", "main", str(up)], check=True, env=clean_env(**SETUP_IDENTITY))
        git(up, "config", "uploadpack.allowAnySHA1InWant", "true")
        (up / "kernel_patches" / "fs").mkdir(parents=True)
        susfs = up / "kernel_patches" / "fs" / "susfs.c"
        susfs.write_text("line1\nline2\nline3\nline4\nline5\nline6\nline7\n")
        git(up, "add", "-A")
        git(up, "commit", "-qm", "base")
        cls.base = git(up, "rev-parse", "HEAD")
        git(up, "checkout", "-q", "-b", "fixes")
        susfs.write_text(susfs.read_text().replace("line2", "line2 fixed"))
        git(up, "commit", "-qam", "fix one")
        cls.fix1 = git(up, "rev-parse", "HEAD")
        susfs.write_text(susfs.read_text().replace("line6", "line6 fixed"))
        git(up, "commit", "-qam", "fix two")
        cls.fix2 = git(up, "rev-parse", "HEAD")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def susfs_checkout(self):
        """A checkout of the base commit, standing in for the susfs4ksu clone the build makes."""
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        dest = work / "susfs"
        subprocess.run(["git", "clone", "-q", str(self.up), str(dest)], check=True, env=clean_env(**SETUP_IDENTITY))
        git(dest, "checkout", "-q", self.base)
        return dest


class TheBug(Fixture):
    def test_without_the_fix_the_cherry_pick_fails_exactly_like_on_the_runner(self):
        """If this stops failing, the test environment no longer reproduces the bug and the tests below prove nothing."""
        dest = self.susfs_checkout()
        code, out = sh(f'git fetch -q "{self.up}" {self.fix1} && git cherry-pick {self.fix1}', env=clean_env(), cwd=dest)
        self.assertNotEqual(code, 0)
        self.assertIn("Committer identity unknown", out)

    def test_an_empty_identity_name_fails_with_the_runners_other_message(self):
        dest = self.susfs_checkout()
        code, out = sh(f'git fetch -q "{self.up}" {self.fix1} && git cherry-pick {self.fix1}',
                       env=clean_env(GIT_COMMITTER_NAME="", GIT_AUTHOR_NAME="", GIT_COMMITTER_EMAIL="r@h", GIT_AUTHOR_EMAIL="r@h"),
                       cwd=dest)
        self.assertNotEqual(code, 0)
        self.assertIn("empty ident name", out)


class TheFix(Fixture):
    def functions(self, body, env=None, cwd=None):
        script = f'WORKDIR="{ROOT}"; source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"\n{body}'
        if cwd is None:
            cwd = tempfile.gettempdir()
        return sh(script, env=env or clean_env(), cwd=cwd)

    def test_ensure_git_identity_lets_a_cherry_pick_succeed(self):
        dest = self.susfs_checkout()
        code, out = self.functions(f'ensure_git_identity; git fetch -q "{self.up}" {self.fix1} && git cherry-pick {self.fix1} && '
                                   'git log -1 --format="AUTHOR=%an COMMITTER=%cn"', cwd=dest)
        self.assertEqual(code, 0, out)
        self.assertIn("AUTHOR=setup COMMITTER=gkid-ci", out)   # the commit keeps its author, the runner is the committer

    def test_the_empty_name_failure_is_fixed_too(self):
        dest = self.susfs_checkout()
        env = clean_env(GIT_COMMITTER_NAME="", GIT_AUTHOR_NAME="", GIT_COMMITTER_EMAIL="r@h", GIT_AUTHOR_EMAIL="r@h")
        code, out = self.functions(f'ensure_git_identity; git fetch -q "{self.up}" {self.fix1} && git cherry-pick {self.fix1}', env=env, cwd=dest)
        self.assertEqual(code, 0, out)

    def test_an_ordinary_commit_works_too(self):
        """The plain KernelSU path runs `git commit`."""
        dest = self.susfs_checkout()
        code, out = self.functions('ensure_git_identity; echo x >> new.txt && git add new.txt && git commit -qm "susfs patch" && echo COMMITTED', cwd=dest)
        self.assertEqual(code, 0, out)
        self.assertIn("COMMITTED", out)

    def test_an_identity_you_configured_yourself_is_kept(self):
        config = Path(tempfile.mkdtemp()) / "gitconfig"
        self.addCleanup(shutil.rmtree, config.parent, True)
        config.write_text("[user]\n\tname = Alice\n\temail = alice@example.com\n")
        before = config.read_text()
        code, out = self.functions('ensure_git_identity; echo "NAME_ENV=[${GIT_COMMITTER_NAME:-}]"; git var GIT_COMMITTER_IDENT',
                                   env=clean_env(global_config=str(config)))
        self.assertEqual(code, 0, out)
        self.assertIn("NAME_ENV=[]", out)           # nothing was exported
        self.assertIn("Alice <alice@example.com>", out)
        self.assertEqual(config.read_text(), before)

    def test_no_git_config_file_is_modified(self):
        config = Path(tempfile.mkdtemp()) / "gitconfig"
        self.addCleanup(shutil.rmtree, config.parent, True)
        code, out = self.functions('ensure_git_identity; git var GIT_COMMITTER_IDENT', env=clean_env(global_config=str(config)))
        self.assertEqual(code, 0, out)
        self.assertFalse(config.exists(), "the identity must come from the environment, not from git config --global")

    def test_it_is_safe_to_call_twice(self):
        code, out = self.functions('ensure_git_identity; ensure_git_identity; git var GIT_COMMITTER_IDENT')
        self.assertEqual(code, 0, out)
        self.assertIn("gkid-ci <ci@localhost>", out)


class PershootFixes(Fixture):
    """The real apply_pershoot_susfs_fixes from functions-extra.sh, with a local repository instead of GitLab."""

    def run_fixes(self, commits, env=None):
        dest = self.susfs_checkout()
        script = f'''
            WORKDIR="{ROOT}"; SUSFS_DIR="{dest}"
            source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"
            PERSHOOT_SUSFS_URL="{self.up}" PERSHOOT_SUSFS_COMMITS="{commits}" apply_pershoot_susfs_fixes
            echo "DONE rc=$?"
            git -C "{dest}" log --format=%s -3
            cat "{dest}/kernel_patches/fs/susfs.c"
        '''
        code, out = sh(script, env=env or clean_env())
        return code, out

    def test_both_fixes_apply_on_a_machine_without_a_git_identity(self):
        code, out = self.run_fixes(f"{self.fix1} {self.fix2}")
        self.assertEqual(code, 0, out)
        self.assertIn("DONE rc=0", out)
        self.assertIn("line2 fixed", out)
        self.assertIn("line6 fixed", out)
        self.assertNotIn("Committer identity unknown", out)

    def test_the_fixes_are_applied_in_order_and_each_becomes_a_commit(self):
        code, out = self.run_fixes(f"{self.fix1} {self.fix2}")
        self.assertEqual(code, 0, out)
        subjects = [l for l in out.splitlines() if l in ("fix one", "fix two", "base")]
        self.assertEqual(subjects, ["fix two", "fix one", "base"])

    def test_a_commit_that_does_not_exist_fails_with_the_documented_message(self):
        code, out = self.run_fixes(f"{self.fix1} {'0' * 40}")
        self.assertNotEqual(code, 0)
        self.assertIn("pershoot susfs4ksu fixes failed to apply", out)
        self.assertNotIn("DONE rc=0", out)

    def test_the_default_commits_are_still_the_two_from_before(self):
        text = (ROOT / "functions-extra.sh").read_text(encoding="utf-8")
        self.assertIn("6387756d64b3ab9ececefc4523627a9ee05a7519 161c058fed5544a6573d87124f346dadd5c4962d", text)
        self.assertIn("https://gitlab.com/pershoot/susfs4ksu.git", text)


class BuildScript(unittest.TestCase):
    def test_build_sh_sets_the_identity_before_any_patch_step_can_run(self):
        text = (ROOT / "build.sh").read_text(encoding="utf-8")
        identity = text.index("\nensure_git_identity\n")
        for later in ("\nclone_kernel_source", "install_ksun", "\nclone_susfs", "git commit"):
            self.assertIn(later, text, later)
            self.assertLess(identity, text.index(later), f"ensure_git_identity must come before {later.strip()}")

    def test_install_ksun_uses_the_extracted_function(self):
        text = (ROOT / "functions-extra.sh").read_text(encoding="utf-8")
        body = text[text.index("install_ksun() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn("apply_pershoot_susfs_fixes", body)
        self.assertNotIn("git cherry-pick", body)


if __name__ == "__main__":
    unittest.main()
