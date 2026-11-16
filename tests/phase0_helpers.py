"""Helpers for the Phase 0 tests: isolated, hermetic git repositories built in temporary directories.

Nothing here touches a real repository. Every git call ignores the user's git configuration and environment."""
import functools
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "phase0" / "delta_report.py"

_counter = [0]


def git_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    env.update(extra)
    return env


def git(cwd, *args, check=True, **env):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=git_env(**env))
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def load_script():
    spec = importlib.util.spec_from_file_location("phase0_delta_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["phase0_delta_report"] = mod
    spec.loader.exec_module(mod)
    return mod


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def makefile(sublevel, extraversion=""):
    return f"VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = {sublevel}\nEXTRAVERSION ={extraversion}\n"


def init_repo(path, branch):
    Path(path).mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    return Path(path)


def commit(repo, files, msg, author=None):
    """Write files (a None value deletes the file), commit, return the commit id. Dates increase, so histories are
    deterministic."""
    for rel, text in files.items():
        if text is None:
            (Path(repo) / rel).unlink()
        else:
            write(repo, rel, text)
    _counter[0] += 1
    stamp = f"2024-01-01T00:{_counter[0] // 60 % 60:02d}:{_counter[0] % 60:02d} +0000"
    env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    if author:
        env.update({"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": f"{author.lower()}@example.invalid"})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg, **env)
    return git(repo, "rev-parse", "HEAD")


def clone(src, dst, branch, start=None):
    subprocess.run(["git", "clone", "-q", str(src), str(dst)], check=True, capture_output=True, env=git_env())
    git(dst, "checkout", "-q", "-b", branch, *([start] if start else []))
    return Path(dst)


def merge(repo, other, msg="merge"):
    _counter[0] += 1
    stamp = f"2024-01-01T01:{_counter[0] // 60 % 60:02d}:{_counter[0] % 60:02d} +0000"
    git(repo, "merge", "-q", "--no-ff", "-m", msg, other, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    return git(repo, "rev-parse", "HEAD")


def orphan_snapshot(src, dst, branch, source_ref, files=None, msg="squashed import"):
    """A tree with NO shared history: the tree of `source_ref` (plus `files`) as one orphan commit."""
    clone(src, dst, "scratch-tmp")
    git(dst, "checkout", "-q", source_ref)
    git(dst, "checkout", "-q", "--orphan", branch)
    return Path(dst), commit(dst, files or {}, msg)


def scratch(tmp, name="scratch"):
    mod = load_script()
    wd = Path(tmp) / name
    return mod, mod.ensure_scratch(wd, Path(tmp) / "no-hypercore-here")


def fetch(mod, repo, remote, url, branches):
    return mod.fetch_remote(repo, remote, str(url), branches)


def run_cli(tmp, hc, ack, ack_branch, gkid, gkid_branch, lum, lum_branch, extra=(), env=None, workdir=None,
            out_name="r"):
    out_md, out_json = Path(tmp) / f"{out_name}.md", Path(tmp) / f"{out_name}.json"
    cmd = [sys.executable, str(SCRIPT), "--workdir", str(workdir or Path(tmp) / "work"), "--hypercore", str(hc),
           "--ack-url", str(ack), "--ack-branch", ack_branch, "--gkid-url", str(gkid), "--gkid-branch", gkid_branch,
           "--luminaire-url", str(lum), "--luminaire-branch", lum_branch, "--out", str(out_md), "--json",
           str(out_json), *extra]
    full_env = {**os.environ, "SOURCE_DATE_EPOCH": "1700000000", **(env or {})}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=full_env)
    result = None
    if proc.returncode == 0:
        result = (json.loads(out_json.read_text()), out_md.read_text())
    return proc, result


def tmpdir():
    return tempfile.mkdtemp(prefix="hc-phase0-test-")


# ---- Hard-link capability and test policy

OPT_OUT_ENV = "HYPERCORE_ALLOW_NO_HARDLINKS"


def is_android():
    return (
        sys.platform == "android"
        or "ANDROID_ROOT" in os.environ
        or "ANDROID_DATA" in os.environ
    )


def probe_hardlinks():
    if not callable(getattr(os, "link", None)):
        return False, "Python os.link is unavailable"

    try:
        with tempfile.TemporaryDirectory(prefix="hc-hardlink-probe-") as name:
            source = Path(name) / "source"
            target = Path(name) / "target"
            source.write_text("probe")
            os.link(source, target)
            return True, "hard links supported"
    except (OSError, NotImplementedError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def hardlink_policy():
    supported, reason = probe_hardlinks()

    if supported:
        return "run", reason

    if is_android() or os.environ.get(OPT_OUT_ENV) == "1":
        return "skip", reason

    return "fail", reason


def needs_hardlinks(test):
    @functools.wraps(test)
    def wrapped(self, *args, **kwargs):
        policy, reason = hardlink_policy()

        if policy == "skip":
            self.skipTest(reason)

        if policy == "fail":
            self.fail(f"Hard links are required but unavailable: {reason}")

        return test(self, *args, **kwargs)

    return wrapped
