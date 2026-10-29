#!/usr/bin/env bash
# FROZEN COPY of the source-selection logic as it was before the source registry (sources.json) existed.
# It is a regression oracle only: tests/test_source_compat.sh runs this and the registry-backed code side by side
# and fails if they ever disagree. Do not edit it, and do not source it from the build.
# Copied verbatim from functions.sh (lines 3-66) and functions-extra.sh (resolve_kernel_source_config).
declare -A ANDROID_RELEASE_FOR_KVER=(
  ["5.10"]="12"
  ["5.15"]="13"
  ["6.1"]="14"
  ["6.6"]="15"
  ["6.12"]="16"
)

declare -A GKI_BRANCH=(
  ["5.10"]="android12-5.10"
  ["5.15"]="android13-5.15"
  ["6.6"]="android15-6.6"
  ["6.12"]="android16-6.12"
)

declare -A GKI_AOSP_BRANCH=(
  ["5.10"]="android12-5.10-lts"
  ["5.15"]="android13-5.15-lts"
  ["6.6"]="android15-6.6-lts"
  ["6.12"]="android16-6.12-lts"
)

declare -A GKI_SUSFS_BRANCH=(
  ["5.10"]="gki-android12-5.10"
  ["5.15"]="gki-android13-5.15"
  ["6.1"]="gki-android14-6.1"
  ["6.6"]="gki-android15-6.6"
  ["6.12"]="gki-android16-6.12"
)

declare -A GKI_SUSFS_COMMIT=(
#  ["6.1"]="153f88df3be2501d2d33364f8fe05247aecb3cef"
#  ["6.6"]="3f0b811b2e105afd8dc858cd71389090e01f404b"
#  ["6.12"]="fb58aef70a9c2aca8f0f85fba14017af94c4e789"
)

android_release_for_version() {
  echo "${ANDROID_RELEASE_FOR_KVER[$1]:-unknown}"
}

resolve_kernel_source() {
  local ver="$1"
  local branch="${GKI_AOSP_BRANCH[$ver]:-}"
  local kmi="${GKI_BRANCH[$ver]:-}"

  if [ -z "$branch" ]; then
    error "No AOSP GKI branch mapped for KERNEL_VERSION='$ver' (see GKI_AOSP_BRANCH in functions.sh)"
    exit 1
  fi

  echo "https://android.googlesource.com/kernel/common|$branch|$kmi"
}

resolve_susfs_branch() {
  local ver="$1"
  local branch="${GKI_SUSFS_BRANCH[$ver]:-}"

  if [ -z "$branch" ]; then
    error "No susfs4ksu branch mapped for KERNEL_VERSION='$ver'"
    exit 1
  fi

  echo "$branch"
}

# Where the kernel comes from, and why. Most specific first:
#   1. KERNEL_REPO_<base> / KERNEL_BRANCH_<base>   any base, written like 6_1, 5_10, 6_12
#   2. 6.1 only: KERNEL_REPO, and KERNEL_BRANCH (or KERNEL_BRANCH_NH for NetHunter builds)
#   3. built-in defaults: the device source for 6.1, the Android Common Kernel -lts branches for the other lines
# Sets KERNEL_REPO, KERNEL_BRANCH, KERNEL_KMI and, for the build summary, where each value came from.
resolve_kernel_source_config() {
    local key="${KERNEL_VERSION//./_}" repo="" branch="" kmi="" repo_src="" branch_src="" v
    local def_repo_61="https://github.com/ahmed-alnassif/GKI-Duchamp-6.1"

    if kernel_version_eq "$KERNEL_VERSION" "6.1"; then
        repo="$def_repo_61"; repo_src="built-in default (device source for 6.1)"
        if [ -n "${KERNEL_REPO:-}" ] && [ "${KERNEL_REPO%/}" != "$def_repo_61" ]; then
            repo="$KERNEL_REPO"; repo_src="variable KERNEL_REPO"
        fi
        if [ "${NH:-false}" = "true" ]; then
            branch="GKID-NH"; branch_src="built-in default (NetHunter build)"
            if [ -n "${KERNEL_BRANCH_NH:-}" ]; then branch="$KERNEL_BRANCH_NH"; branch_src="variable KERNEL_BRANCH_NH"; fi
        else
            branch="GKID-6.1"; branch_src="built-in default"
            if [ -n "${KERNEL_BRANCH:-}" ]; then branch="$KERNEL_BRANCH"; branch_src="variable KERNEL_BRANCH"; fi
        fi
    else
        IFS='|' read -r repo branch kmi <<< "$(resolve_kernel_source "$KERNEL_VERSION")"
        [ -n "$repo" ] && [ -n "$branch" ] || { error "No kernel source is defined for base $KERNEL_VERSION"; exit 1; }
        repo_src="built-in default (Android Common Kernel)"; branch_src="built-in default"
    fi

    if ! kernel_version_eq "$KERNEL_VERSION" "6.1" && { [ -n "${KERNEL_REPO:-}" ] || [ -n "${KERNEL_BRANCH:-}" ]; }; then
        warning "KERNEL_REPO and KERNEL_BRANCH only apply to the 6.1 base; use KERNEL_REPO_${key} and KERNEL_BRANCH_${key} for $KERNEL_VERSION"
    fi
    v="KERNEL_REPO_$key";   if [ -n "${!v:-}" ]; then repo="${!v}";   repo_src="variable $v";   fi
    v="KERNEL_BRANCH_$key"; if [ -n "${!v:-}" ]; then branch="${!v}"; branch_src="variable $v"; fi

    local raw="$repo"
    repo="$(normalize_kernel_repo "$raw")" \
        || { error "Kernel source '$raw' (${repo_src}) must be an https:// URL or owner/repo"; exit 1; }
    [[ "$branch" =~ ^[A-Za-z0-9._/+-]+$ ]] \
        || { error "Kernel branch '$branch' (${branch_src}) contains characters that are not allowed"; exit 1; }

    KERNEL_REPO="$repo"; KERNEL_BRANCH="$branch"; KERNEL_KMI="$kmi"
    KERNEL_REPO_ORIGIN="$repo_src"; KERNEL_BRANCH_ORIGIN="$branch_src"
    log "Kernel source: $KERNEL_REPO ($repo_src), branch $KERNEL_BRANCH ($branch_src)"
}
