# GKI kernel builder

GitHub Actions pipeline that builds GKI kernels (5.10, 5.15, 6.1, 6.6, 6.12) with KernelSU and forks,
SUSFS, Baseband Guard, NTSync, DroidSpaces, NetHunter and performance tuning. Releases are listed on a
GitHub Pages site.

Built on the GKID-Kernels pipeline by Ahmed Al-Nassif and many other projects. See [CREDITS.md](CREDITS.md)
and [NOTICE.md](NOTICE.md).

## Setup

1. Push this repo to your GitHub account.
2. Settings > Pages > Source: **GitHub Actions**.
3. Optional repo variables: `KERNEL_REPO` (your device's GKI source), `KERNEL_BRAND` (zip prefix, default `GKI`),
   `ANYKERNEL_REPO`. Optional secrets for Telegram posts: `BOT_TOKEN`, `CHAT_ID`, `TOPIC_ID`.
4. Actions > **Kernel Builder** > Run workflow.

## Build variants

| Input | Builds |
|---|---|
| `BUILD_VANILLA` | Vanilla, Vanilla+NoLTO |
| `BUILD_KSU` | KernelSU |
| `BUILD_KSU_SUSFS` | KernelSU+SuSFS+Multiple-Managers, BakaSU+SuSFS, each also as a Compat build |
| `BUILD_KSUN_SUSFS` | KernelSU-Next+SuSFS (+Compat) |
| `BUILD_FINA_SUSFS` | FinaSU+SuSFS (+Compat) |

Options: `LTO` (none, thin, full), `KERNEL_VERSION`, `DROIDSPACES`, `NH` (NetHunter), `NM` (NoMount),
`MM` (extra manager signatures on BakaSU, KernelSU-Next, FinaSU), `ZSTD` (zram LZ4 + ZSTD),
`LZ4KD` and `REKERNEL` (need patch files in `extra-patches/`).

`Compat` builds drop LTO and add extra netfilter and filesystem options. Try one first if a standard
build doesn't boot.

## Layout

- `build.sh`, `functions.sh`, `functions-extra.sh`: build logic
- `configs/`: defconfig fragments
- `kernel-patches/`, `patches/`: kernel and KernelSU patches
- `extra-patches/`: optional feature patches you supply
- `py/`: matrix generation and KMI check
- `site/`: GitHub Pages site

Flashing a kernel can bootloop your device. Keep a stock `boot.img` backup.
