#!/usr/bin/env python3
"""Tests for source selection by id and for the compatibility gate that runs before any build job."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py"))

import sources  # noqa: E402

SCRIPT = ROOT / "py" / "sources.py"


def data():
    return json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))


def registry_from(mutate=None):
    d = data()
    if mutate:
        mutate(d)
    problems = sources.check_registry(d)
    assert not problems, problems
    return sources.Registry(d)


def with_alternative_source(d):
    """A second, non-default 6.1 source that nobody has verified for duchamp."""
    d["sources"]["alt-duchamp-6.1"] = {
        "line": "6.1", "repo": "https://github.com/example/alt-kernel", "branch": "alt", "label": "alternative",
        "devices": {"duchamp": {"compatibility": "needs-verification",
                                "build_verification": {"state": "unverified"},
                                "boot_verification": {"state": "unverified"}}},
    }


def make_confirmed(d):
    a = d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]
    a["compatibility"] = "confirmed"
    a["build_verification"] = {"state": "verified", "evidence": "CI log"}
    a["boot_verification"] = {"state": "verified", "evidence": "Booted on a duchamp"}


class Selection(unittest.TestCase):
    def test_default_source_per_line(self):
        r = registry_from()
        self.assertEqual(r.select("6.1")["source_id"], "gkid-duchamp-6.1")
        self.assertEqual(r.select("6.1", source_id="default")["source_id"], "gkid-duchamp-6.1")
        self.assertEqual(r.select("5.10")["source_id"], "aosp-ack-5.10")

    def test_explicit_source(self):
        r = registry_from(with_alternative_source)
        chosen = r.select("6.1", source_id="alt-duchamp-6.1")
        self.assertEqual((chosen["repo"], chosen["branch"]), ("https://github.com/example/alt-kernel", "alt"))

    def test_unknown_source_suggests_the_closest_id(self):
        with self.assertRaises(sources.RegistryError) as ctx:
            registry_from().select("6.1", source_id="gkid-duchamp-6.2")
        self.assertIn("Did you mean 'gkid-duchamp-6.1'?", ctx.exception.problems[0])
        self.assertIn("Sources for 6.1: gkid-duchamp-6.1", ctx.exception.problems[0])

    def test_source_of_another_line_is_refused(self):
        with self.assertRaises(sources.RegistryError) as ctx:
            registry_from().select("6.1", source_id="aosp-ack-5.10")
        self.assertIn("is a 5.10 source but the selected kernel line is 6.1", ctx.exception.problems[0])


class Gate(unittest.TestCase):
    def test_default_duchamp_source_builds_with_a_warning_so_todays_default_keeps_working(self):
        result = sources.gate(registry_from(), "6.1")
        self.assertEqual((result["lines"], result["errors"]), (["6.1"], []))
        text = " ".join(t for _, t in result["notes"])
        self.assertIn("needs-verification for duchamp", text)
        self.assertIn("not boot-verified", text)

    def test_unsupported_line_is_blocked_with_the_reason(self):
        result = sources.gate(registry_from(), "5.10")
        self.assertEqual(result["lines"], [])
        self.assertIn("5.10 (aosp-ack-5.10) is unsupported for duchamp", result["errors"][0])
        self.assertIn("not its kernel line", result["errors"][0])

    def test_all_skips_unsupported_lines_and_keeps_the_rest(self):
        result = sources.gate(registry_from(), "All")
        self.assertEqual((result["lines"], result["errors"]), (["6.1"], []))
        skipped = [t for _, t in result["notes"] if t.startswith("Skipping")]
        self.assertEqual(len(skipped), 4)

    def test_all_fails_when_nothing_is_buildable(self):
        def mutate(d):
            a = d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]
            a.update(compatibility="unsupported", basis="tested", reason="does not boot")
        result = sources.gate(registry_from(mutate), "All")
        self.assertEqual(result["lines"], [])
        self.assertIn("No kernel line is buildable for duchamp", result["errors"])

    def test_confirmed_source_builds_without_any_warning(self):
        result = sources.gate(registry_from(make_confirmed), "6.1")
        self.assertEqual((result["lines"], result["errors"], result["notes"]), (["6.1"], [], []))

    def test_explicit_non_default_source_needs_allow_unverified(self):
        r = registry_from(with_alternative_source)
        blocked = sources.gate(r, "6.1", source_id="alt-duchamp-6.1")
        self.assertEqual(blocked["lines"], [])
        self.assertIn("needs ALLOW_UNVERIFIED=true", blocked["errors"][0])
        allowed = sources.gate(r, "6.1", source_id="alt-duchamp-6.1", allow_unverified=True)
        self.assertEqual((allowed["lines"], allowed["errors"]), (["6.1"], []))

    def test_explicitly_naming_the_default_source_is_the_same_as_the_default(self):
        result = sources.gate(registry_from(with_alternative_source), "6.1", source_id="gkid-duchamp-6.1")
        self.assertEqual((result["lines"], result["errors"]), (["6.1"], []))

    def test_allow_unverified_never_unlocks_an_unsupported_source(self):
        result = sources.gate(registry_from(), "5.10", source_id="aosp-ack-5.10", allow_unverified=True)
        self.assertEqual(result["lines"], [])
        self.assertIn("unsupported", result["errors"][0])

    def test_an_explicit_source_cannot_be_combined_with_all(self):
        with self.assertRaises(sources.RegistryError) as ctx:
            sources.gate(registry_from(), "All", source_id="gkid-duchamp-6.1")
        self.assertIn("cannot be combined with All", ctx.exception.problems[0])

    def test_repository_variables_make_the_source_custom_and_allow_it_with_a_warning(self):
        result = sources.gate(registry_from(), "5.10", custom_lines=["5.10"])
        self.assertEqual((result["lines"], result["errors"]), (["5.10"], []))
        self.assertIn("a repository variable replaces the registry's repository or branch", result["notes"][0][1])

    def test_unknown_line_and_unknown_device(self):
        with self.assertRaises(sources.RegistryError) as ctx:
            sources.gate(registry_from(), "7.0")
        self.assertIn("Configured lines: 5.10, 5.15, 6.1, 6.6, 6.12", ctx.exception.problems[0])
        with self.assertRaises(sources.RegistryError) as ctx:
            sources.gate(registry_from(), "6.1", device="marble")
        self.assertIn("Unknown device 'marble'. Configured devices: duchamp", ctx.exception.problems[0])

    def test_a_device_nobody_assessed_is_allowed_with_a_note(self):
        def mutate(d):
            d["devices"]["marble"] = {"name": "Poco F5", "kernel_line": "5.15", "android_release": "13"}
        result = sources.gate(registry_from(mutate), "5.15", device="marble")
        self.assertEqual((result["lines"], result["errors"]), (["5.15"], []))
        self.assertIn("not assessed for marble", result["notes"][0][1])

    def test_a_not_assessed_source_chosen_explicitly_needs_allow_unverified(self):
        def mutate(d):
            d["devices"]["marble"] = {"name": "Poco F5", "kernel_line": "6.1", "android_release": "14"}
            with_alternative_source(d)
        r = registry_from(mutate)
        self.assertEqual(sources.gate(r, "6.1", source_id="alt-duchamp-6.1", device="marble")["lines"], [])
        self.assertEqual(sources.gate(r, "6.1", source_id="alt-duchamp-6.1", device="marble", allow_unverified=True)["lines"], ["6.1"])


class GateCommandLine(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "gate", *args], capture_output=True, text=True)

    def test_allowed_lines_go_to_stdout_and_warnings_to_stderr(self):
        r = self.run_cli("--kernel-version", "All")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "6.1"))
        self.assertIn("[!] Skipping 5.10", r.stderr)

    def test_blocked_exits_1_with_an_error_and_no_stdout(self):
        r = self.run_cli("--kernel-version", "5.10")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("[-] 5.10 (aosp-ack-5.10) is unsupported for duchamp", r.stderr)

    def test_gha_flag_emits_workflow_annotations(self):
        r = self.run_cli("--kernel-version", "5.10", "--gha")
        self.assertTrue(r.stderr.startswith("::error::"), r.stderr)
        r = self.run_cli("--kernel-version", "6.1", "--gha")
        self.assertTrue(r.stderr.startswith("::warning::"), r.stderr)

    def test_custom_lines_and_allow_flag_are_parsed(self):
        self.assertEqual(self.run_cli("--kernel-version", "5.10", "--custom-lines", "5.10").stdout.strip(), "5.10")
        self.assertEqual(self.run_cli("--kernel-version", "6.1", "--allow-unverified", "TRUE").returncode, 0)

    def test_helper_commands(self):
        def out(*a):
            return subprocess.run([sys.executable, str(SCRIPT), *a], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out("default-device"), "duchamp")
        self.assertEqual(out("sources", "6.1"), "gkid-duchamp-6.1")
        self.assertEqual(out("status", "gkid-duchamp-6.1"), "needs-verification for duchamp (build: reported, boot: unverified)")
        self.assertEqual(out("status", "aosp-ack-6.6", "marble"), "not assessed for marble")


class Matrix(unittest.TestCase):
    def matrix_versions(self, **env):
        import os
        full = dict(os.environ, BUILD_KSU="true", **env)
        r = subprocess.run([sys.executable, str(ROOT / "py" / "generate-matrix.py")], capture_output=True, text=True, env=full)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = next(l for l in r.stdout.splitlines() if l.startswith("matrix="))
        return sorted({e["name"].split("-")[0] for e in json.loads(line[7:])["include"]})

    def test_all_uses_every_registry_line_when_not_gated(self):
        self.assertEqual(self.matrix_versions(KERNEL_VERSION="All"), ["5.10", "5.15", "6.1", "6.12", "6.6"])

    def test_all_is_limited_to_the_lines_the_gate_allowed(self):
        self.assertEqual(self.matrix_versions(KERNEL_VERSION="All", ALLOWED_LINES="6.1"), ["6.1"])

    def test_a_line_the_gate_did_not_allow_is_refused(self):
        import os
        r = subprocess.run([sys.executable, str(ROOT / "py" / "generate-matrix.py")], capture_output=True, text=True,
                           env=dict(os.environ, BUILD_KSU="true", KERNEL_VERSION="5.10", ALLOWED_LINES="6.1"))
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
