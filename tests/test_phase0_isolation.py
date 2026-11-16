"""Phase 0 isolation: the analysis must never touch another repository, the user's index or working tree, the
HyperCore checkout, or the reference repositories it reads. Includes the regression for the audit finding that
revision 1 leaked into an unrelated repository when GIT_DIR was set in the environment."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase0_helpers as h  # noqa: E402

mod = h.load_script()


def tree_hash(root, skip=()):
    """Hash of every file under root (names and contents)."""
    d = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and not any(s in p.parts for s in skip):
            d.update(str(p.relative_to(root)).encode())
            d.update(p.read_bytes())
    return d.hexdigest()


def repo_snapshot(repo):
    """Everything observable about a repository: index bytes, status, refs, remotes, config, object counts, files."""
    g = Path(repo) / ".git"
    return {
        "index": hashlib.sha256((g / "index").read_bytes()).hexdigest(),
        "status": h.git(repo, "status", "--porcelain=v2", "--untracked-files=all"),
        "refs": h.git(repo, "for-each-ref"),
        "remotes": h.git(repo, "remote"),
        "config": (g / "config").read_text(),
        "head": (g / "HEAD").read_text(),
        "objects": h.git(repo, "count-objects", "-v"),
        "worktree": tree_hash(repo, skip=(".git",)),
    }


class World:
    """ACK, GKID and Luminaire fixtures, a HyperCore checkout with patches, and a 'victim' repository with staged,
    unstaged and untracked changes."""

    def __init__(self):
        self.tmp = Path(h.tmpdir())
        t = self.tmp
        self.ack = h.init_repo(t / "ack", "android14-6.1-lts")
        a1 = h.commit(self.ack, {"Makefile": h.makefile(100), "mm/vmscan.c": "int lru = 0;\n"}, "a1")
        self.gk = h.clone(self.ack, t / "gkid", "GKID-6.1", a1)
        h.commit(self.gk, {"mm/vmscan.c": "int lru = 1;\n", "arch/arm64/boot/dts/mediatek/mt6897.dts": "d\n"}, "g")
        self.lm = h.clone(self.ack, t / "lum", "android14-6.1", a1)
        h.commit(self.lm, {"arch/arm64/boot/dts/mediatek/mt6897.dts": "d\n"}, "l")
        self.hc = t / "hc"
        h.write(self.hc, "kernel-patches/common/0001-lru.patch",
                "diff --git a/mm/vmscan.c b/mm/vmscan.c\n--- a/mm/vmscan.c\n+++ b/mm/vmscan.c\n@@ -1 +1 @@\n"
                "-int lru = 0;\n+int lru = 1;\n")
        h.write(self.hc, "sources.json", '{"sources": {}}\n')
        self.reports = Path(h.tmpdir())  # reports never share a directory tree with a scratch repository under test
        self.victim = h.init_repo(t / "victim", "main")
        h.commit(self.victim, {"tracked.txt": "one\n", "other.txt": "x\n"}, "victim base")
        h.write(self.victim, "tracked.txt", "one\nunstaged edit\n")
        h.write(self.victim, "staged.txt", "staged\n")
        h.git(self.victim, "add", "staged.txt")
        h.write(self.victim, "untracked.txt", "u\n")

    def cli(self, workdir, env=None, extra=(), tmpdir=None):
        full = {**os.environ, **(env or {})}
        if tmpdir:
            full["TMPDIR"] = str(tmpdir)
        cmd = [sys.executable, str(h.SCRIPT), "--workdir", str(workdir), "--hypercore", str(self.hc),
               "--ack-url", str(self.ack), "--ack-branch", "android14-6.1-lts", "--gkid-url", str(self.gk),
               "--gkid-branch", "GKID-6.1", "--luminaire-url", str(self.lm), "--luminaire-branch", "android14-6.1",
               "--out", str(self.reports / "o.md"), "--json", str(self.reports / "o.json"), *extra]
        return subprocess.run(cmd, capture_output=True, text=True, env=full)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.reports, ignore_errors=True)


class Isolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = World()

    @classmethod
    def tearDownClass(cls):
        cls.w.cleanup()

    def test_polluted_git_environment_cannot_reach_another_repository(self):
        """Regression for the revision 1 finding: with GIT_DIR (and friends) in the environment, git -C was
        overridden and revision 1 added remotes and refs to the repository GIT_DIR pointed at."""
        w = self.w
        before = repo_snapshot(w.victim)
        vg = w.victim / ".git"
        polluted = {"GIT_DIR": str(vg), "GIT_WORK_TREE": str(w.victim), "GIT_INDEX_FILE": str(vg / "index"),
                    "GIT_OBJECT_DIRECTORY": str(vg / "objects"), "GIT_COMMON_DIR": str(vg),
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true"}
        tmpd = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, tmpd, True)
        proc = w.cli(w.tmp / "scratch-polluted", env=polluted, tmpdir=tmpd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(repo_snapshot(w.victim), before)
        self.assertEqual(h.git(w.victim, "remote"), "")
        self.assertEqual(list(tmpd.glob("hc-phase0-index-*")), [], "temporary index directories were left behind")

    def test_scratch_repository_is_bare_with_no_index_and_a_marker(self):
        w = self.w
        proc = w.cli(w.tmp / "scratch-shape")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sc = w.tmp / "scratch-shape"
        self.assertTrue((sc / mod.SCRATCH_MARKER).exists())
        self.assertFalse((sc / "index").exists(), "the scratch repository must never get an index of its own")
        self.assertFalse((sc / ".git").exists())
        self.assertEqual(h.git(sc, "rev-parse", "--is-bare-repository", GIT_DIR=str(sc)), "true")

    def test_reference_repositories_and_hypercore_are_not_modified(self):
        w = self.w
        before = {"hc": tree_hash(w.hc)}
        for name in ("ack", "gk", "lm"):
            r = getattr(w, name)
            before[name] = (h.git(r, "for-each-ref"), h.git(r, "count-objects", "-v"), tree_hash(r, skip=(".git",)),
                            h.git(r, "status", "--porcelain"))
        proc = w.cli(w.tmp / "scratch-readonly")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(tree_hash(w.hc), before["hc"])
        for name in ("ack", "gk", "lm"):
            r = getattr(w, name)
            self.assertEqual((h.git(r, "for-each-ref"), h.git(r, "count-objects", "-v"), tree_hash(r, skip=(".git",)),
                              h.git(r, "status", "--porcelain")), before[name], name)

    def test_the_patch_review_leaves_the_scratch_repository_without_staged_state(self):
        w = self.w
        proc = w.cli(w.tmp / "scratch-index")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sc = w.tmp / "scratch-index"
        self.assertEqual(h.git(sc, "ls-files", "--stage", GIT_DIR=str(sc)), "")  # default index stays empty

    def test_workdir_is_refused_when_it_could_be_a_repository_that_matters(self):
        w = self.w
        cases = ((w.hc, "the HyperCore checkout", "must be outside the HyperCore checkout"),
                 (w.hc / "inside", "inside HyperCore", "must be outside the HyperCore checkout"),
                 (w.tmp, "an ancestor of HyperCore", "must be outside the HyperCore checkout"),
                 (w.victim, "a non-scratch, non-empty directory", "is not a Phase 0 scratch directory"))
        for bad, why, message in cases:
            before = repo_snapshot(w.victim)
            proc = w.cli(bad)
            self.assertEqual(proc.returncode, 2, f"{why}: {proc.stderr}")
            self.assertIn(message, proc.stderr, why)  # refused by the intended guard, not by a later accident
            self.assertEqual(repo_snapshot(w.victim), before)

    def test_a_scratch_directory_can_be_reused(self):
        w = self.w
        sc = w.tmp / "scratch-reuse"
        self.assertEqual(w.cli(sc).returncode, 0)
        self.assertEqual(w.cli(sc, extra=["--no-fetch"]).returncode, 0)


class OutputsStayOutsideTheCheckout(unittest.TestCase):
    """The reports are never written into the HyperCore checkout, whatever the arguments or working directory."""

    @classmethod
    def setUpClass(cls):
        cls.w = World()

    @classmethod
    def tearDownClass(cls):
        cls.w.cleanup()

    def run_with(self, out, json_out, cwd=None, workdir=None):
        w = self.w
        cmd = [sys.executable, str(h.SCRIPT), "--workdir", str(workdir or w.tmp / "scratch-out"), "--hypercore",
               str(w.hc), "--ack-url", str(w.ack), "--ack-branch", "android14-6.1-lts", "--gkid-url", str(w.gk),
               "--gkid-branch", "GKID-6.1", "--luminaire-url", str(w.lm), "--luminaire-branch", "android14-6.1"]
        if out is not None:
            cmd += ["--out", str(out)]
        if json_out is not None:
            cmd += ["--json", str(json_out)]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env={**os.environ,
                              "PYTHONDONTWRITEBYTECODE": "1"})

    def test_a_report_path_inside_the_checkout_is_refused_before_any_work(self):
        w = self.w
        before = tree_hash(w.hc)
        for out, js in ((w.hc / "r.md", w.tmp / "ok.json"), (w.tmp / "ok.md", w.hc / "r.json"),
                        (w.hc / "sub" / "dir" / "r.md", w.tmp / "ok.json"), (w.hc, w.tmp / "ok.json")):
            scratch = w.tmp / "scratch-refused"
            shutil.rmtree(scratch, ignore_errors=True)
            proc = self.run_with(out, js, workdir=scratch)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("is inside the HyperCore checkout", proc.stderr)
            self.assertFalse(scratch.exists(), "nothing may be fetched or created before the paths are checked")
            self.assertEqual(tree_hash(w.hc), before)
        self.assertFalse((w.hc / "sub").exists(), "no directory was created inside the checkout")

    def test_the_default_relative_names_are_refused_when_run_from_the_checkout(self):
        w = self.w
        before = tree_hash(w.hc)
        proc = self.run_with(None, None, cwd=w.hc)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("is inside the HyperCore checkout", proc.stderr)
        self.assertEqual(tree_hash(w.hc), before)

    def test_a_full_run_from_inside_the_checkout_leaves_it_byte_for_byte_unchanged(self):
        """What the workflow does: cwd is the workspace, reports go to a temporary directory."""
        w = self.w
        before = tree_hash(w.hc)
        report_dir = w.tmp / "runner-temp" / "phase0-report"  # does not exist yet: the script creates it
        proc = self.run_with(report_dir / "delta-report.md", report_dir / "delta-report.json", cwd=w.hc,
                             workdir=w.tmp / "runner-temp" / "delta-work")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((report_dir / "delta-report.md").is_file())
        self.assertTrue((report_dir / "delta-report.json").is_file())
        self.assertEqual(tree_hash(w.hc), before)
        self.assertEqual(sorted(p.name for p in w.hc.iterdir()), ["kernel-patches", "sources.json"])


class OutputsStayOutsideTheScratchRepository(unittest.TestCase):
    """--out and --json must not point inside the bare scratch repository (--workdir) either: a report written to
    `HEAD`, `objects/` or `refs/` would corrupt it. Checked before anything is fetched, analysed or written."""

    @classmethod
    def setUpClass(cls):
        cls.w = World()
        cls.scratch = cls.w.tmp / "scratch-guard"
        proc = cls.w.cli(cls.scratch)  # a populated scratch repository to protect
        assert proc.returncode == 0, proc.stderr
        cls.outside = cls.w.tmp / "outside"
        cls.outside.mkdir()
        (cls.outside / "to-scratch").symlink_to(cls.scratch, target_is_directory=True)
        (cls.outside / "head-link.md").symlink_to(cls.scratch / "HEAD")

    @classmethod
    def tearDownClass(cls):
        cls.w.cleanup()

    def run_with(self, flag, bad, workdir=None, cwd=None, ack_url=None):
        """Run with `bad` as the value of `flag` and a harmless path for the other output."""
        w = self.w
        other = w.tmp / "outside" / "other-report"
        values = {"--out": str(other) + ".md", "--json": str(other) + ".json"}
        values[flag] = str(bad)
        cmd = [sys.executable, str(h.SCRIPT), "--workdir", str(workdir or self.scratch), "--hypercore", str(w.hc),
               "--ack-url", ack_url or str(w.ack), "--ack-branch", "android14-6.1-lts", "--gkid-url", str(w.gk),
               "--gkid-branch", "GKID-6.1", "--luminaire-url", str(w.lm), "--luminaire-branch", "android14-6.1",
               "--out", values["--out"], "--json", values["--json"]]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)

    def bad_paths(self):
        s = self.scratch
        return [s, s / "report.md", s / "objects" / "x.md", s / "refs" / "heads" / "r.json", s / "HEAD",
                s / mod.SCRATCH_MARKER, s / "new" / "sub" / "dir" / "r.md",
                s / ".." / "scratch-guard" / "r.md",                       # `..` that comes back in
                self.outside / ".." / "scratch-guard" / "r.json",
                self.outside / "to-scratch" / "r.md",                      # through a symlinked directory
                self.outside / "head-link.md"]                             # a symlinked file that points at HEAD

    def test_a_markdown_or_json_path_inside_the_scratch_repository_is_refused(self):
        before = tree_hash(self.scratch)
        for flag in ("--out", "--json"):
            for bad in self.bad_paths():
                proc = self.run_with(flag, bad)
                self.assertEqual(proc.returncode, 2, f"{flag} {bad}: {proc.stderr}")
                self.assertIn(f"{flag} ", proc.stderr)
                self.assertIn("is inside the scratch repository", proc.stderr, f"{flag} {bad}")
                self.assertNotIn("[delta]", proc.stderr, f"{flag} {bad}: analysis had started")
        self.assertEqual(tree_hash(self.scratch), before, "the scratch repository was modified")
        self.assertFalse((self.scratch / "new").exists(), "a directory was created inside the scratch repository")
        self.assertEqual(h.git(self.scratch, "fsck", "--no-progress", GIT_DIR=str(self.scratch)) or "ok", "ok")

    def test_the_head_file_is_not_overwritten_by_a_symlinked_report_path(self):
        head = (self.scratch / "HEAD").read_text()
        proc = self.run_with("--out", self.outside / "head-link.md")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual((self.scratch / "HEAD").read_text(), head)

    def test_a_relative_path_resolved_into_the_scratch_repository_is_refused(self):
        proc = self.run_with("--out", "report.md", cwd=self.scratch)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("is inside the scratch repository", proc.stderr)
        self.assertFalse((self.scratch / "report.md").exists())

    def test_a_scratch_directory_that_does_not_exist_yet_is_protected_too(self):
        w = self.w
        new = w.tmp / "scratch-not-yet"
        for flag in ("--out", "--json"):
            for bad in (new / "r.md", new / "objects" / "r.json", new):
                proc = self.run_with(flag, bad, workdir=new)
                self.assertEqual(proc.returncode, 2, f"{flag} {bad}: {proc.stderr}")
                self.assertIn("is inside the scratch repository", proc.stderr)
                self.assertFalse(new.exists(), "refusal must come before the scratch repository is created")

    def test_the_check_happens_before_any_fetch_or_other_failure(self):
        """With an ACK source that does not exist, a fetch would fail with a different error. The output path is
        rejected first, so no network or repository access was attempted."""
        proc = self.run_with("--json", self.scratch / "r.json", ack_url=str(self.w.tmp / "no-such-repo"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("is inside the scratch repository", proc.stderr)
        self.assertNotIn("cannot list branches", proc.stderr)
        self.assertNotIn("[delta]", proc.stderr)

    def test_the_checkout_is_still_refused_and_is_named_as_such(self):
        proc = self.run_with("--out", self.w.hc / "r.md")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("is inside the HyperCore checkout", proc.stderr)
        self.assertNotIn("scratch repository", proc.stderr.split("is inside")[1].split(";")[0])

    def test_a_sibling_directory_with_the_same_prefix_is_not_mistaken_for_the_scratch_repository(self):
        w = self.w
        sibling = w.tmp / "scratch-guard-other"
        out, js = sibling / "r.md", sibling / "r.json"
        cmd = [sys.executable, str(h.SCRIPT), "--workdir", str(self.scratch), "--hypercore", str(w.hc),
               "--ack-url", str(w.ack), "--ack-branch", "android14-6.1-lts", "--gkid-url", str(w.gk),
               "--gkid-branch", "GKID-6.1", "--luminaire-url", str(w.lm), "--luminaire-branch", "android14-6.1",
               "--out", str(out), "--json", str(js), "--skip-patches"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.is_file() and js.is_file())

    def test_check_output_paths_directly(self):
        w, s = self.w, self.scratch
        ok = str(self.outside / "ok")
        mod.check_output_paths(ok + ".md", ok + ".json", str(w.hc), str(s))  # no exception
        for out, js, expected in ((str(s / "a.md"), ok, "scratch repository"), (ok, str(s / "a.json"), "scratch repository"),
                                  (str(w.hc / "a.md"), ok, "HyperCore checkout"), (ok, str(w.hc / "a.json"), "HyperCore checkout")):
            with self.assertRaises(mod.GitError) as cm:
                mod.check_output_paths(out, js, str(w.hc), str(s))
            self.assertIn(expected, str(cm.exception))


class OutputFileEdgeCases(unittest.TestCase):
    """--out and --json must be two different files, and writing a report never writes THROUGH an existing file, so a
    hard link to some other file (in the scratch repository, the checkout, anywhere) is never modified."""

    @classmethod
    def setUpClass(cls):
        cls.w = World()
        cls.scratch = cls.w.tmp / "scratch-edge"
        proc = cls.w.cli(cls.scratch)
        assert proc.returncode == 0, proc.stderr
        cls.out = cls.w.tmp / "edge-out"
        cls.out.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.w.cleanup()

    def run_with(self, out, json_out, workdir=None, cwd=None, extra=()):
        w = self.w
        cmd = [sys.executable, str(h.SCRIPT), "--workdir", str(workdir or self.scratch), "--hypercore", str(w.hc),
               "--ack-url", str(w.ack), "--ack-branch", "android14-6.1-lts", "--gkid-url", str(w.gk),
               "--gkid-branch", "GKID-6.1", "--luminaire-url", str(w.lm), "--luminaire-branch", "android14-6.1",
               "--out", str(out), "--json", str(json_out), "--skip-patches", *extra]
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)

    def fresh(self, name):
        d = self.out / name
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        return d

    # ---- --out and --json must differ
    def test_identical_paths_are_refused_before_any_work(self):
        d = self.fresh("same")
        new_scratch = self.w.tmp / "scratch-never-created"
        proc = self.run_with(d / "r.out", d / "r.out", workdir=new_scratch)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--out and --json must be two different files", proc.stderr)
        self.assertNotIn("[delta]", proc.stderr, "analysis had started")
        self.assertFalse(new_scratch.exists(), "the scratch repository was created before the check")
        self.assertEqual(list(d.iterdir()), [], "a report was written")

    def test_the_same_file_under_different_spellings_is_refused(self):
        d = self.fresh("spellings")
        (d / "sub").mkdir()
        (d / "link.json").symlink_to(d / "r.md")  # dangling symlink to the Markdown path
        cases = [("dotdot", d / "r.md", d / "sub" / ".." / "r.md", None),
                 ("dot", d / "r.md", d / "." / "r.md", None),
                 ("relative", "r.md", d / "r.md", d),
                 ("symlink", d / "r.md", d / "link.json", None)]
        for name, out, js, cwd in cases:
            proc = self.run_with(out, js, cwd=cwd)
            self.assertEqual(proc.returncode, 2, f"{name}: {proc.stderr}")
            self.assertIn("must be two different files", proc.stderr, name)
            self.assertNotIn("[delta]", proc.stderr, name)
        self.assertEqual(sorted(p.name for p in d.iterdir() if not p.is_symlink()), ["sub"])

    @h.needs_hardlinks
    def test_two_names_for_one_hard_linked_file_are_refused(self):
        d = self.fresh("hardpair")
        (d / "a.md").write_text("existing\n")
        os.link(d / "a.md", d / "b.json")
        proc = self.run_with(d / "a.md", d / "b.json")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("the same file under two names", proc.stderr)
        self.assertEqual((d / "a.md").read_text(), "existing\n")

    def test_two_different_files_are_fine(self):
        d = self.fresh("fine")
        proc = self.run_with(d / "r.md", d / "r.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((d / "r.md").read_text().startswith("# HyperCore Phase 0"))
        self.assertTrue((d / "r.json").read_text().lstrip().startswith("{"))
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["r.json", "r.md"], "no temporary files may remain")

    def test_check_output_paths_directly(self):
        d = self.fresh("direct")
        w, s = self.w, self.scratch
        mod.check_output_paths(str(d / "a.md"), str(d / "b.json"), str(w.hc), str(s))
        with self.assertRaises(mod.GitError) as cm:
            mod.check_output_paths(str(d / "a"), str(d / "x" / ".." / "a"), str(w.hc), str(s))
        self.assertIn("must be two different files", str(cm.exception))

    # ---- hard-link protection
    @h.needs_hardlinks
    def test_an_existing_hard_link_to_a_scratch_file_is_never_written_through(self):
        d = self.fresh("hl-scratch")
        head = self.scratch / "HEAD"
        before = (head.read_text(), head.stat().st_ino)
        os.link(head, d / "linked.md")
        self.assertEqual(head.stat().st_nlink, 2)
        proc = self.run_with(d / "linked.md", d / "r.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((head.read_text(), head.stat().st_ino), before, "HEAD was modified through the hard link")
        self.assertEqual(head.stat().st_nlink, 1, "the link name was replaced, not written through")
        report = d / "linked.md"
        self.assertNotEqual(report.stat().st_ino, head.stat().st_ino)
        self.assertEqual(report.stat().st_nlink, 1)
        self.assertTrue(report.read_text().startswith("# HyperCore Phase 0"))
        self.assertEqual(h.git(self.scratch, "fsck", "--no-progress", GIT_DIR=str(self.scratch)) or "ok", "ok")

    @h.needs_hardlinks
    def test_an_existing_hard_link_to_a_checkout_file_is_never_written_through_json_too(self):
        d = self.fresh("hl-checkout")
        target = self.w.hc / "sources.json"
        before, ino, tree = target.read_text(), target.stat().st_ino, tree_hash(self.w.hc)
        os.link(target, d / "linked.json")
        proc = self.run_with(d / "r.md", d / "linked.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((target.read_text(), target.stat().st_ino), (before, ino))
        self.assertEqual(tree_hash(self.w.hc), tree)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertTrue((d / "linked.json").read_text().lstrip().startswith("{"))

    @h.needs_hardlinks
    def test_the_hazard_is_real_a_plain_write_goes_through_a_hard_link(self):
        d = self.fresh("hazard")
        (d / "other").write_text("precious\n")
        os.link(d / "other", d / "report")
        (d / "report").write_text("overwritten\n")  # what a plain write_text does
        self.assertEqual((d / "other").read_text(), "overwritten\n")
        (d / "other").write_text("precious\n")
        os.link(d / "other", d / "report2")
        mod.write_report(str(d / "report2"), "report\n")
        self.assertEqual((d / "other").read_text(), "precious\n")
        self.assertEqual((d / "report2").read_text(), "report\n")

    def test_write_report_is_atomic_leaves_no_temp_files_and_sets_the_mode(self):
        d = self.fresh("atomic")
        target = d / "new" / "sub" / "r.md"  # parents are created
        mod.write_report(str(target), "one\n")
        first_ino = target.stat().st_ino
        mod.write_report(str(target), "two\n")
        self.assertEqual(target.read_text(), "two\n")
        self.assertNotEqual(target.stat().st_ino, first_ino, "the file must be replaced, not rewritten in place")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["r.md"])

    def test_write_failure_is_a_clear_error_and_leaves_nothing_behind(self):
        d = self.fresh("failing")
        (d / "is-a-directory").mkdir()
        with self.assertRaises(mod.GitError) as cm:
            mod.write_report(str(d / "is-a-directory"), "x\n")
        self.assertIn("cannot write", str(cm.exception))
        self.assertEqual([p.name for p in d.iterdir()], ["is-a-directory"], "a temporary file was left behind")
        proc = self.run_with(d / "is-a-directory", d / "r.json")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("cannot write", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.name.startswith(".")), [], "temporary files remain")

    def test_the_remaining_limits_of_a_path_check_are_documented(self):
        doc = (h.ROOT / "PHASE0.md").read_text()
        for fragment in ("bind mount", "case-insensitive", "atomic", "hard link"):
            self.assertIn(fragment, doc, fragment)
        self.assertIn("bind mounts", (h.SCRIPT).read_text())


class TempIndexGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = mod.ensure_scratch(self.tmp / "s", self.tmp / "elsewhere")

    def test_only_a_private_temporary_index_is_accepted(self):
        victim = h.init_repo(self.tmp / "victim", "main")
        h.commit(victim, {"f": "1\n"}, "c")
        for bad in (victim / ".git" / "index", self.tmp / "index", Path(tempfile.gettempdir()) / "index",
                    Path(tempfile.gettempdir()) / "hc-phase0-index-x" / "other"):
            with self.assertRaises(mod.GitError, msg=str(bad)):
                self.repo.run("read-tree", "--empty", env={"GIT_INDEX_FILE": str(bad)})
        self.assertFalse((self.tmp / "index").exists())

    def test_temporary_index_is_private_and_removed_even_on_failure(self):
        seen = []
        with self.assertRaises(mod.GitError):
            with mod.TempIndex() as ti:
                seen.append(ti.dir)
                self.assertTrue(Path(ti.dir).name.startswith("hc-phase0-index-"))
                self.repo.run("read-tree", "no-such-ref", env=ti.env)
        self.assertFalse(Path(seen[0]).exists())

    def test_each_series_run_gets_its_own_index(self):
        victim_like = h.init_repo(self.tmp / "src", "main")
        sha = h.commit(victim_like, {"a.c": "1\n"}, "c")
        mod.fetch_remote(self.repo, "src", str(victim_like), ["main"])
        patch = self.tmp / "p.patch"
        patch.write_text("diff --git a/a.c b/a.c\n--- a/a.c\n+++ b/a.c\n@@ -1 +1 @@\n-1\n+2\n")
        entry = [{"group": "g", "kind": "tree", "name": "p.patch", "path": patch, "must_exist": ["a.c"],
                  "created": [], "sha256": ""}]
        first = mod.run_series(self.repo, sha, entry)
        second = mod.run_series(self.repo, sha, entry)  # would say reverse-applies if the first run had leaked
        self.assertEqual(first["g/p.patch"]["status"], "applies")
        self.assertEqual(second["g/p.patch"]["status"], "applies")

    def test_environment_is_scrubbed_and_config_is_hermetic(self):
        names = ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                 "GIT_CONFIG_VALUE_0", "GIT_CONFIG_PARAMETERS"]
        old = {n: os.environ.get(n) for n in names}
        try:
            for n in names:
                os.environ[n] = "x"
            env = mod.clean_env(hermetic=True)
            self.assertEqual([n for n in names if n in env], [])
            self.assertEqual((env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"]), (os.devnull, "1"))
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            net = mod.clean_env(hermetic=False)  # the fetch keeps the user's config (proxies, credentials)
            self.assertNotEqual(net.get("GIT_CONFIG_GLOBAL"), os.devnull)
            self.assertNotIn("GIT_DIR", net)  # but is still pinned away from other repositories
            self.assertNotIn("GIT_INDEX_FILE", net)
        finally:
            for n, v in old.items():
                os.environ.pop(n, None)
                if v is not None:
                    os.environ[n] = v

    def test_every_git_call_is_pinned_to_the_scratch_repository(self):
        cmd = self.repo._cmd("log")
        self.assertEqual(cmd[:3], ["git", "--git-dir", str(self.repo.git_dir)])


class ErrorMessages(unittest.TestCase):
    def test_a_missing_branch_lists_the_branches_that_exist(self):
        w = World()
        self.addCleanup(w.cleanup)
        proc = subprocess.run([sys.executable, str(h.SCRIPT), "--workdir", str(w.tmp / "s"), "--hypercore", str(w.hc),
                               "--ack-url", str(w.ack), "--ack-branch", "android14-6.1-lts", "--gkid-url", str(w.gk),
                               "--gkid-branch", "GKID-6.1", "--luminaire-url", str(w.lm), "--luminaire-branch",
                               "does-not-exist"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("has no branch does-not-exist", proc.stderr)
        self.assertIn("android14-6.1", proc.stderr)

    def test_the_ack_upstream_cannot_be_a_reference_tree(self):
        w = World()
        self.addCleanup(w.cleanup)
        proc = subprocess.run([sys.executable, str(h.SCRIPT), "--workdir", str(w.tmp / "s"), "--hypercore", str(w.hc),
                               "--ack-url", str(w.gk), "--ack-branch", "GKID-6.1", "--gkid-url", str(w.gk),
                               "--gkid-branch", "GKID-6.1", "--luminaire-url", str(w.lm), "--luminaire-branch",
                               "android14-6.1"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("same repository and branch", proc.stderr)

    def test_without_registry_or_arguments_there_is_no_silent_default_source(self):
        w = World()
        self.addCleanup(w.cleanup)
        proc = subprocess.run([sys.executable, str(h.SCRIPT), "--workdir", str(w.tmp / "s"), "--hypercore", str(w.hc)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no ACK source", proc.stderr)



class HardlinkPolicyTests(unittest.TestCase):
    def test_supported_hardlinks_are_allowed(self):
        original = h.probe_hardlinks
        try:
            h.probe_hardlinks = lambda: (True, "supported")
            self.assertEqual(h.hardlink_policy(), ("run", "supported"))
        finally:
            h.probe_hardlinks = original

    def test_android_without_hardlinks_is_skipped(self):
        original_probe = h.probe_hardlinks
        original_android = h.is_android
        try:
            h.probe_hardlinks = lambda: (False, "unavailable")
            h.is_android = lambda: True
            self.assertEqual(h.hardlink_policy(), ("skip", "unavailable"))
        finally:
            h.probe_hardlinks = original_probe
            h.is_android = original_android

    def test_non_android_without_hardlinks_fails_by_default(self):
        original_probe = h.probe_hardlinks
        original_android = h.is_android
        old_opt_out = os.environ.get(h.OPT_OUT_ENV)
        try:
            h.probe_hardlinks = lambda: (False, "unavailable")
            h.is_android = lambda: False
            os.environ.pop(h.OPT_OUT_ENV, None)
            self.assertEqual(h.hardlink_policy(), ("fail", "unavailable"))
        finally:
            h.probe_hardlinks = original_probe
            h.is_android = original_android
            os.environ.pop(h.OPT_OUT_ENV, None)
            if old_opt_out is not None:
                os.environ[h.OPT_OUT_ENV] = old_opt_out

    def test_explicit_opt_out_skips_when_hardlinks_are_unavailable(self):
        original_probe = h.probe_hardlinks
        original_android = h.is_android
        old_opt_out = os.environ.get(h.OPT_OUT_ENV)
        try:
            h.probe_hardlinks = lambda: (False, "unavailable")
            h.is_android = lambda: False
            os.environ[h.OPT_OUT_ENV] = "1"
            self.assertEqual(h.hardlink_policy(), ("skip", "unavailable"))
        finally:
            h.probe_hardlinks = original_probe
            h.is_android = original_android
            os.environ.pop(h.OPT_OUT_ENV, None)
            if old_opt_out is not None:
                os.environ[h.OPT_OUT_ENV] = old_opt_out

    def test_decorator_runs_when_hardlinks_are_supported(self):
        original = h.probe_hardlinks
        called = []
        try:
            h.probe_hardlinks = lambda: (True, "supported")

            @h.needs_hardlinks
            def sample(test_case):
                called.append(True)

            sample(self)
            self.assertEqual(called, [True])
        finally:
            h.probe_hardlinks = original

    def test_decorator_skips_on_android_without_hardlinks(self):
        original_probe = h.probe_hardlinks
        original_android = h.is_android
        try:
            h.probe_hardlinks = lambda: (False, "unavailable")
            h.is_android = lambda: True

            @h.needs_hardlinks
            def sample(test_case):
                test_case.fail("The test body must not run")

            with self.assertRaises(unittest.SkipTest):
                sample(self)
        finally:
            h.probe_hardlinks = original_probe
            h.is_android = original_android

    def test_decorator_fails_outside_android_without_hardlinks(self):
        original_probe = h.probe_hardlinks
        original_android = h.is_android
        old_opt_out = os.environ.get(h.OPT_OUT_ENV)
        try:
            h.probe_hardlinks = lambda: (False, "unavailable")
            h.is_android = lambda: False
            os.environ.pop(h.OPT_OUT_ENV, None)

            @h.needs_hardlinks
            def sample(test_case):
                test_case.fail("The test body must not run")

            with self.assertRaises(AssertionError):
                sample(self)
        finally:
            h.probe_hardlinks = original_probe
            h.is_android = original_android
            os.environ.pop(h.OPT_OUT_ENV, None)
            if old_opt_out is not None:
                os.environ[h.OPT_OUT_ENV] = old_opt_out

if __name__ == "__main__":
    unittest.main()
