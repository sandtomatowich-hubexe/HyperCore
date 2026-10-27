# License notice

This repository is licensed under the GNU General Public License v3.0 (see LICENSE).

It is derived from GKID-Kernels, Copyright (C) 2026 Ahmed Al-Nassif, GPL-3.0, taken from branch `dev`
at commit 28f169d9bb67ff1db245f6aef547b80a6c2b2c7a. Because of that, this repository must stay under
GPL-3.0 and keep the original copyright and authorship notices (including those in `kernel-patches/` and
`patches/`).

## Changes against that commit

- Removed the author's branding, donation links, Telegram channel posts, banner image and artifact forwarder workflow
- Added KernelSU-Next (`KSUN`) and FinaSU (`FINA`) variants (`functions-extra.sh`, `build.sh`, `py/generate-matrix.py`)
- Added inputs: `MM`, `ZSTD`, `LZ4KD`, `REKERNEL`, `BUILD_KSUN_SUSFS`, `BUILD_FINA_SUSFS`
- Builds abort when a required patch does not apply, instead of skipping it
- Brand name, repo links and commit identity come from repo variables and the GitHub context
- `KERNEL_VERSION` input defaults to 6.1
- Telegram posting is skipped when the `BOT_TOKEN` secret is not set
- Added the GitHub Pages site (`site/`, `.github/workflows/pages.yml`)
- `install_ksu` now checks that the branch exists and fails on download errors instead of running an error page as a script
- BakaSU builds use its `main` branch (override with the `BSU_REF` repo variable); KernelSU-Next defaults to `dev-susfs` (`KSUN_REF`), FinaSU to `main` (`FINA_REF`)
- FinaSU is integrated in-tree with its manager certificate in the second trust slot, and Re-Kernel is built in from Sakion-Team/Re-Kernel, following LingLuo17/AnyKernel3 (GPL-3.0). `scripts/finasu/extract_manager_cert.py` is copied from that repo.
- KernelSU-Next uses pershoot's `dev-susfs` fork with its two susfs4ksu fixes; BakaSU gets Kbuild and SUSFS compat fixes; FinaSU skips the KernelSU-side SUSFS patch when it ships SUSFS natively (steps from WildKernels/GKI_KernelSU_SUSFS)
- `MM` now falls back to the fork's own manager support instead of aborting, and `+MM` appears in the zip name only when the patch was applied
- Release posting to Telegram is done by `.github/scripts/telegram_release.py` (summary message plus the zips as files); the upstream forwarder for rejected patches was removed
- New README
- LZ4KD builds in the upgraded LZ4 library and NEON decompressor (`zram/`, copied from LingLuo17/AnyKernel3) and the LZ4K/LZ4KD zram patches from ShirkNeko/SukiSU_patch, fetched at build time
- Each build writes a `<zip>.meta.json` that the Telegram bot uses to describe the kernel; the bot sends rich messages and falls back to plain documents
- README rewritten
