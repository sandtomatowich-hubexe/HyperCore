# Phase 0: evidence before the move to a HyperCore-owned kernel tree

**Status: prepared and tested offline; nothing has been run against the real repositories.** Phase 0 gathers evidence for
the plan to move HyperCore to its own kernel tree (`HyperCore-kernel`). It does not create that tree and it changes nothing
that builds today.

## Direction

HyperCore's long-term architecture is **ACK-based and fully independent from GKID**.

- **ACK (Android Common Kernel) is the upstream foundation** of HyperCore-kernel.
- **GKID is a read-only reference.** It is used only to find Duchamp-specific adaptations. It is not a kernel source, a
  development foundation or a fallback, and nothing in the new tree inherits from it.
- **Luminaire is a read-only architectural and technical comparison.** Its source and branches are not imported anywhere. Its
  objects exist only in a throw-away scratch repository outside the checkout, and the report carries statistics plus at most
  two short sample lines per file (`--no-samples` removes those).
- HyperCore-kernel will have its own identity, its own development history and an ACK-based branch structure. **It is not
  created in Phase 0.**

### The GKID default is temporary

`gkid-duchamp-6.1` stays the default source of the 6.1 line **only so that Phase 0 cannot change today's builds**. That is a
temporary build-safety measure and **not an architectural commitment to GKID**. It must not be read as approval to keep GKID
in the final design. ACK is not marked build-verified or boot-verified, and nothing in Phase 0 edits a verification state.

## What changed, and what did not

| Changed (additive) | Untouched |
|---|---|
| `sources.json`: new source `aosp-ack-6.1`, gated, unverified | The default source of every line |
| `phase0/delta_report.py`: read-only delta analysis | `build.sh`, `functions*.sh`, `configs/`, all patches |
| `.github/workflows/phase0-delta.yml`: manual, read-only | `.github/workflows/build.yml` and the release flow |
| `tests/test_phase0*.py`, `tests/phase0_helpers.py`, and two pinned assertions in `tests/` | No source removed; no verification state changed |

The two edited assertions (`tests/test_gate.py`, `tests/test_sources.py`) only account for the extra listed source.

## 1. The ACK 6.1 source

```
id:      aosp-ack-6.1
repo:    https://android.googlesource.com/kernel/common
branch:  android14-6.1-lts
status:  needs-verification for duchamp (build: unverified, boot: unverified)
```

It is not the default during Phase 0. Naming it needs `KERNEL_SOURCE=aosp-ack-6.1` **and** `ALLOW_UNVERIFIED=true`, otherwise
the gate stops the run in the first job.

## 2. Test A: the Vanilla build on ACK

Run **Kernel Builder** with:

| Input | Value |
|---|---|
| `TODO` | `kernel` |
| `TEST` | `no` |
| `KERNEL_VERSION` | `6.1` |
| `KERNEL_SOURCE` | `aosp-ack-6.1` |
| `ALLOW_UNVERIFIED` | `true` |
| `KERNEL_EXACT_VERSION` | empty |
| `BUILD_VANILLA` | `true` (builds `Vanilla` and `Vanilla+NoLTO`) |
| every other `BUILD_*`, `DROIDSPACES`, `NH`, `NM`, `REKERNEL`, `ZSTD`, `LZ4KD` | `false` |
| `LTO` | leave the default for the first run |

Optional, cheap, and worth doing first: run once with `TODO=defconfig` and the repository variable `HYPERCORE_PROBE=true`
(see `PROBE.md`). It reads the ACK tree and reports how module version checks behave with HyperCore's release string, without
compiling anything. Delete the variable afterwards.

### Read this before interpreting the result

Test A runs the **existing pipeline** on an ACK tree. It is *not* plain ACK. For a 6.1 build, `build.sh` does the following:

| Step | On a 6.1 build |
|---|---|
| `apply_kernel_patches` (`kernel-patches/common`), `apply_force_load_module_patch`, `apply_extract_cert_key_pass_patch`, `cleanup_abi_gki_protected_exports` | **Skipped.** They are guarded by `! kernel_version_eq 6.1`, which fits GKID carrying them already. On ACK nothing supplies them. |
| BBRv3 patch, NTSync patches, Baseband-guard | **Always applied**, also in Vanilla. |
| `features.config` (`BBG`, `NTSYNC`, `NOMOUNT`), `compat.config`, `custom.config` (for example `HZ_300`) | Always appended to `gki_defconfig`. |
| `CONFIG_LOCALVERSION` | Set to the HyperCore release string. |

So a boot failure in Test A would be **ambiguous**: it could be ACK, or BBRv3 (it changes TCP structures, which vendor modules
may see), or the config changes, or the release string. A boot success is clearer: ACK plus HyperCore's build-time additions boots.

If Test A does not boot, the next step is a **pure ACK build** (Test B) that applies none of the additions above. Test B needs an
opt-in switch in `build.sh` or its own workflow. It is deliberately **not built** in Phase 0, because it would touch the build
path. Say so and it gets proposed as a separate change.

### What to check in the run

- The first job prints `6.1 (aosp-ack-6.1): needs-verification for duchamp ... It will build, but it is not boot-verified`.
- The log clones `android.googlesource.com/kernel/common`, branch `android14-6.1-lts`.
- No `Found N patches in .../kernel-patches/common` line (see the table above).
- `skipped-patches.txt` in the artifacts. A patch that did not apply to ACK is silently missing from the kernel.
- The build summary labels the source as an external project (Android Common Kernel), not HyperCore.

### Flashing and recording

Keep a stock `boot.img` backup (README). Record the result honestly:

- Build: if the log was inspected, `build_verification.state = verified` with the run number and variant as evidence. If only
  seen green, use `reported`.
- Boot: `boot_verification.state = verified` only with who tested, which zip, and what was observed (`uname -r`,
  `/proc/version`, whether Wi-Fi, mobile data, camera, charging, UFS and sensors work).
- `compatibility` can become `confirmed` only with boot evidence. **Nothing is edited in Phase 0**; you review the results first.

## 3. The delta report

Compares GKID and Luminaire with ACK and answers: what do they add, and what does Duchamp need?

**Run it on GitHub:** Actions, **Phase 0 Delta Report**, Run workflow. Download the `phase0-delta-report` artifact
(`delta-report.md` and the full `delta-report.json`); the Markdown also appears in the job summary. The artifact holds
statistics only, no kernel source. The workflow **writes nothing into the checkout**: the scratch repository, both reports and
the summary source live under `$RUNNER_TEMP`, and the artifact is uploaded from there. It fetches three kernel histories without file contents, so the first run takes a while and
needs several GB of runner disk. `AUDIT_ACK_HISTORY` (on by default) reads the Linux version at every Makefile change on the
ACK branch, which adds a few minutes. The GKID branch defaults to the one the registry lists.

**Or locally:**

```bash
python3 phase0/delta_report.py --workdir /tmp/hc-delta --hypercore . --audit-ack-history \
  --out /tmp/delta-report.md --json /tmp/delta-report.json
```

`--workdir`, `--out` and `--json` must all be **outside** the checkout, and `--out` and `--json` must also be outside the
`--workdir` scratch repository: the script refuses any violation before fetching anything, and does not create directories there. `--workdir` must also be new (or a previous scratch directory). Other options: `--ack-branch`
(repeatable; the first is the primary), `--gkid-branch`, `--luminaire-url`, `--luminaire-branch`, `--no-fetch`, `--skip-patches`,
`--no-samples`, `--rows`, `--max-commits`, `--candidate-samples` (at least 2; smaller values are refused up front). Set `SOURCE_DATE_EPOCH` for a byte-reproducible timestamp.

### Isolation

- The scratch repository is **bare**: it has no working tree and no index of its own. Every git call is pinned to it with
  `--git-dir` and runs with `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` and similar variables removed, so a polluted environment
  cannot redirect it to another repository. (Revision 1 did leak into an unrelated repository in that situation. This is
  covered by a regression test.)
- Analysis commands ignore the user's global and system git configuration, so a setting such as `apply.whitespace` cannot change
  a result. Only the network fetch uses the user's configuration.
- The patch review applies patches to a **private temporary index file** that is deleted afterwards. Any other path used as an
  index is refused.
- A `--workdir` that is the checkout, inside it, an ancestor of it, or a non-empty directory without this tool's marker is
  refused. So is an `--out` or `--json` path inside the checkout **or inside the scratch repository** (a report written to
  `HEAD`, `objects/` or `refs/` would corrupt it). Paths are resolved first, so `..` segments and symlinks cannot hide a
  location, and containment is tested on path components, so `scratch-other` is not mistaken for `scratch`. The check runs
  before anything is fetched, analysed, created or written. A full run started from inside a checkout leaves it
  byte-for-byte unchanged (tested).
- `--out` and `--json` must be **two different files**. Identical paths, the same file under two spellings (`..`, `.`, a
  relative and an absolute form, a symlink) and two names for one hard-linked file are refused before any work, because the
  second report would silently overwrite the first.
- Reports are written by **atomic replacement** (a temporary file in the same directory, then rename), never by opening the
  existing file. So if an output path is a **hard link** to another file (inside the scratch repository, the checkout, a backup
  snapshot, anywhere), that other file is not modified: only the output's own directory entry is replaced. A path check alone
  cannot see hard links; this does not depend on one. A reader also never sees a half-written report. (Tested, including a
  hard link to the scratch repository's `HEAD` and to a checkout file.)
- **What a path check still cannot see:** a **bind mount** or overlay that makes the scratch repository or the checkout appear
  under another path, and, on a **case-insensitive filesystem**, two spellings of a not-yet-existing path that differ only by
  case (for example `R.md` and `r.md` as `--out` and `--json`). Keep both reports in a plain directory of their own, as the
  workflow does (`$RUNNER_TEMP/phase0-report`, a sibling of the scratch directory).

### How it decides

1. **ACK reference.** The primary ACK branch is the one the registry names for `aosp-ack-6.1` (`android14-6.1-lts`): it is what
   the Phase 0 build clones and what a HyperCore-kernel mirror would track. `android14-6.1` is used only to improve base
   detection. The relation between the two branches is measured and shown. A difference from the registry's branch is reported.
2. **Base of each tree.** With shared history, the newest ACK commit the tree contains (best merge base across the ACK
   branches): *exact*. With no shared history (squashed or imported), the Linux version is read from the tree and every first-parent
   ACK commit that reports it (or the nearest lower version) becomes a candidate. The candidates are then **compared** with the
   tree: all of them when there are no more than `--candidate-samples` (default 32, minimum 2), otherwise an evenly spaced sample followed
   by a finer pass in a window around the best sample. The chosen commit is the **best of those compared**, and the base is
   labelled *approximate*. When only a sample was compared the report says how many of how many, and that the result is **not
   guaranteed to be the best of all candidates**: the search assumes tree similarity changes fairly smoothly along the history,
   and a small sample can miss the true base (tested). Raising `--candidate-samples` to the number of candidates makes the
   comparison exhaustive, which the report also states. Even then the true base may be a commit that is not on the first-parent
   history.
3. **No monotonic-version assumption.** The version history of the ACK branch is read in full (every first-parent commit that
   changes the Makefile) and audited for decreases and revisited versions, never searched by bisection. `--audit-ack-history`
   prints that audit. Revision 1 bisected on versions; that assumption is gone.
4. **One base per tree throughout.** The delta, the defconfig comparison, the KMI symbols and the commit list all use that same
   base. The defconfig table shows the ACK value at **each** tree's base. Patch applicability is judged against the **ACK tip**
   (what HyperCore-kernel would start from), against each tree's tip, and, in the JSON, against each tree's own base; a patch
   whose result changes between those is flagged.
5. **Per file**, it records who changed it, whether the change is the same, and how many lines are shared. Lines are compared by
   SHA-256 of the whitespace-stripped text. When both trees change the same file differently, **both** trees' signals count: the
   more hardware-significant category wins, a note says so, and the file appears in its own table with both trees' numbers.
6. **Buckets**, using Luminaire (reported to boot on Duchamp) as the witness:

| Bucket | Meaning |
|---|---|
| 1. Essential Duchamp hardware / boot candidates | Hardware or KMI changes present in Luminaire, alone or the same in both trees. "Review" if both touch the file differently. A candidate, not proof of necessity. |
| 2. General performance / functionality | Performance and feature changes by area, with the HyperCore patch that also touches the file. |
| 3. GKID-specific, not demonstrably required | GKID-only branding/CI/other changes, and GKID-only hardware-looking changes that Luminaire does without. |
| 4. Existing HyperCore patches | Each patch applied in build order to ACK, GKID and Luminaire. |

### Patch statuses

| Status | Meaning |
|---|---|
| `applies` | The patch applies cleanly, with exact context, in order. |
| `reverse-applies` | The exact reverse patch applies cleanly. **A heuristic**: it suggests the change is already present but does not prove it (for example, an unrelated change can leave the same lines). The build's own test is fuzzier (see below). Confirm in the ACK history before dropping a patch. |
| `conflict` | Does not apply exactly in either direction: a conflict, context drift, or a missing earlier patch. The detail has git's first message. **The build's fuzz fallback may still apply it.** |
| `target-missing` | A file the patch needs does not exist (a rename needs its source). |
| `builder-side` | `patches/` (applied inside the KernelSU checkout) and `kernel-patches/susfs` (applied to the SUSFS checkout, or to the kernel after the downloaded SUSFS patch). Both depend on content fetched at build time. Not checked. |
| `other-line-by-name` | Its file name says it targets another kernel line. |

A patch that does not apply is skipped, as the build does, so later patches are judged without it.

### How the patch check compares with the build's patch application

The check is **not** the build's method, and the two can disagree. The build applies patches with `functions.sh:apply_patch_file`
(also used for `bbrv3`, `droidspaces`, `nethunter` and the KernelSU patches), which tries, in order:

1. `git apply --check`, then `git apply` (exact context, all-or-nothing);
2. `patch -p1 --fuzz=3 --batch --forward --dry-run`, then the same without `--dry-run` (tolerates up to 3 lines of context
   mismatch, and leaves `.orig` backup files in the tree);
3. `patch -p1 --fuzz=3 --batch -R --dry-run`: if the reverse applies, the patch is treated as already applied.

If none works, the patch name is appended to `skipped-patches.txt` and the build **continues**. This check does step 1 (on a
temporary index) and an exact `git apply -R`, and has no fuzz step. The differences:

| Aspect | Build | This check | Effect |
|---|---|---|---|
| Fuzz | accepts up to 3 lines of context mismatch through patch(1) | exact context only | a `conflict` here may still be applied by the build (tested) |
| Already-applied test | fuzzy reverse (patch -R --fuzz=3) | exact reverse (git apply -R) | `reverse-applies` can be missing where the build would call a patch already applied (tested) |
| Tree state | patches a tree that earlier steps already changed (sed edits, SUSFS files, other patches, downloaded NTSync patches that are not in the repository), only for the variant and flags being built | pristine reference trees plus the repository's own patches in build order, for every group, regardless of flags | results can differ in either direction; the 6.1 build skips `common` and `nethunter`, this check does not |
| Required patches | `require_patch` uses git apply only and aborts the build on failure; `apply_zram_patch` tries fuzzy forward then reverse, else aborts | reports a status and never aborts | a failure here is not necessarily fatal in the build, and some build failures are not reported here |
| Patch discovery | `find` recursively, `sort -V` on full paths | the group directories only (not recursive), ordered with the same `sort -V` when GNU sort is available | identical order for today's flat directories (tested); a patch in a subdirectory would not be reviewed; without GNU sort a natural-sort fallback is used, which differs (the report says so) |
| Downloaded targets | `kernel-patches/susfs` applies to the SUSFS checkout or to the kernel after the downloaded SUSFS patch; `patches/` applies inside the KernelSU checkout | not checked (`builder-side`) | no result for these two groups |
| Atomicity | patch(1) can fail after a successful dry run and leave a partly patched tree (the build has a branch for it) | git apply is all-or-nothing on a temporary index | that failure mode cannot be observed here |
| Working tree vs index | patches a working tree | patches an index of the committed tree (same blobs) | same for ordinary text patches; file modes and attributes are not exercised |

Observed with the build's own functions on generated trees (`tests/test_phase0_buildmethod.py` runs the real `apply_patch_file`,
`apply_kernel_patches` and `require_patch`):

| Tree and patch | Build | This check |
|---|---|---|
| Exact match | applied | `applies` |
| Context drifted by a line (needs fuzz) | **applied with fuzz** | `conflict` |
| Already applied, exact context | already applied | `reverse-applies` |
| Already applied, context drifted afterwards | **already applied** (fuzzy reverse) | `conflict` |
| Genuine conflict | skipped, name recorded | `conflict` |
| Target file missing | skipped | `target-missing` |
| Patch already applied, an unrelated line it could also match elsewhere | already applied, file untouched | `reverse-applies` |
| `require_patch` with drifted context | **aborts the build** (git apply only) | `conflict`, never aborts |

The last-but-one case was a suspected hazard (fuzz re-applying a patch at the wrong place); with GNU patch 2.7.6 it did not
happen, so nothing is claimed beyond what the table shows.

How to read the result: **`conflict` here means "does not apply exactly", not "the build will skip it"**. A patch that needs
fuzz is applied by the build, and fuzz can place a hunk where the exact context no longer matches, so such a patch deserves a
look even though the build accepts it. `applies` and `reverse-applies` match the build's outcome for exact contexts. Patch order
matches the build: the script runs the real `sort -V` (the build's `sort -z -V`) on the group directories; only GNU sort
reproduces it, so without it a fallback is used and the report says so. Today's patch directories are flat and every patch is
in `git` format with no renames, binary hunks or CRLF, so the discovery and parsing differences do not affect the current files
(`test_the_repositorys_patches_fit_what_the_script_can_review` fails if that stops being true).

### Consistency of the reports

The Markdown is rendered from the same object that is written as JSON, so counts, patch statuses, config rows, KMI symbols,
commits and authors cannot disagree. Authors are counted over **all** commits above the base even when the list is truncated, and
a truncated list is always marked. For a tree with no shared history the report says commits cannot be listed, rather than
showing an empty list.

### What it cannot tell you

- Whether anything boots. Only a flash does.
- Whether a GKID-only hardware change is truly unnecessary. Luminaire can reach the same result another way.
- Anything done by Luminaire's or GKID's own build system outside their source repositories (extra configs, patches, packaging).
- Out-of-tree MediaTek modules. They are built against the KMI, so a KMI difference matters even when the source looks unrelated.
- Whether the ACK history audit is clean on the real branch. It could only be tested on generated histories here; read the audit
  table in the first real report before relying on it.
- Anything the heuristics file under the wrong category. The JSON has every file with its raw evidence.

## 4. What the results lead to

| Result | Meaning for Phase 1 |
|---|---|
| Test A boots, bucket 1 is empty or small | ACK needs little or no device adaptation. Phase 1 can port the patches that apply to ACK as commits. |
| Test A boots, bucket 1 has strong items | Keep those items as the tree's device adaptation layer, and port them first. |
| Test A does not boot | Do not conclude ACK fails. Run Test B (pure ACK), then bring in bucket 1 items one at a time. |
| Many patches `conflict` or `target missing` on ACK | Budget rework time per patch before the tree is created. |

Nothing from Phase 1 starts until you have reviewed these results.
