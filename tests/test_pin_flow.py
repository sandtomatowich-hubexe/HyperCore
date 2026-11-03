#!/usr/bin/env python3
"""End-to-end tests of the version pin flow, using the real shell functions the workflow runs.

First job:   preflight_pin     latest-stable / latest-lts / version / commit  ->  version + full commit
Build jobs:  clone_kernel_source, enforce_version_policy, record_kernel_source

The kernel is a synthetic repository (see test_pins.PinFixture) and kernel.org's list is a fixture file. Only
resolve_kernel_source_config is replaced, by a stub that points at the local repository, because that function
deliberately refuses file:// URLs.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pins import BRANCH, ENV, PinFixture, ROOT  # noqa: E402


def releases_file(directory, line_version="6.1.106", extra=None):
    entries = [
        {"version": "6.17.4", "moniker": "stable", "iseol": False, "released": {"isodate": "2026-01-01"}},
        {"version": line_version, "moniker": "longterm", "iseol": False, "released": {"isodate": "2026-01-02"}},
    ] + (extra or [])
    path = Path(directory) / "releases.json"
    path.write_text(json.dumps({"releases": entries}))
    return path


class Flow(PinFixture):
    def run_shell(self, body, releases=None, registry=None, extra_env=None):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        releases = releases or releases_file(work)
        script = f'''
            WORKDIR="{ROOT}"; KSRC="{work}/ksrc"; GITHUB_STEP_SUMMARY="{work}/summary.md"; GITHUB_ENV="{work}/env"
            source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"
            export GIT_CLONE_PROTECTION_ACTIVE=false
            resolve_kernel_source_config() {{
                KERNEL_REPO="file://{self.up}"; KERNEL_BRANCH="{BRANCH}"; KERNEL_SOURCE_ID=gkid-duchamp-6.1
                KERNEL_REPO_ORIGIN="built-in default"; KERNEL_BRANCH_ORIGIN="built-in default"
            }}
            KERNEL_VERSION=6.1
            resolve_kernel_source_config   # as build.sh does before it clones
            {body}
        '''
        env = dict(ENV, PATH=os.environ["PATH"], KERNEL_RELEASES_URL=f"file://{releases}", KERNEL_RELEASES_ATTEMPTS="1")
        if registry:
            env["SOURCES_REGISTRY"] = str(registry)
        env.update(extra_env or {})
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        return result.returncode, result.stdout + result.stderr


class FirstJob(Flow):
    def test_latest_lts_becomes_the_newest_release_of_the_line_and_a_real_commit(self):
        code, out = self.run_shell('KERNEL_EXACT_VERSION=latest-lts; preflight_pin && echo "REQ=$KERNEL_PIN_REQUEST VER=$PIN_VERSION COMMIT=$PIN_COMMIT EXACT=$KERNEL_EXACT_VERSION"')
        self.assertEqual(code, 0, out)
        self.assertIn(f"REQ=latest-lts VER=6.1.106 COMMIT={self.last_reporting(107)} EXACT=6.1.106", out)

    def test_latest_stable_resolves_the_same_way(self):
        code, out = self.run_shell('KERNEL_EXACT_VERSION="Latest Stable"; preflight_pin && echo "REQ=$KERNEL_PIN_REQUEST VER=$PIN_VERSION"')
        self.assertEqual(code, 0, out)
        self.assertIn("REQ=latest-stable VER=6.1.106", out)

    def test_a_plain_version_and_a_commit_still_work_and_expose_commit_and_version(self):
        code, out = self.run_shell('KERNEL_EXACT_VERSION=6.1.105; preflight_pin && echo "REQ=[${KERNEL_PIN_REQUEST:-}] VER=$PIN_VERSION COMMIT=$PIN_COMMIT"')
        self.assertEqual(code, 0, out)
        self.assertIn(f"REQ=[] VER=6.1.105 COMMIT={self.last_reporting(106)}", out)
        code, out = self.run_shell(f'KERNEL_EXACT_VERSION={self.commits[102][:10]}; preflight_pin && echo "VER=$PIN_VERSION COMMIT=$PIN_COMMIT"')
        self.assertEqual(code, 0, out)
        self.assertIn(f"VER=6.1.102 COMMIT={self.commits[102]}", out)

    def test_nothing_set_means_nothing_to_do(self):
        code, out = self.run_shell('KERNEL_EXACT_VERSION=; preflight_pin && echo "OK PIN=[${PIN_VERSION:-}]"')
        self.assertEqual(code, 0, out)
        self.assertIn("OK PIN=[]", out)

    def test_when_the_source_is_behind_kernel_org_the_run_stops_and_does_not_use_an_older_release(self):
        releases = releases_file(tempfile.mkdtemp(), "6.1.110")   # the source stops at 6.1.107
        code, out = self.run_shell('KERNEL_EXACT_VERSION=latest-lts; preflight_pin; echo "VER=[${PIN_VERSION:-}] COMMIT=[${PIN_COMMIT:-}]"', releases=releases)
        self.assertIn("latest-lts means Linux 6.1.110 on the 6.1 line, but the selected source does not have it", out)
        self.assertIn("Not substituting another version", out)
        self.assertIn("Nearest versions: 6.1.107", out)
        self.assertIn("VER=[] COMMIT=[]", out)

    def test_a_release_missing_from_the_middle_of_the_history_is_not_replaced_by_its_neighbour(self):
        releases = releases_file(tempfile.mkdtemp(), "6.1.104")   # the source has 6.1.102 and 6.1.105, not 6.1.104
        code, out = self.run_shell('KERNEL_EXACT_VERSION=latest-lts; preflight_pin; echo "VER=[${PIN_VERSION:-}]"', releases=releases)
        self.assertIn("Nearest versions: 6.1.102, 6.1.105", out)
        self.assertIn("VER=[]", out)

    def test_an_unreadable_release_list_fails_the_run(self):
        code, out = self.run_shell('KERNEL_EXACT_VERSION=latest-lts; preflight_pin; echo "VER=[${PIN_VERSION:-}]"', releases="/nonexistent/releases.json")
        self.assertIn("Nothing was substituted", out)
        self.assertIn("VER=[]", out)

    def test_the_release_list_is_not_needed_for_an_exact_version(self):
        code, out = self.run_shell('KERNEL_EXACT_VERSION=6.1.105; preflight_pin && echo "VER=$PIN_VERSION"', releases="/nonexistent/releases.json")
        self.assertEqual(code, 0, out)
        self.assertIn("VER=6.1.105", out)

    def test_latest_lts_on_a_line_that_is_not_longterm_is_refused(self):
        extra = [{"version": "6.1.9", "moniker": "stable", "iseol": False, "released": {"isodate": "2026-01-03"}}]
        releases = releases_file(tempfile.mkdtemp(), "6.1.5", extra=[])
        data = json.loads(Path(releases).read_text())
        data["releases"] = [e for e in data["releases"] if not e["version"].startswith("6.1.")] + extra
        Path(releases).write_text(json.dumps(data))
        code, out = self.run_shell('KERNEL_EXACT_VERSION=latest-lts; preflight_pin; echo "VER=[${PIN_VERSION:-}]"', releases=releases)
        self.assertIn("not longterm", out)
        self.assertIn("VER=[]", out)


class BuildJob(Flow):
    def test_the_commit_from_the_first_job_is_checked_out_exactly(self):
        """A branch that moved after the first job must not change what the builds use."""
        sha = self.last_reporting(106)
        code, out = self.run_shell(f'KERNEL_EXACT_VERSION=6.1.105 KERNEL_PIN_REQUEST=latest-lts KERNEL_PIN_COMMIT={sha}; '
                                   f'export KERNEL_EXACT_VERSION KERNEL_PIN_REQUEST KERNEL_PIN_COMMIT; clone_kernel_source && echo "HEAD=$(git -C "$KSRC" rev-parse HEAD)"')
        self.assertEqual(code, 0, out)
        self.assertIn(f"HEAD={sha}", out)

    def test_a_commit_that_reports_another_version_than_requested_stops_the_build(self):
        code, out = self.run_shell(f'KERNEL_EXACT_VERSION=6.1.107 KERNEL_PIN_COMMIT={self.last_reporting(106)}; clone_kernel_source; echo "HEAD=$(git -C "$KSRC" rev-parse HEAD 2>/dev/null)"')
        self.assertIn("reports Linux 6.1.105 but Linux 6.1.107 was requested. Not substituting", out)
        self.assertNotRegex(out, r"HEAD=[0-9a-f]{40}")

    def test_a_build_started_by_hand_resolves_the_keyword_itself(self):
        code, out = self.run_shell('KERNEL_EXACT_VERSION=latest-lts; clone_kernel_source && echo "HEAD=$(git -C "$KSRC" rev-parse HEAD) SUB=$(grep ^SUBLEVEL "$KSRC/Makefile" 2>/dev/null | tr -d " ")"')
        self.assertEqual(code, 0, out)
        self.assertIn(f"HEAD={self.last_reporting(107)}", out)

    def test_the_summary_records_what_was_requested_what_it_resolved_to_and_the_full_commit(self):
        sha = self.last_reporting(107)
        code, out = self.run_shell(f'KERNEL_EXACT_VERSION=6.1.106 KERNEL_PIN_REQUEST=latest-lts KERNEL_PIN_COMMIT={sha}; '
                                   'clone_kernel_source; LINUX_VERSION=6.1.106; verify_exact_kernel_version; record_kernel_source; cat "$GITHUB_STEP_SUMMARY"')
        self.assertEqual(code, 0, out)
        self.assertIn("| Requested exact version or commit | latest-lts |", out)
        self.assertIn("| Resolved Linux version | 6.1.106 |", out)
        self.assertIn(f"| Resolved commit | `{sha}` |", out)
        self.assertIn("| Source role | external device source", out)
        self.assertIn("| Upstream project | https://github.com/ahmed-alnassif/GKI-Duchamp-6.1 @ GKID-6.1 |", out)

    def test_a_default_build_without_a_pin_still_records_the_commit_it_used(self):
        code, out = self.run_shell('KERNEL_EXACT_VERSION=; clone_kernel_source; LINUX_VERSION=6.1.107; record_kernel_source; cat "$GITHUB_STEP_SUMMARY"')
        self.assertEqual(code, 0, out)
        self.assertIn("| Requested exact version or commit | none (branch default) |", out)
        self.assertIn(f"| Resolved commit | `{self.commits['107-last']}` |", out)

    def test_the_metadata_for_notifications_carries_source_role_upstream_and_commit(self):
        meta_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, meta_dir, True)
        code, out = self.run_shell(
            f'RELEASE_DIR="{meta_dir}"; KSU=KSU; VARIANT=KSU; LTO=fullLTO; LINUX_VERSION=6.1.107; KERNEL_EXACT_VERSION=; '
            'clone_kernel_source; write_tg_meta test.zip')
        self.assertEqual(code, 0, out)
        meta = json.loads((meta_dir / "test.meta.json").read_text())
        self.assertEqual(meta["source_id"], "gkid-duchamp-6.1")
        self.assertEqual(meta["commit"], self.commits["107-last"])
        self.assertEqual(meta["provenance"]["role"], "external")
        self.assertEqual(meta["provenance"]["upstream_repo"], "https://github.com/ahmed-alnassif/GKI-Duchamp-6.1")
        self.assertFalse(meta["source_overridden"])


class VersionPolicyInTheBuild(Flow):
    def registry_with_range(self):
        d = json.loads((ROOT / "sources.json").read_text())
        d["sources"]["gkid-duchamp-6.1"]["versions"] = {"min": "6.1.100", "max": "6.1.106", "reason": "patches are verified up to here"}
        path = Path(tempfile.mkdtemp()) / "sources.json"
        self.addCleanup(shutil.rmtree, path.parent, True)
        path.write_text(json.dumps(d))
        return path

    def test_a_version_outside_the_supported_range_stops_the_build(self):
        code, out = self.run_shell('LINUX_VERSION=6.1.107; KERNEL_SOURCE_ID=gkid-duchamp-6.1; ( enforce_version_policy ); echo "RC=$?"', registry=self.registry_with_range())
        self.assertIn("above the highest version gkid-duchamp-6.1 supports, 6.1.106 (patches are verified up to here)", out)
        self.assertIn("RC=1", out)

    def test_a_supported_version_passes(self):
        code, out = self.run_shell('LINUX_VERSION=6.1.105; ( enforce_version_policy ); echo "RC=$?"', registry=self.registry_with_range())
        self.assertIn("RC=0", out)

    def test_without_a_range_every_version_passes_with_a_note(self):
        code, out = self.run_shell('LINUX_VERSION=6.1.177; ( enforce_version_policy ); echo "RC=$?"')
        self.assertIn("RC=0", out)
        self.assertIn("no recorded build or boot result", out)

    def test_a_source_replaced_by_a_repository_variable_skips_the_range(self):
        code, out = self.run_shell('LINUX_VERSION=6.1.150; KERNEL_REPO_ORIGIN="variable KERNEL_REPO_6_1"; ( enforce_version_policy ); echo "RC=$?"',
                                   registry=self.registry_with_range())
        self.assertIn("RC=0", out)
        self.assertIn("not applied because a repository variable replaces the source", out)

    def test_build_sh_checks_the_version_right_after_it_is_known(self):
        text = (ROOT / "build.sh").read_text(encoding="utf-8")
        known = text.index("LINUX_VERSION=$(make kernelversion)")
        self.assertLess(known, text.index("\nenforce_version_policy\n"))
        self.assertLess(text.index("\nenforce_version_policy\n"), text.index("\nrecord_kernel_source\n"))


if __name__ == "__main__":
    unittest.main()
