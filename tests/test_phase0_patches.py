"""Phase 0 patch review: forward applicability, in-order dependencies, reverse-check as a HEURISTIC, missing target
files, genuine conflicts, renames, ACK drift between a tree's base and the ACK tip, and independence from the user's
git configuration. Repositories are generated in temporary directories."""
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase0_helpers as h  # noqa: E402


def make_patch(base_files, new_files, rename=None):
    """A unified diff produced by git itself from two file states."""
    t = Path(h.tmpdir())
    try:
        repo = h.init_repo(t / "m", "main")
        h.commit(repo, base_files, "base")
        if rename:
            h.git(repo, "mv", rename[0], rename[1])
            h.git(repo, "add", "-A")
            return h.git(repo, "diff", "--cached", "-M", "--no-color") + "\n"
        for rel, text in new_files.items():
            h.write(repo, rel, text)
        h.git(repo, "add", "-A")
        return h.git(repo, "diff", "--cached", "--no-color") + "\n"
    finally:
        shutil.rmtree(t, ignore_errors=True)


class PatchAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "main")
        cls.b1 = h.commit(ack, {"a.c": "alpha\nbeta\ngamma\n", "b.c": "one\ntwo\n", "old_name.c": "x\n",
                                "lib/string.c": "int slow;\n", "h.c": "int x = 1;\n", "w.c": "line\n"}, "base")
        cls.b2 = h.commit(ack, {"lib/string.c": "int fast;\n", "h.c": "int x = 2;\n"}, "tip: unrelated changes")
        hc = t / "hc"
        c = hc / "kernel-patches" / "common"

        def put(path, text):
            h.write(hc, path, text)

        put("kernel-patches/common/0001-forward.patch",
            make_patch({"a.c": "alpha\nbeta\ngamma\n"}, {"a.c": "alpha\nBETA\ngamma\n"}))
        put("kernel-patches/common/0002-depends-on-0001.patch",
            make_patch({"a.c": "alpha\nBETA\ngamma\n"}, {"a.c": "alpha\nBeta2\ngamma\n"}))
        put("kernel-patches/common/0003-genuine-conflict.patch",
            make_patch({"b.c": "ONE\ntwo\n"}, {"b.c": "ONE\n2\n"}))
        put("kernel-patches/common/0004-independent-after-conflict.patch",
            make_patch({"keep.c": "k\n"}, {"keep.c": "k\n", "n.c": "new\n"}))
        put("kernel-patches/common/0005-depends-on-failed-0003.patch",
            make_patch({"b.c": "ONE\n2\n"}, {"b.c": "ONE\ndeux\n"}))
        put("kernel-patches/common/0006-missing-target.patch",
            make_patch({"nofile.c": "q\n"}, {"nofile.c": "r\n"}))
        put("kernel-patches/common/0007-already-in-tip.patch",
            make_patch({"lib/string.c": "int slow;\n"}, {"lib/string.c": "int fast;\n"}))
        put("kernel-patches/common/0008-rename.patch",
            make_patch({"old_name.c": "x\n"}, {}, rename=("old_name.c", "new_name.c")))
        put("kernel-patches/common/0009-rename-of-missing-source.patch",
            make_patch({"ghost.c": "g\n"}, {}, rename=("ghost.c", "ghost2.c")))
        put("kernel-patches/common/0010-natural-order.patch",
            make_patch({"keep.c": "k\n"}, {"keep.c": "k\n", "n2.c": "new2\n"}))
        put("kernel-patches/common/0011-whitespace-error.patch",
            make_patch({"w.c": "line\n"}, {"w.c": "line\ntrailing   \n"}))
        put("kernel-patches/common/0012-heuristic-false-positive.patch",
            make_patch({"h.c": "int x = 1;\n"}, {"h.c": "int x = 2;\n"}))
        put("kernel-patches/droidspaces/001.GKI-6.12-or-above-fix.patch", "diff --git a/x b/x\n")
        put("kernel-patches/droidspaces/002.5.10_or_lower-fix.patch", "diff --git a/x b/x\n")
        put("kernel-patches/susfs/fs_namespace.patch", make_patch({"ns.c": "a\n"}, {"ns.c": "b\n"}))
        put("patches/0001-ksu.patch", make_patch({"core.c": "a\n"}, {"core.c": "b\n"}))
        cls.hc = hc
        cls.mod, cls.repo = h.scratch(t)
        h.fetch(cls.mod, cls.repo, "ack", ack, ["main"])
        refs = {"ack_tip": "refs/remotes/ack/main", "gkid_tip": "refs/remotes/ack/main",
                "luminaire_tip": "refs/remotes/ack/main", "ack_at_gkid_base": cls.b1,
                "ack_at_luminaire_base": cls.b1}
        cls.rows = cls.mod.analyse_patches(cls.repo, hc, refs)
        cls.by = {r["patch"]: r for r in cls.rows}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tip(self, name):
        return self.by[name]["status"]["ack_tip"]

    def base(self, name):
        return self.by[name]["status"]["ack_at_gkid_base"]

    def test_forward_applicability(self):
        self.assertEqual(self.tip("0001-forward.patch"), "applies")
        self.assertIn("candidate to carry", self.by["0001-forward.patch"]["recommendation"])

    def test_patches_are_judged_in_order_so_dependencies_are_satisfied(self):
        self.assertEqual(self.tip("0002-depends-on-0001.patch"), "applies")

    def test_genuine_conflict(self):
        r = self.by["0003-genuine-conflict.patch"]
        self.assertEqual(r["status"]["ack_tip"], "conflict")
        self.assertIn("does not apply", r["detail"]["ack_tip"])
        self.assertIn("Needs rework", r["recommendation"])

    def test_a_failed_patch_is_skipped_like_the_build_does(self):
        self.assertEqual(self.tip("0004-independent-after-conflict.patch"), "applies")
        self.assertEqual(self.tip("0005-depends-on-failed-0003.patch"), "conflict")

    def test_missing_target_file(self):
        r = self.by["0006-missing-target.patch"]
        self.assertEqual(r["status"]["ack_tip"], "target-missing")
        self.assertIn("nofile.c", r["detail"]["ack_tip"])

    def test_rename_needs_its_source_not_its_destination(self):
        self.assertEqual(self.tip("0008-rename.patch"), "applies")
        self.assertEqual(self.tip("0009-rename-of-missing-source.patch"), "target-missing")

    def test_reverse_check_is_labelled_a_heuristic_not_proof(self):
        r = self.by["0007-already-in-tip.patch"]
        self.assertEqual(r["status"]["ack_tip"], "reverse-applies")
        self.assertTrue(r["heuristic"])
        self.assertIn("heuristic", r["detail"]["ack_tip"])
        self.assertIn("Probably already in ACK", r["recommendation"])
        self.assertIn("Confirm", r["recommendation"])
        self.assertNotIn("drop it.", r["recommendation"])
        self.assertNotIn("already present", r["recommendation"].replace("Probably already", ""))

    def test_reverse_check_can_be_a_false_positive_and_is_reported_the_same_way(self):
        # ACK got `int x = 2;` from an unrelated change. The patch was never applied there, yet the reverse check
        # succeeds. This is why the status is a heuristic and the recommendation says to confirm.
        r = self.by["0012-heuristic-false-positive.patch"]
        self.assertEqual(r["status"]["ack_tip"], "reverse-applies")
        self.assertEqual(r["status"]["ack_at_gkid_base"], "applies")
        self.assertIn("Confirm", r["recommendation"])

    def test_result_that_depends_on_which_ack_commit_is_used_is_flagged(self):
        r = self.by["0007-already-in-tip.patch"]
        self.assertEqual((r["status"]["ack_tip"], r["status"]["ack_at_gkid_base"]), ("reverse-applies", "applies"))
        self.assertTrue(r["drift_sensitive"])
        self.assertIn("differs between the ACK tip and the trees' own bases", r["recommendation"])
        self.assertFalse(self.by["0001-forward.patch"]["drift_sensitive"])

    def test_other_line_and_builder_side_patches_are_not_applied(self):
        self.assertEqual(self.tip("001.GKI-6.12-or-above-fix.patch"), "other-line-by-name")
        self.assertEqual(self.tip("002.5.10_or_lower-fix.patch"), "other-line-by-name")
        self.assertEqual(self.tip("fs_namespace.patch"), "builder-side")
        self.assertEqual(self.tip("0001-ksu.patch"), "builder-side")

    def test_group_and_natural_order(self):
        names = [r["patch"] for r in self.rows if r["group"] == "kernel-patches/common"]
        self.assertEqual(names, sorted(names, key=lambda n: int(n.split("-")[0])))
        self.assertLess(names.index("0009-rename-of-missing-source.patch"), names.index("0010-natural-order.patch"))
        self.assertEqual([r["group"] for r in self.rows][-2:], ["kernel-patches/susfs", "patches"])

    def test_every_row_has_all_five_reference_statuses_and_a_patch_hash(self):
        for r in self.rows:
            self.assertEqual(set(r["status"]), {"ack_tip", "gkid_tip", "luminaire_tip", "ack_at_gkid_base",
                                                "ack_at_luminaire_base"})
            self.assertEqual(len(r["sha256"]), 64)

    def test_patch_target_parser(self):
        text = ("From: a\nSubject: [PATCH] x\n\n"
                "diff --git a/one.c b/one.c\n--- a/one.c\n+++ b/one.c\n@@ -1 +1 @@\n-a\n+b\n"
                "diff --git a/new.c b/new.c\nnew file mode 100644\n--- /dev/null\n+++ b/new.c\n@@ -0,0 +1 @@\n+n\n"
                "diff --git a/old.c b/moved.c\nsimilarity index 100%\nrename from old.c\nrename to moved.c\n")
        must, new = self.mod.patch_targets(text)
        self.assertEqual((must, new), (["one.c", "old.c"], ["new.c"]))

    def test_users_git_config_cannot_change_the_result(self):
        # apply.whitespace=error would reject the trailing-whitespace patch if the user's config were honoured
        cfg = self.tmp / "user-gitconfig"
        cfg.write_text("[apply]\n\twhitespace = error\n")
        old = os.environ.get("GIT_CONFIG_GLOBAL")
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
        try:
            refs = {"ack_tip": "refs/remotes/ack/main", "gkid_tip": "refs/remotes/ack/main",
                    "luminaire_tip": "refs/remotes/ack/main"}
            rows = {r["patch"]: r for r in self.mod.analyse_patches(self.repo, self.hc, refs)}
        finally:
            if old is None:
                os.environ.pop("GIT_CONFIG_GLOBAL")
            else:
                os.environ["GIT_CONFIG_GLOBAL"] = old
        self.assertEqual(rows["0011-whitespace-error.patch"]["status"]["ack_tip"], "applies")

    def test_missing_patch_directories_give_no_rows_instead_of_failing(self):
        empty = self.tmp / "empty-hc"
        empty.mkdir()
        self.assertEqual(self.mod.analyse_patches(self.repo, empty, {"ack_tip": "refs/remotes/ack/main"}), [])


if __name__ == "__main__":
    unittest.main()
