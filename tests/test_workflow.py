#!/usr/bin/env python3
"""Tests for the workflow: input compatibility, job graph, and the CI fixes. Run: python3 -m unittest discover -s tests -v"""

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"

spec = importlib.util.spec_from_file_location("graph", ROOT / ".github" / "scripts" / "validate_workflow_graph.py")
graph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(graph)

# Every input of the workflow form as it was before the source registry. `gh workflow run -f KEY=value` keeps
# working only while all of these exist with the same type and default (options may be added, never removed).
ORIGINAL_INPUTS = json.loads(r"""{"TODO": {"type": "choice", "default": "kernel", "options": ["kernel", "defconfig"]}, "TEST": {"type": "choice", "default": "no", "options": ["no", "yes"]}, "LTO": {"type": "choice", "default": "fullLTO", "options": ["noneLTO", "thinLTO", "fullLTO"]}, "KERNEL_VERSION": {"type": "choice", "default": "6.1", "options": ["All", "5.10", "5.15", "6.1", "6.6", "6.12"]}, "KERNEL_EXACT_VERSION": {"type": "string", "default": ""}, "BUILD_VANILLA": {"type": "boolean", "default": true}, "BUILD_KSU": {"type": "boolean", "default": true}, "BUILD_KSU_SUSFS": {"type": "boolean", "default": true}, "CLEAN_LTO_CACHE": {"type": "boolean", "default": true}, "DROIDSPACES": {"type": "boolean", "default": true}, "NH": {"type": "boolean", "default": true}, "NM": {"type": "boolean", "default": false}, "BUILD_KSUN_SUSFS": {"type": "boolean", "default": false}, "BUILD_FINA_SUSFS": {"type": "boolean", "default": false}, "BUILD_SKSU_SUSFS": {"type": "boolean", "default": false}, "BUILD_KOWSU": {"type": "boolean", "default": false}, "BUILD_RKSU_SUSFS": {"type": "boolean", "default": false}, "BUILD_MAMBO_SUSFS": {"type": "boolean", "default": false}, "MM": {"type": "boolean", "default": true}, "ZSTD": {"type": "boolean", "default": false}, "LZ4KD": {"type": "boolean", "default": false}, "REKERNEL": {"type": "boolean", "default": false}}""")


def load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def form(data):
    return data[True]["workflow_dispatch"]["inputs"]


class InputCompatibility(unittest.TestCase):
    def test_every_original_input_still_exists_with_the_same_type_and_default(self):
        current = form(load())
        for key, old in ORIGINAL_INPUTS.items():
            self.assertIn(key, current, f"input {key} was removed")
            self.assertEqual(current[key].get("type", "string"), old["type"], key)
            self.assertEqual(current[key].get("default"), old["default"], key)
            for option in old.get("options", []):
                self.assertIn(option, current[key]["options"], f"{key}: option {option!r} was removed")

    def test_only_the_documented_inputs_were_added(self):
        added = set(form(load())) - set(ORIGINAL_INPUTS)
        self.assertEqual(added, {"KERNEL_SOURCE", "ALLOW_UNVERIFIED"})

    def test_new_inputs_default_to_todays_behavior(self):
        current = form(load())
        self.assertEqual(current["KERNEL_SOURCE"]["default"], "")
        self.assertIs(current["ALLOW_UNVERIFIED"]["default"], False)

    def test_form_stays_within_githubs_limit(self):
        self.assertLessEqual(len(form(load())), 25)

    def test_labels_are_short_and_grouped(self):
        for key, spec in form(load()).items():
            self.assertLessEqual(len(spec["description"]), 55, key)
            self.assertRegex(spec["description"], r"^\[(Run|Kernel|Variant|Feature|zram)\] ", key)


class GraphValidator(unittest.TestCase):
    def problems(self, mutate=None, root=ROOT):
        data = load()
        if mutate:
            mutate(data)
        return graph.check_workflow(data, "build.yml", root)

    def assertFlagged(self, mutate, fragment):
        found = self.problems(mutate)
        self.assertTrue(any(fragment in p for p in found), f"expected {fragment!r} in {found}")

    def test_the_real_workflows_pass(self):
        self.assertEqual(self.problems(), [])

    def test_need_on_a_missing_job(self):
        self.assertFlagged(lambda d: d["jobs"]["build"].update(needs=["prepare", "ghost"]), "needs 'ghost', which does not exist")

    def test_circular_needs(self):
        self.assertFlagged(lambda d: d["jobs"]["prepare"].update(needs=["build"]), "in a circle")

    def test_output_that_does_not_exist(self):
        def mutate(d):
            d["jobs"]["build"]["strategy"]["matrix"] = "${{ fromJson(needs.prepare.outputs.nothing) }}"
        self.assertFlagged(mutate, "declares no output 'nothing'")

    def test_using_a_job_that_is_not_in_needs(self):
        def mutate(d):
            d["jobs"]["build"]["needs"] = ["prepare"]
            d["jobs"]["build"]["if"] = "${{ needs.ensure-cache-release.result == 'success' }}"
        self.assertFlagged(mutate, "without listing 'ensure-cache-release' in its needs")

    def test_step_id_that_does_not_exist(self):
        def mutate(d):
            d["jobs"]["prepare"]["steps"][-1]["env"]["X"] = "${{ steps.nope.outputs.lines }}"
        self.assertFlagged(mutate, "steps.nope")

    def test_undefined_input(self):
        def mutate(d):
            d["jobs"]["prepare"]["steps"][-1]["env"]["X"] = "${{ github.event.inputs.NOT_AN_INPUT }}"
        self.assertFlagged(mutate, "input 'NOT_AN_INPUT'")

    def test_more_than_25_inputs(self):
        def mutate(d):
            for i in range(3):
                form(d)[f"EXTRA_{i}"] = {"type": "string", "default": ""}
        self.assertFlagged(mutate, "has 27 inputs, GitHub allows 25")

    def test_invalid_defaults(self):
        self.assertFlagged(lambda d: form(d)["LTO"].update(default="superLTO"), "not one of its options")
        self.assertFlagged(lambda d: form(d)["NH"].update(default="yes"), "boolean but its default")

    def test_missing_local_action(self):
        def mutate(d):
            d["jobs"]["build"]["steps"].append({"uses": "./.github/actions/not-there"})
        self.assertFlagged(mutate, "local action ./.github/actions/not-there")


class CiFixes(unittest.TestCase):
    """The reliability fixes from the first round, pinned so they cannot silently regress."""

    def test_builds_wait_for_the_cache_release_but_do_not_depend_on_its_success(self):
        build = load()["jobs"]["build"]
        self.assertEqual(sorted(build["needs"]), ["ensure-cache-release", "prepare"])
        self.assertIn("!cancelled()", build["if"])
        self.assertIn("needs.prepare.result == 'success'", build["if"])

    def test_one_failed_variant_does_not_cancel_the_others(self):
        self.assertIs(load()["jobs"]["build"]["strategy"]["fail-fast"], False)

    def test_release_and_upload_are_skipped_when_no_build_ran(self):
        jobs = load()["jobs"]
        for name in ("release", "upload-all"):
            self.assertIn("needs.build.result != 'skipped'", jobs[name]["if"], name)

    def test_release_is_only_created_when_a_zip_exists(self):
        steps = load()["jobs"]["release"]["steps"]
        upload = next(s for s in steps if s.get("name", "").startswith("Upload All Builds to release repo"))
        self.assertEqual(upload["if"], "steps.sums.outputs.has_zips == 'true'")

    def test_matrix_is_limited_to_the_lines_the_gate_allowed(self):
        steps = load()["jobs"]["prepare"]["steps"]
        matrix = next(s for s in steps if s.get("id") == "set-matrix")
        self.assertEqual(matrix["env"]["ALLOWED_LINES"], "${{ steps.gate.outputs.lines }}")

    def test_kernel_source_variables_are_defined_once_at_workflow_level(self):
        data = load()
        for key in ("KERNEL_REPO", "KERNEL_BRANCH", "KERNEL_BRANCH_NH", "KERNEL_REPO_6_1", "KERNEL_BRANCH_6_12"):
            self.assertIn(key, data["env"], key)
        build_step = next(s for s in data["jobs"]["build"]["steps"] if s.get("name") == "Run Build Script")
        for key in ("KERNEL_REPO", "KERNEL_BRANCH", "KERNEL_REPO_6_1"):
            self.assertNotIn(key, build_step["env"], f"{key} would shadow the workflow-level value")
        for key in ("KERNEL_SOURCE", "KERNEL_EXACT_VERSION", "ALLOW_UNVERIFIED", "HYPERCORE_DEVICE"):
            self.assertIn(key, build_step["env"], key)


if __name__ == "__main__":
    unittest.main()
