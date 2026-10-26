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
