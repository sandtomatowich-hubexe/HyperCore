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

## Pinning an exact version or commit

`KERNEL_EXACT_VERSION` takes a Linux version (`6.1.180`) or a commit SHA (7 to 40 hex characters).

- A version is searched in the first-parent history of the source branch. The build uses the last commit that still
  reports it. If it does not exist, the error names the nearest versions that do.
- A commit must exist, be part of the history of the source branch, and have a Makefile that reports a version of the
  selected line. Otherwise the error says which of these failed and shows the Linux version of the branch tip.
- Both need a single kernel line, not `All`.
- The first job resolves the pin with a commit-only clone, so a wrong pin fails once, early. After checkout the tree
  has to report the pinned version.

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
- Pinning relies on the source's first-parent history having non-decreasing versions, which holds for the Android Common
  Kernel branches and for merge-based device trees. A rebased or squashed history may not resolve every version.
- The toolchain and other build settings are recorded per line, but only the defconfig name is read by the build.
