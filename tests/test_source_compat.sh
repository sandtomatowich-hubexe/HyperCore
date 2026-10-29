#!/usr/bin/env bash
# Backward-compatibility test for source selection.
#
# Runs the frozen pre-registry logic (tests/legacy/legacy_source_functions.sh) and the registry-backed logic in
# functions.sh / functions-extra.sh side by side, in clean environments, for every kernel line and every
# combination of override variables the README documents. Everything the build reads must come out identical.
#
# One difference is expected and checked separately: for 6.1 the old code left KERNEL_KMI empty, the registry
# fills in android14-6.1. KERNEL_KMI is only read on the non-6.1 NTSync path, so the build is unaffected.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0

# The comparison needs both sides. If the frozen baseline (or the registry) is missing, say so once and stop, instead
# of letting every scenario fail for the same reason.
for required in tests/legacy/legacy_source_functions.sh sources.json py/sources.py functions.sh functions-extra.sh; do
    if [ ! -f "$ROOT/$required" ]; then
        echo "MISSING: $required. The compatibility test cannot compare old and new behavior without it." >&2
        echo "         Restore it (the frozen pre-registry code is tests/legacy/legacy_source_functions.sh) and run again." >&2
        exit 2
    fi
done

run() { # run <legacy|new> [VAR=value ...]  -> prints the resolved values and the exit status
    local mode="$1"; shift
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" WORKDIR="$ROOT" MODE="$mode" "$@" bash -c '
        source "$WORKDIR/functions.sh"
        source "$WORKDIR/functions-extra.sh"
        [ "$MODE" = legacy ] && source "$WORKDIR/tests/legacy/legacy_source_functions.sh"
        resolve_kernel_source_config
        printf "REPO=%s\nBRANCH=%s\nKMI=%s\nREPO_ORIGIN=%s\nBRANCH_ORIGIN=%s\n" \
            "$KERNEL_REPO" "$KERNEL_BRANCH" "$KERNEL_KMI" "$KERNEL_REPO_ORIGIN" "$KERNEL_BRANCH_ORIGIN"
    ' 2>&1
    echo "EXIT=$?"
}

ok()  { pass=$((pass + 1)); }
bad() { fail=$((fail + 1)); echo "FAIL: $1"; shift; printf '%s\n' "$@" | sed 's/^/      /'; }

# compare <description> <expect: same|exit-only> VAR=value ...
compare() {
    local desc="$1" mode="$2"; shift 2
    local old new
    old="$(run legacy "$@")"; new="$(run new "$@")"

    if [ "$mode" = exit-only ]; then   # error wording is allowed to differ, the outcome is not
        [ "$(tail -1 <<<"$old")" = "$(tail -1 <<<"$new")" ] && ok || bad "$desc" "legacy: $(tail -1 <<<"$old")" "new:    $(tail -1 <<<"$new")"
        return
    fi
    case " $* " in *" KERNEL_VERSION=6.1 "*) old="$(grep -v '^KMI=' <<<"$old")"; new="$(grep -v '^KMI=' <<<"$new")";; esac
    if [ "$old" = "$new" ]; then ok; else bad "$desc" "--- legacy" "$old" "--- new" "$new"; fi
}

expect_line() { # expect_line <description> <needle> <haystack>
    if grep -qF -- "$2" <<<"$3"; then ok; else bad "$1" "wanted: $2" "got:" "$3"; fi
}

DEF61="https://github.com/ahmed-alnassif/GKI-Duchamp-6.1"

for v in 5.10 5.15 6.1 6.6 6.12; do
    key="${v//./_}"
    for nh in unset true false; do
        nhv=(); [ "$nh" != unset ] && nhv=("NH=$nh")
        compare "$v NH=$nh defaults"                          same KERNEL_VERSION=$v "${nhv[@]}"
        compare "$v NH=$nh KERNEL_REPO full URL"              same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_REPO=https://example.com/me/kernel
        compare "$v NH=$nh KERNEL_REPO owner/repo"            same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_REPO=me/kernel
        compare "$v NH=$nh KERNEL_REPO equal to the default"  same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_REPO=$DEF61
        compare "$v NH=$nh KERNEL_REPO default + slash"       same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_REPO=$DEF61/
        compare "$v NH=$nh KERNEL_BRANCH"                     same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_BRANCH=my-branch
        compare "$v NH=$nh KERNEL_BRANCH_NH"                  same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_BRANCH_NH=nh-branch
        compare "$v NH=$nh KERNEL_BRANCH + KERNEL_BRANCH_NH"  same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_BRANCH=a KERNEL_BRANCH_NH=b
        compare "$v NH=$nh per-base variables"                same KERNEL_VERSION=$v "${nhv[@]}" "KERNEL_REPO_$key=me/base" "KERNEL_BRANCH_$key=base-br"
        compare "$v NH=$nh per-base beats unsuffixed"         same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_REPO=x/y KERNEL_BRANCH=zz "KERNEL_REPO_$key=me/base" "KERNEL_BRANCH_$key=base-br"
        compare "$v NH=$nh another base's variable is ignored" same KERNEL_VERSION=$v "${nhv[@]}" KERNEL_REPO_9_9=me/other KERNEL_BRANCH_9_9=other
    done
    compare "$v invalid repository (spaces)"        same KERNEL_VERSION=$v "KERNEL_REPO_$key=bad repo"
    compare "$v invalid repository (ftp://)"        same KERNEL_VERSION=$v "KERNEL_REPO_$key=ftp://example.com/x"
    compare "$v invalid repository (no slash)"      same KERNEL_VERSION=$v "KERNEL_REPO_$key=justaname"
    compare "$v invalid branch characters"          same KERNEL_VERSION=$v "KERNEL_BRANCH_$key=bad branch!"
    compare "$v invalid unsuffixed branch"          same KERNEL_VERSION=$v KERNEL_BRANCH='x;y'
done

# Lookups used elsewhere in the build
for v in 5.10 5.15 6.1 6.6 6.12 7.0; do
    for fn in android_release_for_version resolve_susfs_branch; do
        old="$(env -i PATH="$PATH" WORKDIR="$ROOT" bash -c 'source "$WORKDIR/functions.sh"; source "$WORKDIR/tests/legacy/legacy_source_functions.sh"; ( '"$fn $v"' ) 2>&1; echo "EXIT=$?"')"
        new="$(env -i PATH="$PATH" WORKDIR="$ROOT" bash -c 'source "$WORKDIR/functions.sh"; ( '"$fn $v"' ) 2>&1; echo "EXIT=$?"')"
        if [ "$v" = 7.0 ]; then
            # unknown line: android_release_for_version must still answer "unknown"; the susfs lookup must still fail
            [ "$fn" = android_release_for_version ] && expect_line "android release for unknown line" "unknown" "$new"
            [ "$fn" = resolve_susfs_branch ] && expect_line "susfs branch for unknown line fails" "EXIT=1" "$new"
        elif [ "$old" = "$new" ]; then ok; else bad "$fn $v" "--- legacy" "$old" "--- new" "$new"; fi
    done
done
compare "unknown kernel line fails like before" exit-only KERNEL_VERSION=7.0

# Pinned expectations, so a registry edit cannot silently change today's defaults
out="$(run new KERNEL_VERSION=6.1 NH=true)"
expect_line "6.1 + NetHunter default repository" "REPO=$DEF61" "$out"
expect_line "6.1 + NetHunter default branch"     "BRANCH=GKID-NH" "$out"
out="$(run new KERNEL_VERSION=6.1 NH=false)"
expect_line "6.1 default branch"                 "BRANCH=GKID-6.1" "$out"
expect_line "6.1 KMI is filled in (documented difference)" "KMI=android14-6.1" "$out"
out="$(run new KERNEL_VERSION=6.1 NH=true KERNEL_REPO=me/fork)"
expect_line "KERNEL_REPO still overrides the registry" "REPO=https://github.com/me/fork" "$out"
expect_line "KERNEL_REPO origin is reported"           "REPO_ORIGIN=variable KERNEL_REPO" "$out"
out="$(run new KERNEL_VERSION=5.10)"
expect_line "5.10 default repository" "REPO=https://android.googlesource.com/kernel/common" "$out"
expect_line "5.10 default branch"     "BRANCH=android12-5.10-lts" "$out"
out="$(run new KERNEL_VERSION=5.10 KERNEL_REPO=me/fork)"
expect_line "unsuffixed KERNEL_REPO is still ignored on 5.10, with a warning" "only apply to the 6.1 base" "$out"

# KERNEL_SOURCE is new: naming the default source (or "default") must behave exactly like not naming one
for v in 6.1 5.10; do
    compare "$v KERNEL_SOURCE=default is a no-op" same KERNEL_VERSION=$v KERNEL_SOURCE=default
done
compare "6.1 KERNEL_SOURCE=gkid-duchamp-6.1 is a no-op" same KERNEL_VERSION=6.1 NH=true KERNEL_SOURCE=gkid-duchamp-6.1
out="$(run new KERNEL_VERSION=6.1 KERNEL_SOURCE=aosp-ack-5.10)"
expect_line "a source of another line is refused" "is a 5.10 source but the selected kernel line is 6.1" "$out"
expect_line "a source of another line fails the selection" "EXIT=1" "$out"

# The gate that build.sh runs after resolving the source (the workflow runs the same check in its first job)
gate_run() { # gate_run VAR=value ...  -> exit status of enforce_source_gate
    env -i PATH="$PATH" HOME="${HOME:-/tmp}" WORKDIR="$ROOT" "$@" bash -c '
        source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"
        ( resolve_kernel_source_config >/dev/null 2>&1 && enforce_source_gate >/dev/null 2>&1 ); echo "GATE=$?"'
}
expect_line "gate: the default 6.1 source builds (today's behavior)" "GATE=0" "$(gate_run KERNEL_VERSION=6.1 NH=true)"
expect_line "gate: 5.10 is blocked as unsupported for duchamp"        "GATE=1" "$(gate_run KERNEL_VERSION=5.10)"
expect_line "gate: ALLOW_UNVERIFIED does not unlock an unsupported source" "GATE=1" "$(gate_run KERNEL_VERSION=5.10 ALLOW_UNVERIFIED=true)"
expect_line "gate: a repository variable makes 5.10 the user's own source" "GATE=0" "$(gate_run KERNEL_VERSION=5.10 KERNEL_REPO_5_10=me/own-5.10)"
expect_line "gate: an unknown device is refused"                       "GATE=1" "$(gate_run KERNEL_VERSION=6.1 HYPERCORE_DEVICE=marble)"

# Which lines have their source replaced by repository variables
over() { env -i PATH="$PATH" HOME="${HOME:-/tmp}" WORKDIR="$ROOT" "$@" bash -c 'source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"; echo "OVER=[$(overridden_lines)]"'; }
expect_line "no override variables set"                      "OVER=[]"     "$(over NH=true)"
expect_line "KERNEL_REPO_5_10 overrides the 5.10 line"       "OVER=[5.10]" "$(over NH=true KERNEL_REPO_5_10=me/x)"
expect_line "KERNEL_BRANCH overrides 6.1 without NetHunter"  "OVER=[6.1]"  "$(over NH=false KERNEL_BRANCH=dev)"
expect_line "KERNEL_BRANCH is ignored for 6.1 with NetHunter (as before)" "OVER=[]" "$(over NH=true KERNEL_BRANCH=dev)"
expect_line "KERNEL_BRANCH_NH overrides 6.1 with NetHunter"  "OVER=[6.1]"  "$(over NH=true KERNEL_BRANCH_NH=nh-dev)"

# A broken registry stops the build early with an actionable message
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
sed 's#"repo": "https://android.googlesource.com/kernel/common"#"repo": "ftp://nope"#' "$ROOT/sources.json" > "$tmp/sources.json"
out="$(env -i PATH="$PATH" WORKDIR="$ROOT" SOURCES_REGISTRY="$tmp/sources.json" bash -c '
    source "$WORKDIR/functions.sh"; validate_source_registry; echo "EXIT=$?"' 2>&1)"
expect_line "broken registry: names the exact field" "sources.aosp-ack-5.10.repo" "$out"
expect_line "broken registry: validation stops the build" "sources.json is invalid" "$out"
out="$(env -i PATH="$PATH" WORKDIR="$ROOT" SOURCES_REGISTRY="$tmp/sources.json" KERNEL_VERSION=6.1 bash -c '
    source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"; resolve_kernel_source_config; echo "EXIT=$?"' 2>&1)"
expect_line "broken registry: selection never succeeds on it" "sources.aosp-ack-5.10.repo" "$out"

echo "source compatibility: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
