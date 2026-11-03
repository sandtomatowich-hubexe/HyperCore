#!/usr/bin/env python3
"""Tests for the Telegram notification: a source HyperCore manages and the upstream project it comes from must never
be presented as the same thing. The managed fork in these tests is a FIXTURE; no such repository exists yet."""

import importlib.util
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("CHAT_ID", "1")

spec = importlib.util.spec_from_file_location("telegram_release", ROOT / ".github" / "scripts" / "telegram_release.py")
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)

UPSTREAM = "https://github.com/ahmed-alnassif/GKI-Duchamp-6.1"
SHA = "a" * 40


def _content(cell):
    """A cell holds plain text or a button chip."""
    inner = cell["text"]
    return inner["button"] if isinstance(inner, dict) else {"text": inner}


def rows(meta):
    """The first table as {label: text-or-chip-label}."""
    table = tg.build_blocks(meta, "x.zip", "kernelzip")[0]["cells"]
    return {left["text"]: _content(right)["text"] for left, right in table}


def urls(meta):
    table = tg.build_blocks(meta, "x.zip", "kernelzip")[0]["cells"]
    return {left["text"]: _content(right).get("url") for left, right in table}


EXTERNAL = {"variant": "KSU", "linux": "6.1.177", "source_repo": "ahmed-alnassif/GKI-Duchamp-6.1", "source_branch": "GKID-6.1",
            "commit": SHA, "provenance": {"role": "external", "kind": "device", "upstream_repo": UPSTREAM, "upstream_branch": "GKID-6.1"}}
MANAGED = {"variant": "KSU", "linux": "6.1.177", "source_repo": "example-org/fork-of-duchamp-6.1", "source_branch": "hypercore/6.1",
           "commit": SHA, "provenance": {"role": "managed", "kind": "device", "upstream_repo": UPSTREAM, "upstream_branch": "GKID-6.1"}}


class Blocks(unittest.TestCase):
    def test_an_external_source_is_labelled_as_the_upstream_project(self):
        table = rows(EXTERNAL)
        self.assertIn("Source (upstream)", table)
        self.assertNotIn("Upstream", table)

    def test_a_managed_source_is_labelled_as_hypercore_and_names_its_upstream(self):
        table = rows(MANAGED)
        self.assertEqual(table["Source (HyperCore-managed)"], "fork-of-duchamp-6.1")
        self.assertEqual(table["Branch"], "hypercore/6.1")
        self.assertEqual(table["Upstream"], "GKI-Duchamp-6.1")
        self.assertEqual(table["Upstream branch"], "GKID-6.1")
        link = urls(MANAGED)
        self.assertEqual(link["Source (HyperCore-managed)"], "https://github.com/example-org/fork-of-duchamp-6.1")
        self.assertEqual(link["Upstream"], UPSTREAM)
        self.assertEqual(link["Upstream branch"], f"{UPSTREAM}/tree/GKID-6.1")

    def test_the_commit_is_shown_short_and_links_to_the_full_one(self):
        self.assertEqual(rows(MANAGED)["Commit"], SHA[:12])
        self.assertEqual(urls(MANAGED)["Commit"], f"https://github.com/example-org/fork-of-duchamp-6.1/commit/{SHA}")

    def test_commit_links_for_googlesource_use_its_own_form(self):
        meta = dict(EXTERNAL, source_repo="https://android.googlesource.com/kernel/common", source_branch="android15-6.6-lts")
        self.assertEqual(urls(meta)["Commit"], f"https://android.googlesource.com/kernel/common/+/{SHA}")

    def test_a_source_replaced_by_a_variable_is_labelled_custom_and_hides_the_upstream_claim(self):
        meta = dict(MANAGED, source_overridden=True)
        table = rows(meta)
        self.assertIn("Source (custom)", table)
        self.assertNotIn("Upstream", table)

    def test_old_metadata_without_provenance_looks_exactly_as_before(self):
        meta = {"variant": "KSU", "linux": "6.1.177", "source_repo": "owner/repo", "source_branch": "main"}
        table = rows(meta)
        self.assertEqual(list(table)[:3], ["Kernel", "Source", "Branch"])
        self.assertNotIn("Commit", table)
        self.assertNotIn("Upstream", table)

    def test_fallback_caption_tells_the_two_apart_too(self):
        managed = tg.fallback_caption(MANAGED)
        self.assertIn("Source (HyperCore-managed): example-org/fork-of-duchamp-6.1 @ hypercore/6.1", managed)
        self.assertIn(f"Upstream: {UPSTREAM} @ GKID-6.1", managed)
        external = tg.fallback_caption(EXTERNAL)
        self.assertIn("Source (upstream): ahmed-alnassif/GKI-Duchamp-6.1 @ GKID-6.1", external)
        self.assertNotIn("Upstream:", external)
        self.assertLessEqual(len(managed), 1000)


if __name__ == "__main__":
    unittest.main()
