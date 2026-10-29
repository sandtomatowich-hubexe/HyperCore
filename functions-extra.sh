#!/usr/bin/env bash
# Additions to the build pipeline derived from GKID-Kernels (Ahmed Al-Nassif, GPL-3.0). See NOTICE.md.
# Integration steps for KernelSU-Next, BakaSU, FinaSU and Re-Kernel follow WildKernels/GKI_KernelSU_SUSFS
# and LingLuo17/AnyKernel3 (both GPL-3.0), see CREDITS.md. Sourced by build.sh after functions.sh.

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

# Checkout of the KSU fork that was linked into drivers/
ksu_root_dir() {
    local link="$KSRC/drivers/kernelsu"
    [ -e "$link" ] || { error "drivers/kernelsu not found; no KSU fork was integrated"; exit 1; }
    dirname "$(readlink -f "$link")"
}

# Extra managers' signatures (patches/0001-feat-add-multiple-managers.patch), MM=true.
# The patch targets tiann's apk_sign.c layout. Forks with their own multi-manager support may not
# match it; then the fork's built-in support is used and the +MM tag is left off the zip name.
add_multi_managers() {
    [ "${MM:-false}" = "true" ] || return 0
    local root; root="$(ksu_root_dir)"
    if (cd "$root" && git apply --check "$PATCHES_DIR/0001-feat-add-multiple-managers.patch" 2>/dev/null); then
        (cd "$root" && git apply "$PATCHES_DIR/0001-feat-add-multiple-managers.patch")
        success "Extra manager signatures applied"
        VARIANT+="+MM"
    elif [ "$KSU" = "BSU" ] && grep -rqs --include='Kconfig*' 'KSU_MULTI_MANAGER_SUPPORT' "$root/kernel"; then
        export MM_KCONFIG=true
        log "BakaSU has its own multi-manager option; enabling CONFIG_KSU_MULTI_MANAGER_SUPPORT"
        VARIANT+="+MM"
    else
        warning "Multi-manager patch does not fit this fork; using the fork's own manager support"
    fi
}

# In-tree integration of a KSU checkout already cloned into ./KernelSU
link_ksu_tree() {
    local drv="$KSRC/drivers"
    [ ! -e "$drv/kernelsu" ] || { error "drivers/kernelsu already exists"; exit 1; }
    [ -f KernelSU/kernel/Kconfig ] && [ -f KernelSU/kernel/Makefile ] \
        || { error "KernelSU checkout has no kernel/Kconfig or kernel/Makefile"; exit 1; }
    ln -s "$(realpath --relative-to="$drv" "$KSRC/KernelSU/kernel")" "$drv/kernelsu"
    grep -q 'obj-.*CONFIG_KSU.*kernelsu/' "$drv/Makefile" || printf '\nobj-$(CONFIG_KSU) += kernelsu/\n' >> "$drv/Makefile"
    grep -q 'source "drivers/kernelsu/Kconfig"' "$drv/Kconfig" || sed -i '/endmenu/i source "drivers/kernelsu/Kconfig"' "$drv/Kconfig"
}

# KernelSU-Next with SUSFS: pershoot's dev-susfs fork already ships SUSFS (no KernelSU-side patch).
# Steps follow WildKernels/GKI_KernelSU_SUSFS.
install_ksun() {
    local repo="${KSUN_REPO:-pershoot/KernelSU-Next}" ref="${KSUN_REF:-dev-susfs}"
    if ! git ls-remote --exit-code "https://github.com/$repo" "refs/heads/$ref" >/dev/null 2>&1; then
        warning "$repo has no branch '$ref'; trying KernelSU-Next/KernelSU-Next"
        repo="KernelSU-Next/KernelSU-Next"
        git ls-remote --exit-code "https://github.com/$repo" "refs/heads/$ref" >/dev/null 2>&1 \
            || { error "No branch '$ref' on pershoot/KernelSU-Next or $repo (set KSUN_REF)"; exit 1; }
    fi
    log "KernelSU-Next from $repo @ $ref"
    export KSUN_USED_REPO="$repo"
    git clone -q "https://github.com/$repo" -b "$ref" KernelSU
    link_ksu_tree

    # Upstream's Kbuild hard-errors at parse time (mrproper) when neither KPROBES nor KSU_SUSFS is set yet
    sed -i 's/$(error "KernelSU requires either CONFIG_KPROBES=y or CONFIG_KSU_SUSFS=y")/$(warning "KernelSU requires either CONFIG_KPROBES=y or CONFIG_KSU_SUSFS=y")/' KernelSU/kernel/Kbuild

    clone_susfs
    if [ "$repo" = "pershoot/KernelSU-Next" ]; then
        # pershoot's two susfs4ksu fixes (touch only kernel_patches/fs/susfs.c)
        ( cd "$SUSFS_DIR" \
          && git fetch -q https://gitlab.com/pershoot/susfs4ksu.git \
               6387756d64b3ab9ececefc4523627a9ee05a7519 161c058fed5544a6573d87124f346dadd5c4962d \
          && git cherry-pick 6387756d64b3ab9ececefc4523627a9ee05a7519 \
          && git cherry-pick 161c058fed5544a6573d87124f346dadd5c4962d ) \
          || { error "pershoot susfs4ksu fixes failed to apply"; exit 1; }
    fi
    apply_susfs_patches
}

# BakaSU: kernel-side compat so the susfs4ksu 50_add patch builds and links.
# The SUSFS patch calls a few hooks that BakaSU may define globally, define statically, or not define at all.
# Each hook is classified from the final source (scripts/bakasu/classify_hooks.py) and handled accordingly:
#   global  -> nothing to do, BakaSU's own implementation is linked
#   static  -> the `static` keyword is removed so BakaSU's real implementation is exported
#   missing -> a weak stub is generated (a strong definition anywhere else always wins, so it can never clash)
# Approach for the stubs from WildKernels/GKI_KernelSU_SUSFS, see CREDITS.md.
bakasu_compat() {
    local root kb mk tool="$WORKDIR/scripts/bakasu/classify_hooks.py"
    local sym st file row refs=() need=() expo=() body="" rows=()
    root="$(ksu_root_dir)"; kb="$root/kernel/Kbuild"; mk="$root/kernel/tools/susfs_compat.mk"

    log "BakaSU @ $(command git -C "$root" rev-parse --short HEAD 2>/dev/null || echo unknown) | SUSFS @ $(command git -C "$SUSFS_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"

    # Kbuild refuses code that isn't a git submodule
    sed -i 's/$(error You should use $(REPO_NAME) as a git submodule instead of copying code directly)/$(warning You should use $(REPO_NAME) as a git submodule instead of copying code directly)/' "$kb"

    # BakaSU can refuse SUSFS revisions it has not caught up with. That guard exists to prevent a kernel that
    # builds but misbehaves, so it is only bypassed on explicit request.
    if [ -f "$mk" ] && grep -q 'Please wait susfs update' "$mk"; then
        if [ "${BSU_ALLOW_SUSFS_MISMATCH:-false}" = "true" ]; then
            warning "Removing BakaSU's SUSFS hard stop on request (BSU_ALLOW_SUSFS_MISMATCH=true); this BakaSU/SUSFS pair is NOT confirmed to work"
            sed -i '/susfs has NOT update with latest KernelSU source/,$d' "$mk"
        else
            error "BakaSU blocks this SUSFS revision ($(command git -C "$SUSFS_DIR" rev-parse --short HEAD)). Pin SUSFS with the SUSFS_COMMIT variable, pin BakaSU with BSU_REF, or set BSU_ALLOW_SUSFS_MISMATCH=true to override at your own risk."
            exit 1
        fi
    fi

    # Only hooks the patched kernel really calls matter
    for sym in my_write_context my_write_access my_setprocattr ksu_setup_preinit_file ksu_preinit_handle_post_execveat; do
        if grep -rqw --include='*.c' --include='*.h' "$sym" "$KSRC/fs" "$KSRC/security" "$KSRC/kernel" "$KSRC/include" "$KSRC/mm" 2>/dev/null; then
            refs+=("$sym")
        fi
    done
    ((${#refs[@]})) || { log "The patched kernel calls none of the BakaSU SUSFS hooks"; return 0; }

    mapfile -t rows < <(python3 "$tool" "$root/kernel" "${refs[@]}")
    {
        echo "### BakaSU SUSFS hook check"
        echo "| Hook | State in BakaSU |"
        echo "|---|---|"
    } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    for row in "${rows[@]}"; do
        read -r sym st file <<< "$row"
        echo "| \`$sym\` | $st |" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
        case "$st" in
            global)  log "$sym: provided by BakaSU (${file#$root/})" ;;
            static)  expo+=("$sym") ;;
            missing) need+=("$sym") ;;
            *)       error "Unexpected classifier output: $row"; exit 1 ;;
        esac
    done

    if ((${#expo[@]})); then
        python3 "$tool" --export-static "$root/kernel" "${expo[@]}" >/dev/null
        warning "Exported BakaSU's static hooks: ${expo[*]}"
    fi

    ((${#need[@]})) || return 0
    for sym in "${need[@]}"; do
        case "$sym" in
            my_write_context)  body+='__attribute__((weak)) ssize_t my_write_context(struct file *file, char *buf, size_t size) { return -EINVAL; }'$'\n' ;;
            my_write_access)   body+='__attribute__((weak)) ssize_t my_write_access(struct file *file, char *buf, size_t size) { return -EINVAL; }'$'\n' ;;
            my_setprocattr)    body+='__attribute__((weak)) int my_setprocattr(const char *name, void *value, size_t size) { return 0; }'$'\n' ;;
            ksu_setup_preinit_file) body+='__attribute__((weak)) int ksu_setup_preinit_file(struct filename **filename_ptr, struct user_arg_ptr *argv_user) { return -EINVAL; }'$'\n' ;;
            ksu_preinit_handle_post_execveat) body+='__attribute__((weak)) int ksu_preinit_handle_post_execveat(int *fd, struct filename **filename_ptr, void *argv_user, void *envp_user, int *flags, int *retval, int *preinit_fd) { return 0; }'$'\n' ;;
        esac
    done
    { printf '%s\n' '/* Weak SUSFS compat stubs: only used when BakaSU defines no strong version (approach from WildKernels/GKI_KernelSU_SUSFS) */' '#include <linux/fs.h>' 'struct filename;' 'struct user_arg_ptr;'; printf '%s' "$body"; } > "$root/kernel/bakasu_susfs_compat.c"
    grep -q 'bakasu_susfs_compat.o' "$kb" || printf '\n# SUSFS compat stubs\nobj-y += bakasu_susfs_compat.o\n' >> "$kb"
    warning "Added weak SUSFS compat stubs for: ${need[*]}"
}

# FinaSU: its kernel/setup.sh clones upstream tiann/KernelSU, so integrate it in-tree by hand,
# then trust the official FinaSU manager through the second signature slot in Kbuild.
# Approach taken from LingLuo17/AnyKernel3 and WildKernels/GKI_KernelSU_SUSFS.
install_finasu() {
    local ref="${FINA_REF:-}"
    git clone -q https://github.com/LyraVoid/FinaSU.git KernelSU
    if [ -n "$ref" ]; then git -C KernelSU checkout -q "$ref" || { error "FinaSU has no ref '$ref'"; exit 1; }; fi
    link_ksu_tree

    if susfs_included; then
        if [ -f KernelSU/kernel/extension/susfs.c ]; then
            log "FinaSU ships SUSFS natively; not applying the susfs4ksu KernelSU-side patch"
        else
            warning "FinaSU checkout has no native SUSFS; applying susfs4ksu's KernelSU-side patch"
            clone_susfs
            (cd KernelSU && require_patch "$SUSFS_PATCHES/KernelSU/10_enable_susfs_for_ksu.patch")
        fi
    fi
    if kernel_version_lt "$KERNEL_VERSION" 6.6; then
        warning "FinaSU on kernels < 6.6 usually needs the selinux_hide fixes from LingLuo17/AnyKernel3 (susfs_fixes/apply.sh) or WildKernels (finasu_compat/); not included yet"
    fi

    # Manager certificate -> second trust slot (otherwise the official KernelSU manager takes over)
    local apk json url size hash
    apk="$(mktemp -d)/manager.apk"
    json="$(curl -fsS --max-time 30 ${GITHUB_TOKEN:+-H "Authorization: Bearer $GITHUB_TOKEN"} https://api.github.com/repos/LyraVoid/FinaSU/releases/latest)" \
        || { error "Could not read FinaSU's latest release"; exit 1; }
    url="$(printf '%s' "$json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(next(a['browser_download_url'] for a in d.get('assets',[]) if a['name'].endswith('.apk')))" 2>/dev/null || true)"
    [ -n "$url" ] || { error "FinaSU latest release has no APK asset"; exit 1; }
    curl -fsSL --retry 3 --max-time 300 -o "$apk" "$url" || { error "Could not download $url"; exit 1; }
    read -r size hash < <(python3 "$WORKDIR/scripts/finasu/extract_manager_cert.py" "$apk" | head -n1)
    [ -n "$size" ] && [ -n "$hash" ] || { error "Could not extract the FinaSU manager certificate"; exit 1; }
    log "FinaSU manager certificate: size=$size sha256=$hash"
    awk -v size="$size" -v hash="$hash" '
        !done && /^ccflags-y \+= -DEXPECTED_HASH=/ {
            print
            print "KSU_EXPECTED_SIZE2 := " size
            print "KSU_EXPECTED_HASH2 := " hash
            print "KSU_SECOND_SIGNATURE_RELEASE := 1"
            done = 1; next
        }
        { print }' KernelSU/kernel/Kbuild > KernelSU/kernel/Kbuild.new && mv KernelSU/kernel/Kbuild.new KernelSU/kernel/Kbuild
    grep -q '^KSU_EXPECTED_HASH2 := ' KernelSU/kernel/Kbuild \
        || { error "Kbuild injection failed (anchor line missing; FinaSU's Kbuild layout changed)"; exit 1; }
}

# Variant tags for optional zram features
apply_extra_features() {
    local feat f
    if [ "${ZSTD:-false}" = "true" ]; then VARIANT+="+ZSTD"; fi
}

# Re-Kernel: built in from Sakion-Team/Re-Kernel (steps from LingLuo17/AnyKernel3)
apply_rekernel() {
    [ "${REKERNEL:-false}" = "true" ] || return 0
    local tmp=/tmp/rekernel d="$KSRC/drivers/rekernel"
    rm -rf "$tmp" "$d"
    git clone -q --depth 1 https://github.com/Sakion-Team/Re-Kernel.git "$tmp"
    [ -d "$tmp/LKM-Source" ] || { error "Re-Kernel has no LKM-Source directory (upstream layout changed)"; exit 1; }
    mkdir -p "$d" && cp -a "$tmp/LKM-Source/." "$d/"
    sed -i 's/^obj-m := rekernel\.o$/obj-$(CONFIG_REKERNEL) += rekernel.o/' "$d/Makefile"
    grep -qF 'ccflags-$(CONFIG_REKERNEL_LEGACY_NETLINK) += -DLEGACY_NETLINK' "$d/Makefile" \
        || echo 'ccflags-$(CONFIG_REKERNEL_LEGACY_NETLINK) += -DLEGACY_NETLINK' >> "$d/Makefile"
    sed -i '/^[[:space:]]*depends on MODULES[[:space:]]*$/d' "$d/Kconfig"
    grep -qF 'source "drivers/rekernel/Kconfig"' "$KSRC/drivers/Kconfig" || sed -i '/^endmenu$/i source "drivers/rekernel/Kconfig"' "$KSRC/drivers/Kconfig"
    grep -qF 'obj-$(CONFIG_REKERNEL) += rekernel/' "$KSRC/drivers/Makefile" || echo 'obj-$(CONFIG_REKERNEL) += rekernel/' >> "$KSRC/drivers/Makefile"
    sed -i 's|#include <../android/binder_internal.h>|#include "../android/binder_internal.h"|g' "$d/rekernel_binder.c"
    grep -qF '#include <linux/seq_file.h>' "$d/rekernel_binder.c" || sed -i '/#include <linux\/kprobes.h>/a #include <linux/seq_file.h>' "$d/rekernel_binder.c"
    success "Re-Kernel integrated"
    VARIANT+="+REKERNEL"
}

# Apply a patch that may already be in the tree: apply it, skip it if it is already there, abort otherwise
apply_zram_patch() {
    local p="$1"
    if patch -p1 -F 3 --dry-run < "$p" >/dev/null 2>&1; then
        patch -p1 -F 3 --no-backup-if-mismatch < "$p" >/dev/null && success "Applied $(basename "$p")"
    elif patch -p1 -R --dry-run < "$p" >/dev/null 2>&1; then
        log "$(basename "$p") is already applied"
    else
        error "$(basename "$p") does not apply to this kernel"; exit 1
    fi
}

# LZ4KD: upgraded LZ4 + NEON decompressor (vendored from LingLuo17/AnyKernel3), then the LZ4K / LZ4KD /
# LZ4K_OPLUS sources and zram patches from ShirkNeko/SukiSU_patch for this kernel line.
apply_lz4kd() {
    [ "${LZ4KD:-false}" = "true" ] || return 0
    local sp=/tmp/SukiSU_patch pd lz4kd oplus n
    rm -rf "$sp"
    git clone -q --depth 1 https://github.com/ShirkNeko/SukiSU_patch.git "$sp"
    pd="$sp/other/zram/zram_patch/$KERNEL_VERSION"
    lz4kd="$pd/lz4kd.patch"; oplus="$pd/lz4k_oplus.patch"
    for n in "$sp/other/zram/lz4k" "$sp/other/zram/lz4k_oplus" "$lz4kd" "$oplus" "$WORKDIR/zram/lz4" "$WORKDIR/zram/apply_lz4_neon.sh"; do
        [ -e "$n" ] || { error "LZ4KD needs $n, which does not exist (no zram patches for kernel $KERNEL_VERSION upstream?)"; exit 1; }
    done

    log "LZ4KD: upgrading LZ4 and adding the NEON decompressor"
    rm -f lib/lz4/lz4_compress.c lib/lz4/lz4_decompress.c lib/lz4/lz4defs.h lib/lz4/lz4hc_compress.c
    cp -r "$WORKDIR"/zram/lz4/. lib/lz4/
    cp -r "$WORKDIR"/zram/include/linux/. include/linux/
    bash "$WORKDIR/zram/apply_lz4_neon.sh" || { error "apply_lz4_neon.sh failed"; exit 1; }
    if [ -f fs/f2fs/Makefile ] && ! grep -qF 'f2fs-$(CONFIG_F2FS_IOSTAT) += iostat.o' fs/f2fs/Makefile; then
        echo 'f2fs-$(CONFIG_F2FS_IOSTAT) += iostat.o' >> fs/f2fs/Makefile
    fi

    log "LZ4KD: adding LZ4K, LZ4KD and LZ4K_OPLUS"
    cp -r "$sp"/other/zram/lz4k/include/linux/. include/linux/
    cp -r "$sp"/other/zram/lz4k/lib/. lib/
    cp -r "$sp"/other/zram/lz4k/crypto/. crypto/
    cp -r "$sp/other/zram/lz4k_oplus" lib/
    apply_zram_patch "$lz4kd"
    apply_zram_patch "$oplus"
    VARIANT+="+LZ4KD"
}

# Per-zip metadata for the Telegram bot (.github/scripts/telegram_release.py)
write_tg_meta() {
    local zip="$1" rname="" rrepo="" rver="" rcount="" root=""
    case "$KSU" in
        KSU)  rname="KernelSU";      rrepo="tiann/KernelSU" ;;
        BSU)  rname="BakaSU";        rrepo="Baka-SU/BakaSU" ;;
        KSUN) rname="KernelSU-Next"; rrepo="${KSUN_USED_REPO:-KernelSU-Next/KernelSU-Next}" ;;
        FINA) rname="FinaSU";        rrepo="LyraVoid/FinaSU" ;;
    esac
    if [ -n "$rname" ] && [ -e "$KSRC/drivers/kernelsu" ]; then
        root="$(dirname "$(readlink -f "$KSRC/drivers/kernelsu")")"
        rver="$(command git -C "$root" describe --tags --abbrev=0 2>/dev/null || command git -C "$root" rev-parse --short HEAD 2>/dev/null || true)"
        rcount="$(command git -C "$root" rev-list --count HEAD 2>/dev/null || true)"
    fi
    ZIP_NAME="$zip" ROOT_NAME="$rname" ROOT_REPO="$rrepo" ROOT_VER="$rver" ROOT_COUNT="$rcount" \
    SRC_REPO="$(simplify_gh_url "$KERNEL_REPO")" SRC_BRANCH="$KERNEL_BRANCH" \
    META_LTO="$LTO" META_COMPILER="${COMPILER_STRING:-}" META_SUSFS="${SUSFS_VERSION:-}" META_LINUX="$LINUX_VERSION" \
    META_VARIANT="$VARIANT" META_OUT="$RELEASE_DIR/${zip%.zip}.meta.json" \
    python3 - <<'PY'
import json, os
e = os.environ.get
def flag(k): return e(k, "false") == "true"
meta = {
    "zip": e("ZIP_NAME"), "variant": e("META_VARIANT"), "linux": e("META_LINUX"),
    "source_repo": e("SRC_REPO"), "source_branch": e("SRC_BRANCH"),
    "compiler": e("META_COMPILER"), "lto": e("META_LTO"), "susfs": e("META_SUSFS"),
    "root": {"name": e("ROOT_NAME"), "repo": e("ROOT_REPO"), "version": e("ROOT_VER"), "count": e("ROOT_COUNT")},
    "features": {
        "droidspaces": flag("DROIDSPACES"), "nethunter": flag("NH"), "nomount": flag("NM"),
        "rekernel": flag("REKERNEL"), "zstd": flag("ZSTD"), "lz4kd": flag("LZ4KD"),
        "mm": "+MM" in (e("META_VARIANT") or ""),
    },
}
with open(e("META_OUT"), "w") as f:
    json.dump(meta, f, indent=1)
PY
}

# ---------------------------------------------------------------------------------------------------------
# Exact kernel version selection and reproducibility records
# ---------------------------------------------------------------------------------------------------------

# Clone the kernel source. Without KERNEL_EXACT_VERSION this is the original shallow clone of the branch tip.
# With it, the branch history is fetched commit-only (no file contents), the commit that carries exactly that
# Linux version is found from the Makefile (py/resolve_kernel_commit.py) and checked out.
clone_kernel_source() {
    if [ -z "${KERNEL_EXACT_VERSION:-}" ]; then
        if kernel_version_eq "$KERNEL_VERSION" "6.1"; then
            git clone -q --depth=1 --recurse-submodules "$KERNEL_REPO" -b "$KERNEL_BRANCH" "$KSRC"
        else
            git clone -q --depth=1 "$KERNEL_REPO" -b "$KERNEL_BRANCH" "$KSRC"
        fi
        return 0
    fi

    python3 "$WORKDIR/py/kernelver.py" "$KERNEL_VERSION" "$KERNEL_EXACT_VERSION" || exit 1
    log "Exact kernel version requested: $KERNEL_EXACT_VERSION (base $KERNEL_VERSION)"

    # Servers that do not support partial clones ignore the filter and send the full history instead
    git clone -q --filter=tree:0 --no-checkout --single-branch "$KERNEL_REPO" -b "$KERNEL_BRANCH" "$KSRC"
    local res sha
    res="$(python3 "$WORKDIR/py/resolve_kernel_commit.py" "$KSRC" "refs/remotes/origin/$KERNEL_BRANCH" "$KERNEL_VERSION" "$KERNEL_EXACT_VERSION")" \
        || { error "Could not resolve '$KERNEL_EXACT_VERSION' on $KERNEL_BRANCH, see the problem above"; exit 1; }
    sha="$(printf '%s' "$res" | python3 -c 'import json,sys; print(json.load(sys.stdin)["commit"])')"
    # For a commit pin, the Linux version is whatever that commit's Makefile reports
    KERNEL_PIN_EXPECTED_VERSION="$(printf '%s' "$res" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
    log "$KERNEL_EXACT_VERSION is commit $sha (Linux $KERNEL_PIN_EXPECTED_VERSION)"
    command git -C "$KSRC" checkout -q --detach "$sha"
    if kernel_version_eq "$KERNEL_VERSION" "6.1"; then
        command git -C "$KSRC" submodule update --init --recursive --depth 1 -q \
            || command git -C "$KSRC" submodule update --init --recursive -q
    fi
}

# The checked-out tree must report exactly the version that was asked for
verify_exact_kernel_version() {
    [ -n "${KERNEL_EXACT_VERSION:-}" ] || return 0
    local expected="${KERNEL_PIN_EXPECTED_VERSION:-$KERNEL_EXACT_VERSION}"
    if [ "${LINUX_VERSION%%[!0-9.]*}" != "$expected" ]; then
        error "Requested Linux $expected ($KERNEL_EXACT_VERSION) but the source reports $LINUX_VERSION"
        exit 1
    fi
}

# Record exactly what was built from, for reproducibility (job summary, build env, release notes).
# Everything is read back from the checked-out tree, then compared with what was configured.
record_kernel_source() {
    local sha origin branch tag link subs repo_label
    sha="$(command git -C "$KSRC" rev-parse HEAD)"
    origin="$(command git -C "$KSRC" remote get-url origin 2>/dev/null || true)"
    branch="$(command git -C "$KSRC" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    tag="$(command git -C "$KSRC" describe --tags --exact-match HEAD 2>/dev/null || true)"
    subs="$(command git -C "$KSRC" submodule status 2>/dev/null | sed 's/^[ +-]//' || true)"

    # the tree must come from the configured repository and branch
    [ "${origin%.git}" = "${KERNEL_REPO%.git}" ] \
        || { error "Kernel source mismatch: configured $KERNEL_REPO but the checkout came from ${origin:-unknown}"; exit 1; }
    if [ -z "${KERNEL_EXACT_VERSION:-}" ] && [ "$branch" != "$KERNEL_BRANCH" ]; then
        error "Kernel source mismatch: configured branch $KERNEL_BRANCH but the checkout is on ${branch:-unknown}"; exit 1
    fi

    KERNEL_COMMIT="$sha"
    repo_label="$(simplify_gh_url "$origin")"
    case "$origin" in
        *github.com*) link="https://github.com/$repo_label/commit/$sha" ;;
        *)            link="${origin%.git}/+/$sha" ;;
    esac
    {
        echo "KERNEL_COMMIT=$sha"
        echo "KERNEL_EXACT_REQUESTED=${KERNEL_EXACT_VERSION:-}"
        echo "KERNEL_SOURCE_URL=$origin"
        echo "KERNEL_SOURCE_REPO=$repo_label"
        echo "KERNEL_SOURCE_BRANCH=$KERNEL_BRANCH"
        echo "KERNEL_TAG=$tag"
    } >> "$GITHUB_ENV"
    log "Kernel source: $origin, branch $KERNEL_BRANCH, commit ${sha:0:12}${tag:+, tag $tag}, Linux $LINUX_VERSION"
    {
        echo "### Kernel source"
        echo "The kernel is fetched from an external repository. It is not part of this repository."
        echo
        echo "| | |"
        echo "|---|---|"
        echo "| This repository (workflow) | ${GITHUB_REPOSITORY:-unknown} @ \`${GITHUB_SHA:0:12}\` |"
        echo "| Kernel source repository | [$origin]($origin) ($KERNEL_REPO_ORIGIN) |"
        echo "| Kernel source branch | \`$KERNEL_BRANCH\` ($KERNEL_BRANCH_ORIGIN) |"
        echo "| Resolved commit | [\`$sha\`]($link) |"
        echo "| Tag | ${tag:-none} |"
        echo "| Kernel base | $KERNEL_VERSION |"
        echo "| Registry source | \`${KERNEL_SOURCE_ID:-unknown}\`$(source_overridden && echo ' (replaced by a repository variable)') |"
        echo "| Device status | $(source_overridden && echo 'not assessed, the source was replaced by a repository variable' || registry status "$KERNEL_SOURCE_ID" ${HYPERCORE_DEVICE:+"$HYPERCORE_DEVICE"} 2>/dev/null || echo unknown) |"
        echo "| Requested exact version or commit | ${KERNEL_EXACT_VERSION:-none (branch default)} |"
        echo "| Linux version | $LINUX_VERSION |"
        if [ -n "$subs" ]; then
            echo "| Submodules | $(printf '%s' "$subs" | sed 's/ (.*//' | awk '{printf "`%s` %s<br>", substr($1,1,12), $2}') |"
        fi
    } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

# SUSFS revision actually used (the branch tip unless pinned)
record_patch_sources() {
    local sc
    sc="$(command git -C "$SUSFS_DIR" rev-parse HEAD 2>/dev/null || true)"
    [ -n "$sc" ] || return 0
    echo "SUSFS_COMMIT_USED=$sc" >> "$GITHUB_ENV"
    {
        echo "### Patch sources"
        echo "| | |"
        echo "|---|---|"
        echo "| susfs4ksu | [\`${sc:0:12}\`](https://gitlab.com/simonpunk/susfs4ksu/-/commit/$sc) (${SUSFS_COMMIT:-branch tip} requested) |"
    } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
}

# Patches that were skipped because they do not apply. Normally only reported; with an exact kernel
# version they are an error, because a silently missing patch means a silently missing feature.
report_skipped_patches() {
    local f="$WORKDIR/skipped-patches.txt"
    [ -s "$f" ] || return 0
    cp "$f" "$RELEASE_DIR/skipped-patches.txt"
    {
        echo "### Patches that did not apply"
        sort -u "$f" | sed 's/^/- /'
    } >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
    warning "Patches that did not apply: $(sort -u "$f" | tr '\n' ' ')"
    if [ -n "${KERNEL_EXACT_VERSION:-}" ] && [ "${ALLOW_SKIPPED_PATCHES:-false}" != "true" ]; then
        error "Linux $KERNEL_EXACT_VERSION was requested and the patches above do not apply to it. Pin SUSFS with SUSFS_COMMIT, pick another version, or set the ALLOW_SKIPPED_PATCHES variable to true to build without them."
        exit 1
    fi
}

# ---------------------------------------------------------------------------------------------------------
# Kernel source configuration (repository and branch), independent of the repository running the workflow
# ---------------------------------------------------------------------------------------------------------

# Accept https://host/path or owner/repo (GitHub shorthand); anything else is refused
normalize_kernel_repo() {
    local r="${1%/}"
    case "$r" in
        https://*) printf '%s\n' "$r" ;;
        */*)       case "$r" in *:*|*" "*) return 1 ;; esac; printf 'https://github.com/%s\n' "$r" ;;
        *)         return 1 ;;
    esac
}

# Where the kernel comes from, and why. Most specific first:
#   1. KERNEL_REPO_<base> / KERNEL_BRANCH_<base>   any base, written like 6_1, 5_10, 6_12
#   2. 6.1 only: KERNEL_REPO, and KERNEL_BRANCH (or KERNEL_BRANCH_NH for NetHunter builds)
#   3. the built-in default of the source registry (sources.json): the device source for 6.1, the Android Common
#      Kernel -lts branches for the other lines
# Layers 1 and 2 are the repository variables and win over the registry. "6.1 only" is the registry's
# legacy_override_line, which keeps the unsuffixed variables working exactly as before.
# Sets KERNEL_REPO, KERNEL_BRANCH, KERNEL_KMI, KERNEL_SOURCE_ID and, for the build summary, where each value came from.
resolve_kernel_source_config() {
    local key="${KERNEL_VERSION//./_}" repo="" branch="" kmi="" repo_src="" branch_src="" v
    local def_repo="" def_branch="" source_id="" variant_flag="" legacy_line

    IFS='|' read -r def_repo def_branch kmi source_id repo_src branch_src variant_flag <<< "$(resolve_kernel_source "$KERNEL_VERSION")"
    [ -n "$def_repo" ] && [ -n "$def_branch" ] || { error "No kernel source is defined for base $KERNEL_VERSION"; exit 1; }
    repo="$def_repo"; branch="$def_branch"
    legacy_line="$(registry legacy-override-line)" || { error "sources.json has no legacy_override_line"; exit 1; }

    if kernel_version_eq "$KERNEL_VERSION" "$legacy_line"; then
        if [ -n "${KERNEL_REPO:-}" ] && [ "${KERNEL_REPO%/}" != "$def_repo" ]; then
            repo="$KERNEL_REPO"; repo_src="variable KERNEL_REPO"
        fi
        if [ -n "$variant_flag" ]; then
            v="KERNEL_BRANCH_${variant_flag}"
            if [ -n "${!v:-}" ]; then branch="${!v}"; branch_src="variable $v"; fi
        else
            if [ -n "${KERNEL_BRANCH:-}" ]; then branch="$KERNEL_BRANCH"; branch_src="variable KERNEL_BRANCH"; fi
        fi
    fi

    if ! kernel_version_eq "$KERNEL_VERSION" "$legacy_line" && { [ -n "${KERNEL_REPO:-}" ] || [ -n "${KERNEL_BRANCH:-}" ]; }; then
        warning "KERNEL_REPO and KERNEL_BRANCH only apply to the ${legacy_line} base; use KERNEL_REPO_${key} and KERNEL_BRANCH_${key} for $KERNEL_VERSION"
    fi
    v="KERNEL_REPO_$key";   if [ -n "${!v:-}" ]; then repo="${!v}";   repo_src="variable $v";   fi
    v="KERNEL_BRANCH_$key"; if [ -n "${!v:-}" ]; then branch="${!v}"; branch_src="variable $v"; fi

    local raw="$repo"
    repo="$(normalize_kernel_repo "$raw")" \
        || { error "Kernel source '$raw' (${repo_src}) must be an https:// URL or owner/repo"; exit 1; }
    [[ "$branch" =~ ^[A-Za-z0-9._/+-]+$ ]] \
        || { error "Kernel branch '$branch' (${branch_src}) contains characters that are not allowed"; exit 1; }

    KERNEL_REPO="$repo"; KERNEL_BRANCH="$branch"; KERNEL_KMI="$kmi"; KERNEL_SOURCE_ID="$source_id"
    KERNEL_REPO_ORIGIN="$repo_src"; KERNEL_BRANCH_ORIGIN="$branch_src"
    log "Kernel source: $KERNEL_REPO ($repo_src), branch $KERNEL_BRANCH ($branch_src)"
}

# Check, before any build job starts, that a pinned version or commit exists in the given source. It makes the same
# commit-only (partial) clone as clone_kernel_source and runs the same resolver, but downloads no file contents.
# Prints the resolved commit on success. On failure the reason goes to stderr and the status is 1.
preflight_pin_for() {  # preflight_pin_for <repo> <branch> <kernel line> <version or commit>
    local repo="$1" branch="$2" line="$3" pin="$4" dir res rc
    dir="$(mktemp -d)"
    GIT_CLONE_PROTECTION_ACTIVE=false git clone -q --filter=tree:0 --no-checkout --single-branch "$repo" -b "$branch" "$dir" 2>/dev/null \
        || { rm -rf "$dir"; error "Could not clone branch $branch of $repo to check $pin"; return 1; }
    res="$(python3 "${WORKDIR:-.}/py/resolve_kernel_commit.py" "$dir" "refs/remotes/origin/$branch" "$line" "$pin")"; rc=$?
    rm -rf "$dir"
    [ "$rc" -eq 0 ] || return 1
    echo "$pin resolves to $(printf '%s' "$res" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["commit"], "(Linux " + r["version"] + ")")')"
}

# The same check for the kernel source that the current KERNEL_VERSION and variables select
preflight_pin() {
    [ -n "${KERNEL_EXACT_VERSION:-}" ] || return 0
    resolve_kernel_source_config >/dev/null || return 1
    preflight_pin_for "$KERNEL_REPO" "$KERNEL_BRANCH" "$KERNEL_VERSION" "$KERNEL_EXACT_VERSION"
}

# True when a repository variable replaced the registry's repository or branch (needs resolve_kernel_source_config
# to have run). The registry's compatibility assessment then no longer describes what will be built.
source_overridden() {
    [[ "${KERNEL_REPO_ORIGIN:-}" == variable* || "${KERNEL_BRANCH_ORIGIN:-}" == variable* ]]
}

# Space-separated kernel lines whose source is currently replaced by repository variables
overridden_lines() {
    local line out=()
    for line in $(registry lines); do
        ( KERNEL_VERSION="$line"; resolve_kernel_source_config >/dev/null 2>&1; source_overridden ) && out+=("$line")
    done
    echo "${out[*]:-}"
}

# Refuse a kernel source that is unsupported for the target device, or a not-verified one chosen explicitly without
# ALLOW_UNVERIFIED. The workflow checks this once in its first job; this repeats it so build.sh cannot be run around it.
# Needs resolve_kernel_source_config to have run.
enforce_source_gate() {
    local custom=""
    source_overridden && custom="$KERNEL_VERSION"
    registry gate --kernel-version "$KERNEL_VERSION" --source "${KERNEL_SOURCE:-}" \
        ${HYPERCORE_DEVICE:+--device "$HYPERCORE_DEVICE"} \
        --allow-unverified "${ALLOW_UNVERIFIED:-false}" --custom-lines "$custom" >/dev/null \
        || { error "Kernel source $KERNEL_VERSION is not allowed for this device, see the problems above"; exit 1; }
}
