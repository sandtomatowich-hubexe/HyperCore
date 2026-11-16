#!/usr/bin/env bash
# termux-verify.sh -- run from the ROOT of the HyperCore checkout:   bash termux-verify.sh
#
# Shows why the hard-link tests may skip on this device (steps 1-2, informational) and verifies, WITHOUT hard links,
# that reports are written by atomic replacement (step 3) and that the Phase 0 isolation tests pass (step 4).
#
# Exit status:   0  steps 3 and 4 both passed
#                1  step 3 or step 4 failed (or errored)
#                2  not run from the root of a HyperCore checkout; nothing was run
#
# There is deliberately no `set -e`: every step runs and is reported, and failures are collected into the exit status.
# Nothing is written into the checkout (bytecode writing is disabled); temporary files are removed on every exit path.
set -u
export PYTHONDONTWRITEBYTECODE=1

if [ ! -f phase0/delta_report.py ] || [ ! -d tests ]; then
  echo "error: run this from the root of the HyperCore checkout (phase0/delta_report.py and tests/ were not found here)" >&2
  exit 2
fi

tmp=""
cleanup() { if [ -n "$tmp" ]; then rm -rf "$tmp"; fi; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "== 1. Platform facts (informational)"
python3 - <<'PY'
import os, sys, sysconfig
print("python        :", sys.version.split()[0])
print("sys.platform  :", sys.platform, "(Python 3.13+ reports 'android' on Android)")
print("platform tag  :", sysconfig.get_platform())
print("os.link       :", "available" if hasattr(os, "link") else "MISSING from this Python build")
print("HAVE_LINK     :", sysconfig.get_config_var("HAVE_LINK"), "(None = not recorded; 1 = Python was built with link())")
print("os.symlink    :", "available" if hasattr(os, "symlink") else "MISSING")
PY
echo
echo "== 2. Does the OS itself allow hard links? (coreutils ln, outside Python; informational)"
tmp="$(mktemp -d)" || tmp=""
if [ -z "$tmp" ] || [ ! -d "$tmp" ]; then
  tmp=""  # nothing to clean up, and nothing below may be created relative to an empty path
  echo "error: could not create a temporary directory (mktemp -d failed); check that \$TMPDIR exists and is writable" >&2
  exit 1
fi
: > "$tmp/a"
if ln "$tmp/a" "$tmp/b" 2>"$tmp/err"; then
  echo "ln a b       : works -> the platform allows hard links; a missing os.link would then be the Python build"
else
  echo "ln a b       : FAILS ($(cat "$tmp/err")) -> the platform/OS blocks hard links; the Python build cannot change that"
fi
rm -rf "$tmp"
tmp=""
echo
echo "== 3. Atomic report replacement, verified without hard links (open handle on the old file)"
python3 - <<'PY'
import importlib.util, pathlib, sys, tempfile

ok = False
# The temporary directory is created first and removed by the context manager even if anything below raises.
with tempfile.TemporaryDirectory(prefix="hc-termux-verify-") as name:
    try:
        d = pathlib.Path(name)
        spec = importlib.util.spec_from_file_location("delta_report", "phase0/delta_report.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        t = d / "r.md"
        t.write_text("OLD\n")
        ino = t.stat().st_ino
        with open(t) as handle:
            m.write_report(str(t), "NEW\n")
            old_seen = handle.read()
        new_content = t.read_text()
        replaced = t.stat().st_ino != ino
        leftovers = sorted(p.name for p in d.iterdir() if p.name != "r.md")
        print("old handle still sees   :", repr(old_seen), "(expected 'OLD\\n': the old file was NOT written through)")
        print("new file content        :", repr(new_content), "(expected 'NEW\\n')")
        print("inode changed           :", replaced, "(expected True: replaced, not rewritten in place)")
        print("leftover temp files     :", leftovers, "(expected [])")
        ok = old_seen == "OLD\n" and new_content == "NEW\n" and replaced and leftovers == []
    except Exception as exc:  # any error is a failure of this check, reported and reflected in the exit status
        print(f"error while checking    : {type(exc).__name__}: {exc}")
print("RESULT                  :", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PY
step3=$?
echo
echo "== 4. The test class (hard-link tests may show SKIPPED on Android; everything else must be ok)"
python3 - <<'PY'
import io, sys, unittest


class R(unittest.TextTestResult):
    def _n(self, t):
        return t.id().split(".")[-1].replace("test_", "")[:78]

    def addSuccess(self, t):
        super().addSuccess(t)
        print("  ok      ", self._n(t))

    def addSkip(self, t, why):
        super().addSkip(t, why)
        print("  SKIPPED ", self._n(t), "-", why[:100])

    def addFailure(self, t, e):
        super().addFailure(t, e)
        print("  FAIL    ", self._n(t))

    def addError(self, t, e):
        super().addError(t, e)
        print("  ERROR   ", self._n(t))


sys.path.insert(0, ".")
stream = io.StringIO()
res = None
try:
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_phase0_isolation.OutputFileEdgeCases")
    res = unittest.TextTestRunner(stream=stream, resultclass=R).run(suite)
except Exception:  # the tests could not even be loaded (for example an error at import time)
    import traceback
    stream.write("The tests could not be loaded or run:\n" + traceback.format_exc())
# The runner's own report (failure and error tracebacks, the Ran/OK/FAILED lines) was captured above; show it now.
print("\n-- unittest output --")
print(stream.getvalue().rstrip())
print("-- end of unittest output --")
if res is None:
    print("\nran=0 failures=0 errors=1 skipped=0")
    print("RESULT: FAIL")
    sys.exit(1)
executed = res.testsRun - len(res.skipped)
passed = res.wasSuccessful() and executed > 0  # all-skipped or nothing-ran is not a pass
print(f"\nran={res.testsRun} failures={len(res.failures)} errors={len(res.errors)} skipped={len(res.skipped)}")
print("RESULT:", "PASS" if passed else "FAIL")
sys.exit(0 if passed else 1)
PY
step4=$?
echo
echo "== Summary"
label() { if [ "$1" -eq 0 ]; then echo PASS; else echo "FAIL (exit $1)"; fi; }
echo "  step 3  atomic replacement without hard links : $(label "$step3")"
echo "  step 4  isolation test class                  : $(label "$step4")"
if [ "$step3" -eq 0 ] && [ "$step4" -eq 0 ]; then
  echo "OVERALL: PASS"
  exit 0
fi
echo "OVERALL: FAIL"
exit 1
