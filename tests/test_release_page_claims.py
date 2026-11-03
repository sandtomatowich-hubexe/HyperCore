#!/usr/bin/env python3
"""The release page must not claim that a GKI kernel works on every device that shares its Linux version and Android
release. Compatibility is recorded per device in the source registry, and the release page has to send readers there.

The release body is produced by the real generator (.github/scripts/prepare_release.py) from fixture build files.
"""

import importlib.util
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "prepare_release.py"

spec = importlib.util.spec_from_file_location("prepare_release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

REPO = "example-owner/example-repo"

# Phrases that would put the blanket claim back, in any wording close to the original
BLANKET_CLAIMS = [
    r"supports\s+\*{0,2}all\*{0,2}\s+devices",
    r"all\*{0,2}\s+devices\s+that\s+shipped",
    r"every\s+device",
    r"works?\s+on\s+(all|every)\s+devices?",
    r"shipped\s+with\s+the\s+matching\s+linux",
]


def release_body():
    work = Path(tempfile.mkdtemp())
    previous = os.getcwd()
    try:
        (work / "release-artifacts").mkdir()
        (work / "release-artifacts" / "build-env-1.txt").write_text(
            "\n".join(["RELEASE_REPO=" + REPO, "RELEASE=v26.11.03-r1", "RELEASE_NAME=Fixture release", "KERNEL_NAME=GKI-r1",
                       "KERNEL_VERSION=6.1", "ANDROID_RELEASE=14", "LINUX_VERSION=6.1.177", "COMPILER_STRING=clang",
                       "SUSFS_VERSION=v2", "KERNEL_SOURCE_REPO=owner/repo", "KERNEL_SOURCE_BRANCH=main"]) + "\n", encoding="utf-8")
        os.chdir(work)
        for name in ("NH_INPUT", "NM_INPUT", "DROIDSPACES_INPUT", "LTO_INPUT", "TEST_INPUT"):
            os.environ.pop(name, None)
        return release.build_release_body()[0]
    finally:
        os.chdir(previous)
        shutil.rmtree(work, ignore_errors=True)


class ReleasePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.body = release_body()
        cls.callout = next(line for line in cls.body.splitlines() if line.startswith("> These are **GKI** kernels"))

    def test_the_blanket_claim_is_gone(self):
        for pattern in BLANKET_CLAIMS:
            self.assertIsNone(re.search(pattern, self.body, re.I), f"the release page still says something like: {pattern}")

    def test_it_says_gki_compatibility_does_not_guarantee_device_compatibility(self):
        self.assertIn("**GKI compatibility does not guarantee device compatibility.**", self.callout)

    def test_it_tells_readers_to_check_the_device_specific_verification_status(self):
        self.assertIn("device-specific verification status", self.callout)
        self.assertIn("before you flash", self.callout)

    def test_it_points_to_the_registry_documentation_of_this_repository(self):
        self.assertIn(f"https://github.com/{REPO}/blob/HEAD/SOURCES.md#duchamp-compatibility", self.callout)

    def test_it_still_says_these_are_gki_kernels(self):
        self.assertTrue(self.callout.startswith("> These are **GKI** kernels, not custom kernels."))

    def test_the_rest_of_the_page_is_intact(self):
        for expected in ("[!Important]", "## 🧭 Which variant should I flash?", "| Variant | Root | SuSFS | LTO | Compat |",
                         "**Not sure? Use a `Compat` variant first**", "## Contents", "- Linux version: 6.1.177",
                         "Kernel source (origin not recorded)"):
            self.assertIn(expected, self.body, expected)


class WhatTheTextPointsAt(unittest.TestCase):
    def test_the_documentation_section_the_link_points_to_exists(self):
        headings = [re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
                    for h in re.findall(r"^#+\s+(.+)$", (ROOT / "SOURCES.md").read_text(encoding="utf-8"), re.M)]
        self.assertIn("duchamp-compatibility", headings)

    def test_that_section_records_status_per_device(self):
        text = (ROOT / "SOURCES.md").read_text(encoding="utf-8")
        section = text[text.index("## duchamp compatibility"):]
        section = section[:section.index("\n## ", 5)]
        for word in ("needs-verification", "unsupported", "Build", "Boot"):
            self.assertIn(word, section)

    def test_the_build_summary_really_has_the_device_status_line_the_page_mentions(self):
        self.assertIn("| Device status |", (ROOT / "functions-extra.sh").read_text(encoding="utf-8"))
        self.assertIn("*Device status*", SCRIPT.read_text(encoding="utf-8"))

    def test_no_user_facing_text_makes_the_blanket_claim(self):
        files = [SCRIPT, ROOT / ".github/scripts/telegram_release.py", ROOT / "README.md", ROOT / "SOURCES.md",
                 ROOT / "PROBE.md", ROOT / "MANAGED_SOURCES.md", ROOT / "functions-extra.sh", ROOT / "build.sh"]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for pattern in BLANKET_CLAIMS:
                self.assertIsNone(re.search(pattern, text, re.I), f"{path.name} contains the blanket claim: {pattern}")


if __name__ == "__main__":
    unittest.main()
