#!/usr/bin/env python3
"""The rule: every build report says who controls the kernel source, and never implies that upstream code came from
HyperCore. A source HyperCore manages and the upstream project it tracks are always named separately.

Reports covered: the job summary, the Telegram notification (table and fallback caption), and the release page.
The managed fork is a FIXTURE. No such repository exists yet.
"""

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pin_flow import Flow  # noqa: E402
from test_pins import ROOT  # noqa: E402

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHAT_ID", "1")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load(ROOT / ".github" / "scripts" / "prepare_release.py", "prepare_release")
tg = load(ROOT / ".github" / "scripts" / "telegram_release.py", "telegram_release")

UPSTREAM = "https://github.com/ahmed-alnassif/GKI-Duchamp-6.1"
FORK = "https://github.com/example-org/fork-of-duchamp-6.1"
SHA = "b" * 40

BUILD = {
    "KERNEL_VERSION": "6.1", "ANDROID_RELEASE": "14", "LINUX_VERSION": "6.1.177", "COMPILER_STRING": "clang", "SUSFS_VERSION": "v2",
    "KERNEL_SOURCE_URL": UPSTREAM, "KERNEL_SOURCE_REPO": "ahmed-alnassif/GKI-Duchamp-6.1", "KERNEL_SOURCE_BRANCH": "GKID-6.1",
    "KERNEL_COMMIT": SHA, "KERNEL_SOURCE_ROLE": "external", "KERNEL_SOURCE_KIND": "device", "KERNEL_SOURCE_OVERRIDDEN": "false",
    "KERNEL_UPSTREAM_REPO": UPSTREAM, "KERNEL_UPSTREAM_BRANCH": "GKID-6.1", "KERNEL_EXACT_REQUESTED": "",
}
MANAGED_BUILD = dict(BUILD, KERNEL_SOURCE_URL=FORK, KERNEL_SOURCE_REPO="example-org/fork-of-duchamp-6.1",
                     KERNEL_SOURCE_BRANCH="hypercore/6.1", KERNEL_SOURCE_ROLE="managed")


def telegram_meta(build):
    return {
        "variant": "KSU", "linux": build["LINUX_VERSION"], "source_repo": build["KERNEL_SOURCE_REPO"],
        "source_branch": build["KERNEL_SOURCE_BRANCH"], "commit": build["KERNEL_COMMIT"],
        "source_overridden": build["KERNEL_SOURCE_OVERRIDDEN"] == "true",
        "provenance": {"role": build["KERNEL_SOURCE_ROLE"], "kind": build["KERNEL_SOURCE_KIND"],
                       "upstream_repo": build["KERNEL_UPSTREAM_REPO"], "upstream_branch": build["KERNEL_UPSTREAM_BRANCH"]},
    }


def _cell_text(cell):
    inner = cell["text"]
    return inner["button"]["text"] if isinstance(inner, dict) else inner


def telegram_text(build):
    meta = telegram_meta(build)
    table = tg.build_blocks(meta, "x.zip", "kernelzip")[0]["cells"]
    rows = " | ".join(f"{_cell_text(left)}: {_cell_text(right)}" for left, right in table)
    return rows, tg.fallback_caption(meta)


class ReleaseNotes(unittest.TestCase):
    def lines(self, **changes):
        return "\n".join(release.kernel_source_lines(dict(BUILD, **changes)))

    def test_an_external_source_is_called_an_upstream_project_not_managed_by_hypercore(self):
        text = self.lines()
        self.assertIn("Kernel source (upstream project, not managed by HyperCore): [ahmed-alnassif/GKI-Duchamp-6.1]", text)
        self.assertIn("@ `GKID-6.1`", text)
        self.assertNotIn("HyperCore-managed", text)
        self.assertNotIn("Upstream project:", text)

    def test_a_managed_source_and_its_upstream_are_named_separately(self):
        text = "\n".join(release.kernel_source_lines(MANAGED_BUILD))
        self.assertIn(f"Kernel source (HyperCore-managed): [example-org/fork-of-duchamp-6.1]({FORK}) @ `hypercore/6.1`", text)
        self.assertIn(f"Upstream project: [ahmed-alnassif/GKI-Duchamp-6.1]({UPSTREAM}) @ `GKID-6.1`", text)

    def test_a_source_replaced_by_a_variable_is_custom_and_makes_no_claim_about_its_origin(self):
        text = "\n".join(release.kernel_source_lines(dict(MANAGED_BUILD, KERNEL_SOURCE_OVERRIDDEN="true")))
        self.assertIn("custom, set by a repository variable, not assessed", text)
        self.assertNotIn("HyperCore-managed", text)
        self.assertNotIn("Upstream project:", text)

    def test_a_build_that_did_not_record_its_role_says_so_instead_of_guessing(self):
        build = {k: v for k, v in BUILD.items() if k not in ("KERNEL_SOURCE_ROLE", "KERNEL_SOURCE_KIND", "KERNEL_UPSTREAM_REPO")}
        text = "\n".join(release.kernel_source_lines(build))
        self.assertIn("Kernel source (origin not recorded)", text)
        self.assertNotIn("HyperCore-managed", text)
        self.assertEqual(release.kernel_source_lines({}), ["- Kernel source: not recorded"])

    def test_the_commit_links_to_the_source_and_the_requested_version_is_shown(self):
        text = self.lines(KERNEL_EXACT_REQUESTED="latest-lts")
        self.assertIn(f"Kernel commit: [`{SHA[:12]}`]({UPSTREAM}/commit/{SHA})", text)
        self.assertIn("Requested version: `latest-lts`", text)
        googlesource = self.lines(KERNEL_SOURCE_URL="https://android.googlesource.com/kernel/common")
        self.assertIn(f"https://android.googlesource.com/kernel/common/+/{SHA}", googlesource)

    def test_the_block_lands_in_the_kernel_section_of_the_release_page(self):
        label, section = release.build_kernel_section("6.1", [BUILD], "owner/repo", "tag", set(),
                                                     {"nh": "true", "nm": "false", "droidspaces": "true", "lto": "fullLTO", "test": "no"})
        build_details = section[section.index("**Build details:**"):section.index("**SuSFS changelog")]
        self.assertIn("- Linux version: 6.1.177", build_details)
        self.assertIn("Kernel source (upstream project, not managed by HyperCore)", build_details)
        self.assertIn("- Compiler: clang", build_details)


class SameStoryEverywhere(Flow):
    """One source, three reports. They must agree on who controls it."""

    def managed_registry(self):
        d = json.loads((ROOT / "sources.json").read_text())
        src = d["sources"]["gkid-duchamp-6.1"]
        src.update(repo=FORK, branch="hypercore/6.1", role="managed", sync={"strategy": "merge"},
                   upstream={"repo": UPSTREAM, "branch": "GKID-6.1"},
                   credits=[{"name": "Ahmed Al-Nassif", "url": UPSTREAM}])
        src["branch_variants"] = [{"when_flag": "NH", "branch": "hypercore/6.1-nh", "label": "NetHunter build"}]
        path = Path(tempfile.mkdtemp()) / "sources.json"
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text(json.dumps(d))
        return path

    def summary_and_env(self, registry=None, extra=""):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        code, out = self.run_shell(
            f'{extra}\nKERNEL_EXACT_VERSION=; clone_kernel_source; LINUX_VERSION=6.1.107; record_kernel_source\n'
            'cat "$GITHUB_STEP_SUMMARY"; echo "=====ENV====="; cat "$GITHUB_ENV"', registry=registry)
        self.assertEqual(code, 0, out)
        summary, env_text = out.split("=====ENV=====")
        env = dict(line.split("=", 1) for line in env_text.strip().splitlines() if "=" in line)
        return summary, env

    def test_an_external_source_in_the_job_summary(self):
        summary, env = self.summary_and_env()
        self.assertIn("The kernel is fetched from an external project's repository, not managed by HyperCore.", summary)
        self.assertIn("| Source role | external device source (the original project, not managed by HyperCore) |", summary)
        self.assertNotIn("managed by HyperCore, which tracks", summary)
        self.assertEqual((env["KERNEL_SOURCE_ROLE"], env["KERNEL_SOURCE_OVERRIDDEN"], env["KERNEL_SOURCE_ID"]), ("external", "false", "gkid-duchamp-6.1"))

    def test_a_managed_source_in_the_job_summary_names_its_upstream_separately(self):
        summary, env = self.summary_and_env(registry=self.managed_registry())
        self.assertIn("The kernel is fetched from a repository managed by HyperCore, which tracks an external upstream project.", summary)
        self.assertIn("| Source role | HyperCore-managed device source, tracks ahmed-alnassif/GKI-Duchamp-6.1 @ GKID-6.1 |", summary)
        self.assertIn(f"| Upstream project | {UPSTREAM} @ GKID-6.1 |", summary)
        self.assertEqual((env["KERNEL_SOURCE_ROLE"], env["KERNEL_UPSTREAM_REPO"], env["KERNEL_UPSTREAM_BRANCH"]), ("managed", UPSTREAM, "GKID-6.1"))

    def test_a_replaced_source_is_never_described_as_managed_or_upstream(self):
        summary, env = self.summary_and_env(registry=self.managed_registry(), extra='KERNEL_REPO_ORIGIN="variable KERNEL_REPO_6_1"')
        self.assertIn("HyperCore has not assessed it.", summary)
        self.assertNotIn("managed by HyperCore, which tracks", summary)
        self.assertEqual(env["KERNEL_SOURCE_OVERRIDDEN"], "true")

    def test_the_three_reports_agree_for_a_managed_source(self):
        summary, env = self.summary_and_env(registry=self.managed_registry())
        build = dict(BUILD, **{k: v for k, v in env.items() if k.startswith("KERNEL_")})
        table, caption = telegram_text(dict(MANAGED_BUILD, KERNEL_COMMIT=env["KERNEL_COMMIT"]))
        notes = "\n".join(release.kernel_source_lines(build))
        for report, text in (("summary", summary), ("telegram table", table), ("telegram caption", caption), ("release notes", notes)):
            self.assertRegex(text, r"(?i)managed", report)
            self.assertRegex(text, r"(?i)upstream", report)
            self.assertIn("GKI-Duchamp-6.1", text, report)

    def test_the_three_reports_agree_for_an_external_source(self):
        summary, env = self.summary_and_env()
        build = dict(BUILD, **{k: v for k, v in env.items() if k.startswith("KERNEL_")})
        table, caption = telegram_text(BUILD)
        notes = "\n".join(release.kernel_source_lines(build))
        for report, text in (("summary", summary), ("telegram table", table), ("telegram caption", caption), ("release notes", notes)):
            self.assertRegex(text, r"(?i)upstream|external", report)
            self.assertNotIn("HyperCore-managed", text, report)
            self.assertNotIn("Source (HyperCore-managed)", text, report)
            self.assertNotIn("managed by HyperCore, which tracks", text, report)


class EnvContract(unittest.TestCase):
    """What the release notes read must be written by the workflow, which must get it from the build."""

    def test_every_key_the_release_notes_read_is_saved_by_the_workflow_and_exported_by_the_build(self):
        wanted = set(re.findall(r'build\.get\("(KERNEL_[A-Z_]+)"', (ROOT / ".github/scripts/prepare_release.py").read_text()))
        self.assertTrue({"KERNEL_SOURCE_ROLE", "KERNEL_UPSTREAM_REPO", "KERNEL_SOURCE_OVERRIDDEN", "KERNEL_COMMIT"} <= wanted)
        saved = set(re.findall(r"^\s+(KERNEL_[A-Z_]+)=\$\{\{ env\.\1 \}\}$", (ROOT / ".github/workflows/build.yml").read_text(), re.M))
        exported = set(re.findall(r'echo "(KERNEL_[A-Z_]+)=', (ROOT / "functions-extra.sh").read_text()))
        exported |= set(re.findall(r'echo "(KERNEL_[A-Z_]+)=', (ROOT / "build.sh").read_text()))
        self.assertEqual(wanted - saved, set(), "read by the release notes but not saved in build-env")
        self.assertEqual(saved - exported, set(), "saved in build-env but never exported by the build")


if __name__ == "__main__":
    unittest.main()
