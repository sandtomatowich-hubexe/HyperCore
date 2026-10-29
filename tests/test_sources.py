#!/usr/bin/env python3
"""Tests for the source registry: loading, schema validation, source selection and the command line.

Run with:  python3 -m unittest discover -s tests -v
"""

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

REGISTRY = ROOT / "sources.json"
SCRIPT = ROOT / "py" / "sources.py"

# What the build selected before the registry existed (the old tables in functions.sh). Written out here on
# purpose: editing sources.json must not change a default without someone updating this test.
LEGACY_LINES = {
    "5.10": {"android_release": "12", "kmi": "android12-5.10", "susfs_branch": "gki-android12-5.10"},
    "5.15": {"android_release": "13", "kmi": "android13-5.15", "susfs_branch": "gki-android13-5.15"},
    "6.1": {"android_release": "14", "kmi": "android14-6.1", "susfs_branch": "gki-android14-6.1"},
    "6.6": {"android_release": "15", "kmi": "android15-6.6", "susfs_branch": "gki-android15-6.6"},
    "6.12": {"android_release": "16", "kmi": "android16-6.12", "susfs_branch": "gki-android16-6.12"},
}
ACK = "https://android.googlesource.com/kernel/common"
LEGACY_DEFAULTS = {
    "5.10": (ACK, "android12-5.10-lts"),
    "5.15": (ACK, "android13-5.15-lts"),
    "6.1": ("https://github.com/ahmed-alnassif/GKI-Duchamp-6.1", "GKID-6.1"),
    "6.6": (ACK, "android15-6.6-lts"),
    "6.12": (ACK, "android16-6.12-lts"),
}


def base():
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def problems_for(mutate):
    data = base()
    mutate(data)
    return sources.check_registry(data)


class RealRegistry(unittest.TestCase):
    def test_loads_and_validates(self):
        registry = sources.load(REGISTRY)
        self.assertEqual(registry.lines, ["5.10", "5.15", "6.1", "6.6", "6.12"])

    def test_line_fields_match_the_legacy_tables(self):
        registry = sources.load(REGISTRY)
        for line, expected in LEGACY_LINES.items():
            for field, value in expected.items():
                self.assertEqual(registry.line_field(line, field), value, f"{line} {field}")
            self.assertEqual(registry.line_field(line, "defconfig"), "gki_defconfig")

    def test_default_sources_are_unchanged(self):
        registry = sources.load(REGISTRY)
        for line, (repo, branch) in LEGACY_DEFAULTS.items():
            chosen = registry.select(line)
            self.assertEqual((chosen["repo"], chosen["branch"]), (repo, branch), line)

    def test_netHunter_flag_only_changes_the_6_1_branch(self):
        registry = sources.load(REGISTRY)
        nh = registry.select("6.1", {"NH": "true"})
        self.assertEqual(nh["branch"], "GKID-NH")
        self.assertEqual(nh["variant_flag"], "NH")
        self.assertEqual(registry.select("6.1", {"NH": "false"})["branch"], "GKID-6.1")
        self.assertEqual(registry.select("6.1")["variant_flag"], "")
        for line in ("5.10", "5.15", "6.6", "6.12"):
            self.assertEqual(registry.select(line, {"NH": "true"})["branch"], LEGACY_DEFAULTS[line][1], line)

    def test_unknown_line_is_an_actionable_error(self):
        registry = sources.load(REGISTRY)
        with self.assertRaises(sources.RegistryError) as ctx:
            registry.select("7.0")
        message = ctx.exception.problems[0]
        self.assertIn("'7.0'", message)
        self.assertIn("5.10, 5.15, 6.1, 6.6, 6.12", message)

    def test_legacy_override_line_is_6_1(self):
        self.assertEqual(sources.load(REGISTRY).legacy_override_line, "6.1")

    def test_nothing_is_confirmed_without_boot_evidence(self):
        """Policy pin: duchamp compatibility is only ever 'confirmed' with boot evidence on the device."""
        data = base()
        for src_id, src in data["sources"].items():
            for dev_id, a in src.get("devices", {}).items():
                if a["compatibility"] == "confirmed":
                    self.assertEqual(a["boot_verification"]["state"], "verified", f"{src_id}/{dev_id}")
                    self.assertTrue(a["boot_verification"].get("evidence"), f"{src_id}/{dev_id}")

    def test_default_duchamp_source_is_not_boot_verified(self):
        a = base()["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]
        self.assertEqual(a["boot_verification"]["state"], "unverified")
        self.assertNotEqual(a["compatibility"], "confirmed")
        # the build result is recorded separately from the boot result
        self.assertEqual(a["build_verification"]["state"], "reported")

    def test_other_kernel_lines_are_unsupported_for_duchamp(self):
        data = base()
        for line in ("5.10", "5.15", "6.6", "6.12"):
            a = data["sources"][f"aosp-ack-{line}"]["devices"]["duchamp"]
            self.assertEqual(a["compatibility"], "unsupported", line)
            self.assertEqual(a["basis"], "reasoned", line)


class InvalidRegistries(unittest.TestCase):
    def assertProblem(self, mutate, *fragments):
        problems = problems_for(mutate)
        text = "\n".join(problems)
        self.assertTrue(problems, "expected the registry to be rejected")
        for fragment in fragments:
            self.assertIn(fragment, text)
        return problems

    def test_valid_baseline_has_no_problems(self):
        self.assertEqual(problems_for(lambda d: None), [])

    def test_wrong_schema_version(self):
        self.assertProblem(lambda d: d.update(schema_version=2), "schema_version")

    def test_missing_required_key_names_the_source(self):
        self.assertProblem(lambda d: d["sources"]["gkid-duchamp-6.1"].pop("repo"),
                           "sources.gkid-duchamp-6.1", "missing required key 'repo'")

    def test_typo_in_a_key_gets_a_suggestion(self):
        def mutate(d):
            d["sources"]["gkid-duchamp-6.1"]["brnach"] = d["sources"]["gkid-duchamp-6.1"].pop("branch")
        self.assertProblem(mutate, "unknown key (did you mean 'branch'?)")

    def test_repo_must_be_https(self):
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(repo="git@github.com:x/y.git"),
                           "sources.aosp-ack-6.6.repo", "https://")
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(repo="https://exa mple.com/x"),
                           "without spaces")

    def test_branch_characters_are_checked(self):
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(branch="bad branch!"),
                           "sources.aosp-ack-6.6.branch", "bad branch!")

    def test_source_on_an_unconfigured_line(self):
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(line="6.7"),
                           "sources.aosp-ack-6.6.line", "'6.7' is not a configured line")

    def test_line_without_a_default_source(self):
        self.assertProblem(lambda d: d["sources"]["aosp-ack-6.6"].update(default_for_line=False),
                           "lines.6.6", "no default source")

    def test_line_with_two_default_sources(self):
        def mutate(d):
            extra = copy.deepcopy(d["sources"]["aosp-ack-6.6"])
            extra["branch"] = "android15-6.6-2025-01"
            d["sources"]["aosp-ack-6.6-pinned"] = extra
        self.assertProblem(mutate, "lines.6.6", "several default sources")

    def test_bad_kmi_and_defconfig(self):
        self.assertProblem(lambda d: d["lines"]["6.1"].update(kmi="14-6.1"), "lines.6.1.kmi")
        self.assertProblem(lambda d: d["lines"]["6.1"].update(defconfig="gki config"), "lines.6.1.defconfig")

    def test_legacy_override_line_must_exist(self):
        self.assertProblem(lambda d: d.update(legacy_override_line="9.9"), "legacy_override_line")

    def test_branch_variant_needs_all_fields_and_unique_flags(self):
        self.assertProblem(lambda d: d["sources"]["gkid-duchamp-6.1"]["branch_variants"][0].pop("branch"),
                           "branch_variants[0]", "missing required key 'branch'")

        def duplicate(d):
            v = d["sources"]["gkid-duchamp-6.1"]["branch_variants"]
            v.append(copy.deepcopy(v[0]))
        self.assertProblem(duplicate, "flag 'NH' is already used")

    def test_unknown_compatibility_value(self):
        def mutate(d):
            d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]["compatibility"] = "compatible"
        self.assertProblem(mutate, "'compatible' is not one of: confirmed, needs-verification, unsupported")

    def test_confirmed_requires_boot_evidence(self):
        def mutate(d):
            d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]["compatibility"] = "confirmed"
        self.assertProblem(mutate, "'confirmed' requires boot_verification.state 'verified'",
                           "A successful build alone is not enough")

    def test_a_successful_build_does_not_count_as_boot_evidence(self):
        def mutate(d):
            a = d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]
            a["compatibility"] = "confirmed"
            a["build_verification"] = {"state": "verified", "evidence": "CI log"}
        self.assertProblem(mutate, "'confirmed' requires boot_verification")

    def test_confirmed_is_accepted_with_boot_evidence(self):
        def mutate(d):
            a = d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]
            a["compatibility"] = "confirmed"
            a["build_verification"] = {"state": "verified", "evidence": "CI log"}
            a["boot_verification"] = {"state": "verified", "evidence": "Booted on a duchamp, dmesg attached"}
        self.assertEqual(problems_for(mutate), [])

    def test_reported_or_verified_state_needs_evidence(self):
        def mutate(d):
            d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]["build_verification"] = {"state": "reported"}
        self.assertProblem(mutate, "state 'reported' needs a non-empty 'evidence'")

    def test_unsupported_needs_reason_and_basis(self):
        def mutate(d):
            a = d["sources"]["aosp-ack-6.6"]["devices"]["duchamp"]
            a.pop("reason")
            a.pop("basis")
        self.assertProblem(mutate, "'unsupported' needs a non-empty 'reason'", "basis")

    def test_basis_is_only_for_unsupported(self):
        def mutate(d):
            d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"]["basis"] = "tested"
        self.assertProblem(mutate, "only used together with compatibility 'unsupported'")

    def test_device_cannot_be_compatible_with_another_kernel_line(self):
        def mutate(d):
            a = d["sources"]["aosp-ack-6.6"]["devices"]["duchamp"]
            a["compatibility"] = "needs-verification"
        self.assertProblem(mutate, "duchamp is a 6.1 device and this source is the 6.6 line")

    def test_unknown_device_in_an_assessment(self):
        def mutate(d):
            d["sources"]["gkid-duchamp-6.1"]["devices"]["marble"] = \
                copy.deepcopy(d["sources"]["gkid-duchamp-6.1"]["devices"]["duchamp"])
        self.assertProblem(mutate, "unknown device 'marble'")

    def test_every_problem_is_reported_not_just_the_first(self):
        def mutate(d):
            d["sources"]["aosp-ack-6.6"]["repo"] = "http://x"
            d["sources"]["aosp-ack-6.6"]["branch"] = "bad branch"
            d["lines"]["6.1"]["kmi"] = "nope"
        self.assertGreaterEqual(len(problems_for(mutate)), 3)


class FileLevelErrors(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink())
        return Path(handle.name)

    def test_missing_file(self):
        with self.assertRaises(sources.RegistryError) as ctx:
            sources.load(Path("/nonexistent/sources.json"))
        self.assertIn("file not found", ctx.exception.problems[0])

    def test_invalid_json_reports_line_and_column(self):
        path = self.write('{\n  "schema_version": 1,\n  "lines": {,}\n}')
        with self.assertRaises(sources.RegistryError) as ctx:
            sources.load(path)
        self.assertRegex(ctx.exception.problems[0], r"invalid JSON at line 3, column \d+")

    def test_duplicate_keys_are_rejected(self):
        path = self.write('{"schema_version": 1, "schema_version": 1}')
        with self.assertRaises(sources.RegistryError) as ctx:
            sources.load(path)
        self.assertIn("duplicate key 'schema_version'", ctx.exception.problems[0])

    def test_top_level_must_be_an_object(self):
        path = self.write("[]")
        with self.assertRaises(sources.RegistryError):
            sources.load(path)


class CommandLine(unittest.TestCase):
    def run_cli(self, *args, registry=None):
        cmd = [sys.executable, str(SCRIPT)]
        if registry:
            cmd += ["--registry", str(registry)]
        return subprocess.run(cmd + list(args), capture_output=True, text=True)

    def test_validate_succeeds(self):
        result = self.run_cli("validate")
        self.assertEqual(result.returncode, 0)
        self.assertIn("OK: 5 kernel lines, 5 sources", result.stdout)

    def test_select_prints_the_pipe_record(self):
        result = self.run_cli("select", "6.1", "--flag", "NH=true")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.strip(),
            "https://github.com/ahmed-alnassif/GKI-Duchamp-6.1|GKID-NH|android14-6.1|gkid-duchamp-6.1|"
            "built-in default (device source for 6.1)|built-in default (NetHunter build)|NH",
        )

    def test_unknown_line_exits_1_with_a_message(self):
        result = self.run_cli("select", "7.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No kernel source is configured for KERNEL_VERSION '7.0'", result.stderr)

    def test_broken_registry_exits_1_with_every_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "sources.json"
            data = base()
            data["sources"]["aosp-ack-6.6"]["repo"] = "ftp://nope"
            bad.write_text(json.dumps(data), encoding="utf-8")
            for args in (("validate",), ("lines",), ("select", "6.1"), ("line-field", "6.1", "kmi")):
                result = self.run_cli(*args, registry=bad)
                self.assertEqual(result.returncode, 1, args)
                self.assertIn("sources.aosp-ack-6.6.repo", result.stderr, args)
                self.assertEqual(result.stdout, "", args)


if __name__ == "__main__":
    unittest.main()
