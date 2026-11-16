"""Phase 0 guarantees: the ACK 6.1 baseline source is gated and never marked verified, the current default source and
the production build files are untouched, the documents state the ACK-based architecture, the script takes its
defaults from the registry, and an end-to-end run produces the expected report. Offline: trees are generated here."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase0_helpers as h  # noqa: E402

ROOT = h.ROOT
SOURCES = ROOT / "py" / "sources.py"


def cli(*args):
    return subprocess.run([sys.executable, str(SOURCES), *args], capture_output=True, text=True)


class RegistryEntry(unittest.TestCase):
    def test_default_for_6_1_is_unchanged(self):
        self.assertEqual(cli("select", "6.1").stdout.split("|")[3], "gkid-duchamp-6.1")

    def test_ack_6_1_is_gated(self):
        blocked = cli("gate", "--kernel-version", "6.1", "--source", "aosp-ack-6.1", "--allow-unverified", "false",
                      "--custom-lines", "")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("ALLOW_UNVERIFIED", blocked.stdout + blocked.stderr)
        allowed = cli("gate", "--kernel-version", "6.1", "--source", "aosp-ack-6.1", "--allow-unverified", "true",
                      "--custom-lines", "")
        self.assertEqual(allowed.returncode, 0)

    def test_ack_is_never_marked_build_or_boot_verified(self):
        entry = json.loads((ROOT / "sources.json").read_text())["sources"]["aosp-ack-6.1"]
        self.assertFalse(entry["default_for_line"])
        dev = entry["devices"]["duchamp"]
        self.assertEqual(dev["compatibility"], "needs-verification")
        self.assertEqual(dev["build_verification"]["state"], "unverified")
        self.assertEqual(dev["boot_verification"]["state"], "unverified")

    def test_the_gkid_default_is_a_temporary_build_safety_measure_not_a_commitment(self):
        """The note on the ACK entry and the documents must say so, so nobody reads the default as approval."""
        entry = json.loads((ROOT / "sources.json").read_text())["sources"]["aosp-ack-6.1"]
        self.assertIn("during Phase 0", entry["devices"]["duchamp"]["notes"])
        phase0 = (ROOT / "PHASE0.md").read_text()
        self.assertIn("temporary", phase0)
        self.assertIn("not an architectural commitment", phase0)
        self.assertIn("fully independent", phase0)


class WorkflowSafety(unittest.TestCase):
    def test_phase0_workflow_is_manual_read_only_and_does_not_build(self):
        wf = (ROOT / ".github" / "workflows" / "phase0-delta.yml").read_text()
        self.assertIn("workflow_dispatch:", wf)
        self.assertNotIn("push:", wf)
        self.assertNotIn("pull_request", wf)
        self.assertIn("contents: read", wf)
        self.assertNotIn("contents: write", wf)
        self.assertNotIn("build.sh", wf)
        self.assertNotIn("gh release", wf)

    def test_phase0_workflow_writes_nothing_into_the_checkout(self):
        """Reports, summary and artifact all live under runner.temp; the checkout ($GITHUB_WORKSPACE) is only read."""
        import yaml
        text = (ROOT / ".github" / "workflows" / "phase0-delta.yml").read_text()
        job = yaml.safe_load(text)["jobs"]["delta"]
        self.assertTrue(job["env"]["REPORT_DIR"].startswith("${{ runner.temp }}/"))
        steps = {st["name"]: st for st in job["steps"]}
        runs = "\n".join(st.get("run", "") for st in job["steps"])
        paths = "\n".join(str(st.get("with", {}).get("path", "")) for st in job["steps"])
        # every mention of a report file is under the temp report directory
        for blob in (runs, paths):
            for m in re.finditer(r"delta-report\.(md|json)", blob):
                before = blob[max(0, m.start() - 40):m.start()]
                self.assertTrue(before.endswith(("$REPORT_DIR/", "${{ runner.temp }}/phase0-report/")),
                                f"report file outside the temp directory: ...{before}{m.group(0)}")
        run_step = steps["Run delta analysis"]["run"]
        self.assertIn('--out "$REPORT_DIR/delta-report.md"', run_step)
        self.assertIn('--json "$REPORT_DIR/delta-report.json"', run_step)
        self.assertIn('--workdir "$RUNNER_TEMP/', run_step)
        self.assertEqual(steps["Run delta analysis"]["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        # the workspace is only ever passed as the (read-only) --hypercore argument
        for line in runs.splitlines():
            if "GITHUB_WORKSPACE" in line:
                self.assertIn("--hypercore", line, line)
        # the only redirection target is the job summary
        for target in re.findall(r">>?\s*(\S+)", runs):
            self.assertIn(target.strip('"'), ("$GITHUB_STEP_SUMMARY", "/dev/null"))
        upload = [st for st in job["steps"] if str(st.get("uses", "")).startswith("actions/upload-artifact")][0]
        for line in str(upload["with"]["path"]).strip().splitlines():
            self.assertTrue(line.startswith("${{ runner.temp }}/"), line)

    def test_phase0_workflow_keeps_reports_out_of_the_scratch_repository(self):
        import yaml
        job = yaml.safe_load((ROOT / ".github" / "workflows" / "phase0-delta.yml").read_text())["jobs"]["delta"]
        run = [st for st in job["steps"] if st.get("name") == "Run delta analysis"][0]["run"]
        workdir = re.search(r'--workdir "(\$RUNNER_TEMP/[^"]+)"', run).group(1)
        report_dir = job["env"]["REPORT_DIR"].replace("${{ runner.temp }}", "$RUNNER_TEMP")
        self.assertNotEqual(workdir, report_dir)
        self.assertFalse(report_dir.startswith(workdir + "/"), "reports must not live inside the scratch repository")
        self.assertFalse(workdir.startswith(report_dir + "/"))

    def test_phase0_workflow_has_no_hard_coded_kernel_source(self):
        wf = (ROOT / ".github" / "workflows" / "phase0-delta.yml").read_text()
        self.assertNotIn("ahmed-alnassif", wf)
        self.assertNotIn("GKID-6.1", wf)  # the GKID reference comes from the registry, or an explicit input


class EndToEndAndRegistryDefaults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(h.tmpdir())
        t = cls.tmp
        ack = h.init_repo(t / "ack", "android14-6.1-lts")
        a1 = h.commit(ack, {"Makefile": h.makefile(100), "mm/vmscan.c": "int lru = 0;\n"}, "a1")
        gk = h.clone(ack, t / "gkid", "GKID-6.1", a1)
        h.commit(gk, {"mm/vmscan.c": "int lru = 1;\n", "arch/arm64/boot/dts/mediatek/mt6897-duchamp.dts": "d\n",
                      "README.md": "gkid\n"}, "g", author="Ahmed")
        lm = h.clone(ack, t / "lum", "android14-6.1", a1)
        h.commit(lm, {"arch/arm64/boot/dts/mediatek/mt6897-duchamp.dts": "d\n"}, "l", author="Chain")
        hc = t / "hc"
        h.write(hc, "kernel-patches/common/0001-lru.patch",
                "diff --git a/mm/vmscan.c b/mm/vmscan.c\n--- a/mm/vmscan.c\n+++ b/mm/vmscan.c\n@@ -1 +1 @@\n"
                "-int lru = 0;\n+int lru = 1;\n")
        h.write(hc, "kernel-patches/common/0002-missing.patch",
                "diff --git a/kernel/module.c b/kernel/module.c\n--- a/kernel/module.c\n+++ b/kernel/module.c\n@@ -1 +1 @@\n-x\n+y\n")
        h.write(hc, "patches/0001-ksu.patch", "diff --git a/k.c b/k.c\n--- a/k.c\n+++ b/k.c\n@@ -1 +1 @@\n-a\n+b\n")
        cls.ack, cls.gk, cls.lm, cls.hc = ack, gk, lm, hc
        cls.proc, cls.result = h.run_cli(t, hc, ack, "android14-6.1-lts", gk, "GKID-6.1", lm, "android14-6.1")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_end_to_end_buckets(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        a, md = self.result
        f = {e["path"]: e for e in a["files"]}
        d = f["arch/arm64/boot/dts/mediatek/mt6897-duchamp.dts"]
        self.assertEqual((d["bucket"], d["provenance"]), (1, "both (same change)"))
        self.assertEqual((f["mm/vmscan.c"]["bucket"], f["mm/vmscan.c"]["provenance"]), (2, "GKID-only"))
        self.assertEqual((f["README.md"]["bucket"], f["README.md"]["provenance"]), (3, "GKID-only"))
        by = {p["patch"]: p for p in a["patches"]}
        self.assertEqual(by["0001-lru.patch"]["status"]["ack_tip"], "applies")
        self.assertEqual(by["0001-lru.patch"]["status"]["gkid_tip"], "reverse-applies")
        self.assertEqual(by["0002-missing.patch"]["status"]["ack_tip"], "target-missing")
        self.assertEqual(by["0001-ksu.patch"]["status"]["ack_tip"], "builder-side")
        self.assertEqual(f["mm/vmscan.c"]["hypercore_patches"], ["0001-lru.patch"])

    def test_defaults_come_from_the_registry_and_gkid_is_only_a_reference(self):
        t = self.tmp
        hc = t / "hc-registry"
        hc.mkdir()
        (hc / "sources.json").write_text(json.dumps({"sources": {
            "aosp-ack-6.1": {"repo": str(self.ack), "branch": "android14-6.1-lts"},
            "gkid-duchamp-6.1": {"repo": str(self.gk), "branch": "GKID-6.1"}}}))
        out = t / "reg.json"
        proc = subprocess.run([sys.executable, str(h.SCRIPT), "--workdir", str(t / "w-reg"), "--hypercore", str(hc),
                               "--luminaire-url", str(self.lm), "--luminaire-branch", "android14-6.1",
                               "--out", str(t / "reg.md"), "--json", str(out), "--skip-patches"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        a = json.loads(out.read_text())
        self.assertEqual(a["ack_reference"]["url"], str(self.ack))
        self.assertEqual(a["ack_reference"]["primary_branch"], "android14-6.1-lts")
        self.assertEqual(a["ack_reference"]["registry_branch"], "android14-6.1-lts")
        self.assertEqual(a["trees"]["gkid"]["role"], "read-only reference")
        self.assertEqual(a["trees"]["gkid"]["url"], str(self.gk))

    def test_the_registry_branch_mismatch_is_reported(self):
        t = self.tmp
        hc = t / "hc-mismatch"
        hc.mkdir()
        (hc / "sources.json").write_text(json.dumps({"sources": {
            "aosp-ack-6.1": {"repo": str(self.ack), "branch": "android14-6.1-lts"}}}))
        proc, result = h.run_cli(t, hc, self.ack, "android14-6.1", self.gk, "GKID-6.1", self.lm, "android14-6.1",
                                 extra=["--skip-patches"], out_name="mm", workdir=t / "w-mm")
        # android14-6.1 does not exist in the fixture ACK, so this must fail loudly instead of falling back
        self.assertEqual(proc.returncode, 2)
        self.assertIn("has no branch android14-6.1", proc.stderr)


if __name__ == "__main__":
    unittest.main()
