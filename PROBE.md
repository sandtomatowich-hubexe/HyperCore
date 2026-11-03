# Compatibility probe

The probe answers one question before the kernel release string is changed: **can the new string break module loading, and what
can only be settled outside CI?** It reads the cloned kernel source and its `.config`, and reports what they say. It is
`py/module_probe.py`.

## It is read-only

- It never writes into the source tree. It refuses a report path inside the tree.
- It never sets `LOCALVERSION`, edits a file, commits or pushes. Git is used only through `rev-parse` and `log`, with
  `--no-optional-locks`, so even the index is left alone.
- The build hook cannot fail a build. If the probe breaks, the build prints a warning and carries on.
- The tests check this byte for byte and timestamp for timestamp, and they fail if the source gains a way to write elsewhere.

## Running it in CI

1. In the repository, add the variable `HYPERCORE_PROBE` with the value `true`
   (Settings, Secrets and variables, Actions, Variables). Optionally add `HYPERCORE_PROBE_BRAND`; it defaults to `HyperCore`.
2. Run **Kernel Builder** with `TODO` set to `defconfig`, the line `6.1`, and a single variant such as Vanilla. No kernel is
   compiled, so it takes a few minutes. Every build job that runs prints the report.
3. Read the result in the **job summary** of the build job, or download `probe-<variant>.md` and `probe-<variant>.json` from the
   build artifacts.
4. Delete the variable afterwards. Without it the probe does not run, and ordinary builds are unchanged.

In `defconfig` mode `CONFIG_LOCALVERSION` is not set (the build sets it only when it compiles), so the probe **computes** the
release string with the build's own formula. A normal build (`TODO=kernel`) also has it in `.config`, and the probe then
compares the two.

## Reading the report

Every finding has one status:

| Status | Meaning |
|---|---|
| VERIFIED | Read from a file of the tree or from the `.config`. The evidence names the file and line |
| COMPUTED | Arithmetic on verified values, for example the length of the release string |
| UNDETERMINED | The probe looked and could not establish it, or it was not given the input. Never treated as "fine" |
| NOT VERIFIABLE | Needs something CI does not have: stock modules, a device, a signing key, a boot |

and a result: `ok`, `risk`, `unknown` or `info`. Risks are listed first. The probe reads source text. It does not compile or run
anything, so it states what the code says, not what a built kernel does.

## What it checks

| Area | Checks |
|---|---|
| Module versioning | `CONFIG_MODVERSIONS` and related options from `.config`. How the loader compares a module's version magic (`same_magic`): whether the first word, which is the kernel release, is skipped for modules that carry symbol CRCs. That `VERMAGIC_STRING` starts with `UTS_RELEASE`. Where the comparison is applied, the forced-load paths, and the log text a rejected module produces |
| Module signing | `CONFIG_MODULE_SIG*` options, what the tree's own Kconfig help says they mean, GKI protected-export files, whether a signing key is in the tree |
| Release string | How `kernel.release` is composed, the state of `scripts/setlocalversion`, the length limit (`__NEW_UTS_LEN`) read from the tree, today's string and the proposed one computed with `build.sh`'s formula, the worst case, allowed characters, and that the Linux version stays first |
| Module files (optional) | Vermagic, symbol CRCs and signature of real `.ko` files, see below |

## Closing what CI cannot

The report ends with a table of what it cannot verify. These are the ways to close them.

**On the device** (adb shell, or a terminal app), with the kernel you are running:

```
uname -r
cat /proc/version
dmesg | grep -iE "version magic|disagrees about version|module verification failed|Unknown symbol"
```

Send the first two lines, and a screenshot of Settings, About phone, Kernel version. That settles what your ROM shows.

**On a PC, with the stock modules.** Extract the `.ko` files of the stock ROM (the vendor module images, whose names depend on the
ROM) into one folder, and run:

```
python3 py/module_probe.py --modules /path/to/modules --markdown modules-report.md
```

It needs no kernel tree. Add `--src`, `--config` and `--brand` to compare against a clone. It reports how many modules carry symbol
CRCs, the release word in their version magic, and whether they are signed. A module **without** CRCs must match the release
string exactly, so any release change would stop it from loading, and the report lists those modules as a risk.

## Limits

- Pattern analysis of C source and configuration, not execution.
- It cannot know which key signed the stock modules, nor whether they load on the device. Only a boot proves that.
- It describes the tree it is given. A different branch or a rebased source needs a new run.
