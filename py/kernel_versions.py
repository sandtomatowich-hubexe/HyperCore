#!/usr/bin/env python3
"""Linux version selection: what the user asked for, and which release that means.

Usage:
    kernel_versions.py classify <selector>
    kernel_versions.py resolve --line <line> --pin <latest-stable|latest-lts> [--attempts N]
    kernel_versions.py latest  --line <line> [--attempts N]

A selector is what the KERNEL_EXACT_VERSION input holds:

    (empty)         the branch default of the source (today's behavior)
    6.1.180         one Linux release, searched in the history of the selected source
    0123abc...      a commit (7 to 40 hex characters) on the selected source branch
    latest-stable   the newest release of the selected kernel line that kernel.org lists as stable or longterm
    latest-lts      the newest release of the selected kernel line, which kernel.org must list as longterm

Both `latest-*` selectors only ever look inside the selected line. They are resolved from kernel.org's official
release list (https://www.kernel.org/releases.json, documented in the kernel.org FAQ), turned into an exact version, and
then found in the history of the selected source like any other exact version. If the source does not have that
version the build stops. Nothing is ever replaced by another version, and if kernel.org cannot be read the run fails.

kernel.org lists only the newest release of each series, so this module never claims to know older versions. Whether an
older version can be built is decided by the history of the source (py/resolve_kernel_commit.py).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

RELEASES_URL = "https://www.kernel.org/releases.json"
KEYWORDS = ("latest-stable", "latest-lts")
USER_AGENT = "HyperCore-CI (kernel version resolver)"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
RELEASE_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


class VersionError(Exception):
    pass


NOTHING_SUBSTITUTED = "Nothing was substituted. Pin an exact version or commit to build without it"


def normalize_keyword(text: str) -> str:
    """'Latest LTS', 'latest_lts' and 'latest-lts' are the same selector."""
    candidate = re.sub(r"[\s_]+", "-", (text or "").strip().lower())
    return candidate if candidate in KEYWORDS else ""


def classify(selector: str) -> str:
    """none | version | commit | latest-stable | latest-lts | invalid"""
    text = (selector or "").strip()
    if not text:
        return "none"
    keyword = normalize_keyword(text)
    if keyword:
        return keyword
    if VERSION_RE.match(text):
        return "version"
    if COMMIT_RE.match(text):
        return "commit"
    return "invalid"


def _vkey(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def full_version(release_version: str) -> Optional[str]:
    """kernel.org writes the first release of a series as '6.12'. The Makefile calls it 6.12.0."""
    if not RELEASE_VERSION_RE.match(release_version or ""):
        return None  # -rc releases and linux-next tags are not releases
    return release_version if release_version.count(".") == 2 else release_version + ".0"


def load_releases(url: Optional[str] = None, timeout: int = 20, attempts: int = 3) -> Dict[str, Any]:
    """Read kernel.org's release list. KERNEL_RELEASES_URL replaces the address (a mirror, or a file in the tests)."""
    url = url or os.environ.get("KERNEL_RELEASES_URL") or RELEASES_URL
    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(8_000_000)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("releases"), list):
                raise ValueError("it does not look like kernel.org's release list (there is no 'releases' list)")
            return data
        except (urllib.error.URLError, OSError, ValueError) as error:  # URLError, timeouts, HTTP errors and bad JSON
            last_error = str(getattr(error, "reason", error))
            if attempt < attempts:
                time.sleep(min(2 * attempt, 6))
    raise VersionError(f"Could not read the kernel.org release list at {url}: {last_error}. {NOTHING_SUBSTITUTED}")


def releases_of_line(data: Dict[str, Any], line: str) -> List[Dict[str, Any]]:
    found = []
    for entry in data.get("releases", []):
        version = full_version(str(entry.get("version", "")))
        if version and version.startswith(line + "."):
            found.append({
                "version": version,
                "moniker": entry.get("moniker", ""),
                "iseol": bool(entry.get("iseol", False)),
                "released": (entry.get("released") or {}).get("isodate", ""),
            })
    return sorted(found, key=lambda r: _vkey(r["version"]), reverse=True)


def monikers_of_line(data: Dict[str, Any], line: str) -> List[str]:
    """Every way kernel.org lists a line, including release candidates ('6.18-rc2' is mainline of the 6.18 line)."""
    found = set()
    for entry in data.get("releases", []):
        version = str(entry.get("version", ""))
        if version.startswith(line + ".") or version.startswith(line + "-") or version == line:
            found.add(entry.get("moniker", "") or "unknown")
    return sorted(found)


def latest_for_line(data: Dict[str, Any], line: str, longterm_only: bool = False) -> Dict[str, Any]:
    """The newest release of one kernel line that kernel.org lists as stable or longterm (longterm only for LTS)."""
    entries = releases_of_line(data, line)
    current = [e for e in entries if e["moniker"] in ("stable", "longterm")]
    if not current:
        seen = ", ".join(monikers_of_line(data, line)) or "nothing"
        raise VersionError(
            f"kernel.org does not list a current stable or longterm release of the {line} line (it lists {seen} for it). "
            "The line may be end-of-life or not released yet"
        )
    best = current[0]
    if longterm_only and best["moniker"] != "longterm":
        raise VersionError(
            f"kernel.org lists {best['version']} as '{best['moniker']}', not longterm. "
            f"The {line} line is not an LTS series, so latest-lts does not apply. Use latest-stable or an exact version"
        )
    return best


def resolve(selector: str, line: str, data: Optional[Dict[str, Any]] = None, attempts: int = 3) -> Dict[str, Any]:
    """Turn latest-stable / latest-lts into one exact version of `line`."""
    kind = classify(selector)
    if kind not in KEYWORDS:
        raise VersionError(f"'{selector}' is not a latest-stable or latest-lts selector")
    data = data if data is not None else load_releases(attempts=attempts)
    best = latest_for_line(data, line, longterm_only=(kind == "latest-lts"))
    return {"requested": kind, "line": line, "version": best["version"], "moniker": best["moniker"],
            "iseol": best["iseol"], "released": best["released"], "source": "kernel.org releases.json"}


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="kernel_versions.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_classify = sub.add_parser("classify")
    p_classify.add_argument("selector")
    for name in ("resolve", "latest"):
        p = sub.add_parser(name)
        p.add_argument("--line", required=True)
        p.add_argument("--attempts", type=int, default=int(os.environ.get("KERNEL_RELEASES_ATTEMPTS", "3") or 3))
        if name == "resolve":
            p.add_argument("--pin", required=True)
    args = parser.parse_args(argv[1:])

    try:
        if args.command == "classify":
            print(classify(args.selector))
        elif args.command == "resolve":
            result = resolve(args.pin, args.line, attempts=args.attempts)
            note = " (end-of-life)" if result["iseol"] else ""
            print(f"[*] {result['requested']} for {args.line}: kernel.org lists Linux {result['version']} "
                  f"({result['moniker']}, released {result['released'] or 'unknown'}){note}", file=sys.stderr)
            print(result["version"])
        elif args.command == "latest":
            print(json.dumps(latest_for_line(load_releases(attempts=args.attempts), args.line)))
    except VersionError as error:
        print(f"[-] {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
