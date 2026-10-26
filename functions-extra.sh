#!/usr/bin/env bash
# Additions to the build pipeline derived from GKID-Kernels (Ahmed Al-Nassif, GPL-3.0). See NOTICE.md.
# Sourced by build.sh after functions.sh.

# Apply a patch in the current directory or abort the whole build.
# (functions.sh:apply_patch_file silently skips patches that don't apply.)
require_patch() {
    local p="$1"
    if git apply --check "$p" 2>/dev/null && git apply "$p"; then
        success "Applied $(basename "$p")"
        return 0
    fi
    error "REQUIRED patch does not apply: $(basename "$p") (cwd: $(pwd))"
    exit 1
}

# Checkout of the KSU fork that setup.sh linked into drivers/
ksu_root_dir() {
    local link="$KSRC/drivers/kernelsu"
    [ -e "$link" ] || { error "drivers/kernelsu not found; did the fork's setup.sh run?"; exit 1; }
    dirname "$(readlink -f "$link")"
}

# Extra managers' signatures (patches/0001-feat-add-multiple-managers.patch), MM=true
add_multi_managers() {
    [ "${MM:-false}" = "true" ] || return 0
    (cd "$(ksu_root_dir)" && require_patch "$PATCHES_DIR/0001-feat-add-multiple-managers.patch")
    VARIANT+="+MM"
}

# Clone a KSU fork, add SUSFS's KernelSU-side patch when the branch lacks it, then run its setup.sh
install_ksu_fork() {
    local repo="$1" ref="$2"
    git ls-remote --exit-code "https://github.com/$repo" "refs/heads/$ref" "refs/tags/$ref" >/dev/null \
        || { error "$repo has no branch/tag '$ref'"; exit 1; }
    git clone -q "https://github.com/$repo" -b "$ref" KernelSU
    [ -f KernelSU/kernel/setup.sh ] || { error "$repo@$ref has no kernel/setup.sh"; exit 1; }

    if susfs_included; then
        clone_susfs
        (
            cd KernelSU
            local sp="$SUSFS_PATCHES/KernelSU/10_enable_susfs_for_ksu.patch"
            if git apply --check "$sp" 2>/dev/null; then
                git apply "$sp" && success "Applied SUSFS KernelSU-side patch"
            else
                warning "SUSFS KernelSU-side patch does not apply; assuming $repo@$ref already ships SUSFS"
            fi
            git add -A
            git -c user.name="gkid-ci" -c user.email="ci@localhost" commit -qm "local patches" || true
        )
    fi

    sed -i '/git pull && echo/d' KernelSU/kernel/setup.sh
    bash KernelSU/kernel/setup.sh "$ref"
    if susfs_included; then apply_susfs_patches; fi
}

# Optional features that need external patches (extra-patches/<name>.patch, optional <name>.config)
apply_extra_features() {
    local feat f
    for feat in LZ4KD REKERNEL; do
        [ "${!feat:-false}" = "true" ] || continue
        f="$WORKDIR/extra-patches/${feat,,}.patch"
        [ -f "$f" ] || { error "$feat is enabled but $f is missing"; exit 1; }
        require_patch "$f"
        VARIANT+="+$feat"
    done
    if [ "${ZSTD:-false}" = "true" ]; then VARIANT+="+ZSTD"; fi
}
