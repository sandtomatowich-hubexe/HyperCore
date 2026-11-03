# Kernel source registry

`sources.json` is where HyperCore decides which kernel it builds, and whether it should. Scripts read it through
`py/sources.py`. Nothing else in the repository hardcodes a kernel repository, branch, KMI, SUSFS branch or the list of
kernel lines.

## How a source is chosen

For one build, from the most specific layer to the least:

1. `KERNEL_REPO_<base>` / `KERNEL_BRANCH_<base>` repository variables (any base, for example `KERNEL_REPO_5_10`).
2. For 6.1 only (`legacy_override_line`): `KERNEL_REPO`, and `KERNEL_BRANCH` or `KERNEL_BRANCH_NH`.
3. The registry: the source named by the `KERNEL_SOURCE` input, or the default source of the line.

Then `KERNEL_EXACT_VERSION` can pin a Linux version or a commit inside the chosen source. Layers 1 and 2 are the
variables that existed before the registry and work exactly as before.

## The compatibility gate

Before any build job starts, the first job checks the selection against the device (`default_device`, or the repository
variable `HYPERCORE_DEVICE`). `build.sh` repeats the check, so it cannot be run around it.

| Registry status for the device | Default source of the line | Source named with `KERNEL_SOURCE` |
|---|---|---|
| `confirmed` | builds | builds |
| `needs-verification`, or not assessed | builds, with a warning that it is not boot-verified | needs `ALLOW_UNVERIFIED` |
| `unsupported` | **blocked** | **blocked** (`ALLOW_UNVERIFIED` does not unlock it) |
| replaced by a repository variable | builds, with a warning | builds, with a warning |

- Naming the default source explicitly is the same as not naming one.
- With `KERNEL_VERSION=All`, blocked lines are skipped with a warning as long as one line remains.
- A repository variable that replaces the repository or branch makes the source your own. The registry's assessment of
  the original source no longer describes it, so the gate allows it and says so.
- Today's default (`6.1`, device source, NetHunter branch) is `needs-verification`. It keeps building, as before, and
  every build says in its summary what is and is not verified.

## Format

```json
{
  "schema_version": 1,
  "legacy_override_line": "6.1",
  "default_device": "duchamp",
  "devices": {
    "duchamp": {"name": "Poco X6 Pro", "soc": "MT6897", "kernel_line": "6.1", "android_release": "14"}
  },
  "lines": {
    "6.1": {"android_release": "14", "kmi": "android14-6.1", "susfs_branch": "gki-android14-6.1",
            "defconfig": "gki_defconfig", "toolchain": {"name": "neutron-clang"}}
  },
  "sources": {
    "gkid-duchamp-6.1": {
      "line": "6.1",
      "repo": "https://github.com/ahmed-alnassif/GKI-Duchamp-6.1",
      "branch": "GKID-6.1",
      "label": "device source for 6.1",
      "default_for_line": true,
      "branch_variants": [{"when_flag": "NH", "branch": "GKID-NH", "label": "NetHunter build"}],
      "devices": {
        "duchamp": {
          "compatibility": "needs-verification",
          "build_verification": {"state": "reported", "evidence": "what was seen, and where"},
          "boot_verification":  {"state": "unverified"}
        }
      }
    }
  }
}
```

| Field | Meaning |
|---|---|
| `legacy_override_line` | The line the unsuffixed `KERNEL_REPO` / `KERNEL_BRANCH` variables apply to |
| `default_device` | The device builds are checked against |
| `lines.<line>` | Android release, KMI, SUSFS branch, defconfig and (recorded only) toolchain of a kernel line |
| `sources.<id>.default_for_line` | Exactly one source per line is the default |
| `sources.<id>.branch_variants` | A different branch when a feature flag is on (for example `NH`) |
| `sources.<id>.devices.<device>` | What is known about this source on this device |
| `sources.<id>.kind`, `role`, `upstream`, `sync`, `credits` | Who controls the source and where it comes from (see below) |
| `sources.<id>.versions` | Supported Linux versions and recorded builds (see below) |

The `toolchain` entries are documentation for now. `build.sh` still downloads Neutron clang on its own.

### Verification fields

Build and boot are recorded separately, because a kernel that builds may still not boot.

| Field | Values |
|---|---|
| `compatibility` | `confirmed`, `needs-verification`, `unsupported` |
| `build_verification.state`, `boot_verification.state` | `unverified`, `reported` (someone said so, not inspected), `verified` (the evidence was inspected) |
| `basis` (for `unsupported`) | `tested`, `documented`, `reasoned` |

The validator enforces the rules, so they cannot be skipped by accident:

- `confirmed` needs `boot_verification.state` of `verified` with evidence. A good build is not enough.
- `reported` and `verified` need an `evidence` text that says what was seen and where.
- `unsupported` needs a `reason` and a `basis`.
- A device runs one kernel line (`devices.<id>.kernel_line`). A source on another line can only be `unsupported` for it.
- Unknown keys are errors, with a hint for typos.

## Choosing the Linux version

`KERNEL_EXACT_VERSION` takes one of:

| Value | Meaning |
|---|---|
| empty | The branch tip of the source, as before |
| `6.1.180` | The last commit of the source that reports Linux 6.1.180 |
| a commit SHA (7 to 40 hex characters) | That commit, if it is on the source branch and reports a version of the selected line |
| `latest-stable` | The newest release of the selected line that kernel.org lists as stable or longterm |
| `latest-lts` | The same, but kernel.org must list the line as longterm |

How it is resolved, in the first job (the build jobs only receive the result):

1. **Format and line.** A version must belong to the selected line. All selectors need one line, not `All`.
2. **Upstream.** `latest-*` reads kernel.org's official `releases.json` and picks the newest release of the selected line
   only. If the list cannot be read, or the line is not listed, the run fails.
3. **Source history.** The version is searched in the first-parent history of the source branch (a commit-only clone). The
   build uses the last commit that still reports it. If the source does not have that version, the error names the
   nearest versions it does have. kernel.org having a release does not mean the source has merged it.
4. **Registry policy.** The `versions` block of the source can declare a supported range and recorded builds (below).
5. **Hand-off.** The first job passes the resolved version and the full commit SHA to every build, so a push that lands while
   the matrix runs cannot give two variants different sources. After checkout the tree has to report the same version.

Nothing in this chain replaces a version by another one. The summary of every build records the requested selector, the
resolved Linux version and the full commit SHA.

## Supported versions of a source

A source can carry a `versions` block. It is optional.

```json
"versions": {
  "min": "6.1.115",
  "max": "6.1.200",
  "reason": "older trees lack the base the patches are written against",
  "verified": {
    "6.1.177": {"duchamp": {
      "build_verification": {"state": "verified", "evidence": "run #12, log inspected, KSU variant"},
      "boot_verification":  {"state": "unverified"}
    }}
  }
}
```

- A version below `min` or above `max` is refused, with the `reason`.
- A version inside the range builds. If it has a `verified` record, the build says what was recorded; if not, it says that
  no build or boot result is recorded. That is information, not an error.
- `verified` records use the same states and evidence rules as the device assessments, and they never change
  `compatibility` by themselves. Only boot evidence on the device does that.
- Versions in the block must belong to the source's line, and devices must exist.
- A source replaced by a repository variable skips the range, with a warning.

The registry ships without a `versions` block for the Duchamp source, because no range or build record has been verified yet.

## Managed sources and upstream attribution

HyperCore can build from a repository it controls (a fork) instead of presenting someone else's repository and branches as its
own. The registry describes this with fields that already exist in the schema:

```json
"gkid-duchamp-6.1": {
  "line": "6.1", "kind": "device",
  "repo": "https://github.com/<owner>/<hypercore-fork>",
  "branch": "hypercore/6.1",
  "role": "managed",
  "upstream": {"repo": "https://github.com/ahmed-alnassif/GKI-Duchamp-6.1", "branch": "GKID-6.1",
               "synced_commit": "<40-character SHA of the last upstream commit merged>"},
  "sync": {"strategy": "merge"},
  "credits": [{"name": "Ahmed Al-Nassif", "url": "https://github.com/ahmed-alnassif/GKI-Duchamp-6.1", "license": "GPL-2.0-only"}]
}
```

| Field | Rule |
|---|---|
| `kind` | `device` (made for one device) or `generic-gki` (a generic GKI source that is only valid where the device's requirements allow it) |
| `role` | `external` (default: the repository belongs to another project) or `managed` (HyperCore controls it) |
| `upstream` | Required for `managed`: the external repository and branch it tracks. It must differ from the source itself |
| `sync.strategy` | Only `merge`. Rebasing or squashing would drop the original authorship and break version resolution, which relies on history |
| `credits` | Required for `managed`: who wrote the original, with a link and the licence |

What this gives you today: the validator refuses a managed source without its upstream, sync policy and credits; version
selection, pins and the gate work on a managed source exactly as on an external one; and every build summary and Telegram
message says whether the source is an external project or HyperCore-managed, and names the upstream project, so an upstream
branch is never shown as a HyperCore one.

**The reporting rule.** Every report that names the kernel source also says who controls it, and a managed source and the
upstream project it tracks are always named separately. Nothing presents an upstream branch or its code as HyperCore's:

| Report | What it says |
|---|---|
| Job summary | `Source role` and `Upstream project` rows, and a sentence that is different for external, managed and custom sources |
| Telegram | `Source (upstream)`, `Source (HyperCore-managed)` or `Source (custom)`, plus `Upstream` and `Upstream branch` rows for a managed source |
| Release page | A "Kernel source" block per kernel line with the same three labels, the upstream project, the commit and the requested version |
| Release metadata | `source_id`, `provenance`, `commit`, `pin_request` |

A source replaced by a repository variable is always called custom and never claimed as either. A build that did not record its
origin says "origin not recorded". `tests/test_report_provenance.py` checks that the reports agree.

The exact requirements for that move (decisions, fork layout, merge-only syncing, conflict handling, attribution) are written
in [MANAGED_SOURCES.md](MANAGED_SOURCES.md). Nothing described there has been created.

What it does not do yet: create the fork, name or protect its branches, or sync it from upstream. That is a separate stage and
needs decisions about where the fork lives and who may write to it.

## duchamp compatibility

This is what the registry holds. Nothing here is marked compatible on the strength of a version number.

| Source | Line | Compatibility | Build | Boot |
|---|---|---|---|---|
| `gkid-duchamp-6.1` (`ahmed-alnassif/GKI-Duchamp-6.1`, `GKID-6.1`, `GKID-NH`) | 6.1 | needs-verification | reported: Kernel Builder run #8 was green on 2026-10-28 (owner's screenshot, logs not inspected) | unverified |
| `aosp-ack-5.10`, `aosp-ack-5.15`, `aosp-ack-6.6`, `aosp-ack-6.12` | 5.10, 5.15, 6.6, 6.12 | unsupported (basis: reasoned) | unverified | unverified |

Why the others are unsupported: they are generic Android Common Kernel branches, and duchamp (Poco X6 Pro, MT6897) is a
6.1 android14 device. Another kernel line does not match its vendor modules or its KMI. This is reasoning, not a test. The
status can change only with evidence (see below).

Not in the registry, and not assessed: Xiaomi's own OEM source for duchamp (`MiCode/Xiaomi_Kernel_OpenSource`, branch
`bsp-duchamp-u-oss`). It is the vendor lineage, but a public issue reports that the WiFi chip source needed to build one of
its modules is missing from it, so it would need verification before it could become a source here.

**What would move `gkid-duchamp-6.1` to `confirmed`:** a build whose log was inspected (`build_verification: verified`,
with the run and variant named) and a boot on a real duchamp (`boot_verification: verified`, with who tested it, which
zip and what was observed). Both need to be written into `sources.json` by someone who has that evidence.

## Adding things

**A new kernel source for an existing line**

1. Add it under `sources` with an `https://` repository and a branch. Leave `default_for_line` off unless it should
   replace the current default.
2. Add an assessment for each device you know about, starting at `needs-verification` with both states `unverified`.
3. Run `bash tests/run_all.sh`. The validator names the exact field of every problem.
4. Build it with `KERNEL_SOURCE=<id>` and `ALLOW_UNVERIFIED=true`. Record what you observe in the assessment.

**A new kernel line**

1. Add it under `lines` (Android release, KMI, SUSFS branch, defconfig).
2. Add a source for it with `default_for_line: true`. Without one, the validator refuses the registry.
3. The build workflow, the matrix and the pin checks pick the line up from the registry. `KERNEL_VERSION` in the workflow
   form is a fixed list, so add the new line to its `options` as well.
4. Review `build.sh`: a few steps are written for specific lines (for example the 6.1 NTSync patch and the 6.1 changelog
   text), and a new line may need the same care.

**A new device**

1. Add it under `devices` with its `kernel_line`.
2. Add an assessment for it under each source you know about. Sources without one count as not assessed.
3. Select it with the repository variable `HYPERCORE_DEVICE`, or change `default_device`.

**Changing a default on purpose.** `tests/test_sources.py` and `tests/test_source_compat.sh` pin today's defaults so a
registry edit cannot change them silently. Update both in the same change.

## Tests

`bash tests/run_all.sh` runs everything: the repository validators, the registry validator, the unit tests (registry,
gate, pins, workflow) and the backward-compatibility test, which runs a frozen copy of the pre-registry logic
(`tests/legacy/`) side by side with the current one. The same command runs in the **Validate** workflow.

## Limits

- No real GitHub Actions run and no real duchamp build have been done with these changes. See the checklist in
  `CHANGELOG.md`.
- The compatibility gate trusts the registry. It cannot find out by itself that a kernel does not boot.
- `latest-*` depends on kernel.org being reachable from the runner. The selected source usually lags upstream, so the newest
  kernel.org release is often not buildable yet. That is reported, never worked around.
- Pinning relies on the source's first-parent history having non-decreasing versions, which holds for the Android Common
  Kernel branches and for merge-based device trees. A rebased or squashed history may not resolve every version.
- The toolchain and other build settings are recorded per line, but only the defconfig name is read by the build.
