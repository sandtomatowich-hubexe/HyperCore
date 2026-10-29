#!/usr/bin/env python3
"""Fail when a script, workflow or document points at a helper script that is not in the repository.

The other validators only check syntax, so a helper that was referenced but never committed used to
pass CI and then fail every build job at run time.
"""

import pathlib
import re
import sys

EXCLUDE_DIRS = {".git", "venv", ".venv", "node_modules"}
# Vendored third-party files: their comments name paths in the upstream repository, not in this one
IGNORE_FILES = {pathlib.PurePosixPath("scripts/finasu/extract_manager_cert.py")}
SCAN_SUFFIXES = {".sh", ".yml", ".yaml", ".py", ".md"}
# py/<name>.py, scripts/<dir>/<name>.py, tests/<dir>/<name>.py/.sh, .github/scripts/<name>.py/.sh
REF_RE = re.compile(r"(?<![\w./-])((?:\.github/scripts|scripts(?:/[\w.-]+)*|tests(?:/[\w.-]+)*|py)/[\w.-]+\.(?:py|sh))\b")


def scan_files(root="."):
    for path in pathlib.Path(root).rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if pathlib.PurePosixPath(path.as_posix()) in IGNORE_FILES:
            continue
        yield path


def main():
    missing = {}
    for path in scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for ref in REF_RE.findall(line):
                if not pathlib.Path(ref).exists():
                    missing.setdefault(ref, []).append(f"{path}:{number}")

    for ref, places in sorted(missing.items()):
        print(f"Missing file: {ref}")
        for place in places[:5]:
            print(f"  referenced at {place}")
    if missing:
        return 1
    print("All referenced helper scripts exist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
