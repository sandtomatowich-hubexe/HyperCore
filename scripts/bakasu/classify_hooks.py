#!/usr/bin/env python3
"""Classify how a KernelSU fork defines the symbols the SUSFS kernel patch calls.

usage: classify_hooks.py [--export-static] <source-dir> <symbol> [<symbol> ...]

For every symbol this prints one line:  <symbol> <global|static|missing> [<file>]

  global   a non-static function definition exists (the linker will find it)
  static   only static definitions/declarations exist (the linker can not see them)
  missing  no definition anywhere under <source-dir>

With --export-static the `static` keyword is removed from every declaration and definition of a symbol
that is only defined statically, so the real implementation is exported instead of replaced by a stub.

The scan works on a comment- and string-masked copy of each file, so a return type on the line above the
name, attributes such as noinline, forward declarations and calls are all told apart correctly.
"""
import os
import re
import sys

KEYWORDS_BEFORE_CALL = {"return", "else", "case", "goto", "sizeof", "do", "if", "while", "for", "switch"}


def mask(src):
    """Blank out comments, string and char literals but keep every offset and newline."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def statement_prefix(m, pos):
    """Text of the statement before pos, without preprocessor lines. Returns (prefix, start_offset)."""
    start = max(m.rfind(";", 0, pos), m.rfind("}", 0, pos), m.rfind("{", 0, pos)) + 1
    lines = m[start:pos].split("\n")
    kept, offset = [], start
    for ln in lines:
        if not ln.lstrip().startswith("#"):
            kept.append(ln)
        offset += len(ln) + 1
    return "\n".join(kept), start


def occurrences(masked, sym):
    """Yield (pos, kind, prefix, prefix_start) for every declaration or definition of sym."""
    for m in re.finditer(r"\b%s\s*\(" % re.escape(sym), masked):
        pos = m.start()
        prefix, pstart = statement_prefix(masked, pos)
        p = prefix.strip()
        if not p or "=" in p or p[-1] in "(,!&|?:+-*/<>" and not p.endswith("*"):
            continue
        words = re.findall(r"[A-Za-z_]\w*", p)
        if not words or words[-1] in KEYWORDS_BEFORE_CALL or any(w in KEYWORDS_BEFORE_CALL for w in words):
            continue
        # find the matching ')' and look at what follows
        depth, j = 0, m.end() - 1
        while j < len(masked):
            if masked[j] == "(":
                depth += 1
            elif masked[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        nxt = masked[j + 1:].lstrip()[:1]
        # attributes may sit between ')' and '{'
        if nxt not in "{;":
            nxt = re.sub(r"__attribute__\s*\(\(.*?\)\)", "", masked[j + 1:j + 200], flags=re.S).lstrip()[:1]
        kind = "def" if nxt == "{" else "decl" if nxt == ";" else None
        if kind:
            yield pos, kind, prefix, pstart


def scan(root, syms):
    found = {s: [] for s in syms}
    for dp, _, fns in os.walk(root):
        for fn in fns:
            if not fn.endswith((".c", ".h")):
                continue
            path = os.path.join(dp, fn)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if not any(s in text for s in syms):
                continue
            masked = mask(text)
            for s in syms:
                for pos, kind, prefix, pstart in occurrences(masked, s):
                    found[s].append((path, pos, kind, bool(re.search(r"\bstatic\b", prefix)), prefix, pstart))
    return found


def classify(found):
    res = {}
    for s, occ in found.items():
        defs = [o for o in occ if o[2] == "def"]
        glob = [o for o in defs if not o[3]]
        stat = [o for o in defs if o[3]]
        res[s] = ("global", glob[0][0]) if glob else ("static", stat[0][0]) if stat else ("missing", "")
    return res


def export_static(found, res):
    """Drop `static` from every declaration/definition of the symbols that are only defined statically.
    All edits for one file are applied in a single pass, last offset first, so earlier offsets stay valid."""
    by_file = {}
    for s, (state, _) in res.items():
        if state != "static":
            continue
        for path, pos, kind, is_static, prefix, pstart in found[s]:
            if is_static:
                by_file.setdefault(path, []).append((pos, pstart))
    for path, spots in by_file.items():
        text = open(path, encoding="utf-8", errors="replace").read()
        for pos, pstart in sorted(set(spots), reverse=True):
            seg = text[pstart:pos]
            text = text[:pstart] + re.sub(r"\bstatic\b[ \t]*", "", seg, count=1) + text[pos:]
        open(path, "w", encoding="utf-8").write(text)


def main(argv):
    exp = "--export-static" in argv
    args = [a for a in argv if a != "--export-static"]
    if len(args) < 2:
        print(__doc__)
        return 2
    root, syms = args[0], args[1:]
    found = scan(root, syms)
    res = classify(found)
    if exp:
        export_static(found, res)
    for s in syms:
        print(s, res[s][0], res[s][1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
