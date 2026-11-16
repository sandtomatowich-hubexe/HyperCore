"""How the Phase 0 patch check differs from the patch application the build really performs.

The build side runs the REAL `apply_patch_file`, `apply_kernel_patches` and `require_patch` from functions.sh and
functions-extra.sh (sourced in a bash subprocess, in a throw-away git repository), so the comparison cannot drift from
the build's code. The script side runs `run_series` on the same files. Every case is generated in a temporary
directory. The table in PHASE0.md and PATCH_METHOD in the script are checked against what these tests observe."""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase0_helpers as h  # noqa: E402

ROOT = h.ROOT
mod = h.load_script()
ANSI = re.compile(r"\x1b\[[0-9;]*m")

PRE = "c1\nc2\nc3\nOLD\nc4\nc5\nc6\n"            # a tree the patch below applies to exactly
POST = "c1\nc2\nc3\nNEW\nc4\nc5\nc6\n"           # the same tree with the patch applied
PATCH = ("diff --git a/f.c b/f.c\n--- a/f.c\n+++ b/f.c\n@@ -1,7 +1,7 @@\n c1\n c2\n c3\n-OLD\n+NEW\n c4\n c5\n c6\n")


def build_apply(files, patches, call="apply_patch_file", extra_source=False):
    """Run the real build function in a git repository holding `files`. Returns rc, output, skipped names and files."""
    t = Path(h.tmpdir())
    try:
        repo = h.init_repo(t / "tree", "main")
        h.commit(repo, files, "tree")
        for name, text in patches.items():
            h.write(t / "patches", name, text)
        work = t / "work"
        work.mkdir()
        sources = f"source {ROOT}/functions.sh; " + (f"source {ROOT}/functions-extra.sh; " if extra_source else "")
        cmd = f'{sources}{call.replace("PATCHES", str(t / "patches"))}; echo "rc=$?"'
        proc = subprocess.run(["bash", "-c", cmd], cwd=repo, capture_output=True, text=True,
                              env=h.git_env(WORKDIR=str(work), KERNEL_PATCHES=str(t / "patches")))
        out = ANSI.sub("", proc.stdout + proc.stderr)
        rc = int(re.search(r"rc=(\d+)", out).group(1)) if "rc=" in out else proc.returncode
        skipped = (work / "skipped-patches.txt").read_text().split() if (work / "skipped-patches.txt").exists() else []
        after = {p.name: p.read_text() for p in repo.iterdir() if p.is_file() and not p.name.startswith(".")}
        return {"rc": rc, "out": out, "skipped": skipped, "files": after}
    finally:
        shutil.rmtree(t, ignore_errors=True)


def build_outcome(files, patch_text):
    r = build_apply(files, {"p.patch": patch_text}, call="apply_patch_file PATCHES/p.patch")
    if r["rc"] != 0:
        outcome = "skipped"
    elif "Applied patch with fuzz" in r["out"]:
        outcome = "applied-with-fuzz"
    elif "Applied patch successfully" in r["out"]:
        outcome = "applied"
    elif "Patch already applied" in r["out"]:
        outcome = "already-applied"
    else:
        outcome = "unknown"
    return outcome, r


def script_statuses(files, patches):
    """Status of each (name, text) patch as the Phase 0 script judges them, in order, on a tree holding `files`."""
    t = Path(h.tmpdir())
    try:
        src = h.init_repo(t / "src", "main")
        sha = h.commit(src, files, "tree")
        scratch_mod, repo = h.scratch(t)
        h.fetch(scratch_mod, repo, "src", src, ["main"])
        entries = []
        for i, (name, text) in enumerate(patches):
            pf = t / f"{i}-{name}"
            pf.write_text(text)
            must, new = scratch_mod.patch_targets(text)
            entries.append({"group": "g", "kind": "tree", "name": name, "path": pf, "must_exist": must,
                            "created": new, "sha256": ""})
        res = scratch_mod.run_series(repo, sha, entries)
        return [res[f"g/{name}"]["status"] for name, _ in patches]
    finally:
        shutil.rmtree(t, ignore_errors=True)


@unittest.skipUnless(shutil.which("patch") and shutil.which("bash"), "needs GNU patch and bash")
class BuildVersusScript(unittest.TestCase):
    """(build outcome, script status) for the same tree and patch."""

    def check(self, files, patch_text, build, script):
        outcome, r = build_outcome(files, patch_text)
        self.assertEqual(outcome, build, r["out"])
        self.assertEqual(script_statuses(files, [("p.patch", patch_text)])[0], script)
        return r

    def test_exact_match_agrees(self):
        r = self.check({"f.c": PRE}, PATCH, "applied", "applies")
        self.assertEqual(r["files"]["f.c"], POST)

    def test_a_patch_that_needs_fuzz_is_applied_by_the_build_but_is_a_conflict_here(self):
        drifted = PRE.replace("c5", "CHANGED5")  # context line c5 differs: needs fuzz
        r = self.check({"f.c": drifted}, PATCH, "applied-with-fuzz", "conflict")
        self.assertEqual(r["files"]["f.c"], drifted.replace("OLD", "NEW"))
        self.assertIn("f.c.orig", r["files"], "the fuzz step leaves a .orig backup in the tree")

    def test_already_applied_exactly_agrees(self):
        self.check({"f.c": POST}, PATCH, "already-applied", "reverse-applies")

    def test_already_applied_with_drifted_context_is_recognised_by_the_build_only(self):
        drifted = POST.replace("c5", "CHANGED5")
        self.check({"f.c": drifted}, PATCH, "already-applied", "conflict")

    def test_a_genuine_conflict_is_skipped_by_both_and_recorded_by_the_build(self):
        other = PRE.replace("OLD", "SOMETHING ELSE")
        outcome, r = build_outcome({"f.c": other}, PATCH)
        self.assertEqual(outcome, "skipped")
        self.assertEqual(r["skipped"], ["p.patch"])
        self.assertEqual(script_statuses({"f.c": other}, [("p.patch", PATCH)]), ["conflict"])

    def test_missing_target_agrees(self):
        missing = PATCH.replace("f.c", "nofile.c")
        outcome, r = build_outcome({"f.c": PRE}, missing)
        self.assertEqual(outcome, "skipped")
        self.assertEqual(script_statuses({"f.c": PRE}, [("p.patch", missing)]), ["target-missing"])

    def test_fuzz_does_not_reapply_a_patch_that_is_already_there(self):
        """A worry that did not materialise: with an unrelated line the patch could also match, GNU patch still
        recognises the already-applied patch and leaves the file alone. Both sides agree here."""
        files = {"f.c": "ctx1\nb\nctx2\n-----\nx1\na\nx2\n"}
        p = "diff --git a/f.c b/f.c\n--- a/f.c\n+++ b/f.c\n@@ -1,3 +1,3 @@\n ctx1\n-a\n+b\n ctx2\n"
        outcome, r = build_outcome(files, p)
        self.assertEqual(outcome, "already-applied")
        self.assertEqual(r["files"]["f.c"], files["f.c"])
        self.assertEqual(script_statuses(files, [("p.patch", p)]), ["reverse-applies"])

    def test_the_build_continues_after_a_failed_patch_like_the_script_does(self):
        conflict = PATCH.replace("OLD", "NOPE").replace("NEW", "NOPE2")
        second = "diff --git a/g.c b/g.c\n--- a/g.c\n+++ b/g.c\n@@ -1 +1 @@\n-one\n+two\n"
        files = {"f.c": PRE, "g.c": "one\n"}
        r = build_apply(files, {"0001-conflict.patch": conflict, "0002-ok.patch": second},
                        call="apply_kernel_patches PATCHES")
        self.assertEqual(r["rc"], 1)  # one failure counted, build carries on
        self.assertEqual(r["skipped"], ["0001-conflict.patch"])
        self.assertEqual(r["files"]["g.c"], "two\n")
        self.assertEqual(script_statuses(files, [("0001-conflict.patch", conflict), ("0002-ok.patch", second)]),
                         ["conflict", "applies"])

    def test_require_patch_is_stricter_than_apply_patch_file(self):
        """`require_patch` (functions-extra.sh) is git apply only and aborts the build; the fuzz fallback does not
        exist there. The Phase 0 check has no equivalent and never aborts."""
        drifted = PRE.replace("c5", "CHANGED5")
        r = build_apply({"f.c": drifted}, {"p.patch": PATCH}, call="require_patch PATCHES/p.patch", extra_source=True)
        self.assertNotEqual(r["rc"], 0)
        self.assertIn("REQUIRED patch does not apply", r["out"])
        self.assertEqual(r["files"]["f.c"], drifted)


class DocumentedMethodStillMatchesTheBuild(unittest.TestCase):
    def test_functions_sh_still_uses_the_documented_sequence(self):
        src = (ROOT / "functions.sh").read_text()
        body = src[src.index("apply_patch_file() {"):src.index("apply_kernel_patches() {")]
        order = [body.index(x) for x in ("git apply --check -", "patch -p1 --fuzz=3 --batch --forward --dry-run",
                                         "patch -p1 --fuzz=3 --batch -R --dry-run")]
        self.assertEqual(order, sorted(order))
        self.assertIn("skipped-patches.txt", body)
        extra = (ROOT / "functions-extra.sh").read_text()
        self.assertIn("git apply --check", extra[extra.index("require_patch() {"):extra.index("ksu_root_dir() {")])

    def test_phase0_documents_every_difference_the_script_reports(self):
        doc = (ROOT / "PHASE0.md").read_text()
        for d in mod.PATCH_METHOD["differences"]:
            self.assertIn(d["aspect"], doc, d["aspect"])
        for fragment in ("patch -p1 --fuzz=3", "apply_patch_file", "require_patch", "skipped-patches.txt", ".orig"):
            self.assertIn(fragment, doc, fragment)

    def test_the_script_describes_its_own_method_accurately(self):
        script = (ROOT / "phase0" / "delta_report.py").read_text()
        self.assertIn('"apply", "--cached", "--check", pf', script)
        self.assertIn('"apply", "--cached", "--check", "-R", pf', script)
        self.assertNotIn("--fuzz", script.replace("--fuzz=3", "").replace("patch --fuzz", ""))  # no fuzz step exists

    def test_the_report_carries_the_comparison(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        a1 = h.commit(ack, {"Makefile": h.makefile(100), "f.c": PRE}, "a1")
        gk = h.clone(ack, t / "gkid", "GKID-6.1", a1)
        h.commit(gk, {"x.c": "x\n"}, "g")
        lm = h.clone(ack, t / "lum", "android14-6.1", a1)
        h.commit(lm, {"y.c": "y\n"}, "l")
        hc = t / "hc"
        h.write(hc, "kernel-patches/common/0001-p.patch", PATCH)
        h.write(hc, "kernel-patches/common/0002-drifted.patch", PATCH.replace("c5", "CHANGED5"))  # needs fuzz
        proc, result = h.run_cli(t, hc, ack, "android14-6.1-lts", gk, "GKID-6.1", lm, "android14-6.1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        a, md = result
        self.assertEqual(a["patch_method"], mod.PATCH_METHOD)
        self.assertIn("### How this check differs from the build's patch application", md)
        self.assertIn("This is not the build's method", md)
        rows = {p["patch"]: p for p in a["patches"]}
        self.assertEqual(rows["0002-drifted.patch"]["status"]["ack_tip"], "conflict")
        self.assertIn("The build's fuzz fallback might still apply it", rows["0002-drifted.patch"]["recommendation"])
        self.assertIn("The build's fuzz fallback might still apply it", md)
        for d in mod.PATCH_METHOD["differences"]:
            self.assertIn(f"| {d['aspect']} |", md)


class PatchDiscoveryMatchesTheBuild(unittest.TestCase):
    """The build finds patches with `find` and `sort -V`; the script reads the group directories and sorts naturally."""

    @staticmethod
    def build_order(directory):
        out = subprocess.run(
            f'find "{directory}" -type f \\( -name "*.patch" -o -name "*.diff" \\) -print0 | sort -z -V | tr "\\0" "\\n"',
            shell=True, capture_output=True, text=True, executable="/bin/bash").stdout.split("\n")
        return [Path(x).name for x in out if x]

    @unittest.skipUnless(shutil.which("sort") and shutil.which("find"), "needs find and sort")
    def test_order_is_identical_for_the_repositorys_own_patch_directories(self):
        for rel, _ in mod.PATCH_GROUPS:
            d = ROOT / rel
            if not d.is_dir():
                continue
            ordered, method = mod.order_like_build([x for x in d.iterdir() if x.suffix in (".patch", ".diff")])
            self.assertEqual(method, mod.ORDER_SORT_V)
            self.assertEqual([p.name for p in ordered], self.build_order(d), rel)

    @unittest.skipUnless(shutil.which("sort") and shutil.which("find"), "needs find and sort")
    def test_order_is_identical_for_tricky_names_where_a_natural_sort_would_differ(self):
        names = ["0001-fix-sync-task_mmu.c-SuSFS.patch", "0001-fixup-Remove-broken-hunk.patch", "0010-b.patch",
                 "0002-a.patch", "0001.b.patch", "001.GKI-below-6.12-fix.patch", "001.GKI-6.12-or-above-fix.patch",
                 "002.5.10_or_lower-fix.patch", "00010-z.patch", "0009~rc.patch", "a.diff", "A.patch"]
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        for n in names:
            h.write(t, n, "x\n")
        ordered, method = mod.order_like_build([p for p in t.iterdir()])
        self.assertEqual(method, mod.ORDER_SORT_V)
        self.assertEqual([p.name for p in ordered], self.build_order(t))
        # the reason the real sort is used: a hand-written natural sort gives a different order
        self.assertNotEqual([p.name for p in sorted(t.iterdir(), key=mod.natural_key)], self.build_order(t))

    def test_without_gnu_sort_the_fallback_is_used_and_reported(self):
        t = Path(h.tmpdir())
        self.addCleanup(shutil.rmtree, t, True)
        hc = t / "hc"
        h.write(hc, "kernel-patches/common/0002-b.patch", PATCH)
        h.write(hc, "kernel-patches/common/0001-a.patch", PATCH)
        real = mod.shutil.which
        mod.shutil.which = lambda name, *a, **k: None if name == "sort" else real(name, *a, **k)
        try:
            entries = mod.load_patches(hc)
        finally:
            mod.shutil.which = real
        self.assertEqual([e["name"] for e in entries], ["0001-a.patch", "0002-b.patch"])
        self.assertEqual({e["order"] for e in entries}, {mod.ORDER_FALLBACK})
        a = {"patches": [{"order_method": mod.ORDER_FALLBACK}], "trees": {"gkid": {"base": "1", "base_version": "v",
             "base_confidence": "exact", "base_version_tuple": None, "tip_version_tuple": None,
             "commits": {"available": False}, "label": "GKID"}, "luminaire": {"base": "1", "base_version": "v",
             "base_confidence": "exact", "base_version_tuple": None, "tip_version_tuple": None,
             "commits": {"available": False}, "label": "Luminaire"}},
             "ack_reference": {"registry_branch": None, "primary_branch": "x"}, "ack_history_audit": None}
        self.assertTrue(any("natural-sort fallback" in w for w in mod.collect_warnings(a)))

    def test_the_repositorys_patches_fit_what_the_script_can_review(self):
        """If one of these assumptions stops holding, the documented comparison needs another look."""
        for rel, _ in mod.PATCH_GROUPS:
            d = ROOT / rel
            if not d.is_dir():
                continue
            self.assertEqual([p for p in d.rglob("*") if p.is_dir()], [], f"{rel} has subdirectories; the build would "
                             "apply patches the script does not review")
            for p in d.iterdir():
                if p.suffix not in (".patch", ".diff"):
                    continue
                text = p.read_bytes().decode("utf-8", "replace")
                self.assertIn("diff --git ", text, f"{p.name} is not in git format")
                self.assertNotIn("\r", text, f"{p.name} has CRLF line endings")
                self.assertNotIn("GIT binary patch", text, p.name)
                self.assertNotRegex(text, r"(?m)^rename from ", p.name)
                must, new = mod.patch_targets(text)
                self.assertTrue(must or new, f"{p.name}: no targets parsed")


if __name__ == "__main__":
    unittest.main()
