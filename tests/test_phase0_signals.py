"""Phase 0 report accuracy: identical, partially overlapping and completely different changes to the same file; signals
from BOTH trees; deterministic SHA-256 fingerprints; commit and author attribution; and agreement between the Markdown
and JSON reports. Repositories are generated in temporary directories."""
import hashlib
import inspect
import json
import re
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase0_helpers as h  # noqa: E402

mod = h.load_script()
DEFCONFIG = "arch/arm64/configs/gki_defconfig"
MK = "VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 100\nEXTRAVERSION =\nKBUILD_X = 0\n"


class SameFileSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        base = h.commit(ack, {
            "Makefile": MK, DEFCONFIG: "CONFIG_DIFF=0\nCONFIG_KEEP=y\n",
            "android/abi_gki_aarch64": "[abi_symbol_list]\n  kfree\n",
            "android/abi_gki_aarch64_mtk": "[abi_symbol_list]\n  kmalloc\n",
            "mm/partial.c": "l1\nl2\nl3\nl4\nl5\n", "mm/ws.c": "int x;\n",
            "drivers/misc/mediatek/different.c": "x\ny\n", "init/export.c": "int a;\n",
            "drivers/gone.c": "g\n", "mm/lines.c": "a\n",
        }, "base", author="Google")
        gk = h.clone(ack, t / "gkid", "GKID-6.1", base)
        h.commit(gk, {
            "Makefile": MK.replace("EXTRAVERSION =", "EXTRAVERSION = -gkid"),
            DEFCONFIG: "CONFIG_DIFF=1\nCONFIG_KEEP=y\nCONFIG_ONLY_G=y\n",
            "android/abi_gki_aarch64": "[abi_symbol_list]\n  kfree\n  sym_a\n",
            "drivers/misc/mediatek/identical.c": "same\ncontent\n",
            "mm/partial.c": "G1\nG2\nl3\nl4\nl5\n", "mm/ws.c": "int x;\n  int y;\n",
            "drivers/misc/mediatek/different.c": "x\ny\nGKID\n",
            "init/export.c": "int a;\nint g;\n", "drivers/gone.c": None, "README.md": "gkid\n",
        }, "gkid changes", author="Ahmed")
        lm = h.clone(ack, t / "lum", "android14-6.1", base)
        h.commit(lm, {
            "Makefile": MK.replace("KBUILD_X = 0", "KBUILD_X = 1"),
            DEFCONFIG: "CONFIG_DIFF=2\nCONFIG_KEEP=y\nCONFIG_ONLY_L=y\n",
            "android/abi_gki_aarch64_mtk": "[abi_symbol_list]\n  kmalloc\n  sym_a\n",
            "drivers/misc/mediatek/identical.c": "same\ncontent\n",
            "mm/partial.c": "G1\nl2\nl3\nl4\nL5\n", "mm/ws.c": "int x;\nint y;\n",
            "drivers/misc/mediatek/different.c": "x\ny\nLUM\n",
            "init/export.c": "int a;\nEXPORT_SYMBOL_GPL(a);\n", "drivers/gone.c": "g2\n",
            "drivers/misc/mediatek/lum_only.c": "l\n",
        }, "lum changes", author="Chain")
        hc = t / "hc"
        h.write(hc, "kernel-patches/common/0001-partial.patch",
                "diff --git a/mm/partial.c b/mm/partial.c\n--- a/mm/partial.c\n+++ b/mm/partial.c\n@@ -1 +1 @@\n-l1\n+G1\n")
        cls.proc, cls.result = h.run_cli(t, hc, ack, "android14-6.1-lts", gk, "GKID-6.1", lm, "android14-6.1",
                                         extra=["--rows", "1000"])
        if cls.proc.returncode == 0:
            cls.a, cls.md = cls.result
            cls.f = {e["path"]: e for e in cls.a["files"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    # ---- identical / partial / different
    def test_identical_change(self):
        e = self.f["drivers/misc/mediatek/identical.c"]
        self.assertEqual((e["provenance"], e["similarity"], e["bucket"], e["confidence"]),
                         ("both (same change)", 1.0, 1, "in-booting-tree"))

    def test_partially_overlapping_change_reports_exact_line_counts(self):
        e = self.f["mm/partial.c"]
        self.assertEqual(e["provenance"], "both (different changes)")
        self.assertEqual(e["overlap"], {"shared_lines": 2, "gkid_only_lines": 2, "luminaire_only_lines": 2})
        self.assertEqual(e["similarity"], 0.333)
        self.assertEqual(e["bucket"], 2)  # performance: general, not hidden because the trees differ

    def test_completely_different_change_to_a_hardware_file_is_a_review_item_showing_both_trees(self):
        e = self.f["drivers/misc/mediatek/different.c"]
        self.assertEqual((e["provenance"], e["similarity"], e["bucket"], e["confidence"]),
                         ("both (different changes)", 0.0, 1, "review"))
        self.assertEqual(e["overlap"]["shared_lines"], 0)
        self.assertEqual(e["gkid"]["sample"], ["GKID"])
        self.assertEqual(e["luminaire"]["sample"], ["LUM"])

    def test_one_tree_only_files(self):
        self.assertEqual(self.f["README.md"]["provenance"], "GKID-only")
        self.assertEqual(self.f["README.md"]["bucket"], 3)
        self.assertEqual(self.f["drivers/misc/mediatek/lum_only.c"]["provenance"], "Luminaire-only")
        self.assertEqual(self.f["drivers/misc/mediatek/lum_only.c"]["bucket"], 1)

    def test_comparison_ignores_indentation_only_differences(self):
        self.assertEqual(self.f["mm/ws.c"]["provenance"], "both (same change)")

    # ---- signals from both trees
    def test_a_signal_seen_only_in_the_luminaire_change_is_not_ignored(self):
        e = self.f["init/export.c"]
        self.assertEqual(e["category_by_tree"], {"gkid": "OTHER", "luminaire": "KMI_ABI"})
        self.assertEqual(e["category"], "KMI_ABI")
        self.assertTrue(any("classify differently" in n for n in e["notes"]))
        self.assertEqual((e["bucket"], e["confidence"]), (1, "review"))

    def test_makefile_bookkeeping_in_one_tree_does_not_hide_a_real_change_in_the_other(self):
        e = self.f["Makefile"]
        self.assertEqual(e["category_by_tree"], {"gkid": "META", "luminaire": "OTHER"})
        self.assertEqual(e["category"], "OTHER")

    def test_deleted_by_one_tree_and_modified_by_the_other_is_noted(self):
        e = self.f["drivers/gone.c"]
        self.assertEqual((e["gkid"]["status"], e["luminaire"]["status"]), ("D", "M"))
        self.assertTrue(any("status differs" in n for n in e["notes"]))

    def test_both_different_files_have_their_own_markdown_section(self):
        sec = self.md.split("## Files both trees change differently")[1].split("## Bucket 2")[0]
        for path in ("drivers/misc/mediatek/different.c", "mm/partial.c", "init/export.c", "Makefile"):
            self.assertIn(f"`{path}`", sec)
        self.assertNotIn("identical.c", sec)

    # ---- defconfig and ABI
    def test_defconfig_rows_cover_every_kind_of_change(self):
        rows = {r["option"]: r for r in self.a["defconfig"]}
        self.assertEqual(rows["CONFIG_ONLY_G"]["change"], "GKID-only")
        self.assertEqual(rows["CONFIG_ONLY_G"]["luminaire"], "(absent)")
        self.assertEqual(rows["CONFIG_ONLY_L"]["change"], "Luminaire-only")
        self.assertEqual((rows["CONFIG_DIFF"]["gkid"], rows["CONFIG_DIFF"]["luminaire"]), ("1", "2"))
        self.assertEqual(rows["CONFIG_DIFF"]["change"], "both (different values)")
        self.assertNotIn("CONFIG_KEEP", rows)

    def test_abi_symbols_are_compared_per_symbol_list_file(self):
        syms = self.a["abi_symbols"]
        self.assertEqual(syms["both"], [])
        self.assertEqual(syms["gkid_only"], ["android/abi_gki_aarch64::sym_a"])
        self.assertEqual(syms["luminaire_only"], ["android/abi_gki_aarch64_mtk::sym_a"])

    # ---- markdown and json agree
    def test_render_takes_only_the_analysis_and_a_row_cap(self):
        self.assertEqual(list(inspect.signature(mod.render).parameters), ["a", "cap"])

    def test_summary_numbers_match_between_markdown_and_json(self):
        s = self.a["summary"]
        for label, key in (("1. Essential Duchamp hardware / boot adaptation candidates (in the tree reported to boot)",
                            "bucket1_in_booting_tree"),
                           ("1b. Hardware / boot files both trees change differently (review)", "bucket1_review"),
                           ("2. General performance / functionality", "bucket2_general"),
                           ("3. GKID-specific, not demonstrably required", "bucket3_gkid_specific"),
                           ("4. HyperCore patches reviewed", "patches_reviewed")):
            self.assertIn(f"| {label} | {s[key]} |", self.md, label)
        counted = {i: sum(1 for e in self.a["files"] if e["bucket"] == i) for i in (1, 2, 3, 5)}
        self.assertEqual(counted[1], s["bucket1_in_booting_tree"] + s["bucket1_review"])
        self.assertEqual((counted[2], counted[3], counted[5]), (s["bucket2_general"], s["bucket3_gkid_specific"], s["neutral"]))

    def test_every_file_in_the_json_appears_in_the_markdown(self):
        for p in self.f:
            self.assertIn(f"`{p}`", self.md, p)

    def test_patch_rows_and_defconfig_rows_match(self):
        for p in self.a["patches"]:
            self.assertRegex(self.md, rf"\| {re.escape(p['group'])} \| {re.escape(p['patch'])} \| {p['status']['ack_tip']}")
        md_rows = re.findall(r"^\| (CONFIG_\w+) \|", self.md, re.M)
        self.assertEqual(sorted(md_rows), sorted(r["option"] for r in self.a["defconfig"]))

    def test_the_report_states_the_architecture_and_the_roles_of_the_trees(self):
        self.assertIn("read-only references", self.md)
        self.assertIn("Neither is a kernel source, a foundation or a fallback", self.md)
        self.assertEqual(self.a["trees"]["gkid"]["role"], "read-only reference")
        self.assertEqual(self.a["trees"]["luminaire"]["role"], "read-only reference")
        self.assertIn("neither is a kernel source", self.a["architecture"]["references"])
        self.assertEqual(self.a["schema"], 2)

    def test_samples_can_be_left_out(self):
        t = self.tmp
        proc, result = h.run_cli(t, t / "hc", t / "ack", "android14-6.1-lts", t / "gkid", "GKID-6.1", t / "lum",
                                 "android14-6.1", extra=["--no-samples", "--skip-patches"], out_name="ns",
                                 workdir=t / "w-ns")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('"sample"', json.dumps(result[0]))


class Determinism(unittest.TestCase):
    def test_line_fingerprint_is_sha256_of_the_stripped_line(self):
        self.assertEqual(mod.line_key("+", "x"), b"+" + hashlib.sha256(b"x").digest())
        self.assertEqual(mod.line_key("-", "x"), b"-" + hashlib.sha256(b"x").digest())
        self.assertNotEqual(mod.line_key("+", "x"), mod.line_key("-", "x"))

    def test_file_fingerprint_is_stable_and_order_independent(self):
        a, b, c = mod.FileDelta("f"), mod.FileDelta("f"), mod.FileDelta("f")
        for text in ("one", "two", "three"):
            a.sig[mod.line_key("+", text)] += 1
        for text in ("three", "one", "two"):
            b.sig[mod.line_key("+", text)] += 1
        c.sig[mod.line_key("+", "one")] += 1
        self.assertEqual(a.fingerprint(), b.fingerprint())
        self.assertNotEqual(a.fingerprint(), c.fingerprint())
        expected = hashlib.sha256()
        for key in sorted(a.sig):
            expected.update(key)
            expected.update((1).to_bytes(4, "big"))
        self.assertEqual(a.fingerprint(), expected.hexdigest())

    def test_two_runs_with_different_hash_seeds_give_identical_json_and_markdown(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        base = h.commit(ack, {"Makefile": h.makefile(100), "mm/a.c": "1\n2\n3\n"}, "base")
        gk = h.clone(ack, t / "gkid", "GKID-6.1", base)
        h.commit(gk, {"mm/a.c": "1\nG\n3\n", "drivers/misc/mediatek/x.c": "x\n"}, "g", author="Ahmed")
        lm = h.clone(ack, t / "lum", "android14-6.1", base)
        h.commit(lm, {"mm/a.c": "1\nL\n3\n", "drivers/misc/mediatek/x.c": "x\n"}, "l", author="Chain")
        hc = t / "hc"
        h.write(hc, "kernel-patches/common/0001-a.patch",
                "diff --git a/mm/a.c b/mm/a.c\n--- a/mm/a.c\n+++ b/mm/a.c\n@@ -2 +2 @@\n-2\n+G\n")
        outs = []
        for i, seed in enumerate(("0", "1", "424242")):
            proc, result = h.run_cli(t, hc, ack, "android14-6.1-lts", gk, "GKID-6.1", lm, "android14-6.1",
                                     env={"PYTHONHASHSEED": seed}, out_name=f"d{i}", workdir=t / f"w{i}")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outs.append(((t / f"d{i}.json").read_text(), (t / f"d{i}.md").read_text()))
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], outs[2])
        # a re-run on the same scratch directory without fetching is identical too
        proc, _ = h.run_cli(t, hc, ack, "android14-6.1-lts", gk, "GKID-6.1", lm, "android14-6.1",
                            extra=["--no-fetch"], out_name="again", workdir=t / "w0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((t / "again.json").read_text(), outs[0][0])


class CommitsAndAuthors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        base = h.commit(ack, {"Makefile": h.makefile(100), "f.c": "0\n"}, "base")
        gk = h.clone(ack, t / "gkid", "GKID-6.1", base)
        for i in range(3):
            h.commit(gk, {"f.c": f"a{i}\n"}, f"change {i}", author="Alice")
        h.commit(gk, {"f.c": "b\n"}, "UPSTREAM: something from mainline", author="Bob")
        h.commit(gk, {"f.c": "c\n"}, "other", author="Carol")
        lm = h.clone(ack, t / "lum", "android14-6.1", base)
        h.commit(lm, {"g.c": "1\n"}, "lum", author="Dana")
        cls.proc, cls.result = h.run_cli(t, t / "hc", ack, "android14-6.1-lts", gk, "GKID-6.1", lm, "android14-6.1",
                                         extra=["--skip-patches", "--max-commits", "2"])
        if cls.proc.returncode == 0:
            cls.a, cls.md = cls.result

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_authors_are_counted_over_all_commits_even_when_the_list_is_truncated(self):
        c = self.a["trees"]["gkid"]["commits"]
        self.assertEqual((c["total"], c["listed_count"], c["truncated"]), (5, 2, True))
        self.assertEqual(c["authors"], [{"name": "Alice", "commits": 3}, {"name": "Bob", "commits": 1},
                                        {"name": "Carol", "commits": 1}])
        self.assertTrue(any("only the newest 2 of 5" in w for w in self.a["warnings"]))

    def test_ack_style_commits_are_flagged(self):
        c = self.a["trees"]["gkid"]["commits"]
        self.assertEqual(c["ack_style_commits"], 1)
        self.assertTrue(any("look like ACK commits" in w for w in self.a["warnings"]))

    def test_markdown_authors_match_json(self):
        self.assertIn("Authors: Alice (3), Bob (1), Carol (1)", self.md)
        self.assertIn("5 non-merge commit(s) above the base; 1 of them look like ACK-style commits.", self.md)
        self.assertIn("_Showing 2 of 5; the JSON lists 2._", self.md)  # a truncated list is never silent
        self.assertNotIn("Showing", self.md.split("### Luminaire")[1])  # Luminaire's single commit is complete


class LuminaireIdenticalToAck(unittest.TestCase):
    def test_no_delta_is_reported_with_its_caveat(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        base = h.commit(ack, {"Makefile": h.makefile(100), "f.c": "0\n"}, "base")
        gk = h.clone(ack, t / "gkid", "GKID-6.1", base)
        h.commit(gk, {"drivers/misc/mediatek/x.c": "x\n"}, "g")
        lm = h.clone(ack, t / "lum", "android14-6.1", base)
        proc, result = h.run_cli(t, t / "hc", ack, "android14-6.1-lts", gk, "GKID-6.1", lm, "android14-6.1",
                                 extra=["--skip-patches"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        a, md = result
        self.assertEqual(a["summary"]["bucket1_in_booting_tree"], 0)
        self.assertEqual(a["summary"]["bucket3_candidate_to_drop"], 1)
        self.assertIn("changes no file and no gki_defconfig option over its ACK base", md)
        self.assertIn("Luminaire's own builds may add configs, patches or packaging", md)
        self.assertIn("Phase 0 build test is there to confirm", md)


if __name__ == "__main__":
    unittest.main()
