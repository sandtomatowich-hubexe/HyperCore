#!/usr/bin/env python3
"""Tests for Linux version selectors (latest-stable, latest-lts, exact version, commit) and the kernel.org lookup.

The release list below is a FIXTURE written in the format kernel.org documents for releases.json. Its version numbers
are made up for the tests and say nothing about the real releases. Reading the real list needs a real run.
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py"))

import kernel_versions as kv  # noqa: E402
import kernelver  # noqa: E402

SCRIPT = ROOT / "py" / "kernel_versions.py"


def entry(version, moniker, iseol=False, date="2026-01-01"):
    return {"iseol": iseol, "version": version, "moniker": moniker, "released": {"isodate": date}}


FIXTURE = {
    "latest_stable": {"version": "6.17.4"},
    "releases": [
        entry("6.18-rc2", "mainline"),
        entry("6.17.4", "stable"),
        entry("6.16.12", "stable", iseol=True),
        entry("6.12.54", "longterm", date="2026-02-02"),
        entry("6.6.113", "longterm"),
        entry("6.1.158", "longterm", date="2026-03-03"),
        entry("5.15.195", "longterm"),
        entry("5.10.245", "longterm"),
        entry("next-20261029", "linux-next"),
    ],
}


class Selectors(unittest.TestCase):
    def test_classification(self):
        cases = {"": "none", "  ": "none", "6.1.180": "version", "0123abc": "commit", "A" * 7: "commit",
                 "latest-lts": "latest-lts", "latest-stable": "latest-stable", "6.1": "invalid", "latest": "invalid",
                 "6.1.x": "invalid", "v6.1.180": "invalid", "xyz": "invalid"}
        for text, expected in cases.items():
            self.assertEqual(kv.classify(text), expected, repr(text))

    def test_spelling_variants_of_the_keywords(self):
        for text in ("Latest LTS", "latest_lts", " LATEST-LTS ", "latest-lts"):
            self.assertEqual(kv.classify(text), "latest-lts", repr(text))
        self.assertEqual(kv.classify("Latest Stable"), "latest-stable")

    def test_a_series_first_release_is_padded(self):
        self.assertEqual(kv.full_version("6.12"), "6.12.0")
        self.assertEqual(kv.full_version("6.1.158"), "6.1.158")
        self.assertIsNone(kv.full_version("6.18-rc2"))
        self.assertIsNone(kv.full_version("next-20261029"))

    def test_the_validator_accepts_keywords_for_one_line_only(self):
        for text in ("latest-lts", "latest-stable", "Latest LTS"):
            self.assertEqual(kernelver.validate("6.1", text), "", text)
            self.assertIn("single kernel line", kernelver.validate("All", text))

    def test_unknown_words_are_refused_with_the_accepted_forms(self):
        message = kernelver.validate("6.1", "latest")
        self.assertIn("latest-stable or latest-lts", message)


class LatestForLine(unittest.TestCase):
    def test_latest_lts_is_the_newest_release_of_the_selected_line(self):
        result = kv.resolve("latest-lts", "6.1", FIXTURE)
        self.assertEqual((result["version"], result["moniker"], result["released"]), ("6.1.158", "longterm", "2026-03-03"))

    def test_latest_stable_is_also_scoped_to_the_selected_line(self):
        self.assertEqual(kv.resolve("latest-stable", "6.6", FIXTURE)["version"], "6.6.113")
        self.assertEqual(kv.resolve("latest-stable", "6.1", FIXTURE)["version"], "6.1.158")

    def test_it_never_answers_with_another_line(self):
        for line in ("6.1", "6.6", "6.12", "5.15", "5.10"):
            for kind in ("latest-lts", "latest-stable"):
                self.assertTrue(kv.resolve(kind, line, FIXTURE)["version"].startswith(line + "."), (kind, line))

    def test_latest_lts_refuses_a_line_that_is_not_longterm(self):
        with self.assertRaises(kv.VersionError) as ctx:
            kv.resolve("latest-lts", "6.17", FIXTURE)
        self.assertIn("not longterm", str(ctx.exception))
        self.assertEqual(kv.resolve("latest-stable", "6.17", FIXTURE)["version"], "6.17.4")

    def test_a_line_kernel_org_does_not_list_is_an_error_not_a_guess(self):
        with self.assertRaises(kv.VersionError) as ctx:
            kv.resolve("latest-stable", "6.2", FIXTURE)
        self.assertIn("does not list a current stable or longterm release of the 6.2 line", str(ctx.exception))

    def test_a_line_that_is_only_listed_as_mainline_is_not_a_release(self):
        with self.assertRaises(kv.VersionError) as ctx:
            kv.resolve("latest-stable", "6.18", FIXTURE)
        self.assertIn("mainline", str(ctx.exception))

    def test_an_end_of_life_line_is_reported_as_such(self):
        self.assertTrue(kv.resolve("latest-stable", "6.16", FIXTURE)["iseol"])

    def test_the_highest_entry_wins_when_a_line_is_listed_twice(self):
        data = {"releases": [entry("6.1.150", "longterm"), entry("6.1.158", "longterm"), entry("6.1.99", "longterm")]}
        self.assertEqual(kv.resolve("latest-lts", "6.1", data)["version"], "6.1.158")

    def test_first_release_of_a_series_matches_the_makefile_spelling(self):
        data = {"releases": [entry("6.13", "stable")]}
        self.assertEqual(kv.resolve("latest-stable", "6.13", data)["version"], "6.13.0")

    def test_a_non_keyword_is_refused(self):
        with self.assertRaises(kv.VersionError):
            kv.resolve("6.1.158", "6.1", FIXTURE)


class Handler(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        status, body = self.routes.get(self.path, (404, b"not found"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class OverHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.routes = {
            "/releases.json": (200, json.dumps(FIXTURE).encode()),
            "/garbage.json": (200, b"this is not json"),
            "/wrong-shape.json": (200, json.dumps({"hello": "world"}).encode()),
            "/error.json": (500, b"boom"),
        }
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_reads_the_list_over_http(self):
        data = kv.load_releases(f"{self.base}/releases.json", attempts=1)
        self.assertEqual(kv.resolve("latest-lts", "6.1", data)["version"], "6.1.158")

    def test_every_failure_says_nothing_was_substituted(self):
        for path in ("/missing.json", "/garbage.json", "/wrong-shape.json", "/error.json"):
            with self.assertRaises(kv.VersionError, msg=path) as ctx:
                kv.load_releases(f"{self.base}{path}", attempts=1)
            self.assertIn("Nothing was substituted", str(ctx.exception), path)
            self.assertIn("Pin an exact version", str(ctx.exception), path)

    def test_an_unreachable_server_fails_cleanly(self):
        with self.assertRaises(kv.VersionError) as ctx:
            kv.load_releases("http://127.0.0.1:9/releases.json", timeout=2, attempts=1)
        self.assertIn("Could not read the kernel.org release list", str(ctx.exception))

    def test_the_address_can_be_replaced_by_the_environment(self):
        os.environ["KERNEL_RELEASES_URL"] = f"{self.base}/releases.json"
        self.addCleanup(os.environ.pop, "KERNEL_RELEASES_URL", None)
        self.assertEqual(kv.resolve("latest-stable", "6.6", attempts=1)["version"], "6.6.113")

    def test_the_default_address_is_kernel_orgs_official_list(self):
        self.assertEqual(kv.RELEASES_URL, "https://www.kernel.org/releases.json")


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.releases = Path(self.tmp) / "releases.json"
        self.releases.write_text(json.dumps(FIXTURE))

    def run_cli(self, *args, url=None):
        env = dict(os.environ, KERNEL_RELEASES_URL=url or f"file://{self.releases}")
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def test_classify(self):
        self.assertEqual(self.run_cli("classify", "Latest LTS").stdout.strip(), "latest-lts")
        self.assertEqual(self.run_cli("classify", "6.1.180").stdout.strip(), "version")

    def test_resolve_prints_only_the_version_on_stdout(self):
        r = self.run_cli("resolve", "--line", "6.1", "--pin", "latest-lts", "--attempts", "1")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "6.1.158"))
        self.assertIn("kernel.org lists Linux 6.1.158 (longterm, released 2026-03-03)", r.stderr)

    def test_resolve_failure_exits_1_with_nothing_on_stdout(self):
        r = self.run_cli("resolve", "--line", "6.17", "--pin", "latest-lts", "--attempts", "1")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("[-] kernel.org lists 6.17.4 as 'stable', not longterm", r.stderr)

    def test_unreadable_list_exits_1(self):
        r = self.run_cli("resolve", "--line", "6.1", "--pin", "latest-lts", "--attempts", "1", url="file:///nonexistent.json")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("Nothing was substituted", r.stderr)


if __name__ == "__main__":
    unittest.main()
