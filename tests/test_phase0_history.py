"""Phase 0 base detection: shared history, different bases, merges, squashed or unrelated history, and ACK
histories whose Linux versions do NOT increase monotonically. All repositories are generated in temporary directories."""
import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase0_helpers as h  # noqa: E402

DEFCONFIG = "arch/arm64/configs/gki_defconfig"
ABI = "android/abi_gki_aarch64"


def abi(*syms):
    return "[abi_symbol_list]\n" + "".join(f"  {s}\n" for s in syms)


class SharedHistory(unittest.TestCase):
    """GKID merged ACK at 6.1.101, Luminaire forked from the ACK tip at 6.1.102: two different, exact bases."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        cls.a1 = h.commit(ack, {"Makefile": h.makefile(100), DEFCONFIG: "CONFIG_FOO=1\nCONFIG_HZ_250=y\n",
                                ABI: abi("kfree"), "lib/string.c": "int slow;\n", "mm/vmscan.c": "int lru = 0;\n"},
                          "base 6.1.100", author="Google")
        cls.a2 = h.commit(ack, {"Makefile": h.makefile(101), ABI: abi("kfree", "ack_sym_101"),
                                "lib/string.c": "int fast;\n"}, "ANDROID: 6.1.101", author="Google")
        cls.a3 = h.commit(ack, {"Makefile": h.makefile(102), DEFCONFIG: "CONFIG_FOO=2\nCONFIG_HZ_250=y\nCONFIG_ACK_102=y\n",
                                ABI: abi("kfree", "ack_sym_101", "ack_sym_102")}, "ANDROID: 6.1.102", author="Google")
        h.git(ack, "branch", "android14-6.1")

        gk = h.clone(ack, t / "gkid", "GKID-6.1", cls.a1)
        h.git(gk, "fetch", "-q", "origin", "android14-6.1-lts")
        h.git(gk, "merge", "-q", "--no-ff", "-m", "merge ack 6.1.101", cls.a2,
              GIT_AUTHOR_DATE="2024-02-01T00:00:00 +0000", GIT_COMMITTER_DATE="2024-02-01T00:00:00 +0000")
        h.commit(gk, {"arch/arm64/boot/dts/mediatek/mt6897-duchamp.dts": "/dts-v1/;\n",
                      DEFCONFIG: "CONFIG_FOO=3\nCONFIG_HZ_250=y\nCONFIG_MTK_DUCHAMP=y\n",
                      ABI: abi("kfree", "ack_sym_101", "gkid_sym", "shared_sym"), "mm/vmscan.c": "int lru = 1;\n"},
                 "duchamp bits", author="Ahmed")
        h.commit(gk, {"README.md": "gkid\n"}, "readme", author="Dev2")

        lm = h.clone(ack, t / "lum", "android14-6.1", cls.a3)
        h.commit(lm, {"arch/arm64/boot/dts/mediatek/mt6897-duchamp.dts": "/dts-v1/;\n",
                      DEFCONFIG: "CONFIG_FOO=3\nCONFIG_HZ_250=y\nCONFIG_ACK_102=y\nCONFIG_MTK_DUCHAMP=y\n",
                      ABI: abi("kfree", "ack_sym_101", "ack_sym_102", "lum_sym", "shared_sym")},
                 "duchamp support", author="Chain")
        hc = t / "hc"
        hc.mkdir()
        cls.proc, cls.result = h.run_cli(t, hc, ack, "android14-6.1-lts", gk, "GKID-6.1", lm, "android14-6.1",
                                         extra=["--skip-patches", "--rows", "1000"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.a, self.md = self.result

    def test_each_tree_gets_its_own_exact_base(self):
        g, l = self.a["trees"]["gkid"], self.a["trees"]["luminaire"]
        self.assertEqual((g["base"], l["base"]), (self.a2, self.a3))
        for t in (g, l):
            self.assertEqual((t["base_confidence"], t["base_method"]), ("exact", "merge-base (shared history)"))
        self.assertEqual((g["base_version"], l["base_version"]), ("6.1.101", "6.1.102"))

    def test_different_bases_raise_a_warning(self):
        self.assertTrue(any("different ACK bases" in w for w in self.a["warnings"]))
        self.assertIn("different ACK bases", self.md)

    def test_acks_own_changes_are_not_counted_as_tree_changes(self):
        paths = {f["path"] for f in self.a["files"]}
        self.assertNotIn("lib/string.c", paths)  # changed by ACK a2, merged into GKID, in both bases
        syms = self.a["abi_symbols"]
        everything = syms["both"] + syms["gkid_only"] + syms["luminaire_only"]
        self.assertFalse([s for s in everything if "ack_sym" in s])
        self.assertNotIn("CONFIG_ACK_102", {r["option"] for r in self.a["defconfig"]})

    def test_abi_symbols_are_split_by_tree_with_file_names(self):
        syms = self.a["abi_symbols"]
        self.assertEqual(syms["both"], [f"{ABI}::shared_sym"])
        self.assertEqual(syms["gkid_only"], [f"{ABI}::gkid_sym"])
        self.assertEqual(syms["luminaire_only"], [f"{ABI}::lum_sym"])

    def test_defconfig_shows_the_ack_value_at_each_trees_own_base(self):
        row = {r["option"]: r for r in self.a["defconfig"]}["CONFIG_FOO"]
        self.assertEqual((row["ack_at_gkid_base"], row["gkid"]), ("1", "3"))
        self.assertEqual((row["ack_at_luminaire_base"], row["luminaire"]), ("2", "3"))
        self.assertEqual(row["change"], "both (same value)")
        self.assertIn("| CONFIG_FOO | 1 | 3 | 2 | 3 | both (same value) |", self.md)

    def test_commits_above_the_base_exclude_ack_and_merges(self):
        g = self.a["trees"]["gkid"]["commits"]
        self.assertEqual(g["total"], 2)
        self.assertEqual({c["subject"] for c in g["listed"]}, {"duchamp bits", "readme"})
        self.assertEqual({a["name"]: a["commits"] for a in g["authors"]}, {"Ahmed": 1, "Dev2": 1})
        l = self.a["trees"]["luminaire"]["commits"]
        self.assertEqual((l["total"], l["authors"]), (1, [{"name": "Chain", "commits": 1}]))

    def test_authors_in_markdown_match_json(self):
        self.assertIn("Authors: Ahmed (1), Dev2 (1)", self.md)
        self.assertIn("Authors: Chain (1)", self.md)


class CrissCrossMerges(unittest.TestCase):
    """A tree that merged ACK twice at different points: the newest ACK commit contained in it is the base."""

    def test_base_is_the_newest_contained_ack_commit(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        a1 = h.commit(ack, {"Makefile": h.makefile(100), "f.c": "1\n"}, "a1")
        a2 = h.commit(ack, {"Makefile": h.makefile(101)}, "a2")
        a3 = h.commit(ack, {"Makefile": h.makefile(102)}, "a3")
        tree = h.clone(ack, t / "tree", "T", a1)
        h.commit(tree, {"own.c": "x\n"}, "own 1")
        h.merge(tree, a2, "merge a2")
        h.commit(tree, {"own.c": "y\n"}, "own 2")
        h.merge(tree, a3, "merge a3")
        h.commit(tree, {"own.c": "z\n"}, "own 3")
        mod, repo = h.scratch(t)
        h.fetch(mod, repo, "ack", ack, ["android14-6.1-lts"])
        h.fetch(mod, repo, "tree", tree, ["T"])
        base = mod.find_base(repo, "refs/remotes/tree/T", ["refs/remotes/ack/android14-6.1-lts"], {}, 32, 5000)
        self.assertEqual((base["base"], base["confidence"], base["ahead"]), (a3, "exact", 5))

    def test_the_ack_branch_closest_to_the_tree_wins(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        a1 = h.commit(ack, {"Makefile": h.makefile(100)}, "a1")
        h.git(ack, "branch", "android14-6.1")
        h.commit(ack, {"Makefile": h.makefile(101)}, "a2 only in lts")
        h.git(ack, "checkout", "-q", "android14-6.1")
        r1 = h.commit(ack, {"Makefile": h.makefile(100), "rel.c": "r\n"}, "release only")
        tree = h.clone(ack, t / "tree", "T", r1)
        h.commit(tree, {"own.c": "x\n"}, "own")
        mod, repo = h.scratch(t)
        h.fetch(mod, repo, "ack", ack, ["android14-6.1-lts", "android14-6.1"])
        h.fetch(mod, repo, "tree", tree, ["T"])
        refs = ["refs/remotes/ack/android14-6.1-lts", "refs/remotes/ack/android14-6.1"]
        base = mod.find_base(repo, "refs/remotes/tree/T", refs, {}, 32, 5000)
        self.assertEqual((base["base"], base["ack_ref"]), (r1, "refs/remotes/ack/android14-6.1"))
        # relationship between the two ACK branches is measured, not assumed
        self.assertEqual(mod.rev(repo, refs[1]), r1)
        self.assertNotEqual(base["base"], a1)


class SquashedHistory(unittest.TestCase):
    """A tree with no shared history: the base is an approximation and says so."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        cls.c1 = h.commit(ack, {"Makefile": h.makefile(100), "a.c": "1\n", "b.c": "1\n", "c.c": "1\n"}, "c1")
        cls.c2 = h.commit(ack, {"a.c": "2\n"}, "c2 same version")
        cls.c3 = h.commit(ack, {"b.c": "2\n"}, "c3 same version")
        cls.c4 = h.commit(ack, {"c.c": "2\n"}, "c4 same version")
        cls.c5 = h.commit(ack, {"Makefile": h.makefile(101)}, "c5 next version")
        cls.c6 = h.commit(ack, {"Makefile": h.makefile(105)}, "c6 jump")
        # squashed GKID-like import of c3's tree, plus its own change
        cls.sq, _ = h.orphan_snapshot(ack, t / "sq", "SQ", cls.c3, {"drivers/misc/mediatek/x.c": "x\n"})
        # a squashed tree whose version (6.1.103) never appears in ACK: the nearest lower version is used
        cls.sq2, _ = h.orphan_snapshot(ack, t / "sq2", "SQ2", cls.c5,
                                       {"Makefile": h.makefile(103), "drivers/misc/mediatek/y.c": "y\n"})
        cls.mod, cls.repo = h.scratch(t)
        h.fetch(cls.mod, cls.repo, "ack", ack, ["android14-6.1-lts"])
        h.fetch(cls.mod, cls.repo, "sq", cls.sq, ["SQ"])
        h.fetch(cls.mod, cls.repo, "sq2", cls.sq2, ["SQ2"])
        cls.refs = ["refs/remotes/ack/android14-6.1-lts"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_picks_the_candidate_whose_tree_differs_least(self):
        base = self.mod.find_base(self.repo, "refs/remotes/sq/SQ", self.refs, {}, 32, 5000)
        self.assertEqual(base["base"], self.c3)
        self.assertEqual(base["confidence"], "approximate")
        d = base["detail"]
        self.assertEqual((d["candidates"], d["evaluated"], d["paths_differing"]), (4, 4, 1))
        self.assertTrue(d["reported_version_exact"])
        self.assertIsNone(base["ahead"])

    def test_version_missing_from_ack_falls_back_to_the_nearest_lower(self):
        base = self.mod.find_base(self.repo, "refs/remotes/sq2/SQ2", self.refs, {}, 32, 5000)
        self.assertEqual(base["base"], self.c5)
        self.assertFalse(base["detail"]["reported_version_exact"])

    def test_sampling_still_finds_the_best_among_many_candidates(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        h.commit(ack, {"Makefile": h.makefile(100), "f0.c": "0\n"}, "start")
        shas = [h.commit(ack, {f"f{i}.c": "x\n"}, f"c{i}") for i in range(1, 61)]  # all report 6.1.100
        h.commit(ack, {"Makefile": h.makefile(101)}, "next")
        target = shas[36]
        sq, _ = h.orphan_snapshot(ack, t / "sq", "SQ", target, {"drivers/own.c": "own\n"})
        mod, repo = h.scratch(t)
        h.fetch(mod, repo, "ack", ack, ["android14-6.1-lts"])
        h.fetch(mod, repo, "sq", sq, ["SQ"])
        base = mod.find_base(repo, "refs/remotes/sq/SQ", ["refs/remotes/ack/android14-6.1-lts"], {}, 8, 5000)
        self.assertEqual(base["base"], target)
        self.assertLess(base["detail"]["evaluated"], base["detail"]["candidates"])  # it really sampled

    def test_squashed_tree_has_no_commit_list_and_says_why(self):
        t = self.tmp
        hc = t / "hc"
        hc.mkdir(exist_ok=True)
        ack = t / "ack"
        lum = h.clone(ack, t / "lum", "LUM", self.c3)
        h.commit(lum, {"drivers/misc/mediatek/l.c": "l\n"}, "lum change")
        proc, result = h.run_cli(t, hc, ack, "android14-6.1-lts", self.sq, "SQ", lum, "LUM",
                                 extra=["--skip-patches", "--audit-ack-history"], out_name="sqr",
                                 workdir=t / "work-sq")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        a, md = result
        g = a["trees"]["gkid"]
        self.assertEqual(g["base_confidence"], "approximate")
        self.assertFalse(g["commits"]["available"])
        self.assertIn("Not available: no shared history", md)
        self.assertTrue(any("APPROXIMATE" in w for w in a["warnings"]))
        self.assertIn("GKID approximation", md)
        self.assertEqual(a["trees"]["luminaire"]["base_confidence"], "exact")
        self.assertIn("### ACK history audit", md)


class ApproximateBaseWording(unittest.TestCase):
    """The approximate base is the best of the candidates that were COMPARED. When only a sample was compared the report
    must say so and must not imply the global best was found."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        h.commit(ack, {"Makefile": h.makefile(100), "f0.c": "0\n"}, "start")
        cls.shas = [h.commit(ack, {f"f{i}.c": "x\n"}, f"c{i}") for i in range(1, 61)]  # with "start": 61 candidates at 6.1.100
        h.commit(ack, {"Makefile": h.makefile(101)}, "next version")
        cls.target = cls.shas[16]  # c17
        cls.sq, _ = h.orphan_snapshot(ack, t / "sq", "SQ", cls.target, {"drivers/misc/mediatek/own.c": "own\n"})
        lm = h.clone(ack, t / "lum", "android14-6.1", cls.shas[0])
        h.commit(lm, {"drivers/misc/mediatek/l.c": "l\n"}, "lum")
        cls.ack, cls.lm = ack, lm
        cls.hc = t / "hc"
        cls.hc.mkdir()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_with(self, samples, name):
        proc, result = h.run_cli(self.tmp, self.hc, self.ack, "android14-6.1-lts", self.sq, "SQ", self.lm,
                                 "android14-6.1", extra=["--skip-patches", "--candidate-samples", str(samples)],
                                 out_name=name, workdir=self.tmp / f"w-{name}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return result

    def test_a_sampled_search_says_so_and_does_not_promise_the_best(self):
        a, md = self.run_with(4, "sampled")
        d = a["trees"]["gkid"]["base_detail"]
        self.assertFalse(d["exhaustive"])
        self.assertEqual((d["candidates"], d["evaluated"] < d["candidates"]), (61, True))
        self.assertIn(f"**{d['evaluated']} of them were compared**", md)
        self.assertIn("an evenly spaced sample, then a finer pass around the best sample", md)
        self.assertIn("best of those compared", md)
        self.assertIn("**It is not guaranteed to be the best of all 61.**", md)
        self.assertNotIn("were compared**. The chosen", md)  # no "all ... were compared" claim
        self.assertIn("best of the compared ACK commits", a["trees"]["gkid"]["base_method"])
        self.assertTrue(any(f"only {d['evaluated']} of 61 candidates were compared, so the best candidate may have "
                            "been missed" in w for w in a["warnings"]))
        self.assertIn("--candidate-samples", md)

    def test_the_limitation_is_real_a_small_sample_can_miss_the_true_base(self):
        a, _ = self.run_with(4, "missed")
        self.assertNotEqual(a["trees"]["gkid"]["base"], self.target)  # documented behaviour, not a goal
        self.assertGreater(a["trees"]["gkid"]["base_detail"]["paths_differing"], 1)

    def test_comparing_all_candidates_is_reported_as_exhaustive_and_finds_the_base(self):
        a, md = self.run_with(64, "exhaustive")
        d = a["trees"]["gkid"]["base_detail"]
        self.assertTrue(d["exhaustive"])
        self.assertEqual((d["candidates"], d["evaluated"]), (61, 61))
        self.assertEqual(a["trees"]["gkid"]["base"], self.target)
        self.assertIn("**all 61 were compared**", md)
        self.assertNotIn("not guaranteed to be the best", md)
        self.assertIn("all candidates were compared", " ".join(a["warnings"]))
        self.assertIn("may be a commit that is not on the first-parent history", md)

    def test_json_fields_describe_what_was_compared(self):
        a, _ = self.run_with(8, "fields")
        d = a["trees"]["gkid"]["base_detail"]
        for key in ("candidates", "evaluated", "exhaustive", "paths_differing", "runner_up", "tied_at_best",
                    "reported_version_exact"):
            self.assertIn(key, d)
        self.assertEqual(a["trees"]["gkid"]["base_confidence"], "approximate")


class CandidateSamplesValidation(unittest.TestCase):
    """--candidate-samples below 2 used to crash with ZeroDivisionError (the samples are spaced between the first and
    the last candidate). It is rejected up front with a clear message."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        h.commit(ack, {"Makefile": h.makefile(100), "f0.c": "0\n"}, "start")
        cls.shas = [h.commit(ack, {f"f{i}.c": "x\n"}, f"c{i}") for i in range(1, 9)]  # 9 candidates at 6.1.100
        h.commit(ack, {"Makefile": h.makefile(101)}, "next")
        cls.sq, _ = h.orphan_snapshot(ack, t / "sq", "SQ", cls.shas[4], {"drivers/own.c": "own\n"})
        lm = h.clone(ack, t / "lum", "android14-6.1", cls.shas[0])
        h.commit(lm, {"drivers/misc/mediatek/l.c": "l\n"}, "lum")
        cls.ack, cls.lm = ack, lm
        cls.hc = t / "hc"
        cls.hc.mkdir()
        cls.mod, cls.repo = h.scratch(t, "direct")
        h.fetch(cls.mod, cls.repo, "ack", ack, ["android14-6.1-lts"])
        h.fetch(cls.mod, cls.repo, "sq", cls.sq, ["SQ"])
        cls.ref = "refs/remotes/ack/android14-6.1-lts"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def cli(self, value, name, ack=None):
        workdir = self.tmp / f"w-{name}"
        proc, result = h.run_cli(self.tmp, self.hc, ack or self.ack, "android14-6.1-lts", self.sq, "SQ", self.lm,
                                 "android14-6.1", extra=["--skip-patches", "--candidate-samples", str(value)],
                                 out_name=name, workdir=workdir)
        return proc, result, workdir

    def test_values_below_two_are_rejected_with_a_clear_error(self):
        for value in (1, 0, -1, -32):
            proc, result, workdir = self.cli(value, f"bad{abs(value)}")
            self.assertEqual(proc.returncode, 2, f"{value}: {proc.stderr}")
            self.assertIn(f"--candidate-samples must be at least 2 (got {value})", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertNotIn("ZeroDivisionError", proc.stderr)
            self.assertNotIn("[delta]", proc.stderr, f"{value}: work had started")
            self.assertFalse(workdir.exists(), f"{value}: the scratch repository was created")
            self.assertFalse((self.tmp / f"bad{abs(value)}.md").exists())

    def test_the_check_comes_before_any_other_work(self):
        proc, _, workdir = self.cli(1, "early", ack=self.tmp / "no-such-repo")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--candidate-samples must be at least 2", proc.stderr)
        self.assertNotIn("cannot list branches", proc.stderr)  # a fetch would have failed differently
        self.assertFalse(workdir.exists())

    def test_two_is_the_smallest_accepted_value_and_works_on_many_candidates(self):
        proc, result, _ = self.cli(2, "two")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        d = result[0]["trees"]["gkid"]["base_detail"]
        self.assertEqual(d["candidates"], 9)
        self.assertFalse(d["exhaustive"])
        self.assertGreaterEqual(d["evaluated"], 2)

    def test_other_valid_values_and_the_default_still_work(self):
        for value in (3, 9, 32, 1000):
            proc, result, _ = self.cli(value, f"ok{value}")
            self.assertEqual(proc.returncode, 0, f"{value}: {proc.stderr}")
        d = result[0]["trees"]["gkid"]["base_detail"]
        self.assertTrue(d["exhaustive"])  # 1000 >= 9 candidates
        proc, result = h.run_cli(self.tmp, self.hc, self.ack, "android14-6.1-lts", self.sq, "SQ", self.lm,
                                 "android14-6.1", extra=["--skip-patches"], out_name="default",
                                 workdir=self.tmp / "w-default")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_non_integer_is_rejected_by_the_argument_parser(self):
        proc, _, _ = self.cli("many", "text")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid int value", proc.stderr)

    def test_the_function_itself_refuses_a_sample_below_two(self):
        cands = self.repo.out("rev-list", "--first-parent", "--reverse", self.ref).split()[:9]
        for value in (1, 0, -3):
            with self.assertRaises(self.mod.GitError) as cm:
                self.mod.pick_by_content(self.repo, cands, "refs/remotes/sq/SQ", value)
            self.assertIn("samples must be at least 2", str(cm.exception))
        with self.assertRaises(self.mod.GitError):  # also when a single candidate would not have divided by zero
            self.mod.pick_by_content(self.repo, cands[:1], "refs/remotes/sq/SQ", 1)
        with self.assertRaises(self.mod.GitError):  # and through find_base
            self.mod.find_base(self.repo, "refs/remotes/sq/SQ", [self.ref], {}, 1, 5000)

    def test_two_samples_compare_the_first_and_last_candidate_then_refine(self):
        cands = self.repo.out("rev-list", "--first-parent", "--reverse", self.ref).split()[1:10]
        self.assertEqual(len(cands), 9)
        pick = self.mod.pick_by_content(self.repo, cands, "refs/remotes/sq/SQ", 2)
        self.assertGreaterEqual(pick["evaluated"], 2)
        self.assertLess(pick["evaluated"], pick["candidates"] + 1)
        self.assertIn(pick["commit"], cands)


class NonMonotonicVersions(unittest.TestCase):
    """ACK first-parent history whose Linux version goes 100, 101, 102, 101 (rollback), 103."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        cls.c100 = h.commit(ack, {"Makefile": h.makefile(100), "f.c": "100\n"}, "100")
        cls.c101 = h.commit(ack, {"Makefile": h.makefile(101), "f.c": "101\n"}, "101")
        cls.x1 = h.commit(ack, {"g.c": "1\n"}, "work in 101 (1)")
        cls.x2 = h.commit(ack, {"g.c": "2\n"}, "work in 101 (2)")
        cls.c102 = h.commit(ack, {"Makefile": h.makefile(102), "f.c": "102\n"}, "102")
        cls.c101b = h.commit(ack, {"Makefile": h.makefile(101), "f.c": "101b\n"}, "rollback to 101")
        cls.y1 = h.commit(ack, {"g.c": "3\n"}, "work after rollback")
        cls.c103 = h.commit(ack, {"Makefile": h.makefile(103), "f.c": "103\n"}, "103")
        cls.ack = ack
        # squashed tree = the tree after the rollback, at version 6.1.101
        cls.sq, _ = h.orphan_snapshot(ack, t / "sq", "SQ", cls.y1, {"drivers/misc/mediatek/z.c": "z\n"})
        cls.mod, cls.repo = h.scratch(t)
        h.fetch(cls.mod, cls.repo, "ack", ack, ["android14-6.1-lts"])
        h.fetch(cls.mod, cls.repo, "sq", cls.sq, ["SQ"])
        cls.ref = "refs/remotes/ack/android14-6.1-lts"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_audit_detects_the_decrease_and_the_revisited_version(self):
        segs = self.mod.version_timeline(self.repo, self.ref, 5000)
        au = self.mod.audit_timeline("android14-6.1-lts", segs)
        self.assertFalse(au["monotonic"])
        self.assertEqual(au["violation_count"], 1)
        self.assertEqual(au["violations"][0]["from"], "6.1.102")
        self.assertEqual(au["violations"][0]["to"], "6.1.101")
        self.assertEqual(au["revisited_versions"], ["6.1.101"])
        self.assertEqual([self.mod.fmt_version(s.version) for s in segs],
                         ["6.1.100", "6.1.101", "6.1.102", "6.1.101", "6.1.103"])

    def test_every_stretch_reporting_the_version_is_a_candidate(self):
        base = self.mod.find_base(self.repo, "refs/remotes/sq/SQ", [self.ref], {}, 32, 5000)
        # stretch one: c101, x1, x2 (3 commits); stretch two: c101b, y1 (2 commits)
        self.assertEqual(base["detail"]["candidates"], 5)
        self.assertEqual(base["detail"]["version_segments"], 2)
        self.assertEqual(base["base"], self.y1)  # the right stretch is chosen by content, not by version order

    def test_squashed_from_the_earlier_stretch_is_not_pulled_to_the_later_one(self):
        """Regression for revision 1, which bisected on versions: for a tree squashed from the EARLIER 6.1.101
        stretch it chose the later stretch (y1), three paths away from the tree instead of one."""
        t = self.tmp
        sq, _ = h.orphan_snapshot(self.ack, t / "sq-early", "SQE", self.x2, {"drivers/misc/mediatek/z.c": "z\n"})
        h.fetch(self.mod, self.repo, "sqe", sq, ["SQE"])
        base = self.mod.find_base(self.repo, "refs/remotes/sqe/SQE", [self.ref], {}, 32, 5000)
        self.assertEqual(base["base"], self.x2)
        self.assertEqual(base["detail"]["paths_differing"], 1)
        self.assertEqual(h.git(self.repo.git_dir, "diff", "--name-only", self.y1, "refs/remotes/sqe/SQE",
                               GIT_DIR=str(self.repo.git_dir)).count("\n") + 1, 3)

    def test_a_non_monotonic_history_is_reported_as_a_warning(self):
        t = self.tmp
        hc = t / "hc"
        hc.mkdir(exist_ok=True)
        lum = h.clone(self.ack, t / "lum", "LUM", self.c103)
        h.commit(lum, {"drivers/misc/mediatek/l.c": "l\n"}, "lum")
        proc, result = h.run_cli(t, hc, self.ack, "android14-6.1-lts", self.sq, "SQ", lum, "LUM",
                                 extra=["--skip-patches", "--audit-ack-history"], out_name="nm", workdir=t / "w-nm")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        a, md = result
        self.assertFalse(a["ack_history_audit"][0]["monotonic"])
        self.assertTrue(any("NOT monotonic" in w for w in a["warnings"]))
        self.assertIn("**NO**", md)


class TimelineCompleteness(unittest.TestCase):
    """`git log --first-parent -- Makefile` must enumerate every first-parent version change, including merge commits
    that bring the change in from a second parent. Checked against reading the version at EVERY first-parent commit."""

    def test_timeline_equals_a_brute_force_scan(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "main")
        h.commit(ack, {"Makefile": h.makefile(100), "a.c": "1\n"}, "m1")
        h.commit(ack, {"a.c": "2\n"}, "m2 no version change")
        h.git(ack, "checkout", "-q", "-b", "side")
        h.commit(ack, {"Makefile": h.makefile(101), "s.c": "1\n"}, "side bumps version")
        h.git(ack, "checkout", "-q", "main")
        h.commit(ack, {"a.c": "3\n"}, "m3")
        h.merge(ack, "side", "merge: version change arrives from the second parent")
        h.git(ack, "checkout", "-q", "-b", "side2")
        h.commit(ack, {"s2.c": "1\n"}, "side2 without version change")
        h.git(ack, "checkout", "-q", "main")
        h.commit(ack, {"a.c": "4\n"}, "m4")
        h.merge(ack, "side2", "merge without version change")
        h.commit(ack, {"Makefile": h.makefile(102, " -rc1")}, "m5 bump with extraversion")
        h.commit(ack, {"Makefile": h.makefile(102, "")}, "m6 makefile edit, same version")
        mod, repo = h.scratch(t)
        h.fetch(mod, repo, "ack", ack, ["main"])
        ref = "refs/remotes/ack/main"
        segs = mod.version_timeline(repo, ref, 5000)
        brute, last = [], object()
        for sha in repo.out("rev-list", "--first-parent", "--reverse", ref).split():
            v = mod.makefile_version(repo, sha)
            if v != last:
                brute.append((sha, v))
                last = v
        self.assertEqual([(s.sha, s.version) for s in segs], brute)
        self.assertEqual(len(segs), 3)

    def test_unreadable_makefile_is_recorded_not_fatal(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "main")
        h.commit(ack, {"Makefile": h.makefile(100)}, "ok")
        h.commit(ack, {"Makefile": "# no version here\n"}, "broken")
        h.commit(ack, {"Makefile": h.makefile(101)}, "ok again")
        mod, repo = h.scratch(t)
        h.fetch(mod, repo, "ack", ack, ["main"])
        au = mod.audit_timeline("main", mod.version_timeline(repo, "refs/remotes/ack/main", 5000))
        self.assertEqual(au["unreadable_segments"], 1)
        self.assertTrue(au["monotonic"])

    def test_timeline_limit_is_enforced(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "main")
        for i in range(5):
            h.commit(ack, {"Makefile": h.makefile(100 + i)}, f"v{i}")
        mod, repo = h.scratch(t)
        h.fetch(mod, repo, "ack", ack, ["main"])
        with self.assertRaises(mod.GitError):
            mod.version_timeline(repo, "refs/remotes/ack/main", 3)


if __name__ == "__main__":
    unittest.main()
