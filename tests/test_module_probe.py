#!/usr/bin/env python3
"""Tests for the read-only compatibility probe (py/module_probe.py).

The kernel trees below are FIXTURES: small stand-ins written for these tests, in the shape of the files the probe reads.
They are not copies of any real kernel source. What these tests prove is how the probe reads a tree, what it reports as
verified, undetermined or not verifiable, and that it cannot change anything. They say nothing about the real Duchamp tree;
that is what running the probe in CI on the real clone is for.
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "py"))

import module_probe as mp  # noqa: E402

SCRIPT = ROOT / "py" / "module_probe.py"
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")

MAKEFILE = """# fixture
VERSION = 6
PATCHLEVEL = 1
SUBLEVEL = 177
EXTRAVERSION =
NAME = Curry Ramen
filechk_kernel.release = \\
	echo "$(KERNELVERSION)$$($(CONFIG_SHELL) $(srctree)/scripts/setlocalversion $(srctree))"
include/config/kernel.release: FORCE
	$(call filechk,kernel.release)
KERNELRELEASE = $(call read-file, include/config/kernel.release)
"""

SETLOCALVERSION = """#!/bin/sh
# fixture
if test "$CONFIG_LOCALVERSION_AUTO" = "y"; then
	{active}echo "+"
fi
{active2}echo "+"
"""

VERMAGIC_H = """/* fixture */
#define VERMAGIC_STRING \\
	{first} " " \\
	MODULE_VERMAGIC_SMP MODULE_VERMAGIC_PREEMPT \\
	MODULE_VERMAGIC_MODULE_UNLOAD MODULE_VERMAGIC_MODVERSIONS \\
	MODULE_ARCH_VERMAGIC
"""

MAIN_C_SKIP = """/* fixture: a simplified stand-in for the module loader */
static inline int same_magic(const char *amagic, const char *bmagic, bool has_crcs)
{
	if (has_crcs) {
		amagic += strcspn(amagic, " ");
		bmagic += strcspn(bmagic, " ");
	}
	return strcmp(amagic, bmagic) == 0;
}

static int check_modinfo(struct module *mod, struct load_info *info, int flags)
{
	const char *modmagic = get_modinfo(info, "vermagic");
	int err;

	if (flags & MODULE_INIT_IGNORE_VERMAGIC)
		modmagic = NULL;
	if (!modmagic) {
		err = try_to_force_load(mod, "bad vermagic");
		if (err)
			return err;
	} else if (!same_magic(modmagic, vermagic, info->index.vers)) {
		pr_err("%s: version magic '%s' should be '%s'\\n", info->name, modmagic, vermagic);
		return -ENOEXEC;
	}
	return 0;
}
"""

MAIN_C_NO_SKIP = MAIN_C_SKIP.replace("""	if (has_crcs) {
		amagic += strcspn(amagic, " ");
		bmagic += strcspn(bmagic, " ");
	}
""", "")

VERSION_C = """/* fixture */
int check_version(const struct load_info *info, const char *symname, struct module *mod, const s32 *crc)
{
	pr_warn("%s: disagrees about version of symbol %s\\n", info->name, symname);
	return 0;
}

int check_modstruct_version(const struct load_info *info, struct module *mod)
{
	return check_version(info, "module_layout", mod, NULL);
}
"""

KCONFIG = """# fixture
config MODULE_SIG
	bool "Module signature verification"

config MODULE_SIG_FORCE
	bool "Require modules to be validly signed"
	depends on MODULE_SIG
	help
	  Reject unsigned modules or signed modules for which we don't have a
	  key. Without this, such modules will simply taint the kernel.

config MODULE_SIG_PROTECT
	bool "Android GKI protected exports"
	depends on MODULE_SIG
	help
	  Modules without a valid signature cannot use the protected exports.
"""

CONFIG = """CONFIG_MODULES=y
CONFIG_MODVERSIONS=y
# CONFIG_MODULE_SRCVERSION_ALL is not set
CONFIG_MODULE_UNLOAD=y
# CONFIG_MODULE_FORCE_LOAD is not set
# CONFIG_MODULE_FORCE_UNLOAD is not set
CONFIG_MODULE_SIG=y
# CONFIG_MODULE_SIG_FORCE is not set
CONFIG_MODULE_SIG_ALL=y
CONFIG_MODULE_SIG_PROTECT=y
CONFIG_MODULE_SIG_HASH="sha1"
CONFIG_SYSTEM_TRUSTED_KEYS=""
CONFIG_TRIM_UNUSED_KSYMS=y
CONFIG_UNUSED_KSYMS_WHITELIST="abi_gki_aarch64"
CONFIG_LOCALVERSION="-GKI-r12-abc1234"
# CONFIG_LOCALVERSION_AUTO is not set
"""


def make_tree(root, *, skip=True, first="UTS_RELEASE", uts=64, commented=True, main=True, gki=True, config=CONFIG):
    root = Path(root)

    def put(rel, text):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    put("Makefile", MAKEFILE)
    put("scripts/setlocalversion", SETLOCALVERSION.format(active="# " if commented else "", active2="# " if commented else ""))
    put("include/linux/vermagic.h", VERMAGIC_H.format(first=first))
    if main:
        put("kernel/module/main.c", MAIN_C_SKIP if skip else MAIN_C_NO_SKIP)
        put("kernel/module/version.c", VERSION_C)
    put("kernel/module/Kconfig", KCONFIG)
    put("include/uapi/linux/utsname.h", f"#define __NEW_UTS_LEN {uts}\n")
    put("init/version-timestamp.c", "struct uts_namespace init_uts_ns = { .name = { .release = UTS_RELEASE, }, };\n")
    if gki:
        put("kernel/module/gki_module.c", "/* fixture */\n")
        put("android/abi_gki_protected_exports_aarch64", "some_symbol\n")
    put("../config", config)
    return root


def tree_state(root):
    """Content hash, size and modification time of every file, so any change at all would show."""
    state = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            st = path.stat()
            state[str(path.relative_to(root))] = (hashlib.sha256(path.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns)
    return state


def make_ko(path, vermagic, crcs=2, signed=False, name="mod"):
    modinfo = b"".join(item.encode() + b"\0" for item in (f"name={name}", f"vermagic={vermagic}", "intree=Y"))
    versions = b"".join(struct.pack("<Q", 0x1000 + i) + f"sym{i}".encode().ljust(56, b"\0") for i in range(crcs))
    blobs = [(".modinfo", modinfo)] + ([("__versions", versions)] if crcs else [])
    shstrtab, name_off = b"\0", {}
    for n, _ in blobs + [(".shstrtab", b"")]:
        name_off[n] = len(shstrtab)
        shstrtab += n.encode() + b"\0"
    data, offsets = bytearray(64), {}
    for n, blob in blobs + [(".shstrtab", shstrtab)]:
        offsets[n] = len(data)
        data += blob
    shoff = len(data)
    headers = [struct.pack("<IIQQQQIIQQ", *([0] * 10))]
    for n, blob in blobs + [(".shstrtab", shstrtab)]:
        headers.append(struct.pack("<IIQQQQIIQQ", name_off[n], 3 if n == ".shstrtab" else 1, 0, 0, offsets[n], len(blob), 0, 0, 1, 0))
    data += b"".join(headers)
    ident = b"\x7fELF" + bytes([2, 1, 1]) + b"\0" * 9
    data[:64] = struct.pack("<16sHHIQQQIHHHHHH", ident, 1, 183, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(headers), len(headers) - 1)
    if signed:
        data += b"SIGDATA" + mp.SIGNATURE_MARKER
    Path(path).write_bytes(bytes(data))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src"
        self.src.mkdir()

    def probe(self, *extra, config=True, tree=None, no_git=True, **tree_options):
        tree = tree or make_tree(self.src, **tree_options)
        cmd = [sys.executable, str(SCRIPT), "--src", str(tree), "--kernel-name", "GKI", "--run-num", "-r12", "--sha7", "abc1234", "--brand", "HyperCore"]
        if config:
            cmd += ["--config", str(self.tmp / "config")]
        if no_git:
            cmd.append("--no-git")
        return subprocess.run(cmd + list(extra), capture_output=True, text=True)

    def report(self, *extra, **options):
        out = self.tmp / "report.json"
        result = self.probe("--json", str(out), *extra, **options)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(out.read_text())

    @staticmethod
    def find(report, title_part):
        found = [f for f in report["findings"] if title_part in f["title"]]
        assert found, f"no finding with '{title_part}' in {[f['title'] for f in report['findings']]}"
        return found[0]


class WhatIsVerified(Case):
    def test_the_loader_rule_is_read_from_the_source_with_file_and_line_evidence(self):
        f = self.find(self.report(), "Version magic comparison")
        self.assertEqual((f["status"], f["result"]), (mp.VERIFIED, mp.OK))
        self.assertIn("first word of the version magic (the kernel release) is skipped", f["detail"])
        self.assertTrue(f["evidence"][0].startswith("kernel/module/main.c:"), f["evidence"])
        self.assertTrue(any("strcspn" in line for line in f["evidence"]))

    def test_the_comparison_is_located_and_the_kernel_log_text_is_given(self):
        r = self.report()
        applied = self.find(r, "check_modinfo")
        self.assertIn("forced loading with MODULE_INIT_IGNORE_VERMAGIC: handled", applied["detail"])
        self.assertIn("present", applied["detail"])
        self.assertIn("version magic '<module>' should be '<kernel>'", self.find(r, "Log text")["detail"])

    def test_the_release_is_the_first_word_of_the_version_magic(self):
        f = self.find(self.report(), "Layout of the version magic")
        self.assertEqual((f["status"], f["result"]), (mp.VERIFIED, mp.OK))
        self.assertIn("starts with UTS_RELEASE", f["detail"])

    def test_the_symbol_crc_checks_are_found(self):
        f = self.find(self.report(), "Per-symbol CRC checks")
        self.assertEqual(f["status"], mp.VERIFIED)
        self.assertIn("check_version, check_modstruct_version", f["detail"])
        self.assertIn("reasoned from the function names, not proven", f["detail"])

    def test_config_values_are_read_from_the_dot_config(self):
        r = self.report()
        for name, value in (("CONFIG_MODVERSIONS", "y"), ("CONFIG_MODULE_SRCVERSION_ALL", "not set"), ("CONFIG_MODULE_SIG", "y"),
                            ("CONFIG_MODULE_SIG_FORCE", "not set"), ("CONFIG_MODULE_SIG_PROTECT", "y"), ("CONFIG_MODULE_SIG_HASH", "sha1"),
                            ("CONFIG_MODULE_FORCE_LOAD", "not set")):
            f = self.find(r, name)
            self.assertEqual((f["status"], f["detail"]), (mp.VERIFIED, value), name)
        self.assertEqual(self.find(r, "CONFIG_SYSTEM_TRUSTED_KEYS")["detail"], "")
        self.assertEqual(self.find(r, "Symbol version CRCs")["result"], mp.OK)

    def test_an_option_that_is_not_mentioned_is_reported_as_absent_not_as_off(self):
        self.assertEqual(self.find(self.report(), "CONFIG_RANDSTRUCT")["detail"], "absent")

    def test_the_trees_own_kconfig_help_explains_the_signing_options(self):
        r = self.report()
        force = self.find(r, "CONFIG_MODULE_SIG_FORCE means")
        self.assertEqual(force["status"], mp.VERIFIED)
        self.assertIn("Reject unsigned modules or signed modules for which we don't have a key", force["detail"])
        self.assertEqual(force["evidence"], ["kernel/module/Kconfig"])
        self.assertIn("cannot use the protected exports", self.find(r, "CONFIG_MODULE_SIG_PROTECT means")["detail"])

    def test_gki_protection_files_and_the_signing_key_are_reported(self):
        r = self.report()
        self.assertIn("kernel/module/gki_module.c", self.find(r, "GKI module protection")["detail"])
        self.assertIn("abi_gki_protected_exports_aarch64", self.find(r, "GKI module protection")["detail"])
        self.assertIn("absent: the build generates a key", self.find(r, "Signing key in the tree")["detail"])

    def test_how_the_release_is_composed_is_quoted_from_the_makefile(self):
        f = self.find(self.report(), "How the release is composed")
        self.assertEqual(f["status"], mp.VERIFIED)
        self.assertTrue(any("setlocalversion" in e for e in f["evidence"]), f["evidence"])

    def test_the_state_of_setlocalversion_is_counted(self):
        self.assertIn("2 commented out", self.find(self.report(tree=None, commented=True), "setlocalversion state")["detail"])
        other = self.tmp / "other"
        other.mkdir()
        make_tree(other, commented=False)
        self.assertIn("2 active, 0 commented", self.find(self.report(tree=other), "setlocalversion state")["detail"])


class ReleaseString(Case):
    def test_today_s_release_and_the_proposed_one_are_computed_with_build_sh_s_formula(self):
        r = self.report()
        today, proposed = self.find(r, "forms today"), self.find(r, "Proposed release string")
        self.assertEqual((today["status"], today["detail"]), (mp.COMPUTED, "6.1.177-GKI-r12-abc1234 (23 characters)"))
        self.assertEqual((proposed["status"], proposed["detail"]), (mp.COMPUTED, "6.1.177-HyperCore-r12-abc1234 (29 characters)"))
        self.assertEqual(r["release"]["proposed"], "6.1.177-HyperCore-r12-abc1234")

    def test_the_formula_is_the_one_build_sh_uses(self):
        build = (ROOT / "build.sh").read_text()
        self.assertIn('SUFFIX="${RUN_NUM}-${LATEST_COMMIT_HASH}"', build)
        self.assertIn('config --set-str CONFIG_LOCALVERSION "-${KERNEL_NAME}${SUFFIX}"', build)
        self.assertEqual(mp.compose_release("6.1.177", "GKI", "-r12", "abc1234"), "6.1.177-GKI-r12-abc1234")

    def test_the_computed_string_is_compared_with_the_dot_config(self):
        f = self.find(self.report(), "Computed string matches .config")
        self.assertEqual((f["status"], f["result"]), (mp.VERIFIED, mp.OK))

    def test_the_length_limit_is_read_from_the_tree_and_the_margin_is_computed(self):
        r = self.report()
        self.assertIn("__NEW_UTS_LEN is 64", self.find(r, "Release length limit")["detail"])
        self.assertEqual(self.find(r, "Release length limit")["evidence"], ["include/uapi/linux/utsname.h:1"])
        fits = self.find(r, "Proposed: length against the limit")
        self.assertEqual((fits["status"], fits["result"], fits["detail"]), (mp.COMPUTED, mp.OK, "29 of 64 characters, 35 to spare"))

    def test_the_worst_case_is_also_within_the_limit(self):
        worst = self.find(self.report(), "Worst case")
        self.assertEqual(worst["result"], mp.OK)
        self.assertIn("6.1.999-HyperCore-r999999999-000000000000", worst["evidence"][0])

    def test_a_release_that_is_too_long_is_a_risk(self):
        r = self.report("--brand", "H" * 60)
        self.assertEqual(self.find(r, "Proposed: length against the limit")["result"], mp.RISK)

    def test_spaces_and_slashes_are_a_risk(self):
        for brand in ("Hyper Core", "Hyper/Core"):
            r = self.report("--brand", brand)
            self.assertEqual(self.find(r, "Characters in the proposed release")["result"], mp.RISK, brand)

    def test_the_upstream_version_stays_first(self):
        self.assertEqual(self.find(self.report(), "Upstream version stays first")["result"], mp.OK)

    def test_a_smaller_limit_in_the_tree_changes_the_verdict(self):
        r = self.report(uts=24)
        self.assertEqual(self.find(r, "Proposed: length against the limit")["result"], mp.RISK)


class Risks(Case):
    def test_a_loader_that_compares_the_whole_string_is_a_risk(self):
        r = self.report(skip=False)
        f = self.find(r, "Version magic comparison")
        self.assertEqual((f["status"], f["result"]), (mp.VERIFIED, mp.RISK))
        self.assertIn("Any change to the release string would stop every module", f["detail"])
        self.assertIs(r["module_loader"]["release_ignored_with_crcs"], False)

    def test_a_version_magic_that_does_not_start_with_the_release_is_a_risk(self):
        self.assertEqual(self.find(self.report(first="MODULE_VERMAGIC_SMP"), "Layout of the version magic")["result"], mp.RISK)

    def test_modversions_off_is_a_risk(self):
        make_tree(self.src, config=CONFIG.replace("CONFIG_MODVERSIONS=y", "# CONFIG_MODVERSIONS is not set"))
        r = self.report(tree=self.src)
        self.assertEqual(self.find(r, "Symbol version CRCs")["result"], mp.RISK)

    def test_strict_mode_fails_on_a_risk_and_normal_mode_does_not(self):
        make_tree(self.src, skip=False)
        self.assertEqual(self.probe("--strict", tree=self.src).returncode, 1)
        self.assertEqual(self.probe(tree=self.src).returncode, 0)
        make_tree(self.src, skip=True)
        self.assertEqual(self.probe("--strict", tree=self.src).returncode, 0)

    def test_the_summary_lists_the_risks_first(self):
        text = self.probe(skip=False).stdout
        self.assertIn("**Risks found: 1.", text)
        self.assertIn("- RISK: Version magic comparison (same_magic)", text)


class WhatIsNotKnown(Case):
    def test_a_missing_file_is_undetermined_never_verified(self):
        r = self.report(main=False)
        for title in ("Module loader source", "Per-symbol CRC checks"):
            f = self.find(r, title)
            self.assertEqual((f["status"], f["result"]), (mp.UNDETERMINED, mp.UNKNOWN), title)
        self.assertIsNone(r["module_loader"]["release_ignored_with_crcs"])
        self.assertNotIn(mp.VERIFIED, [f["status"] for f in r["findings"] if f["area"] == "Module versioning" and "comparison" in f["title"].lower()])

    def test_without_a_dot_config_the_config_findings_are_undetermined(self):
        r = self.report(config=False)
        self.assertEqual(self.find(r, ".config values")["status"], mp.UNDETERMINED)
        self.assertEqual(self.find(r, "Signing configuration")["status"], mp.UNDETERMINED)
        self.assertNotIn("CONFIG_MODVERSIONS", [f["title"] for f in r["findings"]])

    def test_an_unreadable_config_is_reported(self):
        out = self.tmp / "r.json"
        make_tree(self.src)
        subprocess.run([sys.executable, str(SCRIPT), "--src", str(self.src), "--config", str(self.tmp / "missing"), "--no-git", "--json", str(out)], check=True, capture_output=True)
        titles = [f["title"] for f in json.loads(out.read_text())["findings"]]
        self.assertIn(".config", titles)

    def test_every_finding_has_one_of_the_four_statuses_and_a_result(self):
        r = self.report()
        for f in r["findings"]:
            self.assertIn(f["status"], (mp.VERIFIED, mp.COMPUTED, mp.UNDETERMINED, mp.NOT_VERIFIABLE), f["title"])
            self.assertIn(f["result"], (mp.OK, mp.RISK, mp.UNKNOWN, mp.INFO), f["title"])
            self.assertTrue(f["title"] and f["area"])

    def test_computed_values_are_never_labelled_verified(self):
        r = self.report()
        for title in ("forms today", "Proposed release string", "Proposed: length", "Characters in the proposed", "Upstream version stays first"):
            self.assertEqual(self.find(r, title)["status"], mp.COMPUTED, title)

    def test_what_ci_cannot_verify_is_listed_separately_and_completely(self):
        r = self.report()
        items = [i["item"] for i in r["not_verifiable"]]
        for needle in ("Stock vendor modules carry symbol CRCs", "Signature and signer", "What Settings shows", "Whether modules load at runtime",
                       "Userspace that parses the kernel release", "Behaviour of the compiled kernel"):
            self.assertTrue(any(needle in i for i in items), needle)
        text = self.probe().stdout
        self.assertIn("### Cannot be verified in CI", text)
        self.assertIn("needs a boot on the device", text)

    def test_the_markdown_explains_the_four_statuses(self):
        text = self.probe().stdout
        for word in ("VERIFIED = read from the tree or config", "COMPUTED = arithmetic", "UNDETERMINED = looked for and not found", "NOT VERIFIABLE = needs something CI does not have"):
            self.assertIn(word, text)
        self.assertIn("Nothing was changed", text)


class ReadOnly(Case):
    def test_the_source_tree_is_byte_for_byte_and_timestamp_for_timestamp_unchanged(self):
        make_tree(self.src)
        before = tree_state(self.src)
        result = self.probe("--json", str(self.tmp / "o.json"), "--markdown", str(self.tmp / "o.md"), tree=self.src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree_state(self.src), before)

    def test_a_git_checkout_is_left_exactly_as_it_was_including_the_index(self):
        make_tree(self.src)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.src)], check=True, env=GIT_ENV)
        subprocess.run(["git", "-C", str(self.src), "add", "-A"], check=True, env=GIT_ENV)
        subprocess.run(["git", "-C", str(self.src), "commit", "-qm", "fixture"], check=True, env=GIT_ENV)
        head = subprocess.run(["git", "-C", str(self.src), "rev-parse", "HEAD"], capture_output=True, text=True, env=GIT_ENV).stdout.strip()
        before = tree_state(self.src)          # includes .git: the index, refs and objects
        result = self.probe("--json", str(self.tmp / "o.json"), tree=self.src, no_git=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree_state(self.src), before)
        status = subprocess.run(["git", "-C", str(self.src), "status", "--porcelain"], capture_output=True, text=True, env=GIT_ENV).stdout
        self.assertEqual(status, "")
        report = json.loads((self.tmp / "o.json").read_text())
        self.assertEqual(report["source"]["commit"], head)
        self.assertEqual(self.find(report, "Commit inspected")["status"], mp.VERIFIED)

    def test_it_refuses_to_write_a_report_inside_the_source_tree(self):
        make_tree(self.src)
        before = tree_state(self.src)
        for flag in ("--json", "--markdown"):
            result = self.probe(flag, str(self.src / "report.out"), tree=self.src)
            self.assertEqual(result.returncode, 2, flag)
            self.assertIn("inside the source tree", result.stderr)
            self.assertIn("The probe is read-only", result.stderr)
        self.assertEqual(self.probe("--json", str(self.src / "sub" / "dir" / "r.json"), tree=self.src).returncode, 2)
        self.assertEqual(tree_state(self.src), before)

    def test_git_is_limited_to_two_commands_that_cannot_change_a_repository(self):
        make_tree(self.src)
        for command in ("checkout", "commit", "config", "status", "add", "reset", "clean", "push", "pull", "fetch", "apply", "stash"):
            with self.assertRaises(mp.ProbeError, msg=command) as ctx:
                mp.run_git(self.src, command, "x")
            self.assertIn("the probe only uses rev-parse, log", str(ctx.exception))
        with self.assertRaises(mp.ProbeError):
            mp.run_git(self.src)

    def test_the_source_has_exactly_two_places_that_write_and_both_write_reports(self):
        text = SCRIPT.read_text(encoding="utf-8")
        writers = [line.strip() for line in text.splitlines() if ".write_text(" in line or ".write_bytes(" in line or "open(" in line and ('"w' in line or "'w" in line)]
        self.assertEqual(writers, ["json_path.write_text(json.dumps(result, indent=1), encoding=\"utf-8\")", "md_path.write_text(markdown, encoding=\"utf-8\")"])
        for banned in ("shutil.", "os.remove", "os.rename", "os.unlink", "os.chmod", "os.system", "touch(", "unlink(", "rmdir(", "rename(", "chmod("):
            self.assertFalse(banned in text, f"the probe source contains {banned}")
        subprocess_calls = [line.strip() for line in text.splitlines() if "subprocess.run(" in line]
        self.assertEqual(len(subprocess_calls), 1)
        self.assertIn('"git", "--no-optional-locks"', subprocess_calls[0])

    def test_the_source_contains_no_way_to_set_the_release_or_the_configuration(self):
        import re
        text = SCRIPT.read_text(encoding="utf-8")
        for banned in ("set-str", "scripts/config", "sed -i", "make "):
            self.assertFalse(banned in text, f"the probe source contains {banned}")
        # every git call names a subcommand that cannot change a repository
        used = set(re.findall(r'run_git\(\s*\w+\s*,\s*"([a-z-]+)"', text))
        self.assertEqual(used, {"rev-parse"})
        self.assertEqual(set(mp.GIT_READ_ONLY), {"rev-parse", "log"})


class ModuleFiles(Case):
    def modules_dir(self, *specs):
        directory = self.tmp / "modules"
        directory.mkdir(exist_ok=True)
        for name, vermagic, crcs, signed in specs:
            make_ko(directory / f"{name}.ko", vermagic, crcs, signed, name)
        return directory

    GOOD = "6.1.75-android14-11-g1234567 SMP preempt mod_unload modversions aarch64"

    def test_the_vermagic_crcs_and_signature_are_read_from_the_elf_files(self):
        d = self.modules_dir(("wifi", self.GOOD, 5, True), ("audio", self.GOOD, 2, False))
        modules = {m["name"]: m for m in (mp.inspect_module(p) for p in sorted(d.glob("*.ko")))}
        self.assertEqual(modules["wifi"]["vermagic"], self.GOOD)
        self.assertEqual(modules["wifi"]["release"], "6.1.75-android14-11-g1234567")
        self.assertEqual((modules["wifi"]["crc_entries"], modules["wifi"]["has_crcs"], modules["wifi"]["signed"]), (5, True, True))
        self.assertEqual((modules["audio"]["crc_entries"], modules["audio"]["signed"]), (2, False))

    def test_modules_with_crcs_ignore_a_changed_release_when_the_loader_rule_is_verified(self):
        d = self.modules_dir(("wifi", self.GOOD, 5, True), ("audio", self.GOOD, 2, True))
        r = self.report("--modules", str(d))
        self.assertEqual(self.find(r, "Modules inspected")["detail"].split(";")[0], "2 modules read (0 could not be read)")
        effect = self.find(r, "Effect of a different release")
        self.assertEqual((effect["status"], effect["result"]), (mp.VERIFIED, mp.OK))
        self.assertEqual(self.find(r, "Modules without symbol CRCs")["result"], mp.OK)

    def test_a_module_without_crcs_is_a_risk_because_it_needs_the_exact_release(self):
        d = self.modules_dir(("wifi", self.GOOD, 5, True), ("legacy", self.GOOD, 0, False))
        r = self.report("--modules", str(d))
        f = self.find(r, "Modules without symbol CRCs")
        self.assertEqual((f["status"], f["result"]), (mp.VERIFIED, mp.RISK))
        self.assertIn("legacy", f["detail"])
        self.assertIn("Changing the release would stop them from loading", f["detail"])

    def test_without_the_source_rule_the_effect_on_modules_is_not_claimed(self):
        d = self.modules_dir(("wifi", self.GOOD, 5, True))
        r = self.report("--modules", str(d), main=False)
        effect = self.find(r, "Effect of a different release")
        self.assertEqual((effect["status"], effect["result"]), (mp.UNDETERMINED, mp.UNKNOWN))

    def test_a_loader_that_compares_everything_makes_crc_modules_a_risk_too(self):
        d = self.modules_dir(("wifi", self.GOOD, 5, True))
        r = self.report("--modules", str(d), skip=False)
        self.assertEqual(self.find(r, "Effect of a different release")["result"], mp.RISK)

    def test_modules_can_be_inspected_without_a_kernel_tree_and_nothing_is_claimed_about_the_loader(self):
        d = self.modules_dir(("wifi", self.GOOD, 5, True), ("legacy", self.GOOD, 0, False))
        out = self.tmp / "m.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "--modules", str(d), "--json", str(out)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        r = json.loads(out.read_text())
        self.assertIn("no kernel tree was given", result.stdout)
        self.assertEqual(self.find(r, "Effect of a different release")["status"], mp.UNDETERMINED)
        self.assertEqual(self.find(r, "Modules without symbol CRCs")["result"], mp.RISK)
        self.assertNotIn("Module versioning", {f["area"] for f in r["findings"]})

    def test_nothing_to_inspect_is_a_usage_error(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("give --src", result.stderr)

    def test_unreadable_files_and_empty_directories_are_reported_not_ignored(self):
        d = self.modules_dir(("ok", self.GOOD, 1, False))
        (d / "broken.ko").write_bytes(b"this is not an ELF file at all")
        r = self.report("--modules", str(d))
        self.assertIn("1 modules read (1 could not be read)", self.find(r, "Modules inspected")["detail"])
        self.assertEqual(self.find(r, "Unreadable module")["status"], mp.UNDETERMINED)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(self.find(self.report("--modules", str(empty)), "Module files given")["status"], mp.UNDETERMINED)

    def test_the_release_words_found_in_the_modules_are_listed(self):
        d = self.modules_dir(("a", self.GOOD, 1, False), ("b", "6.1.99-other SMP mod_unload modversions aarch64", 1, False))
        words = self.find(self.report("--modules", str(d)), "Release words")["detail"]
        self.assertEqual(words, "6.1.75-android14-11-g1234567, 6.1.99-other")


class Wiring(unittest.TestCase):
    def test_the_hook_runs_after_dot_config_exists_and_before_the_early_exits(self):
        text = (ROOT / "build.sh").read_text()
        config = text.index('make ${MAKE_ARGS[@]} "$KERNEL_DEFCONFIG"')
        hook = text.index("\nrun_compat_probe\n")
        self.assertLess(config, hook)
        self.assertLess(hook, text.index('if [ "$TEST" = "yes" ]'))
        self.assertLess(hook, text.index('if [[ $TODO == "defconfig" ]]'))

    def test_the_build_s_release_string_lines_are_exactly_what_they_were(self):
        lines = [l.strip() for l in (ROOT / "build.sh").read_text().splitlines() if "LOCALVERSION" in l]
        self.assertEqual(lines, ['config --set-str CONFIG_LOCALVERSION "-${KERNEL_NAME}${SUFFIX}"', "config --disable CONFIG_LOCALVERSION_AUTO"])
        self.assertIn('SUFFIX="${RUN_NUM}-${LATEST_COMMIT_HASH}"', (ROOT / "build.sh").read_text())

    def test_the_workflow_passes_the_two_optional_repository_variables(self):
        import yaml
        env = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())["env"]
        self.assertEqual(env["HYPERCORE_PROBE"], "${{ vars.HYPERCORE_PROBE }}")
        self.assertEqual(env["HYPERCORE_PROBE_BRAND"], "${{ vars.HYPERCORE_PROBE_BRAND }}")

    def run_hook(self, **env):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        src = work / "ksrc"
        src.mkdir()
        make_tree(src)
        (work / "out").mkdir()
        shutil.copy(work / "config", work / "out" / ".config")
        script = f'''
            WORKDIR="{ROOT}"; KSRC="{src}"; OUTDIR="{work}/out"; RELEASE_DIR="{work}/artifacts"; VARIANT=KSU
            KERNEL_NAME=GKI; RUN_NUM=-r12; GITHUB_STEP_SUMMARY="{work}/summary.md"
            source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"
            run_compat_probe; echo "RC=$?"
        '''
        before = tree_state(src)
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": str(work), **env})
        return work, src, before, result.returncode, result.stdout + result.stderr

    def test_the_hook_does_nothing_unless_asked(self):
        work, src, before, code, out = self.run_hook()
        self.assertEqual((code, "RC=0" in out), (0, True))
        self.assertFalse((work / "artifacts").exists())
        self.assertFalse((work / "summary.md").exists())
        self.assertEqual(tree_state(src), before)

    def test_the_hook_writes_reports_next_to_the_artifacts_and_leaves_the_tree_alone(self):
        work, src, before, code, out = self.run_hook(HYPERCORE_PROBE="true")
        self.assertIn("RC=0", out)
        self.assertTrue((work / "artifacts" / "probe-KSU.md").is_file())
        report = json.loads((work / "artifacts" / "probe-KSU.json").read_text())
        self.assertEqual(report["release"]["proposed"], "6.1.177-HyperCore-r12-" + report["release"]["proposed"].rsplit("-", 1)[-1])
        self.assertIn("HyperCore compatibility probe (read-only)", (work / "summary.md").read_text())
        self.assertEqual(tree_state(src), before)

    def test_the_brand_can_be_proposed_through_a_variable(self):
        work, _, _, _, out = self.run_hook(HYPERCORE_PROBE="true", HYPERCORE_PROBE_BRAND="Other")
        self.assertIn("6.1.177-Other-r12-", json.loads((work / "artifacts" / "probe-KSU.json").read_text())["release"]["proposed"])

    def test_a_failing_probe_never_fails_the_build(self):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        script = f'''
            WORKDIR="{ROOT}"; KSRC="{work}/does-not-exist"; OUTDIR="{work}"; RELEASE_DIR="{work}/artifacts"; VARIANT=KSU
            source "$WORKDIR/functions.sh"; source "$WORKDIR/functions-extra.sh"
            run_compat_probe; echo "RC=$?"
        '''
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": str(work), "HYPERCORE_PROBE": "true"})
        out = result.stdout + result.stderr
        self.assertIn("RC=0", out)
        self.assertIn("The compatibility probe failed. The build continues unchanged.", out)


if __name__ == "__main__":
    unittest.main()
