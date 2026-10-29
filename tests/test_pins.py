#!/usr/bin/env python3
"""Tests for exact-version and commit pins: validation, resolution against a realistic repository, and cloning.

A small kernel-like repository is built on the fly and served as a partial-clone remote (the same
`--filter=tree:0` clone the workflow uses), with merge commits and skipped stable versions.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py"))

import kernelver  # noqa: E402
import resolve_kernel_commit as rkc  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t", GIT_TERMINAL_PROMPT="0")
BRANCH = "GKID-6.1"
REF = f"refs/remotes/origin/{BRANCH}"


def git(cwd, *args, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=ENV)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout.strip()


def makefile(sub, version=6, level=1):
    return f"# SPDX\nVERSION = {version}\nPATCHLEVEL = {level}\nSUBLEVEL = {sub}\nEXTRAVERSION =\nNAME = Curry Ramen\n"


@unittest.skipUnless(shutil.which("git"), "git is required")
class PinFixture(unittest.TestCase):
    """upstream: 6.1.100, then 101, 102, 105, 106, 107 (103 and 104 never existed on this branch)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        up = cls.tmp / "up"
        subprocess.run(["git", "init", "-q", "-b", BRANCH, str(up)], check=True, env=ENV)
        git(up, "config", "uploadpack.allowFilter", "true")
        git(up, "config", "uploadpack.allowAnySHA1InWant", "true")
        (up / "Makefile").write_text(makefile(100))
        (up / "f").write_text("a\n")
        git(up, "add", "-A")
        git(up, "commit", "-qm", "base")
        cls.commits = {}
        for sub in (101, 102, 105, 106, 107):
            with open(up / "f", "a") as handle:
                handle.write(f"device fix {sub}\n")
            git(up, "commit", "-qam", f"device fix before {sub}")
            git(up, "checkout", "-q", "-b", f"stable-{sub}")
            (up / "Makefile").write_text(makefile(sub))
            git(up, "commit", "-qam", f"stable {sub}")
            git(up, "checkout", "-q", BRANCH)
            (up / "g").write_text(f"x{sub}\n")
            git(up, "add", "g")
            git(up, "commit", "-qm", f"device commit after fork {sub}")
            git(up, "merge", "-q", "--no-ff", f"stable-{sub}", "-m", f"Merge stable {sub}", "-X", "theirs", check=False)
            (up / "Makefile").write_text(makefile(sub))
            git(up, "add", "-A")
            git(up, "commit", "-qm", f"Merge stable {sub}", check=False)
            cls.commits[sub] = git(up, "rev-parse", "HEAD")
            with open(up / "f", "a") as handle:
                handle.write(f"post-merge fix {sub}\n")
            git(up, "commit", "-qam", f"post-merge device fix {sub}")
            cls.commits[f"{sub}-last"] = git(up, "rev-parse", "HEAD")
        # a commit that exists upstream but only on another branch
        git(up, "checkout", "-q", "-b", "side", cls.commits[102])
        (up / "side").write_text("side\n")
        git(up, "add", "side")
        git(up, "commit", "-qm", "side branch only")
        cls.side = git(up, "rev-parse", "HEAD")
        git(up, "checkout", "-q", BRANCH)
        cls.up = up
        cls.clone = cls.tmp / "k"
        subprocess.run(["git", "clone", "-q", "--filter=tree:0", "--no-checkout", "--single-branch",
                        f"file://{up}", "-b", BRANCH, str(cls.clone)], check=True, env=ENV)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def resolve(self, base, pin):
        return rkc.resolve(str(self.clone), REF, base, pin)

    def last_reporting(self, next_sub):
        """The last commit that still reports the release before `next_sub`: the parent of the merge that bumped it."""
        return git(self.up, "rev-parse", f"{self.commits[next_sub]}^1")


class VersionPins(PinFixture):
    def test_finds_the_last_commit_that_reports_the_version(self):
        result = self.resolve("6.1", "6.1.106")
        self.assertEqual(result["version"], "6.1.106")
        self.assertEqual(result["commit"], self.last_reporting(107))

    def test_missing_version_names_the_nearest_ones(self):
        with self.assertRaises(rkc.ResolveError) as ctx:
            self.resolve("6.1", "6.1.103")
        self.assertIn("Nearest versions: 6.1.102, 6.1.105", str(ctx.exception))

    def test_version_above_and_below_the_history(self):
        with self.assertRaises(rkc.ResolveError):
            self.resolve("6.1", "6.1.200")
        with self.assertRaises(rkc.ResolveError):
            self.resolve("6.1", "6.1.50")

    def test_version_from_another_line_is_refused(self):
        with self.assertRaises(rkc.ResolveError) as ctx:
            self.resolve("6.1", "6.6.10")
        self.assertIn("belongs to the 6.6 line, but the selected kernel base is 6.1", str(ctx.exception))


class CommitPins(PinFixture):
    def test_full_sha(self):
        result = self.resolve("6.1", self.commits[105])
        self.assertEqual((result["commit"], result["version"], result["mode"]), (self.commits[105], "6.1.105", "commit"))

    def test_abbreviated_sha(self):
        result = self.resolve("6.1", self.commits[102][:9])
        self.assertEqual(result["commit"], self.commits[102])
        self.assertEqual(result["version"], "6.1.102")

    def test_a_commit_between_releases_reports_the_release_it_sits_in(self):
        self.assertEqual(self.resolve("6.1", self.commits["101-last"])["version"], "6.1.101")

    def test_unknown_commit(self):
        with self.assertRaises(rkc.ResolveError) as ctx:
            self.resolve("6.1", "deadbeef")
        message = str(ctx.exception)
        self.assertIn("does not exist in the history of", message)
        self.assertIn("The branch tip is Linux 6.1.107", message)

    def test_commit_on_another_branch_is_refused(self):
        with self.assertRaises(rkc.ResolveError) as ctx:
            self.resolve("6.1", self.side)
        self.assertRegex(str(ctx.exception), r"history of|not part of")

    def test_commit_of_another_kernel_line_is_refused(self):
        with self.assertRaises(rkc.ResolveError) as ctx:
            self.resolve("6.6", self.commits[105])
        self.assertIn("belongs to the 6.1 line, but the selected kernel base is 6.6", str(ctx.exception))


class Validation(unittest.TestCase):
    def test_versions_and_commits_are_accepted_for_one_line(self):
        for pin in ("6.1.180", "abcdef1", "0123456789abcdef0123456789abcdef01234567", "ABCDEF1"):
            self.assertEqual(kernelver.validate("6.1", pin), "", pin)
        self.assertEqual(kernelver.validate("6.1", ""), "")

    def test_all_is_refused_for_both_kinds(self):
        for pin in ("6.1.180", "abcdef1"):
            self.assertIn("single kernel line", kernelver.validate("All", pin))

    def test_garbage_is_refused_with_an_explanation(self):
        for pin in ("6.1", "6.1.x", "abc", "xyz1234", "6.1.180-rc1", "abcdef1 "):
            message = kernelver.validate("6.1", pin)
            self.assertTrue(message, pin)
        self.assertIn("neither a Linux version nor a commit", kernelver.validate("6.1", "6.1.x"))
        self.assertIn("drop the leading 'v'", kernelver.validate("6.1", "v6.1.180"))

    def test_line_mismatch(self):
        self.assertIn("belongs to the 6.6 line, but the selected kernel base is 6.1", kernelver.validate("6.1", "6.6.1"))

    def test_lines_come_from_the_registry(self):
        self.assertEqual(kernelver.validate("6.12", "6.12.3"), "")
        self.assertIn("Unknown KERNEL_VERSION '7.0'", kernelver.validate("7.0", "7.0.1"))
        self.assertIn("5.10, 5.15, 6.1, 6.6, 6.12", kernelver.validate("7.0", "7.0.1"))


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class CloneEndToEnd(PinFixture):
    """The real clone_kernel_source / verify_exact_kernel_version from functions-extra.sh."""

    def run_clone(self, pin, linux_version=None):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        script = f'''
            WORKDIR="{ROOT}"; KSRC="{work}/ksrc"
            source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"
            export GIT_CLONE_PROTECTION_ACTIVE=false
            KERNEL_REPO="file://{self.up}"; KERNEL_BRANCH="{BRANCH}"; KERNEL_VERSION=6.1; KERNEL_EXACT_VERSION="{pin}"
            clone_kernel_source
            echo "HEAD=$(git -C "$KSRC" rev-parse HEAD)"
            echo "SUBLEVEL=$(grep '^SUBLEVEL' "$KSRC/Makefile" | tr -d ' ')"
            LINUX_VERSION="{linux_version or ''}"
            [ -z "$LINUX_VERSION" ] || {{ verify_exact_kernel_version; echo VERIFIED; }}
        '''
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=dict(ENV, PATH=os.environ["PATH"]))
        return result.returncode, result.stdout + result.stderr

    def test_version_pin_checks_out_the_right_commit(self):
        code, out = self.run_clone("6.1.105", "6.1.105")
        self.assertEqual(code, 0, out)
        self.assertIn(f"HEAD={self.last_reporting(106)}", out)
        self.assertIn("SUBLEVEL=105", out)
        self.assertIn("VERIFIED", out)

    def test_commit_pin_checks_out_that_commit_and_verifies_its_version(self):
        code, out = self.run_clone(self.commits[102][:10], "6.1.102")
        self.assertEqual(code, 0, out)
        self.assertIn(f"HEAD={self.commits[102]}", out)
        self.assertIn("VERIFIED", out)

    def test_commit_pin_fails_verification_when_the_tree_reports_another_version(self):
        code, out = self.run_clone(self.commits[102][:10], "6.1.999")
        self.assertNotEqual(code, 0)
        self.assertIn("but the source reports 6.1.999", out)

    def test_missing_version_stops_the_clone_with_nearby_versions(self):
        code, out = self.run_clone("6.1.103")
        self.assertEqual(code, 1, out)   # clone_kernel_source exits 1 itself
        self.assertNotIn("HEAD=", out)
        self.assertIn("Nearest versions: 6.1.102, 6.1.105", out)

    def test_unknown_commit_stops_the_clone(self):
        code, out = self.run_clone("deadbeef")
        self.assertEqual(code, 1, out)
        self.assertNotIn("HEAD=", out)
        self.assertIn("does not exist in the history", out)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class Preflight(PinFixture):
    """The first workflow job checks a pin with preflight_pin_for before any build job starts."""

    def run_preflight(self, pin, repo=None, branch=BRANCH, line="6.1"):
        script = f'''
            WORKDIR="{ROOT}"; source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"
            preflight_pin_for "{repo or 'file://' + str(self.up)}" "{branch}" "{line}" "{pin}"
        '''
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=dict(ENV, PATH=os.environ["PATH"]))
        return r.returncode, r.stdout + r.stderr

    def test_existing_version_resolves_without_downloading_file_contents(self):
        code, out = self.run_preflight("6.1.105")
        self.assertEqual(code, 0, out)
        self.assertIn(f"6.1.105 resolves to {self.last_reporting(106)} (Linux 6.1.105)", out)

    def test_existing_commit_resolves(self):
        code, out = self.run_preflight(self.commits[102][:10])
        self.assertEqual(code, 0, out)
        self.assertIn("(Linux 6.1.102)", out)

    def test_missing_version_fails_early_with_nearby_versions(self):
        code, out = self.run_preflight("6.1.103")
        self.assertEqual(code, 1)
        self.assertIn("Nearest versions: 6.1.102, 6.1.105", out)

    def test_wrong_line_fails_early(self):
        code, out = self.run_preflight("6.6.4")
        self.assertEqual(code, 1)
        self.assertIn("belongs to the 6.6 line", out)

    def test_unreachable_source_or_branch_fails_with_a_clear_message(self):
        code, out = self.run_preflight("6.1.105", branch="no-such-branch")
        self.assertEqual(code, 1)
        self.assertIn("Could not clone branch no-such-branch", out)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class BuildSummary(PinFixture):
    def summary(self, extra=""):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        script = f'''
            WORKDIR="{ROOT}"; KSRC="{work}/ksrc"; GITHUB_STEP_SUMMARY="{work}/summary.md"; GITHUB_ENV="{work}/env"
            source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"
            export GIT_CLONE_PROTECTION_ACTIVE=false
            KERNEL_REPO="file://{self.up}"; KERNEL_BRANCH="{BRANCH}"; KERNEL_VERSION=6.1; KERNEL_EXACT_VERSION="6.1.105"
            KERNEL_SOURCE_ID=gkid-duchamp-6.1; KERNEL_REPO_ORIGIN="built-in default"; KERNEL_BRANCH_ORIGIN="built-in default"
            {extra}
            clone_kernel_source; LINUX_VERSION=6.1.105; verify_exact_kernel_version; record_kernel_source
            cat "$GITHUB_STEP_SUMMARY"
        '''
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=dict(ENV, PATH=os.environ["PATH"]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_summary_states_the_source_id_and_what_is_verified(self):
        out = self.summary()
        self.assertIn("| Registry source | `gkid-duchamp-6.1` |", out)
        self.assertIn("| Device status | needs-verification for duchamp (build: reported, boot: unverified) |", out)
        self.assertIn("| Requested exact version or commit | 6.1.105 |", out)

    def test_summary_says_when_a_variable_replaced_the_source(self):
        out = self.summary(extra='KERNEL_REPO_ORIGIN="variable KERNEL_REPO_6_1"')
        self.assertIn("(replaced by a repository variable)", out)
        self.assertIn("not assessed, the source was replaced by a repository variable", out)


if __name__ == "__main__":
    unittest.main()
