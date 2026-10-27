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
