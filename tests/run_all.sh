#!/usr/bin/env bash
# Runs every check of this repository. Usage: bash tests/run_all.sh
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
status=0
echo "== repository validators"
for v in validate_workflows validate_shell validate_python validate_references validate_workflow_graph; do
    python3 ".github/scripts/$v.py" || status=1
done
echo "== source registry validates";         python3 py/sources.py validate        || status=1
echo "== unit tests (registry, gate, pins, workflow)"
python3 -m unittest discover -s tests         || status=1
echo "== backward compatibility with the pre-registry logic (bash)"
bash tests/test_source_compat.sh              || status=1
[ "$status" -eq 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit $status
