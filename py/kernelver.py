#!/usr/bin/env python3
"""Validate the kernel line and the optional exact pin (a Linux version or a commit).

Usage:
    kernelver.py <KERNEL_VERSION> [<KERNEL_EXACT_VERSION>] [--gha]

An empty pin is always fine (the branch default is used). Otherwise the pin is either a Linux version written
MAJOR.MINOR.SUBLEVEL (for example 6.1.180), which must belong to the selected line, or a commit SHA of 7 to 40
hex characters. Either needs one kernel line, not All. Whether the version or commit actually exists in the
selected source is checked later, against its history (py/resolve_kernel_commit.py).

Exit status is 0 when the request is valid and 1 when it is not. With --gha the error is printed
as a GitHub Actions annotation, so it shows up on the run page instead of only inside a job log.
"""

import re
import sys

EXACT_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def supported_lines():
    """The kernel lines configured in sources.json, so the list lives in one place."""
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    import sources
    return tuple(sources.load().lines)


def is_commit(pin: str) -> bool:
    return bool(COMMIT_RE.match(pin))


def validate(base: str, exact: str) -> str:
    """Return an empty string when the request is valid, otherwise the reason it is not."""
    if exact == "":
        return ""

    if exact != exact.strip():
        return f"KERNEL_EXACT_VERSION '{exact}' has leading or trailing spaces"

    if base.lower() == "all":
        return (
            f"KERNEL_EXACT_VERSION {exact} needs a single kernel line. "
            "Select one KERNEL_VERSION instead of All"
        )

    try:
        lines = supported_lines()
    except Exception as error:  # a broken registry is reported by the registry validator
        return f"sources.json could not be read ({error}). Run: python3 py/sources.py validate"
    if base not in lines:
        return f"Unknown KERNEL_VERSION '{base}'. Expected one of: {', '.join(lines)}"

    if is_commit(exact):
        return ""

    match = EXACT_RE.match(exact)
    if not match:
        hint = " (drop the leading 'v')" if exact[:1] in ("v", "V") else ""
        return (
            f"KERNEL_EXACT_VERSION '{exact}' is neither a Linux version nor a commit{hint}. "
            "Write a version as MAJOR.MINOR.SUBLEVEL, for example 6.1.180, or a commit as 7 to 40 hex characters"
        )

    line = f"{match.group(1)}.{match.group(2)}"
    if line != base:
        return (
            f"Linux {exact} belongs to the {line} line, "
            f"but the selected kernel base is {base}"
        )

    return ""


def main(argv: list) -> int:
    gha = "--gha" in argv
    args = [a for a in argv[1:] if a != "--gha"]
    if not 1 <= len(args) <= 2:
        print(__doc__, file=sys.stderr)
        return 2

    base = args[0]
    exact = args[1] if len(args) == 2 else ""

    problem = validate(base, exact)
    if problem:
        print(f"::error::{problem}" if gha else f"[-] {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
