# Changelog

## 2026-10-29

### Fixed
- **All builds failing with `can't open file 'py/kernelver.py'`.** The exact-version path (`KERNEL_EXACT_VERSION`) calls two
  helper scripts that were never committed. Both are now in `py/`:
  - `py/kernelver.py` validates `KERNEL_VERSION` and `KERNEL_EXACT_VERSION`.
  - `py/resolve_kernel_commit.py` finds the last commit of the source branch that reports the requested version (a binary
    search over the Makefile history). If the version does not exist, the error names the nearest versions that do.
- **Release job failing with `size must be greater than or equal to 1`.** After every build failed, the job still created a
  release and uploaded empty checksum files. It now skips the upload when no zip was produced, and the Telegram message no
  longer links to a release that was not created.
- **ccache release race.** `build` now waits for `ensure-cache-release`, so the first cache restore and save no longer race its
  creation. A failure creating the cache release does not block the builds.
- `upload-all` and `release` no longer run when `build` never started, for example when input validation fails.
- **Duplicate jobs.** With `DROIDSPACES` off, the `NoDS` variants were identical to their twins and are no longer built.
- A failed SUSFS lookup in `build.sh` no longer carries on with an empty branch.

### Added
- **Kernel source registry.** `sources.json` and `py/sources.py` hold the kernel lines, sources, devices and what is known about
  each source on each device. The schema is validated, and every problem names the exact field. See `SOURCES.md`.
- **Build and boot verification are recorded separately.** `confirmed` is only accepted with boot evidence on the device. A
  successful build alone is rejected by the validator.
- **Compatibility gate.** It runs in the first job and again in `build.sh`, before any build job starts. Unsupported sources are
  blocked, an unverified source chosen explicitly needs `ALLOW_UNVERIFIED`, and the default source keeps building with a warning.
- **New inputs:** `KERNEL_SOURCE` (choose a registry source by id) and `ALLOW_UNVERIFIED`. Repository variable
  `HYPERCORE_DEVICE` targets another device configured in the registry.
- **Commit pins.** `KERNEL_EXACT_VERSION` also accepts a commit SHA (7 to 40 hex characters). The first job checks that the version or
  commit exists in the selected source, using a commit-only clone, so a wrong pin fails once and early.
- Build summaries name the registry source and what is known about it for the device.
- **Checks:** `validate_references.py` fails when a referenced helper script does not exist, and `validate_workflow_graph.py`
  checks `needs`, job outputs, step ids, inputs, the 25-input limit and local actions.
- **Tests** in `tests/`, run by `bash tests/run_all.sh` and by the Validate workflow on every push: registry, gate, pins, workflow, and a
  backward-compatibility test that runs a frozen copy of the pre-registry logic side by side with the current one.

### Changed
- **Workflow form cleaned up.** Inputs are grouped as Run, Kernel, Variant, Feature and zram, every label carries its group tag and
  is 55 characters or fewer, and `All` moved to the end of the kernel list. All original input keys, types and defaults are kept.
- Kernel lines, Android releases, KMI, SUSFS branches, the defconfig name and the default sources now come from `sources.json`
  instead of tables in `functions.sh`, `build.sh`, `generate-matrix.py` and `kernelver.py`.
- First-job input validation: a bad exact version, `All` with an exact version or source id, an unknown source id, RKSU or MamboSU
  selected without `RKSU_REPO` or `MAMBOSU_REPO` now give one clear error instead of one failure per build job.
- The repository variables that replace a kernel source are defined once, at workflow level.
- `KERNEL_VERSION=All` now means every kernel line that may be built for the device.
- README options table lists all inputs (SukiSU, KowSU, RKSU and MamboSU were missing), plus the new inputs and troubleshooting rows.

### Migration
- **Unsupported lines are blocked.** With the default registry, `KERNEL_VERSION` of 5.10, 5.15, 6.6 and 6.12 now fails in the first job:
  those are generic Android Common Kernel sources and duchamp is a 6.1 device. `All` builds 6.1 only. To build one of those lines
  anyway, set your own source with the `KERNEL_REPO_<base>` / `KERNEL_BRANCH_<base>` repository variables (the gate allows it with a
  warning), or add a device that runs that line to `sources.json`.
- Nothing else changes. `gh workflow run -f KEY=value` keeps working for every existing input, and the default build (6.1, NetHunter
  branch) selects the same repository and branch as before.
- `KERNEL_KMI` for 6.1 is now `android14-6.1` instead of empty. It is only read on the non-6.1 NTSync path.

### duchamp compatibility
| Source | Compatibility | Build | Boot |
|---|---|---|---|
| `gkid-duchamp-6.1` | needs-verification | reported (run #8 green, logs not inspected) | unverified |
| 5.10, 5.15, 6.6, 6.12 (Android Common Kernel) | unsupported (reasoned, not tested) | unverified | unverified |

### Tested here
- 112 Python tests and 230 shell checks pass, including mutation checks that confirmed the tests fail when a default or the gate is broken.
- The real prepare-job script was run for 12 input combinations, and the real `clone_kernel_source` against synthetic partial-clone repositories.
- All repository validators pass, and the patch applies cleanly on the previously delivered state.

### Not verified here (needs a real run)
- [ ] A real **Kernel Builder** run on GitHub Actions with default inputs.
- [ ] A run with `KERNEL_EXACT_VERSION` set to a real version of `GKID-6.1`, to confirm the pre-flight against the real remote.
- [ ] A real duchamp build with its log inspected, then a boot test on the device. Only then can `gkid-duchamp-6.1` become `confirmed`.
- [ ] Delete the empty releases left by the earlier failed runs (for example `v26.10.28-r11`).
