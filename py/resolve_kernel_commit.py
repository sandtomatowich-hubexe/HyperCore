#!/usr/bin/env python3
"""Find the commit that reports an exact Linux version, or validate a pinned commit.

Usage:
    resolve_kernel_commit.py <repo_dir> <ref> <base> <exact_version>

<repo_dir> is a clone made with `--filter=tree:0 --no-checkout`, so the commit history is present
but file contents are fetched lazily. The first-parent history of <ref> is walked, and a binary
search over the VERSION / PATCHLEVEL / SUBLEVEL lines of the top-level Makefile finds the LAST
commit that still reports <exact_version>. That is the branch state right before the next stable
release was merged, so it contains every fix that landed for the requested release.

<exact_version> may also be a commit SHA (7 to 40 hex characters). The commit must be part of the history of <ref>
and its Makefile must report a version of the <base> line, otherwise the request is refused.

On success a JSON object with the commit is printed to stdout and the exit status is 0.
On failure the reason is printed to stderr and the exit status is 1. When the version does not
exist on the branch, the error names the nearest versions that do.
"""

import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

Version = Tuple[int, int, int]

GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CLONE_PROTECTION_ACTIVE="false")
FIELD_RE = {
    "VERSION": re.compile(r"^VERSION\s*=\s*(\d+)\s*$"),
    "PATCHLEVEL": re.compile(r"^PATCHLEVEL\s*=\s*(\d+)\s*$"),
    "SUBLEVEL": re.compile(r"^SUBLEVEL\s*=\s*(\d+)\s*$"),
}


class ResolveError(Exception):
    pass


def git(repo: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            env=GIT_ENV,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise ResolveError(f"git {' '.join(args[:2])} timed out")
    if result.returncode != 0:
        raise ResolveError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def fmt(version: Version) -> str:
    return ".".join(str(part) for part in version)


def makefile_version(repo: str, sha: str) -> Version:
    head = git(repo, "show", f"{sha}:Makefile")
    found: Dict[str, int] = {}
    for line in head.splitlines()[:20]:
        for key, pattern in FIELD_RE.items():
            m = pattern.match(line)
            if m:
                found[key] = int(m.group(1))
    if len(found) != 3:
        raise ResolveError(f"commit {sha[:12]} has no readable VERSION/PATCHLEVEL/SUBLEVEL in its Makefile")
    return (found["VERSION"], found["PATCHLEVEL"], found["SUBLEVEL"])


def resolve_commit_pin(repo: str, ref: str, base: str, pin: str) -> dict:
    """Validate a pinned commit: it exists, it is on the branch, and it belongs to the requested kernel line."""
    try:
        sha = git(repo, "rev-parse", "--verify", "--quiet", f"{pin}^{{commit}}").strip()
    except ResolveError as error:
        if "ambiguous" in str(error).lower():
            raise ResolveError(f"Commit '{pin}' is ambiguous in {ref}. Use more characters of the SHA")
        sha = ""
    if not sha:
        tip = _tip_version(repo, ref)
        raise ResolveError(
            f"Commit '{pin}' does not exist in the history of {ref}. Check the SHA and that it was made on this branch"
            + (f". The branch tip is Linux {fmt(tip)}" if tip else ""))

    probe = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", sha, ref],
                           capture_output=True, text=True, env=GIT_ENV, timeout=600)
    if probe.returncode == 1:
        raise ResolveError(f"Commit {sha[:12]} exists but is not part of the history of {ref}, so it is not on the selected source branch")
    if probe.returncode != 0:
        raise ResolveError(f"git merge-base failed: {probe.stderr.strip()}")

    version = makefile_version(repo, sha)
    line = f"{version[0]}.{version[1]}"
    if line != base:
        tip = _tip_version(repo, ref)
        raise ResolveError(
            f"Commit {sha[:12]} reports Linux {fmt(version)}, which belongs to the {line} line, "
            f"but the selected kernel base is {base}"
            + (f". The branch tip is Linux {fmt(tip)}" if tip else ""))

    return {"commit": sha, "version": fmt(version), "ref": ref, "mode": "commit"}


def _tip_version(repo: str, ref: str) -> Optional[Version]:
    try:
        return makefile_version(repo, ref)
    except ResolveError:
        return None


def parse_exact(exact: str) -> Version:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", exact)
    if not match:
        raise ResolveError(f"'{exact}' is not a Linux version (expected MAJOR.MINOR.SUBLEVEL)")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


class History:
    """First-parent history of a ref, oldest commit first, with a cache of Makefile versions."""

    def __init__(self, repo: str, ref: str):
        self.repo = repo
        self.ref = ref
        out = git(repo, "rev-list", "--first-parent", "--reverse", ref)
        self.commits: List[str] = out.split()
        if not self.commits:
            raise ResolveError(f"{ref} has no commits")
        self._cache: Dict[int, Version] = {}
        self.lookups = 0

    def version_at(self, index: int) -> Version:
        if index in self._cache:
            return self._cache[index]
        sha = self.commits[index]
        self.lookups += 1
        head = git(self.repo, "show", f"{sha}:Makefile")
        found: Dict[str, int] = {}
        for line in head.splitlines()[:20]:
            for key, pattern in FIELD_RE.items():
                m = pattern.match(line)
                if m:
                    found[key] = int(m.group(1))
        if len(found) != 3:
            raise ResolveError(f"commit {sha[:12]} has no readable VERSION/PATCHLEVEL/SUBLEVEL in its Makefile")
        version = (found["VERSION"], found["PATCHLEVEL"], found["SUBLEVEL"])
        self._cache[index] = version
        return version


def resolve(repo: str, ref: str, base: str, exact: str) -> dict:
    if re.match(r"^[0-9a-fA-F]{7,40}$", exact):
        return resolve_commit_pin(repo, ref, base, exact)
    target = parse_exact(exact)
    if f"{target[0]}.{target[1]}" != base:
        raise ResolveError(
            f"Linux {exact} belongs to the {target[0]}.{target[1]} line, "
            f"but the selected kernel base is {base}"
        )

    history = History(repo, ref)
    last = len(history.commits) - 1

    oldest = history.version_at(0)
    if oldest > target:
        raise ResolveError(
            f"Linux {exact} does not exist on {ref}: the history starts at {fmt(oldest)}. "
            f"Nearest versions: {fmt(oldest)}"
        )

    # Last index whose version is <= target (versions never go down along first-parent history)
    lo, hi = 0, last
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if history.version_at(mid) <= target:
            lo = mid
        else:
            hi = mid - 1

    found = history.version_at(lo)
    if found != target:
        nearest = [fmt(found)]
        if lo < last:
            nearest.append(fmt(history.version_at(lo + 1)))
        raise ResolveError(
            f"Linux {exact} does not exist on {ref}. Nearest versions: {', '.join(nearest)}"
        )

    return {
        "commit": history.commits[lo],
        "version": fmt(found),
        "ref": ref,
        "mode": "version",
        "commits_in_history": len(history.commits),
        "makefile_lookups": history.lookups,
    }


def main(argv: List[str]) -> int:
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    repo, ref, base, exact = argv[1:5]
    try:
        result = resolve(repo, ref, base, exact)
    except ResolveError as error:
        print(f"[-] {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
