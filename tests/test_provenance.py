#!/usr/bin/env python3
"""Tests for the registry fields that make HyperCore-managed sources possible: provenance (role, upstream, sync,
credits) and per-source version policy (supported range, recorded builds).

The managed fork used here is a FIXTURE. No such repository exists yet; the tests only prove that the registry can
describe one, and refuses to describe one without its upstream, its sync policy and its credits.
"""

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py"))

import sources  # noqa: E402

SCRIPT = ROOT / "py" / "sources.py"
UPSTREAM_REPO = "https://github.com/ahmed-alnassif/GKI-Duchamp-6.1"
FORK_REPO = "https://github.com/example-org/fork-of-duchamp-6.1"
SHA = "0123456789abcdef0123456789abcdef01234567"


def data():
    return json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))


def problems(mutate):
    d = data()
    mutate(d)
    return sources.check_registry(d)


def registry(mutate=None):
    d = data()
    if mutate:
        mutate(d)
    found = sources.check_registry(d)
    assert not found, found
    return sources.Registry(d)


def make_managed(d):
    src = d["sources"]["gkid-duchamp-6.1"]
    src["repo"] = FORK_REPO
    src["branch"] = "hypercore/6.1"
    src["branch_variants"] = [{"when_flag": "NH", "branch": "hypercore/6.1-nh", "label": "NetHunter build"}]
    src["role"] = "managed"
    src["upstream"] = {"repo": UPSTREAM_REPO, "branch": "GKID-6.1", "synced_commit": SHA}
    src["sync"] = {"strategy": "merge"}
    src["credits"] = [{"name": "Ahmed Al-Nassif", "url": UPSTREAM_REPO, "license": "GPL-2.0-only"}]


class Provenance(unittest.TestCase):
    def assertProblem(self, mutate, *fragments):
        found = "\n".join(problems(mutate))
        self.assertTrue(found, "expected the registry to be rejected")
        for fragment in fragments:
            self.assertIn(fragment, found)

    def test_the_real_registry_marks_every_source_as_external_and_says_what_it_is(self):
        r = registry()
        for src_id in r.source_ids():
            self.assertEqual(r.provenance(src_id)["role"], "external", src_id)
        self.assertEqual(r.provenance("gkid-duchamp-6.1")["kind"], "device")
        self.assertEqual(r.provenance("aosp-ack-6.6")["kind"], "generic-gki")

    def test_an_external_source_is_its_own_upstream(self):
        p = registry().provenance("gkid-duchamp-6.1")
        self.assertEqual((p["upstream_repo"], p["upstream_branch"]), (UPSTREAM_REPO, "GKID-6.1"))
        self.assertIn("external device source", registry().describe_provenance("gkid-duchamp-6.1"))
        self.assertIn("not managed by HyperCore", registry().describe_provenance("gkid-duchamp-6.1"))

    def test_nothing_claims_a_license_that_was_not_checked(self):
        for credit in data()["sources"]["gkid-duchamp-6.1"]["credits"]:
            self.assertNotIn("license", credit)

    def test_a_managed_source_is_valid_with_upstream_sync_and_credits(self):
        r = registry(make_managed)
        p = r.provenance("gkid-duchamp-6.1")
        self.assertEqual((p["role"], p["repo"], p["upstream_repo"], p["upstream_branch"]), ("managed", FORK_REPO, UPSTREAM_REPO, "GKID-6.1"))
        text = r.describe_provenance("gkid-duchamp-6.1")
        self.assertIn("HyperCore-managed device source", text)
        self.assertIn("ahmed-alnassif/GKI-Duchamp-6.1 @ GKID-6.1", text)
        self.assertIn(f"synced at {SHA[:12]}", text)

    def test_the_managed_source_is_still_selected_like_any_other(self):
        chosen = registry(make_managed).select("6.1", {"NH": "true"})
        self.assertEqual((chosen["repo"], chosen["branch"]), (FORK_REPO, "hypercore/6.1-nh"))

    def test_a_managed_source_without_upstream_sync_or_credits_is_refused(self):
        def mutate(d):
            make_managed(d)
            src = d["sources"]["gkid-duchamp-6.1"]
            for key in ("upstream", "sync", "credits"):
                src.pop(key)
        self.assertProblem(mutate, "needs an 'upstream'", "needs a 'sync' policy", "needs 'credits' that name the original authors")

    def test_a_managed_source_cannot_track_itself(self):
        def mutate(d):
            make_managed(d)
            d["sources"]["gkid-duchamp-6.1"]["upstream"] = {"repo": FORK_REPO, "branch": "hypercore/6.1"}
        self.assertProblem(mutate, "is the same repository and branch as the source itself")

    def test_only_merge_syncs_are_allowed_because_they_keep_authorship_and_history(self):
        for strategy in ("rebase", "squash"):
            def mutate(d, strategy=strategy):
                make_managed(d)
                d["sources"]["gkid-duchamp-6.1"]["sync"] = {"strategy": strategy}
            self.assertProblem(mutate, f"'{strategy}' is not one of: merge")

    def test_upstream_fields_are_checked(self):
        def mutate(d):
            make_managed(d)
            d["sources"]["gkid-duchamp-6.1"]["upstream"] = {"repo": "git@github.com:x/y.git", "branch": "bad branch", "synced_commit": "abc"}
        self.assertProblem(mutate, "upstream.repo", "upstream.branch", "full 40-character commit SHA")

    def test_upstream_and_sync_are_only_for_managed_sources(self):
        def mutate(d):
            d["sources"]["gkid-duchamp-6.1"]["upstream"] = {"repo": UPSTREAM_REPO, "branch": "GKID-6.1"}
            d["sources"]["gkid-duchamp-6.1"]["sync"] = {"strategy": "merge"}
        self.assertProblem(mutate, "upstream: is only for managed sources", "sync: is only for managed sources")

    def test_credits_kind_and_role_are_validated(self):
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(kind="vendor"), "'vendor' is not one of: device, generic-gki")
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(role="forked"), "'forked' is not one of: external, managed")
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(credits=[]), "non-empty list")
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(credits=[{"url": UPSTREAM_REPO}]), "missing required key 'name'")
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(credits=[{"name": "x", "licence": "GPL"}]), "did you mean 'license'")

    def test_command_line(self):
        def run(*args):
            return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
        text = run("provenance", "gkid-duchamp-6.1")
        self.assertEqual(text.returncode, 0)
        self.assertIn("external device source", text.stdout)
        as_json = json.loads(run("provenance", "aosp-ack-5.10", "--format", "json").stdout)
        self.assertEqual((as_json["role"], as_json["kind"], as_json["upstream_branch"]), ("external", "generic-gki", "android12-5.10-lts"))
        bad = run("provenance", "nope")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("Unknown kernel source 'nope'", bad.stderr)


def with_versions(d):
    d["sources"]["gkid-duchamp-6.1"]["versions"] = {
        "min": "6.1.100", "max": "6.1.200", "reason": "older trees lack the patch base",
        "verified": {"6.1.177": {"duchamp": {
            "build_verification": {"state": "verified", "evidence": "run #12, log inspected"},
            "boot_verification": {"state": "unverified"}}}},
    }


class VersionPolicySchema(unittest.TestCase):
    def assertProblem(self, mutate, *fragments):
        found = "\n".join(problems(lambda d: (with_versions(d), mutate(d))))
        self.assertTrue(found, "expected the registry to be rejected")
        for fragment in fragments:
            self.assertIn(fragment, found)

    def versions(self, d):
        return d["sources"]["gkid-duchamp-6.1"]["versions"]

    def test_valid(self):
        self.assertEqual(problems(with_versions), [])

    def test_min_and_max_must_be_versions_of_the_sources_line(self):
        self.assertProblem(lambda d: self.versions(d).update(min="6.6.10"), "versions.min", "does not belong to the 6.1 line")
        self.assertProblem(lambda d: self.versions(d).update(max="6.1"), "versions.max", "Linux version like 6.1.115")

    def test_min_above_max(self):
        self.assertProblem(lambda d: self.versions(d).update(min="6.1.300"), "min 6.1.300 is higher than max 6.1.200")

    def test_recorded_builds_need_evidence_a_known_device_and_the_right_line(self):
        v = lambda d: self.versions(d)["verified"]
        self.assertProblem(lambda d: v(d)["6.1.177"]["duchamp"].update(build_verification={"state": "reported"}),
                           "state 'reported' needs a non-empty 'evidence'")
        self.assertProblem(lambda d: v(d)["6.1.177"].update(marble=copy.deepcopy(v(d)["6.1.177"]["duchamp"])), "unknown device 'marble'")
        self.assertProblem(lambda d: v(d).update({"6.6.5": copy.deepcopy(v(d)["6.1.177"])}), "does not belong to the 6.1 line")
        self.assertProblem(lambda d: v(d)["6.1.177"]["duchamp"].pop("build_verification"), "missing required key 'build_verification'")

    def test_unknown_keys_get_a_hint(self):
        self.assertProblem(lambda d: self.versions(d).update(minimum="6.1.1"), "unknown key")


class VersionPolicyChecks(unittest.TestCase):
    def test_without_a_versions_block_every_version_is_allowed_and_noted(self):
        result = registry().check_version("6.1", "", "6.1.177")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["notes"][0][0], "notice")
        self.assertIn("no recorded build or boot result for gkid-duchamp-6.1 on duchamp", result["notes"][0][1])

    def test_below_and_above_the_range_are_errors_with_the_reason(self):
        r = registry(with_versions)
        low = r.check_version("6.1", "", "6.1.99")
        self.assertIn("below the lowest version gkid-duchamp-6.1 supports, 6.1.100 (older trees lack the patch base)", low["errors"][0])
        high = r.check_version("6.1", "", "6.1.201")
        self.assertIn("above the highest version gkid-duchamp-6.1 supports, 6.1.200", high["errors"][0])

    def test_range_edges_are_inclusive_and_compare_numerically(self):
        r = registry(with_versions)
        for version in ("6.1.100", "6.1.200", "6.1.150", "6.1.177"):
            self.assertEqual(r.check_version("6.1", "", version)["errors"], [], version)
        self.assertTrue(r.check_version("6.1", "", "6.1.99")["errors"], "6.1.99 is lower than 6.1.100, not higher as text")

    def test_a_recorded_build_is_reported_with_its_states(self):
        note = registry(with_versions).check_version("6.1", "", "6.1.177")["notes"][0][1]
        self.assertEqual(note, "Linux 6.1.177 on gkid-duchamp-6.1: build verified, boot unverified recorded for duchamp")

    def test_a_replaced_source_skips_the_range_with_a_warning(self):
        result = registry(with_versions).check_version("6.1", "", "6.1.50", custom=True)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["notes"][0][0], "warning")

    def test_a_version_of_another_line_or_a_malformed_one_is_refused(self):
        r = registry(with_versions)
        with self.assertRaises(sources.RegistryError) as ctx:
            r.check_version("6.1", "", "6.6.10")
        self.assertIn("belongs to the 6.6 line, but the selected kernel base is 6.1", ctx.exception.problems[0])
        with self.assertRaises(sources.RegistryError):
            r.check_version("6.1", "", "6.1")

    def test_command_line(self):
        d = data()
        with_versions(d)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            path.write_text(json.dumps(d))

            def run(*args):
                return subprocess.run([sys.executable, str(SCRIPT), "--registry", str(path), "version-check", "--kernel-version", "6.1", *args],
                                      capture_output=True, text=True)
            ok = run("--version", "6.1.177")
            self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "6.1.177"))
            bad = run("--version", "6.1.99", "--gha")
            self.assertEqual((bad.returncode, bad.stdout), (1, ""))
            self.assertTrue(bad.stderr.startswith("::notice::") or "::error::" in bad.stderr)
            self.assertIn("::error::Linux 6.1.99 is below the lowest version", bad.stderr)
            custom = run("--version", "6.1.50", "--custom-lines", "6.1")
            self.assertEqual(custom.returncode, 0)


if __name__ == "__main__":
    unittest.main()
