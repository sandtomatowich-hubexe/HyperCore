# Credits

This repository would not exist without the following projects and people.

## Build pipeline

**GKID-Kernels** by Ahmed Al-Nassif (https://github.com/ahmed-alnassif/GKID-Kernels, GPL-3.0).
The workflow, `build.sh`, `functions.sh`, configs, kernel patches, KernelSU patches (multiple managers,
AVC log spoofing, kthread offloads, supercalls), the WirelessKSU module and the matrix, release and
validation scripts started from this repository. Patch files keep their original authorship headers.

External pieces his pipeline uses, which this one still pulls in:

- Device kernel source `ahmed-alnassif/GKI-Duchamp-6.1`, AnyKernel3 config `ahmed-alnassif/AK3-GKID`, `ahmed-alnassif/rtw88`
- ReSuSFS (companion module, not bundled): https://github.com/ahmed-alnassif/ReSuSFS

## Components

| Project | Author | Used for |
|---|---|---|
| susfs4ksu | simonpunk | SUSFS kernel and KernelSU patches |
| KernelSU | tiann | Root |
| KernelSU-Next | KernelSU-Next team | Root variant |
| ReSukiSU | ReSukiSU team | Root variant (upstream of BakaSU) |
| BakaSU | Baka-SU | Root variant |
| FinaSU | LyraVoid | Root variant |
| kernel_patches (NTSync) | WildKernels | NTSync backport |
| Baseband-guard | vc-teahouse | Baseband Guard |
| nomount | maxsteeel | NoMount |
| DroidSpaces kernel config | ravindu644 | `configs/droidspaces.config` |
| Neutron clang / antman | Neutron-Toolchains | Compiler |
| AnyKernel3 | osm0sis | Flashable zips |
| AnyKernel3 (GKI builder) | LingLuo17 | FinaSU in-tree integration and manager certificate slot, Re-Kernel integration steps, `scripts/finasu/extract_manager_cert.py`, and the upgraded LZ4 / NEON files in `zram/` (GPL-3.0) |
| GKI_KernelSU_SUSFS | WildKernels, zzh20188 | KernelSU-Next, BakaSU and FinaSU integration steps, BakaSU SUSFS compat stubs (GPL-3.0) |
| KernelSU-Next (SUSFS fork) | pershoot | `dev-susfs` branch and susfs4ksu fixes |
| Re-Kernel | Sakion-Team | Re-Kernel driver |
| SukiSU_patch | ShirkNeko | LZ4K, LZ4KD and LZ4K_OPLUS zram sources and patches |
| pahole | kernel.org / Arnaldo Carvalho de Melo | BTF generation |

If you maintain one of these and want the credit changed, please open an issue.
