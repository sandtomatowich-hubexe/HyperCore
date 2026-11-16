#!/usr/bin/env python3
"""HyperCore Phase 0: delta analysis of Duchamp kernel trees against Android Common Kernel (ACK).  (revision 2.3)

Architecture this serves
  * ACK (Android Common Kernel) is the upstream foundation of the future HyperCore-kernel.
  * GKID is a READ-ONLY REFERENCE, used only to find Duchamp-specific adaptations. It is never a kernel source,
    development foundation or fallback.
  * Luminaire is a READ-ONLY comparison reference (reported to boot on Duchamp). Nothing of it is imported into
    HyperCore: its objects live only in a throw-away scratch repository outside the HyperCore checkout, and the
    report contains statistics plus at most two short sample lines per file (--no-samples removes those).

What it does
  Fetches the trees into a BARE scratch repository (no working tree, no index of its own), finds the ACK base of
  each tree, measures what each tree changes over that base, and reviews HyperCore's own patches against ACK.

Isolation guarantees (tested in tests/test_phase0_isolation.py)
  * Every git call is pinned to the scratch repository with --git-dir and runs with GIT_DIR, GIT_WORK_TREE,
    GIT_INDEX_FILE and similar variables removed from the environment, so nothing can reach another repository.
  * Analysis commands run with the user's global and system git configuration disabled, so results do not depend on
    settings such as apply.whitespace. Only the network fetch uses the user's configuration (proxies, credentials).
  * The patch review uses a temporary index file in a private temporary directory. Any other index path is refused.
  * The scratch directory must be new or carry this tool's marker file, and must not contain or sit inside the
    HyperCore checkout.

Evidence and limits (repeated in the report)
  * Luminaire is reported to BOOT on Duchamp (the owner's test, not verified here). A change it carries is
    compatibility evidence, not proof of necessity. Only a boot test decides what is required.
  * Categories and buckets come from path and content rules. They sort work for a human.
  * "reverse-applies" is a HEURISTIC: the reverse patch applies cleanly, which suggests, but does not prove, that
    the change is already present.
  * The patch check is NOT the build's method: it uses exact `git apply`, while the build also falls back to
    `patch --fuzz=3`. A `conflict` here may still be applied by the build. See PATCH_METHOD and PHASE0.md.
  * A base found without shared history is APPROXIMATE: it is the best of the ACK commits that were compared, which
    may be a sample, and is not guaranteed to be the best of all candidates.

Outputs are never written inside the HyperCore checkout or inside the scratch repository (--workdir): --out and
--json must point elsewhere, and must be two different files. Both are checked before anything is fetched, analysed
or written. Reports are written by atomic replacement, never through an existing file, so a hard link to some other
file (inside the scratch repository, the checkout, or anywhere) is never modified. Not detectable by a path check
(documented in PHASE0.md): bind mounts or overlays that alias a directory, and spellings that differ only by case on
a case-insensitive filesystem.

Usage (see PHASE0.md):
  python3 phase0/delta_report.py --workdir /tmp/hc-delta --hypercore . \\
      --out /tmp/delta-report.md --json /tmp/delta-report.json
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = 2
SCRATCH_MARKER = "hypercore-phase0-scratch"
DEFCONFIG = "arch/arm64/configs/gki_defconfig"
FALLBACK_LUMINAIRE = ("https://github.com/chainonyourdoor/LuminaireKernel-6.1", "android14-6.1")
EXTRA_ACK_BRANCHES = ["android14-6.1"]
REGISTRY_ACK_ID = "aosp-ack-6.1"
REGISTRY_GKID_ID = "gkid-duchamp-6.1"

PRIMARY_ACK_RATIONALE = (
    "The primary ACK reference is the branch HyperCore's registry names for `aosp-ack-6.1` (`android14-6.1-lts`). "
    "It is what the Phase 0 build test clones and what a HyperCore-kernel upstream mirror would track, so patch "
    "applicability is judged against the code HyperCore would actually start from. The other ACK branch is used only "
    "to improve base detection (a tree may have merged from either). The branch relationship is measured and shown, "
    "not assumed.")


class GitError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[delta] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------------------------------
# isolated git access
# --------------------------------------------------------------------------------------------------------------

_SCRUB_EXACT = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX", "GIT_QUARANTINE_PATH", "GIT_SHALLOW_FILE", "GIT_GRAFT_FILE",
    "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS", "GIT_INDEX_VERSION", "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG",
}
_TMP_INDEX_PREFIX = "hc-phase0-index-"


def clean_env(hermetic: bool, extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in _SCRUB_EXACT and not re.match(r"^GIT_CONFIG_(KEY|VALUE)_\d+$", k)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if hermetic:
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
    if extra:
        env.update(extra)
    return env


def _check_index_path(path: str) -> None:
    real = Path(path).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if real.name != "index" or not real.parent.name.startswith(_TMP_INDEX_PREFIX) or tmp not in real.parents:
        raise GitError(f"refusing to use {path} as GIT_INDEX_FILE: only a private temporary index is allowed")


class TempIndex:
    """A private temporary index file. Nothing else may be used as GIT_INDEX_FILE."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix=_TMP_INDEX_PREFIX)
        self.path = os.path.join(self.dir, "index")
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)

    @property
    def env(self) -> dict:
        return {"GIT_INDEX_FILE": self.path}


class Repo:
    """A bare scratch repository. All access goes through here."""

    def __init__(self, git_dir: Path):
        self.git_dir = Path(git_dir)

    def _cmd(self, *args) -> list[str]:
        return ["git", "--git-dir", str(self.git_dir), "-c", "core.quotepath=off", "-c", "gc.auto=0",
                "-c", "maintenance.auto=false", *args]

    def _env(self, env, hermetic):
        if env and "GIT_INDEX_FILE" in env:
            _check_index_path(env["GIT_INDEX_FILE"])
        return clean_env(hermetic, env)

    def run(self, *args, env=None, check=True, input_text=None, hermetic=True):
        proc = subprocess.run(self._cmd(*args), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self._env(env, hermetic), input=input_text)
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
        return proc

    def out(self, *args, **kw) -> str:
        return self.run(*args, **kw).stdout

    def ok(self, *args, **kw) -> bool:
        return self.run(*args, check=False, **kw).returncode == 0


def ensure_scratch(workdir: Path, hypercore: Path) -> Repo:
    wd, hc = workdir.resolve(), hypercore.resolve()
    if wd == hc or hc.is_relative_to(wd) or wd.is_relative_to(hc):
        raise GitError(f"--workdir {wd} must be outside the HyperCore checkout {hc}")
    if wd.exists() and any(wd.iterdir()):
        if not (wd / SCRATCH_MARKER).exists():
            raise GitError(f"--workdir {wd} exists, is not empty and is not a Phase 0 scratch directory; refusing to "
                           f"use it (it could be a repository that matters)")
    else:
        wd.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["git", "init", "--bare", "-q", str(wd)], capture_output=True, text=True,
                              env=clean_env(True))
        if proc.returncode != 0:
            raise GitError(f"git init failed: {proc.stderr.strip()}")
        (wd / SCRATCH_MARKER).write_text("Scratch repository of phase0/delta_report.py. Safe to delete.\n")
    if not (wd / "HEAD").exists() or not (wd / "objects").is_dir():
        raise GitError(f"{wd} carries the scratch marker but is not a bare git repository")
    return Repo(wd)


def check_output_paths(out: str, json_out: str, hypercore: str, workdir: str) -> None:
    """Refuse report paths that are unsafe, before anything is fetched, analysed or written: a path inside the
    HyperCore checkout or inside the scratch repository, or --out and --json being the same file. Paths are resolved
    first (`..` segments and symlinks cannot hide a location), and containment is tested on path components, so a
    sibling such as `scratch-other` is not mistaken for `scratch`. The same-file test compares resolved paths and,
    when both files exist, their identity (`samefile`), which also catches a hard link under another name."""
    forbidden = (("the HyperCore checkout", Path(hypercore).resolve()),
                 ("the scratch repository", Path(workdir).resolve()))
    targets = {}
    for flag, value in (("--out", out), ("--json", json_out)):
        target = targets[flag] = Path(value).resolve()
        for what, root in forbidden:
            if target == root or root in target.parents:
                raise GitError(f"{flag} {target} is inside {what} {root}; this tool never writes there. Pass a path "
                               f"outside both the checkout and the scratch directory (for example under "
                               f"$RUNNER_TEMP or /tmp)")
    a, b = targets["--out"], targets["--json"]
    if a == b or (a.exists() and b.exists() and os.path.samefile(a, b)):
        raise GitError(f"--out and --json must be two different files, but both resolve to {a}"
                       + ("" if a == b else f" and {b} (the same file under two names, for example a hard link)")
                       + "; the second report would overwrite the first")


def write_report(path: str, text: str) -> None:
    """Write a report by ATOMIC REPLACEMENT: a temporary file in the same directory, then os.replace. The target is
    never opened for writing, so if it is a hard link to another file (inside the scratch repository, the checkout or
    anywhere else) that other file is left untouched: only this directory entry is replaced. A reader never sees a
    half-written report. Not a path check, so it also holds for links the path checks cannot see."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        tmp = None
    except OSError as exc:
        raise GitError(f"cannot write {target}: {exc.strerror or exc}") from exc
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def remote_heads(url: str) -> dict[str, str]:
    proc = subprocess.run(["git", "ls-remote", "--heads", url], capture_output=True, text=True,
                          env=clean_env(False), cwd=tempfile.gettempdir())
    if proc.returncode != 0:
        raise GitError(f"cannot list branches of {url}: {proc.stderr.strip()[:300]}")
    heads = {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        heads[ref.removeprefix("refs/heads/")] = sha
    return heads


def fetch_remote(repo: Repo, name: str, url: str, branches: list[str], optional: list[str] | None = None) -> list[str]:
    heads = remote_heads(url)
    missing = [b for b in branches if b not in heads]
    if missing:
        avail = ", ".join(sorted(heads)[:40]) or "(none)"
        raise GitError(f"{url} has no branch {', '.join(missing)}. Branches there: {avail}")
    want = list(branches) + [b for b in (optional or []) if b in heads and b not in branches]
    if repo.ok("remote", "get-url", name):
        repo.run("remote", "set-url", name, url)
    else:
        repo.run("remote", "add", name, url)
    specs = [f"+refs/heads/{b}:refs/remotes/{name}/{b}" for b in want]
    log(f"fetching {name}: {url} {' '.join(want)} (blobless; large the first time)")
    rc = subprocess.run(repo._cmd("fetch", "--no-tags", "--filter=blob:none", name, *specs),
                        env=clean_env(False)).returncode
    if rc != 0:
        raise GitError(f"fetch of {name} failed (exit {rc})")
    return want


def rev(repo: Repo, ref: str) -> str:
    return repo.out("rev-parse", "--verify", f"{ref}^{{commit}}").strip()


# --------------------------------------------------------------------------------------------------------------
# Linux versions and ACK history
# --------------------------------------------------------------------------------------------------------------

def parse_version(text: str):
    vals = {}
    for line in text.splitlines()[:15]:
        m = re.match(r"^(VERSION|PATCHLEVEL|SUBLEVEL)\s*=\s*(\d+)", line)
        if m:
            vals[m.group(1)] = int(m.group(2))
    if len(vals) < 3:
        return None
    return (vals["VERSION"], vals["PATCHLEVEL"], vals["SUBLEVEL"])


def makefile_version(repo: Repo, ref: str):
    proc = repo.run("show", f"{ref}:Makefile", check=False)
    return parse_version(proc.stdout) if proc.returncode == 0 else None


def fmt_version(v) -> str:
    return ".".join(str(x) for x in v) if v else "unknown"


def batch_makefile_versions(repo: Repo, shas: list[str]) -> list:
    """Linux version of each commit's Makefile, one `git cat-file --batch` process (blobs are fetched lazily)."""
    if not shas:
        return []
    proc = subprocess.run(repo._cmd("cat-file", "--batch"), input="".join(f"{s}:Makefile\n" for s in shas).encode(),
                          capture_output=True, env=clean_env(True))
    if proc.returncode != 0:
        raise GitError(f"git cat-file --batch failed: {proc.stderr.decode(errors='replace')[:300]}")
    data, pos, versions = proc.stdout, 0, []
    for _ in shas:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].decode(errors="replace").split()
        pos = nl + 1
        if len(header) == 3 and header[1] == "blob":
            size = int(header[2])
            versions.append(parse_version(data[pos:pos + size].decode(errors="replace")))
            pos += size + 1
        else:
            versions.append(None)
    return versions


class Segment:
    __slots__ = ("sha", "version")

    def __init__(self, sha, version):
        self.sha, self.version = sha, version


def version_timeline(repo: Repo, ref: str, limit: int) -> list[Segment]:
    """The COMPLETE sequence of Linux versions along the first-parent history of `ref`.

    `git log --first-parent -- Makefile` lists exactly the first-parent commits whose Makefile differs from their
    first parent, so the version is constant between consecutive entries. Nothing is assumed about the order of the
    versions: they are read here and audited by `audit_timeline`."""
    shas = repo.out("log", "--first-parent", "--reverse", "--format=%H", ref, "--", "Makefile").split()
    if len(shas) > limit:
        raise GitError(f"{ref}: {len(shas)} first-parent commits change the Makefile (limit {limit})")
    log(f"{ref}: reading the Makefile version at {len(shas)} change points")
    segs: list[Segment] = []
    for sha, ver in zip(shas, batch_makefile_versions(repo, shas)):
        if segs and segs[-1].version == ver:
            continue  # the version did not change: same segment
        segs.append(Segment(sha, ver))
    return segs


def audit_timeline(ref: str, segs: list[Segment]) -> dict:
    violations, seen, revisited = [], set(), set()
    for i, s in enumerate(segs):
        if s.version is not None:
            if s.version in seen:
                revisited.add(s.version)
            seen.add(s.version)
        if i and s.version and segs[i - 1].version and s.version < segs[i - 1].version:
            violations.append({"index": i, "commit": s.sha[:12], "from": fmt_version(segs[i - 1].version),
                               "to": fmt_version(s.version)})
    versions = [s.version for s in segs if s.version]
    return {
        "ref": ref, "segments": len(segs),
        "first_version": fmt_version(versions[0]) if versions else "unknown",
        "last_version": fmt_version(versions[-1]) if versions else "unknown",
        "monotonic": not violations, "violation_count": len(violations), "violations": violations[:10],
        "revisited_versions": sorted(fmt_version(v) for v in revisited),
        "unreadable_segments": sum(1 for s in segs if s.version is None),
    }


def segment_commits(repo: Repo, ref: str, segs: list[Segment], idx: int) -> list[str]:
    """First-parent commits (oldest first) of `ref` that report the version of segment `idx`."""
    start = segs[idx].sha
    end = f"{segs[idx + 1].sha}^1" if idx + 1 < len(segs) else ref
    return [start] + repo.out("rev-list", "--first-parent", "--reverse", end, f"^{start}").split()


def diff_path_count(repo: Repo, a: str, b: str) -> int:
    return len(repo.out("diff", "--name-only", "--no-renames", a, b).splitlines())


def pick_by_content(repo: Repo, cands: list[str], tree_ref: str, samples: int) -> dict:
    """Among commits that report the right version, the best of those COMPARED: the one whose tree differs least from
    the target tree. When there are more candidates than `samples`, only a subset is compared: an evenly spaced
    coarse pass, then a finer pass in a window around the best coarse candidate. That finds the best candidate only if
    tree similarity changes fairly smoothly along the history. It does NOT guarantee the best of all candidates, so
    the result records how many were compared (`evaluated` of `candidates`) and whether the search was `exhaustive`."""
    if samples < 2:
        raise GitError(f"samples must be at least 2 (got {samples}): the sample is spaced between the first and the "
                       "last candidate")
    memo: dict[str, int] = {}
    index = {c: i for i, c in enumerate(cands)}

    def score(c):
        if c not in memo:
            memo[c] = diff_path_count(repo, c, tree_ref)
        return memo[c]

    def spaced(seq, n):
        if len(seq) <= n:
            return list(seq)
        return [seq[round(i * (len(seq) - 1) / (n - 1))] for i in range(n)]

    for c in spaced(cands, samples):
        score(c)
    best = min(memo, key=lambda c: (memo[c], -index[c]))
    step = max(1, len(cands) // samples)
    lo, hi = max(0, index[best] - step), min(len(cands) - 1, index[best] + step)
    for c in spaced(cands[lo:hi + 1], samples):
        score(c)
    best = min(memo, key=lambda c: (memo[c], -index[c]))  # ties: the newest candidate
    others = sorted(v for c, v in memo.items() if c != best)
    return {"commit": best, "paths_differing": memo[best], "runner_up": others[0] if others else None,
            "evaluated": len(memo), "candidates": len(cands), "exhaustive": len(memo) == len(cands),
            "tied_at_best": sum(1 for v in memo.values() if v == memo[best])}


def find_base(repo: Repo, tree_ref: str, ack_refs: list[str], timelines: dict, samples: int, limit: int) -> dict:
    """The ACK commit a tree was built on.

    1. Shared history (exact): the newest ACK commit contained in the tree, i.e. the best merge base with each ACK
       ref; the ref with the fewest commits above its base wins. No version is involved.
    2. No shared history (squashed or imported tree): APPROXIMATE. Read the tree's Linux version and take every
       first-parent ACK commit that reports it (or the nearest lower version when none does) as a candidate. Compare
       candidates with the tree (all of them when there are few, otherwise a sample refined around the best sample;
       see pick_by_content) and choose the best of those compared. That is not guaranteed to be the best of all
       candidates. The version timeline is read in full and audited, not searched by bisection, so a non-monotonic
       history cannot mislead the choice of candidates."""
    best = None
    for ack in ack_refs:
        proc = repo.run("merge-base", "--all", ack, tree_ref, check=False)
        for base in proc.stdout.split():
            ahead = int(repo.out("rev-list", "--count", f"{base}..{tree_ref}").strip() or 0)
            if best is None or ahead < best["ahead"]:
                best = {"base": base, "ack_ref": ack, "ahead": ahead, "method": "merge-base (shared history)",
                        "confidence": "exact", "detail": {}}
    if best:
        return best
    tv = makefile_version(repo, tree_ref)
    if not tv:
        raise GitError(f"{tree_ref}: no shared history with ACK and no readable Linux version")
    result = None
    for ack in ack_refs:
        if ack not in timelines:
            timelines[ack] = version_timeline(repo, ack, limit)
        segs = timelines[ack]
        exact = [i for i, s in enumerate(segs) if s.version == tv]
        lower = sorted({s.version for s in segs if s.version and s.version < tv})
        idxs = exact or [i for i, s in enumerate(segs) if lower and s.version == lower[-1]]
        if not idxs:
            continue
        cands = []
        for i in idxs:
            cands += segment_commits(repo, ack, segs, i)
        pick = pick_by_content(repo, list(dict.fromkeys(cands)), tree_ref, samples)
        pick.update({"reported_version_exact": bool(exact), "version_segments": len(idxs)})
        if result is None or pick["paths_differing"] < result[1]["paths_differing"]:
            result = (ack, pick)
    if result is None:
        raise GitError(f"{tree_ref}: no ACK commit reports Linux {fmt_version(tv)} or a lower version")
    ack, pick = result
    return {"base": pick["commit"], "ack_ref": ack, "ahead": None,
            "method": "approximate: best of the compared ACK commits that report the same Linux version "
                      "(no shared history)",
            "confidence": "approximate", "detail": pick}


# --------------------------------------------------------------------------------------------------------------
# diff parsing
# --------------------------------------------------------------------------------------------------------------

KMI_CONTENT = re.compile(
    r"EXPORT_SYMBOL|EXPORT_TRACEPOINT_SYMBOL|ANDROID_VENDOR_DATA|ANDROID_OEM_DATA|ANDROID_KABI"
    r"|DECLARE_(RESTRICTED_)?HOOK|trace_android_(vh|rvh)_")


def line_key(sign: str, text: str) -> bytes:
    """Deterministic fingerprint of one changed line: SHA-256 of its whitespace-stripped text, with its sign."""
    return sign.encode() + hashlib.sha256(text.encode("utf-8", "surrogateescape")).digest()


class FileDelta:
    __slots__ = ("path", "status", "sig", "n_add", "n_del", "binary", "kmi_content", "sample", "raw_added",
                 "raw_removed")

    def __init__(self, path: str):
        self.path = path
        self.status = "M"
        self.sig = collections.Counter()
        self.n_add = self.n_del = 0
        self.binary = False
        self.kmi_content = False
        self.sample: list[str] = []
        self.raw_added = None
        self.raw_removed = None

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for key, n in sorted(self.sig.items()):
            h.update(key)
            h.update(n.to_bytes(4, "big"))
        return h.hexdigest()


def keep_raw(path: str) -> bool:
    return path == "Makefile" or path.startswith("android/abi_")


def diff_tree(repo: Repo, base: str, tree: str, max_files: int) -> dict[str, FileDelta]:
    deltas: dict[str, FileDelta] = {}
    cur = None
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(
            repo._cmd("diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", "--src-prefix=a/",
                      "--dst-prefix=b/", base, tree),
            stdout=subprocess.PIPE, stderr=errf, text=True, encoding="utf-8", errors="replace",
            env=clean_env(True))
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("diff --git "):
                rest = line[len("diff --git "):].rstrip("\n")
                n = (len(rest) - 5) // 2  # "a/X b/X" with --no-renames: both paths are identical
                path = rest[2:2 + n] if n > 0 and rest[2:2 + n] == rest[5 + n:] else rest.split(" b/", 1)[-1]
                cur = FileDelta(path)
                deltas[path] = cur
                if len(deltas) > max_files:
                    proc.kill()
                    proc.wait()
                    raise GitError(f"more than {max_files} changed files; raise --max-delta-files if intended")
                if keep_raw(path):
                    cur.raw_added, cur.raw_removed = [], []
                continue
            if cur is None:
                continue
            if line.startswith("new file mode"):
                cur.status = "A"
            elif line.startswith("deleted file mode"):
                cur.status = "D"
            elif line.startswith(("Binary files", "GIT binary patch")):
                cur.binary = True
            elif line.startswith(("+++", "---", "@@")):
                continue
            elif line.startswith("+"):
                text = line[1:].strip()
                cur.n_add += 1
                if text:
                    cur.sig[line_key("+", text)] += 1
                    cur.kmi_content = cur.kmi_content or bool(KMI_CONTENT.search(text))
                    if len(cur.sample) < 4:
                        cur.sample.append(text[:110])
                    if cur.raw_added is not None:
                        cur.raw_added.append(text)
            elif line.startswith("-"):
                text = line[1:].strip()
                cur.n_del += 1
                if text:
                    cur.sig[line_key("-", text)] += 1
                    if cur.raw_removed is not None:
                        cur.raw_removed.append(text)
        rc = proc.wait()
        if rc not in (0, 1):
            errf.seek(0)
            raise GitError(f"git diff failed: {errf.read().decode(errors='replace').strip()[:300]}")
    return deltas


def similarity(a: collections.Counter, b: collections.Counter) -> float:
    union = sum((a | b).values())
    return 1.0 if union == 0 else sum((a & b).values()) / union


# --------------------------------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------------------------------

HW_STRONG = re.compile(r"(mediatek|/mtk|mtk_|(^|/)mt[0-9]{4}|duchamp|xiaomi|miui)", re.I)
FEATURE_RULES = [
    ("root (KernelSU/SUSFS/BBG/NoMount/Re:Kernel)",
     re.compile(r"(kernelsu|(^|/)ksu|susfs|baseband[_-]guard|nomount|rekernel)", re.I)),
    ("ntsync", re.compile(r"ntsync", re.I)),
    ("NetHunter / wifi monitor", re.compile(r"(^net/mac80211|^net/wireless|^drivers/net/wireless|rtw88|nethunter)", re.I)),
    ("containers (DroidSpaces)", re.compile(r"(^ipc/|^kernel/cgroup/|^kernel/nsproxy|droidspaces)", re.I)),
    ("zram / lz4", re.compile(r"(drivers/block/zram|(^|/)lz4|lz4kd)", re.I)),
]
PERF_RULES = [
    ("scheduler", re.compile(r"^kernel/sched/")),
    ("memory management", re.compile(r"^mm/")),
    ("filesystems / block", re.compile(r"^(fs/|block/)")),
    ("networking", re.compile(r"^net/(core|ipv4|ipv6|sched)/|bbr|tcp")),
    ("arm64 / lib", re.compile(r"^(lib/|arch/arm64/(lib|mm|kernel)/)")),
    ("timers / power / irq / printk", re.compile(r"^kernel/(time|power|irq|freezer|printk|cpu|workqueue)")),
    ("cpufreq / devfreq", re.compile(r"^drivers/(cpufreq|devfreq)/")),
    ("crypto", re.compile(r"^crypto/")),
]
META_PATH = re.compile(
    r"(^README|^LICENSE|^CREDITS|^NOTICE|^COPYING|\.md$|^\.github/|^\.git|^Documentation/|^OWNERS$|^MAINTAINERS$"
    r"|^scripts/setlocalversion|(^|/)anykernel|^build\.sh$|^\.clang-format|^\.mailmap)", re.I)
KMI_PATH = re.compile(r"^(android/|include/trace/hooks/|drivers/android/vendor_hooks\.c|build\.config|BUILD\.bazel"
                      r"|modules\.bzl)|protected_exports")
BOOKKEEPING = re.compile(r"^(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION|NAME)\s*=")
HW_CATEGORIES = ("HW_BOOT", "KMI_ABI", "HW_GENERIC")
# When the two trees' own content signals disagree about a file, the most hardware-significant reading wins, so a
# signal seen in only one tree is never silently dropped.
CATEGORY_PRECEDENCE = ["KMI_ABI", "HW_BOOT", "HW_GENERIC", "FEATURE", "PERF", "OTHER", "META", "CONFIG"]

CATEGORY_LABEL = {
    "HW_BOOT": "hardware / boot (MediaTek, device tree, device defconfig)",
    "KMI_ABI": "KMI / ABI (symbol lists, vendor hooks, exports, build config)",
    "HW_GENERIC": "hardware-surface driver or arch code (not clearly device-specific)",
    "PERF": "performance / tuning",
    "FEATURE": "feature (root, containers, wifi, zram, ...)",
    "META": "branding / CI / docs / version bookkeeping",
    "OTHER": "unclassified",
}


def classify(d: FileDelta) -> tuple[str, str]:
    """Category and topic from ONE tree's change to the file."""
    p = d.path
    if p == DEFCONFIG:
        return "CONFIG", "gki_defconfig (see the defconfig section)"
    if p == "Makefile":
        changed = (d.raw_added or []) + (d.raw_removed or [])
        if changed and all(BOOKKEEPING.match(x) for x in changed):
            return "META", "version bookkeeping"
        return "OTHER", "Makefile"
    if META_PATH.search(p):
        return "META", "branding / CI / docs"
    for topic, rx in FEATURE_RULES:
        if rx.search(p):
            return "FEATURE", topic
    if KMI_PATH.search(p):
        return "KMI_ABI", "symbol lists / hooks / build config"
    if p.startswith("arch/arm64/boot/dts/") or HW_STRONG.search(p) or (
            p.startswith("arch/arm64/configs/") and p != DEFCONFIG):
        return "HW_BOOT", "device tree / MediaTek / device config"
    for topic, rx in PERF_RULES:
        if rx.search(p):
            return "PERF", topic
    if d.kmi_content:
        return "KMI_ABI", "adds exports or vendor-hook declarations"
    if p.startswith(("drivers/", "arch/arm64/", "sound/", "include/")):
        return "HW_GENERIC", p.split("/")[0] + "/" + (p.split("/")[1] if "/" in p else "")
    return "OTHER", p.split("/")[0]


def combine_classification(g: FileDelta | None, l: FileDelta | None) -> tuple[str, str, dict, list[str]]:
    """Classify with the signals of BOTH trees. Returns category, topic, per-tree categories, notes."""
    per = {}
    if g:
        per["gkid"] = classify(g)
    if l:
        per["luminaire"] = classify(l)
    notes = []
    cats = {c for c, _ in per.values()}
    if len(cats) == 1:
        cat, topic = next(iter(per.values()))
    else:
        cat = min(cats, key=CATEGORY_PRECEDENCE.index)
        topic = next(t for c, t in per.values() if c == cat)
        notes.append("the two trees' changes classify differently (" +
                     ", ".join(f"{k}: {v[0]}" for k, v in sorted(per.items())) + "); the more hardware-significant "
                     "reading is used")
    return cat, topic, {k: v[0] for k, v in per.items()}, notes


def provenance(g: FileDelta | None, l: FileDelta | None) -> tuple[str, float | None]:
    if g and not l:
        return "GKID-only", None
    if l and not g:
        return "Luminaire-only", None
    sim = similarity(g.sig, l.sig)  # type: ignore[union-attr]
    return ("both (same change)" if sim >= 0.9 else "both (different changes)"), sim


def bucket_for(category: str, prov: str) -> tuple[int, str, str]:
    """(bucket, confidence, why). 1 essential-candidate hardware/boot, 2 general performance/functionality,
    3 GKID-specific and not demonstrably required, 5 neutral. Bucket 4 is the HyperCore patch review."""
    if category in HW_CATEGORIES:
        if prov in ("both (same change)", "Luminaire-only"):
            return 1, "in-booting-tree", "hardware/KMI change present in the tree reported to boot on Duchamp"
        if prov == "both (different changes)":
            return 1, "review", "hardware/KMI file that both trees change, differently"
        return 3, "candidate-to-drop", "hardware-looking GKID change that the booting Luminaire tree does without"
    if category in ("PERF", "FEATURE"):
        return 2, "general", prov
    if prov == "GKID-only":
        return 3, "gkid-only", "GKID-only change, not hardware-related"
    return 5, "neutral", "neutral / unclassified"


def tree_view(d: FileDelta | None, with_samples: bool) -> dict | None:
    if d is None:
        return None
    v = {"status": d.status, "add": d.n_add, "del": d.n_del, "binary": d.binary, "fingerprint": d.fingerprint()}
    if with_samples:
        v["sample"] = d.sample[:2]
    return v


def build_entries(dg, dl, patch_by_file, with_samples: bool) -> list[dict]:
    entries = []
    for path in sorted(set(dg) | set(dl)):
        g, l = dg.get(path), dl.get(path)
        cat, topic, per_tree, notes = combine_classification(g, l)
        if cat == "CONFIG":
            continue
        prov, sim = provenance(g, l)
        bucket, confidence, why = bucket_for(cat, prov)
        overlap = None
        if g and l:
            shared = sum((g.sig & l.sig).values())
            overlap = {"shared_lines": shared, "gkid_only_lines": sum(g.sig.values()) - shared,
                       "luminaire_only_lines": sum(l.sig.values()) - shared}
            if g.status != l.status:
                notes.append(f"status differs: GKID {g.status}, Luminaire {l.status} (A added, M modified, D deleted)")
        entries.append({
            "path": path, "category": cat, "topic": topic, "category_by_tree": per_tree, "provenance": prov,
            "similarity": None if sim is None else round(sim, 3), "overlap": overlap,
            "gkid": tree_view(g, with_samples), "luminaire": tree_view(l, with_samples),
            "bucket": bucket, "confidence": confidence, "why": why, "notes": notes,
            "hypercore_patches": patch_by_file.get(path, []),
        })
    return entries


# --------------------------------------------------------------------------------------------------------------
# defconfig and ABI symbols
# --------------------------------------------------------------------------------------------------------------

def parse_config(text: str) -> dict[str, str]:
    cfg = {}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(CONFIG_\w+)=(.*)$", line)
        if m:
            cfg[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^# (CONFIG_\w+) is not set$", line)
        if m:
            cfg[m.group(1)] = "n"
    return cfg


def read_defconfig(repo: Repo, ref: str) -> dict[str, str]:
    proc = repo.run("show", f"{ref}:{DEFCONFIG}", check=False)
    return parse_config(proc.stdout) if proc.returncode == 0 else {}


def config_rows(repo: Repo, g: dict, l: dict) -> list[dict]:
    """Options each tree changes over ITS OWN ACK base. Every cell is a real value (or '(absent)'): the value at the
    tree's base for ACK, the value at its tip for the tree. Both bases are shown, because they may differ."""
    cg0, cg1 = read_defconfig(repo, g["base"]), read_defconfig(repo, g["tip"])
    cl0, cl1 = read_defconfig(repo, l["base"]), read_defconfig(repo, l["tip"])
    absent = "(absent)"
    changed_g = {k for k in set(cg0) | set(cg1) if cg0.get(k, absent) != cg1.get(k, absent)}
    changed_l = {k for k in set(cl0) | set(cl1) if cl0.get(k, absent) != cl1.get(k, absent)}
    rows = []
    for opt in sorted(changed_g | changed_l):
        gv, lv = cg1.get(opt, absent), cl1.get(opt, absent)
        if opt in changed_g and opt in changed_l:
            kind = "both (same value)" if gv == lv else "both (different values)"
        else:
            kind = "GKID-only" if opt in changed_g else "Luminaire-only"
        rows.append({"option": opt, "ack_at_gkid_base": cg0.get(opt, absent), "gkid": gv,
                     "ack_at_luminaire_base": cl0.get(opt, absent), "luminaire": lv, "change": kind,
                     "hardware_related": bool(re.search(r"MEDIATEK|MTK|_MT6|DUCHAMP|XIAOMI", opt))})
    rows.sort(key=lambda r: (not r["hardware_related"], r["option"]))
    return rows


def abi_symbols(deltas: dict[str, FileDelta]) -> set[tuple[str, str]]:
    syms = set()
    for path, d in deltas.items():
        if path.startswith("android/abi_gki_aarch64") and d.raw_added:
            for text in d.raw_added:
                if text and not text.startswith(("#", "[")):
                    syms.add((path, text))
    return syms


# --------------------------------------------------------------------------------------------------------------
# commits and authors
# --------------------------------------------------------------------------------------------------------------

ACK_STYLE = re.compile(r"^(BACKPORT: |UPSTREAM: |FROMGIT: |FROMLIST: |ANDROID: )")


def commit_summary(repo: Repo, base: str, tree: str, limit: int) -> dict:
    """All non-merge commits above the base are counted and attributed; the newest `limit` are listed."""
    raw = repo.out("log", "--no-merges", "--format=%H%x1f%aN%x1f%s", f"{base}..{tree}")
    authors, listed, ack_style, total = collections.Counter(), [], 0, 0
    for line in raw.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) < 3:
            continue
        sha, author, subject = parts
        total += 1
        authors[author] += 1
        if ACK_STYLE.match(subject):
            ack_style += 1
        if len(listed) < limit:
            listed.append({"sha": sha[:12], "author": author, "subject": subject})
    return {"available": True, "total": total, "listed": listed, "listed_count": len(listed),
            "truncated": total > len(listed), "ack_style_commits": ack_style,
            "authors": [{"name": n, "commits": c} for n, c in sorted(authors.items(), key=lambda x: (-x[1], x[0]))]}


# --------------------------------------------------------------------------------------------------------------
# HyperCore patches (isolated temporary index)
# --------------------------------------------------------------------------------------------------------------

PATCH_GROUPS = [
    ("kernel-patches/common", "tree"),
    ("kernel-patches/bbrv3", "tree"),
    ("kernel-patches/droidspaces", "tree"),
    ("kernel-patches/nethunter", "tree"),
    ("kernel-patches/susfs", "builder"),
    ("patches", "builder"),
]


PATCH_METHOD = {
    "script": {
        "forward": "git apply --cached --check, then git apply --cached (exact context, atomic) on a private temporary "
                   "index of the reference tree",
        "reverse": "git apply --cached --check -R (exact context), tried only when the forward check fails",
        "tree": "each reference tree as committed, plus only the earlier HyperCore patches of this series",
    },
    "build": {
        "source": "functions.sh: apply_patch_file (used for the kernel, bbrv3, droidspaces, nethunter, KernelSU patches)",
        "sequence": ["git apply --check, then git apply (exact context, atomic)",
                     "patch -p1 --fuzz=3 --batch --forward --dry-run, then the same without --dry-run",
                     "patch -p1 --fuzz=3 --batch -R --dry-run: treated as already applied"],
        "on failure": "the patch name is appended to skipped-patches.txt and the build continues",
    },
    "differences": [
        {"aspect": "Fuzz", "build": "accepts up to 3 lines of context mismatch through patch(1)",
         "script": "exact context only", "effect": "a `conflict` here may still be applied by the build (tested)"},
        {"aspect": "Already-applied test", "build": "fuzzy reverse (patch -R --fuzz=3)",
         "script": "exact reverse (git apply -R)",
         "effect": "`reverse-applies` can be missing where the build would call a patch already applied (tested)"},
        {"aspect": "Tree state", "build": "patches a tree that earlier steps already changed (sed edits, SUSFS files, "
         "other patches, downloaded NTSync patches that are not in the repository), only for the variant and flags "
         "being built",
         "script": "pristine reference trees plus the repository's own patches in build order, for every group, "
         "regardless of flags",
         "effect": "results can differ in either direction; the 6.1 build skips `common` and `nethunter`, this check "
         "does not"},
        {"aspect": "Required patches", "build": "`require_patch` uses git apply only and aborts the build on failure; "
         "`apply_zram_patch` tries fuzzy forward then reverse, else aborts",
         "script": "reports a status and never aborts", "effect": "a failure here is not necessarily fatal in the "
         "build, and some build failures are not reported here"},
        {"aspect": "Patch discovery", "build": "`find` recursively, `sort -V` on full paths",
         "script": "the group directories only (not recursive), ordered with the same `sort -V` when GNU sort is "
                   "available",
         "effect": "identical order for today's flat directories (tested); a patch in a subdirectory would not be "
                   "reviewed; without GNU sort a natural-sort fallback is used, which differs (the report says so)"},
        {"aspect": "Downloaded targets", "build": "`kernel-patches/susfs` applies to the SUSFS checkout or to the kernel "
         "after the downloaded SUSFS patch; `patches/` applies inside the KernelSU checkout",
         "script": "not checked (`builder-side`)", "effect": "no result for these two groups"},
        {"aspect": "Atomicity", "build": "patch(1) can fail after a successful dry run and leave a partly patched tree "
         "(the build has a branch for it)", "script": "git apply is all-or-nothing on a temporary index",
         "effect": "that failure mode cannot be observed here"},
        {"aspect": "Working tree vs index", "build": "patches a working tree", "script": "patches an index of the "
         "committed tree (same blobs)", "effect": "same for ordinary text patches; file modes and attributes are not "
         "exercised"},
    ],
}


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


ORDER_SORT_V = "sort -V"
ORDER_FALLBACK = "natural sort (fallback: NOT identical to sort -V)"


def order_like_build(paths: list[Path]) -> tuple[list[Path], str]:
    """Order patches the way the build does (`find ... | sort -z -V`). The real `sort -V` is used when available,
    because a hand-written natural sort differs from it (for example `0001-fixup-x` sorts before `0001-fix-x` under
    sort -V, which ranks non-alphanumerics after letters). Without GNU sort the fallback is used and reported."""
    exe = shutil.which("sort")
    if exe and paths:
        data = b"".join(str(p).encode("utf-8", "surrogateescape") + b"\0" for p in paths)
        proc = subprocess.run([exe, "-z", "-V"], input=data, capture_output=True)
        if proc.returncode == 0 and proc.stdout.count(b"\0") == len(paths):
            return [Path(x.decode("utf-8", "surrogateescape")) for x in proc.stdout.split(b"\0") if x], ORDER_SORT_V
    return sorted(paths, key=natural_key), ORDER_FALLBACK


def patch_targets(text: str) -> tuple[list[str], list[str]]:
    """(files that must already exist, files the patch creates). A rename needs its source to exist."""
    must_exist, created = [], []
    for block in re.split(r"(?m)^diff --git ", text)[1:]:
        header, _, body = block.partition("\n")
        m = re.match(r"a/(.*) b/(.*)$", header)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        if re.search(r"(?m)^new file mode", body):
            created.append(b)
        else:
            must_exist.append(a)
    return must_exist, created


def load_patches(hc_root: Path) -> list[dict]:
    entries = []
    for rel, kind in PATCH_GROUPS:
        d = hc_root / rel
        if not d.is_dir():
            continue
        ordered, order_method = order_like_build([x for x in d.iterdir() if x.suffix in (".patch", ".diff")])
        for p in ordered:
            raw = p.read_bytes()
            must, new = patch_targets(raw.decode("utf-8", "replace"))
            entries.append({"group": rel, "kind": kind, "name": p.name, "path": p, "must_exist": must,
                            "created": new, "sha256": hashlib.sha256(raw).hexdigest(), "order": order_method})
    return entries


def run_series(repo: Repo, ref: str, entries: list[dict]) -> dict:
    """Apply the patches in build order to a private temporary index of `ref`. Patches that do not apply are skipped,
    as the build does, so later patches are judged without them. Nothing but the temporary index is written."""
    results = {}
    with TempIndex() as ti:
        env = ti.env
        repo.run("read-tree", ref, env=env)
        for e in entries:
            key = f"{e['group']}/{e['name']}"
            if e["kind"] == "builder":
                results[key] = {"status": "builder-side"}
                continue
            if re.search(r"5\.10|6\.12-or-above", e["name"]):
                results[key] = {"status": "other-line-by-name"}
                continue
            missing = [f for f in e["must_exist"]
                       if not repo.out("ls-files", "--cached", "--", f, env=env).strip()]
            if missing:
                results[key] = {"status": "target-missing", "detail": "missing in tree: " + ", ".join(missing[:3])}
                continue
            pf = str(e["path"])
            fwd = repo.run("apply", "--cached", "--check", pf, env=env, check=False)
            if fwd.returncode == 0:
                repo.run("apply", "--cached", pf, env=env)
                results[key] = {"status": "applies"}
                continue
            first = " | ".join(fwd.stderr.strip().splitlines()[:2])[:200]
            if repo.ok("apply", "--cached", "--check", "-R", pf, env=env):
                results[key] = {"status": "reverse-applies", "detail": "heuristic: the reverse patch applies cleanly; "
                                "the change is probably already present, not proven"}
            else:
                results[key] = {"status": "conflict", "detail": first}
    return results


def analyse_patches(repo: Repo, hc_root: Path, refs: dict[str, str]) -> list[dict]:
    entries = load_patches(hc_root)
    per_ref: dict[str, dict] = {}
    cache: dict[str, dict] = {}
    for label, ref in refs.items():
        sha = rev(repo, ref)
        if sha not in cache:
            cache[sha] = run_series(repo, sha, entries)
        per_ref[label] = cache[sha]
    rows = []
    for e in entries:
        key = f"{e['group']}/{e['name']}"
        st = {label: per_ref[label][key]["status"] for label in refs}
        detail = {label: per_ref[label][key]["detail"] for label in refs if per_ref[label][key].get("detail")}
        ack_states = {st["ack_tip"], st.get("ack_at_gkid_base"), st.get("ack_at_luminaire_base")} - {None}
        drift = len(ack_states) > 1 and e["kind"] == "tree"
        top = st["ack_tip"]
        if e["kind"] == "builder" and e["group"] == "patches":
            rec = "Builder-side (KernelSU): applied inside the KernelSU checkout at build time; not checked here"
        elif e["kind"] == "builder":
            rec = ("Builder-side (SUSFS): applied to the SUSFS checkout or to the kernel after the downloaded SUSFS "
                   "patch, depending on the file and kernel line; cannot be checked from this repository alone")
        elif top == "other-line-by-name":
            rec = "Not for 6.1 (its file name says it targets another line)"
        elif top == "reverse-applies":
            rec = ("Probably already in ACK (heuristic: the exact reverse patch applies cleanly; the build's own test is "
                   "fuzzier). Confirm in the ACK history before dropping it")
        elif top == "applies":
            rec = "Applies to the ACK tip: candidate to carry as a commit in the kernel tree"
            if st.get("gkid_tip") == "reverse-applies":
                rec += " (GKID appears to carry it, heuristic; that is why the 6.1 build skips it)"
        elif top == "target-missing":
            rec = "Needs rework, or is not meant for this tree: a target file does not exist in ACK"
        else:
            rec = ("Needs rework: does not apply exactly in order (conflict, context drift or a missing earlier patch). "
                   "The build's fuzz fallback might still apply it; this check cannot tell")
        if drift:
            rec += ". Result differs between the ACK tip and the trees' own bases"
        rows.append({"group": e["group"], "patch": e["name"], "sha256": e["sha256"], "kind": e["kind"],
                     "files": sorted(set(e["must_exist"] + e["created"])), "status": st, "detail": detail,
                     "heuristic": any(v == "reverse-applies" for v in st.values()), "drift_sensitive": drift,
                     "order_method": e["order"], "recommendation": rec})
    return rows


# --------------------------------------------------------------------------------------------------------------
# analysis assembly
# --------------------------------------------------------------------------------------------------------------

def load_registry(hc_root: Path) -> dict:
    try:
        return json.loads((hc_root / "sources.json").read_text(encoding="utf-8")).get("sources", {})
    except (OSError, ValueError):
        return {}


def summarise(entries: list[dict], patches: list[dict]) -> dict:
    by = collections.Counter(e["bucket"] for e in entries)
    conf = collections.Counter((e["bucket"], e["confidence"]) for e in entries)
    pst = collections.Counter(p["status"]["ack_tip"] for p in patches)
    return {
        "bucket1_in_booting_tree": conf[(1, "in-booting-tree")], "bucket1_review": conf[(1, "review")],
        "bucket2_general": by[2], "bucket3_gkid_specific": by[3],
        "bucket3_candidate_to_drop": conf[(3, "candidate-to-drop")], "neutral": by[5],
        "patches_reviewed": len(patches), "patch_status_at_ack_tip": dict(sorted(pst.items())),
        "files_changed_total": len(entries),
    }


def collect_warnings(a: dict) -> list[str]:
    w = []
    g, l = a["trees"]["gkid"], a["trees"]["luminaire"]
    if g["base"] != l["base"]:
        w.append(f"GKID and Luminaire have different ACK bases ({g['base'][:12]} {g['base_version']} vs "
                 f"{l['base'][:12]} {l['base_version']}). Each is compared with ITS OWN base; the defconfig table "
                 "shows both ACK values.")
    for t in (g, l):
        if t["base_confidence"] == "approximate":
            d = t["base_detail"]
            how = ("all candidates were compared" if d.get("exhaustive") else
                   f"only {d['evaluated']} of {d['candidates']} candidates were compared, so the best candidate may "
                   "have been missed")
            w.append(f"{t['label']}: the ACK base is APPROXIMATE (no shared history; {how}). Its delta may contain ACK "
                     "commits between the true base and the chosen one.")
        if t["base_version_tuple"] and t["tip_version_tuple"] and t["base_version_tuple"] > t["tip_version_tuple"]:
            w.append(f"{t['label']}: its ACK base reports a newer Linux version than the tree itself.")
        c = t["commits"]
        if c.get("available") and c["ack_style_commits"]:
            w.append(f"{t['label']}: {c['ack_style_commits']} commit(s) above the base look like ACK commits "
                     "(ANDROID:/UPSTREAM:/BACKPORT:...). They may come from an ACK branch outside the selected ones.")
        if c.get("available") and c["truncated"]:
            w.append(f"{t['label']}: only the newest {c['listed_count']} of {c['total']} commits are listed; authors "
                     "are counted over all of them.")
    ref = a["ack_reference"]
    if ref["registry_branch"] and ref["registry_branch"] != ref["primary_branch"]:
        w.append(f"The primary ACK branch ({ref['primary_branch']}) differs from the registry's "
                 f"`{REGISTRY_ACK_ID}` branch ({ref['registry_branch']}).")
    if any(p.get("order_method") == ORDER_FALLBACK for p in a["patches"]):
        w.append("GNU `sort -V` was not available, so patches were ordered with a natural-sort fallback that is not "
                 "identical to the build's order. Statuses that depend on the order may differ.")
    for au in (a["ack_history_audit"] or []):
        if not au["monotonic"]:
            w.append(f"ACK history audit: Linux versions along {au['ref']} are NOT monotonic "
                     f"({au['violation_count']} decrease(s)). A bisection on versions would be unreliable there; this "
                     "tool does not use one. See the audit table.")
    return w


def build_analysis(args, repo: Repo, hc_root: Path) -> dict:
    registry = load_registry(hc_root)
    reg_ack, reg_gkid = registry.get(REGISTRY_ACK_ID, {}), registry.get(REGISTRY_GKID_ID, {})
    ack_url = args.ack_url or reg_ack.get("repo")
    ack_branches = args.ack_branch or ([reg_ack["branch"]] if reg_ack.get("branch") else [])
    gkid_url = args.gkid_url or reg_gkid.get("repo")
    gkid_branch = args.gkid_branch or reg_gkid.get("branch")
    lum_url = args.luminaire_url or FALLBACK_LUMINAIRE[0]
    lum_branch = args.luminaire_branch or FALLBACK_LUMINAIRE[1]
    if not (ack_url and ack_branches):
        raise GitError("no ACK source: add `aosp-ack-6.1` to sources.json or pass --ack-url and --ack-branch")
    if not (gkid_url and gkid_branch):
        raise GitError("no GKID reference: pass --gkid-url and --gkid-branch (it is read as a reference only)")
    primary_name, extra = ack_branches[0], ack_branches[1:]
    optional = [b for b in EXTRA_ACK_BRANCHES if b != primary_name] if not extra else []
    for url, br, what in ((gkid_url, gkid_branch, "GKID"), (lum_url, lum_branch, "Luminaire")):
        if (url.rstrip("/"), br) == (ack_url.rstrip("/"), primary_name):
            raise GitError(f"the ACK upstream and the {what} reference are the same repository and branch")

    if not args.no_fetch:
        fetched = fetch_remote(repo, "ack", ack_url, [primary_name] + extra, optional)
        fetch_remote(repo, "gkid", gkid_url, [gkid_branch])
        fetch_remote(repo, "lum", lum_url, [lum_branch])
    else:
        fetched = [primary_name] + extra + optional
    primary_ref = f"refs/remotes/ack/{primary_name}"
    if not repo.ok("rev-parse", "--verify", primary_ref):
        raise GitError(f"primary ACK branch {primary_name} is not in the work directory")
    ack_refs = [primary_ref] + [f"refs/remotes/ack/{b}" for b in fetched
                                if b != primary_name and repo.ok("rev-parse", "--verify", f"refs/remotes/ack/{b}")]
    g_ref, l_ref = f"refs/remotes/gkid/{gkid_branch}", f"refs/remotes/lum/{lum_branch}"
    for r in (g_ref, l_ref):
        if not repo.ok("rev-parse", "--verify", r):
            raise GitError(f"{r} is not in the work directory")

    relation = []
    for other in ack_refs[1:]:
        only_other, only_primary = (int(x) for x in repo.out("rev-list", "--left-right", "--count",
                                                             f"{other}...{primary_ref}").split())
        relation.append({"other": other.replace("refs/remotes/ack/", ""), "primary_ahead": only_primary,
                         "other_ahead": only_other, "other_tip_version": fmt_version(makefile_version(repo, other))})
    ack_reference = {
        "url": ack_url, "primary_branch": primary_name, "primary_tip": rev(repo, primary_ref),
        "primary_tip_version": fmt_version(makefile_version(repo, primary_ref)),
        "other_branches": relation, "registry_branch": reg_ack.get("branch"), "rationale": PRIMARY_ACK_RATIONALE,
        "used_for": {"patch applicability": "primary tip (what HyperCore-kernel would start from), plus each tree's base",
                     "delta, defconfig, ABI symbols, commits": "each tree's own base commit",
                     "base detection": "all listed ACK branches"},
    }

    timelines: dict[str, list[Segment]] = {}
    audit = None
    if args.audit_ack_history:
        timelines[primary_ref] = version_timeline(repo, primary_ref, args.max_timeline)
        audit = [audit_timeline(primary_name, timelines[primary_ref])]

    trees, deltas = {}, {}
    for key, label, ref, url, branch in (("gkid", "GKID", g_ref, gkid_url, gkid_branch),
                                         ("luminaire", "Luminaire", l_ref, lum_url, lum_branch)):
        log(f"{label}: finding the ACK base")
        base = find_base(repo, ref, ack_refs, timelines, args.candidate_samples, args.max_timeline)
        log(f"{label}: base {base['base'][:12]} ({base['confidence']}); diffing")
        deltas[key] = diff_tree(repo, base["base"], ref, args.max_delta_files)
        tip_v, base_v = makefile_version(repo, ref), makefile_version(repo, base["base"])
        commits = (commit_summary(repo, base["base"], ref, args.max_commits) if base["confidence"] == "exact" else
                   {"available": False, "reason": "no shared history with ACK, so the commits above the base cannot "
                                                  "be listed or attributed"})
        trees[key] = {
            "label": label, "role": "read-only reference", "url": url, "branch": branch, "tip": rev(repo, ref),
            "tip_version": fmt_version(tip_v), "tip_version_tuple": tip_v,
            "ack_ref": base["ack_ref"].replace("refs/remotes/ack/", ""), "base": base["base"],
            "base_version": fmt_version(base_v), "base_version_tuple": base_v, "base_method": base["method"],
            "base_confidence": base["confidence"], "base_detail": base["detail"],
            "commits_above_base_incl_merges": base["ahead"], "commits": commits,
        }

    patches, patch_by_file = [], collections.defaultdict(list)
    if not args.skip_patches:
        log("reviewing HyperCore patches in an isolated temporary index")
        refs = {"ack_tip": primary_ref, "gkid_tip": g_ref, "luminaire_tip": l_ref,
                "ack_at_gkid_base": trees["gkid"]["base"], "ack_at_luminaire_base": trees["luminaire"]["base"]}
        patches = analyse_patches(repo, hc_root, refs)
        for p in patches:
            for f in p["files"]:
                patch_by_file[f].append(p["patch"])
        if not patches:
            log("no patches found under kernel-patches/ or patches/")

    entries = build_entries(deltas["gkid"], deltas["luminaire"], patch_by_file, not args.no_samples)
    sg, sl = abi_symbols(deltas["gkid"]), abi_symbols(deltas["luminaire"])

    def fmt(s):
        return sorted(f"{f}::{sym}" for f, sym in s)

    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    now = dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc) if epoch else dt.datetime.now(dt.timezone.utc)
    analysis = {
        "schema": SCHEMA, "generated": now.strftime("%Y-%m-%d %H:%M UTC"),
        "architecture": {"upstream": "Android Common Kernel (ACK)",
                         "references": "GKID and Luminaire are read-only references; neither is a kernel source, "
                                       "foundation or fallback of HyperCore-kernel"},
        "ack_reference": ack_reference, "ack_history_audit": audit, "trees": trees, "files": entries,
        "defconfig": config_rows(repo, trees["gkid"], trees["luminaire"]),
        "abi_symbols": {"both": fmt(sg & sl), "gkid_only": fmt(sg - sl), "luminaire_only": fmt(sl - sg)},
        "patches": patches, "patch_method": PATCH_METHOD,
        "patch_status_columns": ["ack_tip", "gkid_tip", "luminaire_tip", "ack_at_gkid_base", "ack_at_luminaire_base"],
    }
    analysis["summary"] = summarise(entries, patches)
    analysis["warnings"] = collect_warnings(analysis)
    return analysis


# --------------------------------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------------------------------

def esc(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def table(headers, rows) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def pm(x) -> str:
    return "-" if not x else f"+{x['add']}/-{x['del']}"


def render(a: dict, cap: int) -> str:
    """Markdown view of the analysis. It reads only `a`, the same object that becomes the JSON report."""
    L: list[str] = []
    w = L.append
    files, s = a["files"], a["summary"]
    bucket = {i: [e for e in files if e["bucket"] == i] for i in (1, 2, 3, 5)}
    g, l = a["trees"]["gkid"], a["trees"]["luminaire"]
    ref = a["ack_reference"]
    w("# HyperCore Phase 0: delta report\n")
    w(f"Generated {a['generated']}. Read-only analysis: nothing in HyperCore was changed.\n")
    w("> **Direction.** ACK (Android Common Kernel) is the upstream foundation of the future HyperCore-kernel. "
      "GKID and Luminaire appear here only as **read-only references** to find Duchamp-specific adaptations. "
      "Neither is a kernel source, a foundation or a fallback.\n")
    w("## How to read this\n")
    w("- **Luminaire is reported to boot on Duchamp** (the owner's test; not verified here). A change it carries is "
      "compatibility evidence, not proof of necessity. A hardware-looking GKID change it does without is only a "
      "*candidate to drop*. **Only a boot test decides what is required.**\n"
      "- Categories and buckets come from path and content rules. They sort work for a human.\n"
      "- Deltas are measured against each tree's own ACK base, so Linux version drift is not counted as a change. "
      "Lines are compared by SHA-256 of the whitespace-stripped text.\n"
      "- `reverse-applies` for a patch is a **heuristic**: the reverse patch applies cleanly, which suggests but does "
      "not prove the change is already present.\n")
    w("## Inputs\n")
    w(table(["Tree", "Role", "Repository", "Branch", "Tip", "Linux at tip"],
            [["ACK", "upstream", ref["url"], ref["primary_branch"], ref["primary_tip"][:12], ref["primary_tip_version"]]] +
            [[t["label"], t["role"], t["url"], t["branch"], t["tip"][:12], t["tip_version"]] for t in (g, l)]))
    w("## ACK reference\n")
    w(ref["rationale"] + "\n")
    if ref["other_branches"]:
        w(table(["Other ACK branch (base detection only)", "Linux at tip", "Commits only in it", "Commits only in primary"],
                [[o["other"], o["other_tip_version"], o["other_ahead"], o["primary_ahead"]] for o in ref["other_branches"]]))
    w(table(["Analysis", "Measured against"], [[k, v] for k, v in ref["used_for"].items()]))
    if a["ack_history_audit"]:
        w("### ACK history audit\n")
        w("The Linux version was read at every first-parent commit that changes the Makefile (complete, not sampled) "
          "and checked for ordering.\n")
        w(table(["Branch", "Change points", "First", "Last", "Monotonic", "Decreases", "Versions seen in separate stretches"],
                [[x["ref"], x["segments"], x["first_version"], x["last_version"], "yes" if x["monotonic"] else "**NO**",
                  x["violation_count"], ", ".join(x["revisited_versions"][:6]) or "-"] for x in a["ack_history_audit"]]))
        for x in a["ack_history_audit"]:
            if x["violations"]:
                w(table(["Commit", "From", "To"], [[v["commit"], v["from"], v["to"]] for v in x["violations"]]))
    else:
        w("_ACK history audit was not requested (`--audit-ack-history`)._\n")
    w("## ACK base of each tree\n")
    w(table(["Tree", "ACK branch", "Base commit", "Linux at base", "Confidence", "How found", "Commits above base"],
            [[t["label"], t["ack_ref"], t["base"][:12], t["base_version"], t["base_confidence"], t["base_method"],
              t["commits"]["total"] if t["commits"].get("available") else "n/a"] for t in (g, l)]))
    for t in (g, l):
        d = t["base_detail"]
        if t["base_confidence"] == "approximate":
            which = "this" if d.get("reported_version_exact") else "the nearest lower"
            if d.get("exhaustive"):
                w(f"**{t['label']} approximation**: {d['candidates']} ACK commits report {which} Linux version and "
                  f"**all {d['candidates']} were compared**. The chosen commit differs from the tree in "
                  f"{d['paths_differing']} paths (next best {d['runner_up']}; {d['tied_at_best']} tied at the best "
                  "score). Even so, the true base may be a commit that is not on the first-parent history.\n")
            else:
                w(f"**{t['label']} approximation**: {d['candidates']} ACK commits report {which} Linux version; "
                  f"**{d['evaluated']} of them were compared** (an evenly spaced sample, then a finer pass around the "
                  f"best sample). The chosen commit is the best of those compared: it differs from the tree in "
                  f"{d['paths_differing']} paths (next best compared {d['runner_up']}; {d['tied_at_best']} tied among "
                  f"those compared). **It is not guaranteed to be the best of all {d['candidates']}.** The search "
                  "assumes tree similarity changes fairly smoothly along the history; rerun with a larger "
                  "`--candidate-samples` to compare more.\n")
    if a["warnings"]:
        w("## Consistency checks and warnings\n")
        for x in a["warnings"]:
            w(f"- {x}")
        w("")
    w("## Summary\n")
    w(table(["Bucket", "Files"], [
        ["1. Essential Duchamp hardware / boot adaptation candidates (in the tree reported to boot)", s["bucket1_in_booting_tree"]],
        ["1b. Hardware / boot files both trees change differently (review)", s["bucket1_review"]],
        ["2. General performance / functionality", s["bucket2_general"]],
        ["3. GKID-specific, not demonstrably required", s["bucket3_gkid_specific"]],
        ["   of which hardware-looking candidates to drop", s["bucket3_candidate_to_drop"]],
        ["4. HyperCore patches reviewed", s["patches_reviewed"]],
        ["Neutral / unclassified (outside the four buckets)", s["neutral"]],
    ]))
    lum_files = [e for e in files if e["luminaire"]]
    lum_cfg = [r for r in a["defconfig"] if r["change"] != "GKID-only"]
    if not lum_files and not lum_cfg:
        w("- **Luminaire's source tree changes no file and no gki_defconfig option over its ACK base.** That would mean "
          "the device adaptation layer may be empty. Caveat: this compares source trees only. Luminaire's own builds "
          "may add configs, patches or packaging outside that repository, so confirm with the Phase 0 build test.")
    else:
        hw = [e for e in lum_files if e["category"] in HW_CATEGORIES]
        w(f"- Luminaire changes {len(lum_files)} file(s) over its ACK base, {len(hw)} of them hardware / KMI looking.")
    if not s["bucket1_in_booting_tree"] and not any(
            r["hardware_related"] and r["change"] != "GKID-only" for r in a["defconfig"]):
        w("- No hardware / boot adaptation was found in the tree reported to boot. That points to ACK booting without "
          "device-specific changes, which the Phase 0 build test is there to confirm.")
    w("")

    def file_table(title, rows):
        w(f"## {title}\n")
        w(table(["File", "Category", "Who has it", "GKID", "Luminaire", "Why"],
                [[f"`{e['path']}`", e["topic"], e["provenance"] + (f" ({round(e['similarity'] * 100)}% overlap)"
                                                                   if e["similarity"] is not None else ""),
                  pm(e["gkid"]), pm(e["luminaire"]), e["why"]] for e in rows[:cap]]))
        if len(rows) > cap:
            w(f"_{len(rows) - cap} more in the JSON report._\n")

    file_table("Bucket 1: Essential Duchamp hardware and boot adaptation candidates", bucket[1])
    sym = a["abi_symbols"]
    if any(sym.values()):
        w("### KMI symbol-list additions\n")
        names = lambda xs: ", ".join(x.split("::", 1)[1] for x in xs[:6])  # noqa: E731
        w(table(["Added to android/abi_gki_aarch64*", "Count", "Examples"],
                [["both trees", len(sym["both"]), names(sym["both"])],
                 ["Luminaire only", len(sym["luminaire_only"]), names(sym["luminaire_only"])],
                 ["GKID only", len(sym["gkid_only"]), names(sym["gkid_only"])]]))
    differing = [e for e in files if e["provenance"] == "both (different changes)"]
    w("## Files both trees change differently\n")
    w("Neither tree's change is dropped: both are shown, with how much they share.\n")
    w(table(["File", "Category", "Shared lines", "GKID-only lines", "Luminaire-only lines", "GKID", "Luminaire", "Notes"],
            [[f"`{e['path']}`", e["topic"], e["overlap"]["shared_lines"], e["overlap"]["gkid_only_lines"],
              e["overlap"]["luminaire_only_lines"], pm(e["gkid"]), pm(e["luminaire"]), "; ".join(e["notes"])]
             for e in differing[:cap]]))

    w("## Bucket 2: General performance and functionality patches\n")
    topics = collections.defaultdict(list)
    for e in bucket[2]:
        topics[(e["category"], e["topic"])].append(e)
    if not topics:
        w("_none_\n")
    for (cat, topic), rows in sorted(topics.items()):
        w(f"### {CATEGORY_LABEL[cat].split(' (')[0]}: {topic}\n")
        w(table(["File", "Who has it", "GKID", "Luminaire", "HyperCore patch that also touches it"],
                [[f"`{e['path']}`", e["provenance"], pm(e["gkid"]), pm(e["luminaire"]),
                  ", ".join(e["hypercore_patches"][:2])] for e in rows[:cap]]))
    file_table("Bucket 3: GKID-specific changes that are not demonstrably required", bucket[3])

    w("## Bucket 4: Existing HyperCore patches\n")
    w("Applied in build order, in sequence, to a private temporary index of each reference (nothing is checked out). "
      "A patch that does not apply is skipped, as the build does. The ACK column is the **ACK tip**; the "
      "`differs at base` flag means the result changes if ACK is taken at either tree's own base (the JSON has all five "
      "references). `reverse-applies` is a heuristic. **This is not the build's method**: the build also falls back to "
      "`patch --fuzz=3`, so a `conflict` below may still be applied by the build.\n")
    w(table(["Group", "Patch", "ACK tip", "GKID tip", "Luminaire tip", "Recommendation"],
            [[p["group"], p["patch"], p["status"]["ack_tip"] + (" (differs at base)" if p["drift_sensitive"] else ""),
              p["status"]["gkid_tip"], p["status"]["luminaire_tip"], p["recommendation"]] for p in a["patches"]]))

    w("### How this check differs from the build's patch application\n")
    pm_ = a["patch_method"]
    w(f"This check: {pm_['script']['forward']}; reverse: {pm_['script']['reverse']}. The build ({pm_['build']['source']}) "
      "tries, in order: " + "; ".join(f"({i}) {x}" for i, x in enumerate(pm_["build"]["sequence"], 1)) + ". On failure "
      + pm_["build"]["on failure"] + ".\n")
    w(table(["Aspect", "Build", "This check", "Effect"],
            [[d["aspect"], d["build"], d["script"], d["effect"]] for d in pm_["differences"]]))

    w("## gki_defconfig differences\n")
    w("Options each tree changes over ITS OWN ACK base; the two ACK columns are the values at the two bases. "
      "HyperCore's `configs/*.config` fragments are appended later and may override some of these.\n")
    w(table(["Option", "ACK @ GKID base", "GKID", "ACK @ Luminaire base", "Luminaire", "Change", "Hardware-related"],
            [[r["option"], r["ack_at_gkid_base"], r["gkid"], r["ack_at_luminaire_base"], r["luminaire"], r["change"],
              "yes" if r["hardware_related"] else ""] for r in a["defconfig"][:cap]]))
    if len(a["defconfig"]) > cap:
        w(f"_{len(a['defconfig']) - cap} more in the JSON report._\n")

    w("## Commits and authors above the ACK base (for CREDITS)\n")
    for t in (g, l):
        c = t["commits"]
        w(f"### {t['label']}\n")
        if not c.get("available"):
            w(f"Not available: {c['reason']}.\n")
            continue
        w(f"{c['total']} non-merge commit(s) above the base; {c['ack_style_commits']} of them look like ACK-style commits.\n")
        if c["authors"]:
            w("Authors: " + ", ".join(f"{x['name']} ({x['commits']})" for x in c["authors"][:25]) +
              (f", and {len(c['authors']) - 25} more" if len(c["authors"]) > 25 else "") + "\n")
        if c["listed"]:
            shown = c["listed"][:20]
            w(table(["Commit", "Author", "Subject"], [[x["sha"], x["author"], x["subject"]] for x in shown]))
            if len(shown) < c["total"]:
                w(f"_Showing {len(shown)} of {c['total']}; the JSON lists {c['listed_count']}._\n")

    w("## Neutral / unclassified changes\n")
    w(table(["File", "Where", "Who has it", "GKID", "Luminaire"],
            [[f"`{e['path']}`", e["topic"], e["provenance"], pm(e["gkid"]), pm(e["luminaire"])] for e in bucket[5][:cap]]))
    w("## What this report cannot tell you\n")
    w("- Whether anything boots. Only a flash on a real Duchamp does, and every result must be recorded as evidence.\n"
      "- Whether a hardware-looking change is truly required. The booting Luminaire tree is a witness, not a proof: it can "
      "reach the same result by a different route.\n"
      "- Out-of-tree vendor modules (`MTK_kernel_modules`). They are built against the KMI, so a KMI or symbol-list "
      "difference matters even when the source looks unrelated.\n"
      "- Whether a `reverse-applies` patch is really upstream. Check the ACK history.\n"
      "- Anything the heuristics file under the wrong category. The JSON has every file with its raw evidence.\n")
    return "\n".join(L)


# --------------------------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------------------------

def validate_args(args) -> None:
    if args.candidate_samples < 2:
        raise GitError(f"--candidate-samples must be at least 2 (got {args.candidate_samples}): the sampled search "
                       "spaces its samples between the first and the last candidate, so it needs at least two")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", required=True, help="NEW or previously scratch directory (bare repo); outside HyperCore")
    ap.add_argument("--hypercore", default=".", help="root of the HyperCore checkout (patch review, registry defaults)")
    ap.add_argument("--out", default="delta-report.md",
                    help="report path; must be OUTSIDE the HyperCore checkout and the --workdir scratch repository")
    ap.add_argument("--json", dest="json_out", default="delta-report.json",
                    help="JSON path; must be OUTSIDE the HyperCore checkout and the --workdir scratch repository")
    ap.add_argument("--ack-url", help="default: the registry's aosp-ack-6.1 repo")
    ap.add_argument("--ack-branch", action="append",
                    help="repeatable; the FIRST is the primary ACK reference (default: the registry's aosp-ack-6.1 "
                         "branch). Further ones only improve base detection")
    ap.add_argument("--gkid-url", help="read-only REFERENCE; default: the registry's gkid-duchamp-6.1 repo")
    ap.add_argument("--gkid-branch")
    ap.add_argument("--luminaire-url", help="read-only comparison reference")
    ap.add_argument("--luminaire-branch")
    ap.add_argument("--no-fetch", action="store_true", help="reuse what is already in --workdir")
    ap.add_argument("--skip-patches", action="store_true")
    ap.add_argument("--no-samples", action="store_true", help="leave sample source lines out of the reports")
    ap.add_argument("--audit-ack-history", action="store_true",
                    help="read the Linux version along the primary ACK branch's first-parent history and check its order")
    ap.add_argument("--candidate-samples", type=int, default=32,
                    help="candidates compared when a tree has no shared history; at least 2 (default 32)")
    ap.add_argument("--max-timeline", type=int, default=5000)
    ap.add_argument("--max-delta-files", type=int, default=20000)
    ap.add_argument("--max-commits", type=int, default=3000, help="commits listed per tree (authors count all)")
    ap.add_argument("--rows", type=int, default=150, help="rows per table in the Markdown report")
    args = ap.parse_args(argv)

    validate_args(args)
    hc_root = Path(args.hypercore)
    check_output_paths(args.out, args.json_out, str(hc_root), args.workdir)
    repo = ensure_scratch(Path(args.workdir), hc_root)
    analysis = build_analysis(args, repo, hc_root)
    write_report(args.out, render(analysis, args.rows))
    write_report(args.json_out, json.dumps(analysis, indent=2, sort_keys=True, default=str) + "\n")
    log(f"wrote {args.out} and {args.json_out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
