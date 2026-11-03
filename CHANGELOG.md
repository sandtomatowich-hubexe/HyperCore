# Changelog

## 2026-10-29

### Fixed
- **KernelSU-Next builds failing with `Committer identity unknown`.** The two `git cherry-pick` calls that apply pershoot's susfs4ksu
  fixes need a git identity, and a CI runner has none. The build only ever set one inside the plain KernelSU branch, so
  every KernelSU-Next variant (including Compat) stopped there. This was already in your original code, not caused by the
  later changes. The build now gives itself an identity through the environment (`ensure_git_identity`), only when git has no
  identity of its own, and without modifying any git config file. The fixes moved into `apply_pershoot_susfs_fixes`, with the
  same source and the same two commits, so they can be tested.
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
- **Read-only compatibility probe** (`py/module_probe.py`, guide in `PROBE.md`). Before the release string is changed, it reads the
  cloned source and `.config` and reports how module versioning works in that tree: `CONFIG_MODVERSIONS`, how the loader compares a
  module's version magic and whether the kernel release word is skipped for modules with symbol CRCs, the module signing
  settings and what the tree's own Kconfig help says about them, the release length limit, and the release string `build.sh`
  forms today against the proposed one. Every finding is `VERIFIED`, `COMPUTED`, `UNDETERMINED` or `NOT VERIFIABLE`, and a list of
  what CI cannot establish closes the report. Optionally it reads real `.ko` files for their vermagic, symbol CRCs and signature.
  It is opt-in (repository variable `HYPERCORE_PROBE=true`), changes nothing (no LOCALVERSION, no source edit, no commit) and cannot
  fail a build.
- **`MANAGED_SOURCES.md`**: the exact requirements for moving to a HyperCore-managed source, including merge-only syncing, conflict
  handling, upstream updates and preservation of authorship and history. Documentation only; no fork was created.
- **Version selection by release.** `KERNEL_EXACT_VERSION` also accepts `latest-stable` and `latest-lts`. Both stay inside the
  selected kernel line: the newest release of that line that kernel.org lists (stable or longterm, and longterm only for
  `latest-lts`). They read kernel.org's official `releases.json`, become one exact version in the first job, and are then
  found in the history of the selected source like any other version. If kernel.org cannot be read, or the source does not have
  that version, the run stops and names the nearest versions the source has. It never builds another version instead.
  (`py/kernel_versions.py`)
- **One resolution for the whole matrix.** The first job resolves the pin and passes the version and the full commit SHA to every
  build job (`KERNEL_PIN_COMMIT`), so a push that lands mid-run cannot give two variants different sources. Each build checks the
  tree reports the same version.
- **Build summary** records the requested selector, the resolved Linux version, the full commit SHA, whether the source is an
  external project or HyperCore-managed, and which upstream project it comes from.
- **Registry: provenance for HyperCore-managed sources.** Sources can declare `kind` (device or generic-gki), `role` (external or
  managed), `upstream`, `sync` (merge only) and `credits`. A managed source without its upstream, sync policy and credits is
  refused. Nothing is managed yet; the fields exist so that a fork can be adopted without another redesign.
- **Registry: supported versions.** An optional `versions` block per source: a supported range, and recorded build or boot results
  per version and device. A version outside the range is refused; a version without a record builds and says so.
- **Notifications.** Telegram tells apart `Source (HyperCore)` and `Source (upstream)`, shows the upstream project and branch of a
  managed source, a source replaced by a repository variable as `Source (custom)`, and the commit. The release metadata files carry
  the same information.
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
- **Release page: no blanket compatibility claim.** The release page said every line "supports all devices that shipped with the
  matching Linux version and Android release". It now says that GKI compatibility does not guarantee device compatibility, that
  sharing a Linux version and Android release does not make a kernel verified for a device, and sends readers to the
  device-specific verification status (the registry documentation, and the Device status line of the build summary).
  (`tests/test_release_page_claims.py`)
- **Form label of `KERNEL_EXACT_VERSION`** now names both selectors: `[Kernel] Version, commit, latest-lts or latest-stable`. The
  `(empty=default)` hint was dropped to stay within the 55-character limit of the form. A test fails if the label ever stops naming
  a selector the resolver supports.
- **Every report states who controls the kernel source.** The release page now has a "Kernel source" block per kernel line (it had
  none): upstream project or HyperCore-managed, the upstream project of a managed source, the commit and the requested version.
  The job summary sentence and the build log line follow the role. Telegram says `Source (HyperCore-managed)` for a managed source.
  A source replaced by a repository variable is called custom in every report. (`tests/test_report_provenance.py`)
- The form label of `KERNEL_EXACT_VERSION` now reads `[Kernel] Version, commit or latest-lts (empty=default)`. The key, type and default
  are unchanged. Build jobs receive the resolved pin from the first job instead of reading the raw input.
- `sources.json` was reformatted by the tooling (same data) and its sources gained `kind` and `credits`. No licence is claimed for
  the Duchamp source, because it has not been checked.
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
- 285 Python tests and 230 shell checks pass, including mutation checks that confirmed the tests fail when a default or the gate is broken.
- The real prepare-job script was run for 12 input combinations, and the real `clone_kernel_source` against synthetic partial-clone repositories.
- All repository validators pass, and the patch applies cleanly on the previously delivered state.

### Not verified here (needs a real run)
- [ ] Reading kernel.org's real `releases.json` from a runner. Here it was tested against a fixture in the documented format, a
      local HTTP server, and failure cases only.
- [ ] A run with `KERNEL_EXACT_VERSION=latest-lts` on the real source. The Duchamp source usually trails upstream, so expect the
      "source does not have it" message, listing the nearest versions it has.
- [ ] **The probe on the real Duchamp tree.** It was tested against fixture trees and synthetic module files only. Its result on the
      real clone and `.config` is what decides the release string, and nothing about the real tree is claimed until it has run
      (see `PROBE.md`). The release string itself is unchanged.
- [ ] The stock vendor modules and the device: whether they carry symbol CRCs, which key signed them, what Settings shows, and
      whether they load. Only the module files and a boot can settle these (listed in the probe report).
- [ ] A KernelSU-Next build (and its Compat variant) past the point where it used to stop. Only the git identity step was reproduced and fixed here.
- [ ] Neutron clang: the newest toolchain tag asks for glibc 2.43 and the runner has 2.39. `antman` prints a warning and offers
      `--patch=glibc`. Clang did run for the `pahole` build, but the kernel compile has not been seen with it. If a compile fails
      with a `GLIBC_2.43 not found` message, that is the cause.
- [ ] A real **Kernel Builder** run on GitHub Actions with default inputs.
- [ ] A run with `KERNEL_EXACT_VERSION` set to a real version of `GKID-6.1`, to confirm the pre-flight against the real remote.
- [ ] A real duchamp build with its log inspected, then a boot test on the device. Only then can `gkid-duchamp-6.1` become `confirmed`.
- [ ] Delete the empty releases left by the earlier failed runs (for example `v26.10.28-r11`).
